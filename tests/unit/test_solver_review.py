"""The independent review of the solver (2026-11-25): each finding as a test that failed before its fix.

1. the cap and the record: a reservation on disk before anything is sent, settled after, the lock let go for the
   network call, a record that cannot be read or written refusing;
2. the sign-up, login and payment guard: run again on the page as it is before the token goes in, more address
   words and the query, every frame, Shopify's card frame (the page scripts: test_solver_page_js);
3. the hold: an image puzzle is left unheld only when a service could really take it now, and the tool holds when
   it ends early;
4. a widget inside another site's frame is not sent;
5. a key in a long message is redacted before the message is cut;
6. reCAPTCHA Enterprise is refused even when the page's own markup looks like v2;
7. `solver add --sites '*'` says what it lets a hostile page do;
8. the record's lines are checked, a malformed answer is a failed solve, a timeout counts its assumed cost;
9. Cloudflare's full-page check is refused by its own markers too.
No real service is called: a fake `services.solve`, or a command hook."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from types import SimpleNamespace

import pytest
from solver_fake import FakeService
from typer.testing import CliRunner

from openajar.core.models import Signal
from openajar.node.interaction import autocheck, checks
from openajar.node.lanes.watch import SignalWatch
from openajar.node.sites import SignalReport
from openajar.node.solver import ledger, services, settings, steps, widgets
from openajar.node.solver.settings import SolverEntry

KEY = f"fake-review-key-{0x5ec2e7:x}abcdef0123456789"
URL = "https://shop.example.com/contact"
TURNSTILE = SignalReport(Signal.CAPTCHA, "Cloudflare Turnstile frame", URL)
PUZZLE = SignalReport(Signal.CAPTCHA, "hCaptcha frame", URL)
OK = SignalReport(Signal.OK, None, URL)
WIDGET = widgets.Widget("turnstile", "0x4AAAAAAAexample00000")


class Context:
    pass


class Page:
    def __init__(self, url=URL, frames=()):
        self.context = Context()
        self.url = url
        self.main_frame = SimpleNamespace(url=url)
        self.frames = [self.main_frame, *frames]
        self.handed: list = []
        self.full_page = False

    def is_closed(self):
        return False

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def evaluate(self, script, arg=None):
        if script == widgets._HAND_JS:
            self.handed.append((self.url, arg))
            return {"filled": 1, "called": "none"}
        if script == widgets._FULL_PAGE_JS:
            return self.full_page
        raise AssertionError(script[:60])


class Ix:
    def __init__(self, report=TURNSTILE):
        self.report_ = report

    async def report(self, page):
        return OK if page.handed else self.report_


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(autocheck, "AUTO_POLL_S", 0.0)
    monkeypatch.setattr(steps, "WAIT_S", 0.01)
    monkeypatch.setattr(steps, "WAIT_OTHER_S", 0.01)
    monkeypatch.setattr(services, "FIRST_POLL_S", 0.0)
    monkeypatch.setattr(services, "POLL_S", 0.01)

    async def found(page):
        return WIDGET, ""

    async def plain(page, w):
        return []

    async def none(page):
        return []
    monkeypatch.setattr(widgets, "find", found)
    monkeypatch.setattr(widgets, "sensitive", plain)
    monkeypatch.setattr(checks, "find_checks", none)


def env_page(**kw):
    p = Page(**kw)
    checks.allow(p.context)
    return p


def entry(home, *, cap=0.01, cost=0.003, provider="command", sites=("example.com",)):
    settings.save(home, [SolverEntry(name="main", provider=provider, command="true", key=KEY, monthly_cap=cap,
                                     sites=list(sites), assumed_cost=cost)])


# ---- 1. the cap and the record --------------------------------------------------------------------------------

async def test_1_a_solve_cut_off_after_it_was_sent_still_counts(tmp_path, monkeypatch):
    entry(tmp_path, cap=0.01, cost=0.003)
    sent = []

    async def slow(e, job):
        sent.append(job)
        await asyncio.sleep(10)
        return services.Answer("t", 0.003)
    monkeypatch.setattr(services, "solve", slow)
    for _ in range(5):
        page = env_page()
        task = asyncio.ensure_future(steps.get_past(Ix(), page, browser="env/x", home=tmp_path))
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    spent = ledger.spent(tmp_path, "main")
    assert len(sent) == 3  # the cap had room for three at the assumed cost: the fourth and fifth were refused
    assert spent == 0.009 and spent <= 0.01
    assert [s.result for s in ledger.read(tmp_path)].count(ledger.CANCELLED) == 3


async def test_1_a_pending_line_nobody_settled_counts_at_its_assumed_cost(tmp_path):
    entry(tmp_path, cap=1.0, cost=0.004)
    base = ledger.Solve(settings.now_iso(), "env/x", "shop.example.com", "turnstile", "main", "command",
                        ledger.PENDING)
    ledger.reserve(tmp_path, base, assumed=0.004, cap=1.0)  # the node stopped here: no settle
    assert ledger.spent(tmp_path, "main") == 0.004
    with pytest.raises(ledger.CapReached):
        ledger.reserve(tmp_path, base, assumed=0.004, cap=0.005)


async def test_1_the_lock_is_not_held_while_the_service_works(tmp_path, monkeypatch):
    entry(tmp_path, cap=1.0)
    in_flight, most = [0], [0]

    async def slow(e, job):
        in_flight[0] += 1
        most[0] = max(most[0], in_flight[0])
        await asyncio.sleep(0.3)
        in_flight[0] -= 1
        return services.Answer("t", 0.001)
    monkeypatch.setattr(services, "solve", slow)
    pages = [env_page(), env_page()]
    outs = await asyncio.gather(*(steps.get_past(Ix(), p, browser="env/x", home=tmp_path) for p in pages))
    assert most[0] == 2 and [o.ended_by for o in outs] == [steps.SOLVER, steps.SOLVER]


@pytest.mark.parametrize("break_it", ["symlink", "unreadable", "unwritable"])
async def test_1_a_record_that_cannot_be_read_or_written_refuses(tmp_path, monkeypatch, break_it):
    if os.geteuid() == 0 and break_it != "symlink":
        pytest.skip("root reads and writes anything")
    entry(tmp_path, cap=1.0)
    sent = []

    async def solve(e, job):
        sent.append(job)
        return services.Answer("t", 0.001)
    monkeypatch.setattr(services, "solve", solve)
    record = ledger.record_path(tmp_path)
    record.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("")
    if break_it == "symlink":
        record.symlink_to(elsewhere)
    elif break_it == "unreadable":
        record.write_text("")
        record.chmod(0)
    else:
        record.parent.chmod(0o500)
    try:
        page = env_page()
        out = await steps.get_past(Ix(), page, browser="env/x", home=tmp_path)
    finally:
        record.parent.chmod(0o700)
        with contextlib.suppress(OSError):
            record.chmod(0o600)
    assert sent == [] and page.handed == [] and out.ended_by == steps.HUMAN
    assert "solve record" in out.steps[-2] and "nothing is sent" in out.steps[-2]


# ---- 2. the guard, again before the token goes in ---------------------------------------------------------------

async def test_2_a_page_that_moved_to_a_login_on_the_same_host_does_not_get_the_token(tmp_path, monkeypatch):
    entry(tmp_path, cap=1.0)
    page = env_page()

    async def nav(e, job):
        page.url = "https://shop.example.com/login"
        return services.Answer("tok", 0.001)
    monkeypatch.setattr(services, "solve", nav)
    out = await steps.get_past(Ix(), page, browser="env/x", home=tmp_path)
    assert page.handed == [] and out.ended_by == steps.HUMAN
    assert "the token was not used: the page moved on" in out.steps[-2]
    [line] = ledger.read(tmp_path)
    assert (line.result, line.cost) == (ledger.NOT_PASSED, 0.001)  # it was paid for all the same


async def test_2_a_page_that_became_a_login_where_it_was_is_not_given_the_token(tmp_path, monkeypatch):
    entry(tmp_path, cap=1.0)
    page = env_page()
    calls = []

    async def guard(p, w):
        calls.append(p.url)
        return [] if len(calls) == 1 else ["a password field"]  # a login form appeared while the service worked
    monkeypatch.setattr(widgets, "sensitive", guard)

    async def answer(e, job):
        return services.Answer("tok", 0.001)
    monkeypatch.setattr(services, "solve", answer)
    out = await steps.get_past(Ix(), page, browser="env/x", home=tmp_path)
    assert page.handed == [] and "now a sign-up, login or payment page (a password field)" in out.steps[-2]


@pytest.mark.parametrize("url", [
    "https://a.example.com/users/sign_in", "https://a.example.com/wp-login.php",
    "https://a.example.com/index.php?route=account/register", "https://a.example.com/session/new",
    "https://a.example.com/checkouts/cn/abc", "https://a.example.com/user-login",
    "https://a.example.com/signin-oidc", "https://a.example.com/u/login/identifier",
    "https://a.example.com/my-account", "https://a.example.com/x?action=login",
])
def test_2_address_words(url):
    assert widgets._path_words(url), url


def test_2_address_words_do_not_catch_a_word_in_the_middle():
    assert widgets._path_words("https://example.com/blog/how-to-login-faster") == []
    assert widgets._path_words("https://example.com/contact?topic=support") == []


async def test_2_every_frame_is_looked_at_for_a_payment_provider(monkeypatch):
    monkeypatch.undo()  # the real guard
    frames = [SimpleNamespace(url=f"https://ads{i}.example.net/", evaluate=_no_reasons) for i in range(14)]
    frames.append(SimpleNamespace(url="https://checkout.shopifycs.com/number?id=1", evaluate=_no_reasons))
    page = Page(frames=frames)

    async def main(script, arg=None):
        return {"why": [], "action": ""}
    page.evaluate = main
    assert "a payment frame (checkout.shopifycs.com)" in await widgets.sensitive(page, WIDGET)


async def test_2_every_frame_is_looked_at_for_a_login_form(monkeypatch):
    monkeypatch.undo()
    frames = [SimpleNamespace(url=f"https://ads{i}.example.net/", evaluate=_no_reasons) for i in range(12)]
    frames.append(SimpleNamespace(url="https://id.example.org/", evaluate=_password))
    page = Page(frames=frames)

    async def main(script, arg=None):
        return {"why": [], "action": ""}
    page.evaluate = main
    assert "a password field in a frame (id.example.org)" in await widgets.sensitive(page, WIDGET)


async def _no_reasons(script, arg=None):
    return {"why": [], "action": ""}


async def _password(script, arg=None):
    return {"why": ["a password field"], "action": ""}


# ---- 3. the hold --------------------------------------------------------------------------------------------

class Lanes:
    def __init__(self):
        self.held: list = []

    def lane_for_page(self, page):
        return "env/e1-browser"

    async def report_signal(self, lane_id, signal, detail=None):
        self.held.append((lane_id, signal, detail))


@pytest.mark.parametrize("why", ["only_built_ins", "cap_reached", "login_page", "enterprise", "unreadable_record"])
async def test_3_a_puzzle_is_held_when_no_service_could_take_it_now(tmp_path, monkeypatch, why):
    lanes = Lanes()
    watch = SignalWatch(SimpleNamespace(), lanes)
    page = env_page()
    hcaptcha = widgets.Widget("hcaptcha", "10000000-ffff-ffff-ffff-000000000001")

    async def found(p):
        return (None, "a reCAPTCHA Enterprise widget") if why == "enterprise" else (hcaptcha, "")
    monkeypatch.setattr(widgets, "find", found)
    entry(tmp_path, cap=0.01, provider="capsolver" if why == "only_built_ins" else "command")
    if why == "cap_reached":
        ledger.reserve(tmp_path, ledger.Solve(settings.now_iso(), "b", "s", "hcaptcha", "main", "command",
                                              ledger.PENDING), assumed=0.009, cap=0.01)
    if why == "login_page":
        async def login(p, w):
            return ["a password field"]
        monkeypatch.setattr(widgets, "sensitive", login)
    if why == "unreadable_record":
        ledger.record_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        ledger.record_path(tmp_path).write_text("not a line\n")
    settings.bind_home(tmp_path)
    try:
        await watch._seen(page, Signal.CAPTCHA, URL, "hCaptcha frame")
    finally:
        settings.bind_home(None)
    assert lanes.held == [("env/e1-browser", Signal.CAPTCHA, "shop.example.com")]


async def test_3_a_puzzle_a_service_could_take_is_left_for_browser_challenge(tmp_path, monkeypatch):
    lanes = Lanes()
    watch = SignalWatch(SimpleNamespace(), lanes)

    async def found(p):
        return widgets.Widget("hcaptcha", "10000000-ffff-ffff-ffff-000000000001"), ""
    monkeypatch.setattr(widgets, "find", found)
    entry(tmp_path, cap=1.0)
    settings.bind_home(tmp_path)
    try:
        await watch._seen(env_page(), Signal.CAPTCHA, URL, "hCaptcha frame")
    finally:
        settings.bind_home(None)
    assert lanes.held == []


@pytest.mark.parametrize("how", ["deadline", "error"])
async def test_3_the_tool_holds_when_it_ends_early_on_a_page_with_a_check(tmp_path, monkeypatch, how):
    from openajar.node.tools import challenge

    page = env_page()

    @contextlib.asynccontextmanager
    async def session(ctx, on, **kw):
        yield page

    @contextlib.asynccontextmanager
    async def unguarded(ctx, on, page):
        yield []
    monkeypatch.setattr(challenge, "driven_session", session)
    monkeypatch.setattr(challenge, "guarded", unguarded)

    async def stuck(ix, p, report):
        if how == "deadline":
            await asyncio.sleep(10)
        raise RuntimeError("the page broke")
    monkeypatch.setattr(steps, "_click", stuck)
    lanes = Lanes()

    class Ctx:
        runtime = SimpleNamespace(interaction=Ix(PUZZLE), lanes=lanes, config=SimpleNamespace(home=tmp_path))

        async def authorize(self, effect, **kw):
            return None
    task = asyncio.ensure_future(challenge.browser_challenge(Ctx(), on="env/e1-browser"))
    if how == "deadline":
        await asyncio.sleep(0.2)
        task.cancel()
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await task
    assert lanes.held == [("env/e1-browser", Signal.CAPTCHA, "shop.example.com")]


# ---- 4. a widget inside another site's frame --------------------------------------------------------------------

async def test_4_a_widget_inside_another_sites_frame_is_not_sent(monkeypatch):
    monkeypatch.undo()
    page = Page()
    third = SimpleNamespace(url="https://forms.other.example.net/embed", parent_frame=page.main_frame)
    anchor = SimpleNamespace(url="https://www.google.com/recaptcha/api2/anchor?k=6LcOtherSitesKey0000000000000000"
                                 "0000000000&size=normal", parent_frame=third)
    page.frames = [page.main_frame, third, anchor]

    async def nothing(script, arg=None):
        return {"widgets": [], "enterprise": False}
    page.evaluate = nothing
    widget, why = await widgets.find(page)
    assert widget is None and "inside a frame of another site" in why
    direct = SimpleNamespace(url=anchor.url, parent_frame=page.main_frame)
    page.frames = [page.main_frame, direct]
    widget, _ = await widgets.find(page)
    assert widget is not None and widget.site_key.startswith("6LcOtherSitesKey")


# ---- 5. the key in a long message -------------------------------------------------------------------------------

async def test_5_a_key_in_a_long_message_is_redacted_before_it_is_cut(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("import json, os\nprint(json.dumps({'error': 'x' * 290 + os.environ['OPENAJAR_SOLVER_KEY']}))\n")
    e = SolverEntry(name="m", provider="command", command=f"{sys.executable} {script}", key=KEY, monthly_cap=1,
                    sites=["*"])
    with pytest.raises(services.SolveError) as err:
        await services.solve(e, services.Job("turnstile", "k", "https://x.example/"))
    shown = str(err.value)
    assert not any(KEY[:n] in shown for n in range(6, len(KEY) + 1)), shown[-60:]
    stderr = tmp_path / "loud.py"
    stderr.write_text("import os, sys\nsys.stderr.write(os.environ['OPENAJAR_SOLVER_KEY'] + 'y' * 290)\nsys.exit(2)\n")
    e2 = e.model_copy(update={"command": f"{sys.executable} {stderr}"})
    with pytest.raises(services.SolveError) as err:
        await services.solve(e2, services.Job("turnstile", "k", "https://x.example/"))
    assert not any(KEY[-n:] in str(err.value) for n in range(6, len(KEY) + 1))


# ---- 6. reCAPTCHA Enterprise ----------------------------------------------------------------------------------

async def test_6_enterprise_is_refused_though_the_markup_looks_like_v2(monkeypatch):
    monkeypatch.undo()
    page = Page()

    async def markup(script, arg=None):
        return {"widgets": [{"kind": "recaptcha_v2", "key": "6LcEnterpriseKey000000000000000000000000",
                             "passed": False}], "enterprise": False}
    page.evaluate = markup
    page.frames = [page.main_frame, SimpleNamespace(
        url="https://www.google.com/recaptcha/enterprise/anchor?k=6LcEnterpriseKey0000", parent_frame=page.main_frame)]
    widget, why = await widgets.find(page)
    assert widget is None and "Enterprise" in why


# ---- 7. --sites '*' ---------------------------------------------------------------------------------------------

def test_7_any_site_says_what_a_hostile_page_can_do(tmp_path, monkeypatch):
    from openajar.cli import app

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    out = CliRunner().invoke(app, ["solver", "add", "s", "webhook", "--cap", "1", "--sites", "*", "--url",
                                   "https://hook.example.net/x"])
    assert out.exit_code == 0 and "a hostile page" in out.output
    out = CliRunner().invoke(app, ["solver", "add", "t", "webhook", "--cap", "1", "--sites", "example.com", "--url",
                                   "https://hook.example.net/x"])
    assert "a hostile page" not in out.output


# ---- 8. the record's lines and odd answers ----------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    {"cost": -1e9}, {"cost": "1"}, {"at": 5}, {"cost": float("nan")}, None,
])
async def test_8_a_line_that_cannot_be_read_never_lowers_the_spend(tmp_path, monkeypatch, line):
    from openajar.cli import app

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    tmp_path = tmp_path / "home"
    entry(tmp_path, cap=0.01)
    sent = []

    async def solve(e, job):
        sent.append(job)
        return services.Answer("t", 0.001)
    monkeypatch.setattr(services, "solve", solve)
    p = ledger.record_path(tmp_path)
    p.parent.mkdir(parents=True)
    good = {"at": settings.now_iso(), "browser": "b", "site": "s", "kind": "turnstile", "service": "main",
            "provider": "command", "result": "passed", "cost": 0.0}
    p.write_bytes(b"\xff\xfe not utf-8\n" if line is None else (json.dumps({**good, **line}) + "\n").encode())
    page = env_page()
    out = await steps.get_past(Ix(), page, browser="env/x", home=tmp_path)
    assert sent == [] and out.ended_by == steps.HUMAN and "line 1 of the solve record" in out.steps[-2]
    for command in (["solver", "list"], ["solver", "log"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1 and "line 1 of the solve record" in result.output, result.output


async def test_8_a_service_that_says_ready_with_no_solution_object_is_a_failed_solve(tmp_path):
    with FakeService("2captcha", key=KEY) as svc:
        svc.answer = lambda path, body, headers: (200, {"errorId": 0, "taskId": 7} if path == "/createTask" else
                                                  {"errorId": 0, "status": "ready", "solution": "x"})
        e = SolverEntry(name="main", provider="2captcha", key=KEY, endpoint=svc.url, monthly_cap=1,
                        sites=["example.com"])
        with pytest.raises(services.SolveError, match="no solution object") as err:
            await services.solve(e, services.Job("turnstile", "k", URL))
    assert err.value.may_have_cost


async def test_8_a_timeout_counts_its_assumed_cost_and_an_error_answer_counts_none(tmp_path, monkeypatch):
    entry(tmp_path, cap=1.0, cost=0.003)

    async def late(e, job):
        raise services.SolveError("no token within 120 s (the service may still charge for it)")
    monkeypatch.setattr(services, "solve", late)
    await steps.get_past(Ix(), env_page(), browser="env/x", home=tmp_path)
    assert ledger.spent(tmp_path, "main") == 0.003

    async def refused(e, job):
        raise services.SolveError("ERROR_ZERO_BALANCE", may_have_cost=False)
    monkeypatch.setattr(services, "solve", refused)
    await steps.get_past(Ix(), env_page(), browser="env/x", home=tmp_path)
    assert ledger.spent(tmp_path, "main") == 0.003  # the error answer added nothing


def test_8_the_cli_test_solve_reserves_like_the_node(tmp_path, monkeypatch):
    from openajar.cli import app

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    script = tmp_path / "hook.py"
    script.write_text("import json\nprint(json.dumps({'token': 'tok', 'cost': 0.004}))\n")
    CliRunner().invoke(app, ["solver", "add", "mine", "command", "--cap", "0.01", "--sites", "*", "--command",
                             f"{sys.executable} {script}", "--assumed-cost", "0.004"])
    home = tmp_path / "home"
    ledger.reserve(home, ledger.Solve(settings.now_iso(), "env/x", "s", "turnstile", "mine", "command",
                                      ledger.PENDING), assumed=0.004, cap=0.01)  # the node is mid-solve
    out = CliRunner().invoke(app, ["solver", "test", "mine"])
    assert out.exit_code == 0, out.output
    out = CliRunner().invoke(app, ["solver", "test", "mine"])
    assert out.exit_code == 1 and "cap is reached" in out.output
    assert ledger.spent(home, "mine") == 0.008


# ---- 9. Cloudflare's full-page check by its markers ------------------------------------------------------------

async def test_9_the_full_page_check_is_refused_by_its_markers_whatever_the_frames_say(tmp_path, monkeypatch):
    entry(tmp_path, cap=1.0)
    sent = []

    async def solve(e, job):
        sent.append(job)
        return services.Answer("t", 0.001)
    monkeypatch.setattr(services, "solve", solve)
    page = env_page()
    page.full_page = True  # #challenge-form on the page, and the signal read its widget frame first
    out = await steps.get_past(Ix(TURNSTILE), page, browser="env/x", home=tmp_path)
    assert sent == [] and "not for Cloudflare's full-page check" in out.steps[-2]
