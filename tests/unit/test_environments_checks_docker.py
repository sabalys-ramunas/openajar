"""A site's "are you human" checkbox in an environment's browser on its machine, for real
(openajar.node.interaction.checks): the docker provider's machine serves a page shaped like Cloudflare's
challenge page from one origin, with its widget frame from another, in a closed shadow root, and the checkbox in
a closed shadow root of the frame's own. Playwright's outline has no node for it; the snapshot shows it with a
ref, the click lands on it as a trusted event with a hand's timing, the page moves on, and the environment's
browser is not held for its human.

Skipped unless docker answers and the image has the browser (see test_environments_vmbrowser_docker)."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import unquote, urlsplit

import pytest
from test_environments_vmbrowser_docker import _ready, docker_node

from openajar.core.models import ControlState

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker with the environments image built with its browser")

OUTER = """<!doctype html><title>Just a moment...</title><body style="margin:0;font:16px sans-serif">
<div id="challenge-form" style="display:none"></div>
<h1 style="margin:40px">Verify you are human by completing the action below.</h1>
<div style="margin:40px 0 0 300px;width:480px;height:69px;text-align:center"><div id="host"></div>
<input type="hidden" name="cf-turnstile-response" value=""></div>
<script>
const root = document.getElementById('host').attachShadow({mode: 'closed'});
const f = document.createElement('iframe');
f.src = 'http://127.0.0.1:3001/widget.html';
f.style.cssText = 'width:300px;height:65px;border:0';
root.appendChild(f);
addEventListener('message', (e) => {
  if (!e.data || e.data.kind !== 'solved') return;
  document.getElementsByName('cf-turnstile-response')[0].value = 'token';
  setTimeout(() => location.replace('/done.html#' + encodeURIComponent(JSON.stringify(e.data))), 300);
});
</script></body>"""
WIDGET = """<!doctype html><body style="margin:0;background:#fafafa"><div id="w"></div><script>
const root = document.getElementById('w').attachShadow({mode: 'closed'});
root.innerHTML = `<div style="display:flex;align-items:center;height:63px;border:1px solid #e0e0e0;padding-left:8px">
  <label style="display:flex;align-items:center;font:15px sans-serif"><input type="checkbox" id="cb"
  style="appearance:none;width:24px;height:24px;border:2px solid #666;margin:0 10px 0 0;background:#fff">
  Verify you are human</label></div>`;
const moves = []; let down = null, up = null;
addEventListener('mousemove', (e) => moves.push(e.timeStamp), true);
root.addEventListener('pointerdown', (e) => { down = e; }, true);
root.addEventListener('pointerup', (e) => { up = e; }, true);
root.getElementById('cb').addEventListener('click', (e) => parent.postMessage({kind: 'solved', trusted: e.isTrusted,
  moves: moves.length, held_ms: down && up ? Math.round(up.timeStamp - down.timeStamp) : null,
  still_ms: down && moves.length ? Math.round(down.timeStamp - moves[moves.length - 1]) : null,
  pointer: down && down.pointerType, button: e.button}, '*'));
</script></body>"""


async def test_the_agent_sees_and_clicks_a_check_in_a_closed_shadow_root_across_origins(tmp_path, monkeypatch):
    from openajar.node.interaction import autocheck, create_interaction

    monkeypatch.setattr(autocheck, "AUTO_CHECK_S", 2.0)  # this page never passes by itself
    async with docker_node(tmp_path) as node:
        mgr, envs, lanes, grant = node["mgr"], node["envs"], node["lanes"], node["grant"]
        eid = mgr.planned_id("session:c1", "A check")
        plan = await mgr.plan(eid, size=None, hours=1)
        env = await mgr.create(eid, plan, binding="session:c1", title="A check", grant_id=grant.id,
                               grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
        env = await mgr.wait_ready(env.id, 600)
        assert env.state == "ready", env.error
        await envs.exec(env.terminal_lane, "mkdir -p /srv/a /srv/b && cat > /srv/a/index.html && "
                        "echo '<title>Through</title><h1>Through</h1>' > /srv/a/done.html", stdin=OUTER, timeout_s=30)
        await envs.exec(env.terminal_lane, "cat > /srv/b/widget.html", stdin=WIDGET, timeout_s=30)
        for port, root in ((3000, "/srv/a"), (3001, "/srv/b")):
            await envs.job_start(env.terminal_lane, f"cd {root} && exec python3 -m http.server {port} --bind 127.0.0.1")
        await asyncio.sleep(1.0)

        lane = env.browser_lane
        await lanes.open(lane)
        interaction = lanes.watch(create_interaction())  # as the node composes it: with its holds
        page = await lanes.live_session(lane).page()
        nav = await interaction.navigate(page, "http://localhost:3000/")
        assert (nav.signal.value, nav.signal_detail) == ("captcha", "Cloudflare challenge page")

        snap = await interaction.snapshot(page)
        line = '- checkbox "Verify you are human (Cloudflare check)" [ref=c1] [cursor=pointer]'
        assert snap.text.splitlines()[0] == line, snap.text[:400]
        assert snap.text.count("Verify you are human") == 2  # the check's line and the page's heading: no other node
        assert snap.text.splitlines()[1].startswith("- text: a site's check on this browser. Click c1 once")
        assert lanes.get(lane).control.state is not ControlState.PAUSED  # its agent may click it

        result = await interaction.act(page, "c1", "click")
        print(f"the check: {result.detail}")
        assert result.ok and result.detail.endswith("the check passed"), result
        assert result.signal.value == "ok" and lanes.get(lane).control.state is not ControlState.PAUSED
        assert urlsplit(page.url).path == "/done.html"
        seen = json.loads(unquote(urlsplit(page.url).fragment))
        print(f"what the widget saw: {seen}")
        assert seen["trusted"] is True and seen["button"] == 0 and seen["pointer"] == "mouse"
        assert seen["moves"] >= 3  # the pointer came along a path into the frame, not a jump
        assert 30 <= seen["held_ms"] <= 400  # a press held as a hand holds it
        assert seen["still_ms"] >= 40  # the hand settles before it presses
        assert re.search(r"on the machine's screen|with the browser's pointer", result.detail)
