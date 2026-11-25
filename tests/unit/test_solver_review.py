"""The independent review of the solver (2026-11-25): the findings that still apply once the owner's limits went
(the cap, the site list and the page guard), each as a test that failed before its fix.

2. the token goes in only while the page is at the address it was asked for;
3. the hold: an image puzzle is left unheld only when a service could take it, and the tool holds when it ends
   early;
4. a widget inside another site's frame is not sent;
5. a key in a long message is redacted before the message is cut;
6. reCAPTCHA Enterprise is refused even when the page's own markup looks like v2;
8. a malformed answer is a failed solve;
9. Cloudflare's full-page check is refused by its own markers too.
No real service is called: a fake `services.solve`, or a command hook."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import SimpleNamespace

import pytest
from solver_fake import FakeService

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

    async def none(page):
        return []
    monkeypatch.setattr(widgets, "find", found)
    monkeypatch.setattr(checks, "find_checks", none)


def env_page(**kw):
    p = Page(**kw)
    checks.allow(p.context)
    return p


def entry(home, *, provider="command"):
    settings.save(home, [SolverEntry(name="main", provider=provider, command="true", key=KEY)])


# ---- 2. the address the token was asked for --------------------------------------------------------------------

async def test_2_a_page_that_moved_on_the_same_host_does_not_get_the_token(tmp_path, monkeypatch):
    entry(tmp_path)
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


async def test_2_a_page_that_moved_before_the_write_gets_no_token(tmp_path, monkeypatch):
    entry(tmp_path)
    page = env_page()
    asked = []

    async def evaluate(script, arg=None):
        if script == widgets._HAND_JS:
            asked.append(arg[3])
            return {"filled": 0, "called": "none", "moved": True}  # the document's own address was another by then
        return False
    page.evaluate = evaluate

    async def answer(e, job):
        return services.Answer("tok", 0.001)
    monkeypatch.setattr(services, "solve", answer)
    out = await steps.get_past(Ix(), page, browser="env/x", home=tmp_path)
    assert asked == [URL] and out.ended_by == steps.HUMAN
    assert "the token was not used: the page moved on" in out.steps[-2]
    [line] = ledger.read(tmp_path)
    assert (line.result, line.cost) == (ledger.NOT_PASSED, 0.001)


# ---- 3. the hold --------------------------------------------------------------------------------------------

class Lanes:
    def __init__(self):
        self.held: list = []

    def lane_for_page(self, page):
        return "env/e1-browser"

    async def report_signal(self, lane_id, signal, detail=None):
        self.held.append((lane_id, signal, detail))


@pytest.mark.parametrize("why", ["only_built_ins", "enterprise"])
async def test_3_a_puzzle_is_held_when_no_service_could_take_it(tmp_path, monkeypatch, why):
    lanes = Lanes()
    watch = SignalWatch(SimpleNamespace(), lanes)
    page = env_page()
    hcaptcha = widgets.Widget("hcaptcha", "10000000-ffff-ffff-ffff-000000000001")

    async def found(p):
        return (None, "a reCAPTCHA Enterprise widget") if why == "enterprise" else (hcaptcha, "")
    monkeypatch.setattr(widgets, "find", found)
    entry(tmp_path, provider="capsolver" if why == "only_built_ins" else "command")
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
    entry(tmp_path)
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
    e = SolverEntry(name="m", provider="command", command=f"{sys.executable} {script}", key=KEY)
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


# ---- 8. odd answers -------------------------------------------------------------------------------------------

async def test_8_a_service_that_says_ready_with_no_solution_object_is_a_failed_solve(tmp_path):
    with FakeService("2captcha", key=KEY) as svc:
        svc.answer = lambda path, body, headers: (200, {"errorId": 0, "taskId": 7} if path == "/createTask" else
                                                  {"errorId": 0, "status": "ready", "solution": "x"})
        e = SolverEntry(name="main", provider="2captcha", key=KEY, endpoint=svc.url)
        with pytest.raises(services.SolveError, match="no solution object"):
            await services.solve(e, services.Job("turnstile", "k", URL))


# ---- 9. Cloudflare's full-page check by its markers ------------------------------------------------------------

async def test_9_the_full_page_check_is_refused_by_its_markers_whatever_the_frames_say(tmp_path, monkeypatch):
    entry(tmp_path)
    sent = []

    async def solve(e, job):
        sent.append(job)
        return services.Answer("t", 0.001)
    monkeypatch.setattr(services, "solve", solve)
    page = env_page()
    page.full_page = True  # #challenge-form on the page, and the signal read its widget frame first
    out = await steps.get_past(Ix(TURNSTILE), page, browser="env/x", home=tmp_path)
    assert sent == [] and "not for Cloudflare's full-page check" in out.steps[-2]


# ---- a real run (2026-11-25): reCAPTCHA's puzzle stays over the page after the token went in ---------------------

@pytest.mark.parametrize(("seen", "said", "clicked"), [
    ([{"open": True, "point": [4, 4]}, {"open": False}], "was closed with one click outside it", [(4.0, 4.0)]),
    ([{"open": True, "point": None}], "still covers part of the page: a click outside it closes it", []),
    ([{"open": True, "point": [4, 4]}], "still covers part of the page", [(4.0, 4.0)]),  # one click, never more
    ([{"open": False}], "", []),
])
async def test_puzzle_an_open_puzzle_is_closed_by_one_click_on_its_own_overlay(tmp_path, monkeypatch, seen, said,
                                                                               clicked):
    entry(tmp_path)
    page = env_page()
    answers = list(seen)

    async def evaluate(script, arg=None):
        if script == widgets._HAND_JS:
            page.handed.append(arg)
            return {"filled": 1, "called": "called"}
        if script == widgets._PUZZLE_JS:
            return answers.pop(0) if len(answers) > 1 else answers[0]
        return False
    page.evaluate = evaluate
    clicks = []

    async def click(p, point):
        clicks.append(point)
    monkeypatch.setattr(checks, "_page_click", click)
    monkeypatch.setattr(widgets, "CLOSE_WAIT_S", 0.0)

    async def answer(e, job):
        return services.Answer("tok", 0.00299)
    monkeypatch.setattr(services, "solve", answer)
    out = await steps.get_past(Ix(PUZZLE), page, browser="env/x", home=tmp_path)
    assert out.ended_by == steps.SOLVER and clicks == clicked
    assert said in out.steps[-1] and (said or "puzzle" not in out.steps[-1])
