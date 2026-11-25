"""browser_challenge's steps in an environment's browser on its machine, for real (openajar.node.solver.steps): the
docker provider's machine serves a contact form with Cloudflare's real Turnstile widget and its always-fail test
key (2x00000000000000000000AB), which no click passes. The order runs: the wait, the click step (the widget shows no
checkbox to click), then the owner's solver: a fake service on this computer with the documented 2Captcha shape
(no real service is called), whose answer is Cloudflare's dummy token. The token lands in the form's
cf-turnstile-response field, the page's data-callback enables the form, and the form carries the token to the
site. The key never reaches the machine. A widget one click passes never reaches the service.

The machine's browser loads Turnstile from challenges.cloudflare.com, so this needs the internet (marked network);
it is skipped when the machine cannot reach it, and unless docker answers and the image has the browser (see
test_environments_vmbrowser_docker)."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from solver_fake import DUMMY_TOKEN, FakeService
from test_environments_vmbrowser_docker import _ready, docker_node

from openajar.core.models import ControlState

pytestmark = [pytest.mark.skipif(not _ready(), reason="needs docker with the environments image built with its browser"),
              pytest.mark.network]

KEY = f"fake-solver-key-{0xd0c4:x}"
SITE_KEY = "2x00000000000000000000AB"  # Cloudflare's always-fail Turnstile test key (visible)
ASK_KEY = "3x00000000000000000000FF"   # its test key that forces the interactive check: a checkbox, which a click passes
FORM = """<!doctype html><title>Contact</title><body style="margin:0;font:16px sans-serif">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<h1 style="margin:40px">Contact us</h1>
<form action="/done.html" method="get" style="margin:40px">
<label>Email <input type="email" name="email" value="someone@example.test"></label><br><br>
<label>Message <textarea name="message">Hello</textarea></label><br><br>
%(extra)s<div class="cf-turnstile" data-sitekey="%(key)s" data-callback="onPass" style="width:300px"></div><br>
<button id="send" disabled>Send</button></form>
<script>
window.onPass = (token) => { document.getElementById('send').disabled = false; document.title = 'Ready to send'; };
</script></body>"""


async def test_the_owners_solver_answers_a_widget_a_click_does_not_pass(tmp_path, monkeypatch):
    from openajar.node.interaction import autocheck, checks, create_interaction
    from openajar.node.solver import ledger, services, settings, steps
    from openajar.node.solver.settings import SolverEntry

    monkeypatch.setattr(autocheck, "AUTO_CHECK_S", 2.0)  # this widget never passes by itself
    monkeypatch.setattr(steps, "WAIT_S", 2.0)
    monkeypatch.setattr(checks, "OUTCOME_S", 5.0)
    monkeypatch.setattr(services, "FIRST_POLL_S", 0.5)
    monkeypatch.setattr(services, "POLL_S", 0.5)
    with FakeService("2captcha", key=KEY, processing=2) as svc:
        def owner(home):
            settings.save(home, [SolverEntry(name="main", provider="2captcha", key=KEY, endpoint=svc.url)])

        async with docker_node(tmp_path, home_setup=owner) as node:
            mgr, envs, lanes, grant, home = node["mgr"], node["envs"], node["lanes"], node["grant"], node["home"]
            settings.bind_home(home)
            try:
                eid = mgr.planned_id("session:s1", "A solver")
                plan = await mgr.plan(eid, size=None, hours=1)
                env = await mgr.create(eid, plan, binding="session:s1", title="A solver", grant_id=grant.id,
                                       grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
                env = await mgr.wait_ready(env.id, 600)
                assert env.state == "ready", env.error
                reach = await envs.exec(env.terminal_lane, "curl -s -o /dev/null -w '%{http_code}' -m 20 "
                                        "https://challenges.cloudflare.com/turnstile/v0/api.js", timeout_s=40)
                if reach.stdout.strip() not in ("200", "302"):
                    pytest.skip(f"the machine cannot reach challenges.cloudflare.com ({reach.stdout or reach.stderr})")
                hosts = await envs.exec(env.terminal_lane, "echo '127.0.0.1 shop.example.test' "
                                        "| { tee -a /etc/hosts || sudo -n tee -a /etc/hosts; } >/dev/null && echo mapped", timeout_s=30)
                assert "mapped" in hosts.stdout, hosts
                page_html = FORM % {"key": SITE_KEY, "extra": ""}
                await envs.exec(env.terminal_lane, "mkdir -p /srv/a /srv/b && cat > /srv/a/contact.html && echo "
                                "'<title>Sent</title><h1>Sent</h1>' > /srv/a/done.html", stdin=page_html, timeout_s=30)
                await envs.exec(env.terminal_lane, "cat > /srv/a/ask.html", timeout_s=30,
                                stdin=FORM % {"key": ASK_KEY, "extra": ""})
                await envs.job_start(env.terminal_lane, "cd /srv/a && exec python3 -m http.server 3000 --bind 127.0.0.1")
                await asyncio.sleep(1.0)
                await _run(envs, env, lanes, svc, home, create_interaction, steps, ledger)
            finally:
                settings.bind_home(None)


async def _run(envs, env, lanes, svc, home, create_interaction, steps, ledger):
    lane = env.browser_lane
    await lanes.open(lane)
    interaction = lanes.watch(create_interaction())  # as the node composes it: with its holds
    page = await lanes.live_session(lane).page()

    # a widget a click passes: the browser's own click ends it, and the service is never asked
    await interaction.navigate(page, "http://shop.example.test:3000/ask.html")
    report = await _widget_shows(interaction, page)
    assert (report.signal.value, report.detail) == ("captcha", "Cloudflare Turnstile frame"), report
    out = await steps.get_past(interaction, page, browser=lane, home=home)
    print(f"the interactive widget: {out.ended_by}: {out.steps}")
    assert (out.ended_by, out.passed) == (steps.CLICK, True), out.steps
    assert out.steps[1].startswith("click: clicked the Cloudflare check's checkbox once") and svc.requests == []

    # the contact form: the click does not pass, the owner's solver does
    await interaction.navigate(page, "http://shop.example.test:3000/contact.html?ref=newsletter")
    await _widget_shows(interaction, page)
    snap = await interaction.snapshot(page)
    assert "call browser_challenge once (toolset browser)" in snap.text.lower(), snap.text[:600]
    out = await steps.get_past(interaction, page, browser=lane, home=home)
    print(f"the contact form: {out.ended_by}: {out.steps}")
    assert (out.ended_by, out.passed) == (steps.SOLVER, True), out.steps
    assert out.steps[2].startswith("solver: main (2Captcha) answered the Cloudflare Turnstile widget for 0.00145 USD: "
                                   "the check passed; token in 1 field(s); the page's callback: called")
    assert out.report.signal.value == "ok" and lanes.get(lane).control.state is not ControlState.PAUSED
    task = next(body["task"] for path, body, _ in svc.requests if path == "/createTask")
    assert task == {"type": "TurnstileTaskProxyless", "websiteURL": "http://shop.example.test:3000/contact.html",
                    "websiteKey": SITE_KEY}  # the page's query stays here, and no proxy goes
    assert await page.title() == "Ready to send"

    # the form carries the token to the site
    snap = await interaction.snapshot(page)
    ref = re.search(r'button "Send"(?! \[disabled\])[^\n]*\[ref=((?:f\d+)?e\d+)\]', snap.text).group(1)  # enabled now
    await interaction.act(page, ref, "click")
    await page.wait_for_url(re.compile(r"/done\.html"), timeout=15_000)
    sent = parse_qs(urlsplit(page.url).query)
    assert sent["cf-turnstile-response"] == [DUMMY_TOKEN] and sent["email"] == ["someone@example.test"]

    record = ledger.read(home)
    assert [(s.result, s.site, s.kind, s.cost) for s in record] == [("passed", "shop.example.test", "turnstile", 0.00145)]
    # the key stayed on this computer: not on the machine, not in the log
    # the key never on this command line either: the machine gets it reversed and turns it round itself
    found = await envs.exec(env.terminal_lane, f"k=$(echo {KEY[::-1]} | rev); grep -rIl \"$k\" /home /tmp /srv "
                            "/root /var/log /etc /opt 2>/dev/null; echo end", timeout_s=120)
    assert found.stdout.strip() == "end", found
    assert KEY not in ledger.record_path(home).read_text()


async def _widget_shows(interaction, page):
    """The page's report once Turnstile's frame is on it (api.js renders it after the page loads)."""
    for _ in range(30):
        report = await interaction.report(page)
        if report.signal.value == "captcha":
            return report
        await asyncio.sleep(1.0)
    return report
