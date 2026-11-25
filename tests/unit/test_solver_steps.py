"""browser_challenge's order (openajar.node.solver.steps): wait, the browser's own click, the owner's solver, the
human, each said plainly; what the solver step does not send (no solver, Cloudflare's full-page check, a widget no
service of the owner's takes); all the same in an account's browser as in an environment's."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from solver_fake import DUMMY_TOKEN, FakeService

from openajar.core.models import Signal
from openajar.node.interaction import autocheck, checks
from openajar.node.lanes.watch import SignalWatch
from openajar.node.sites import SignalReport
from openajar.node.solver import ledger, services, settings, steps, widgets
from openajar.node.solver.settings import SolverEntry

KEY = f"fake-solver-key-{0x77d0:x}"
URL = "https://shop.example.com/contact?order=42"
TURNSTILE = SignalReport(Signal.CAPTCHA, "Cloudflare Turnstile frame", URL)
CF_PAGE = SignalReport(Signal.CAPTCHA, "Cloudflare challenge page", URL)
PUZZLE = SignalReport(Signal.CAPTCHA, "reCAPTCHA frame", URL)
CHECKBOX = SignalReport(Signal.CAPTCHA, "reCAPTCHA checkbox", URL)
OK = SignalReport(Signal.OK, None, URL)
WIDGET = widgets.Widget("turnstile", "0x4AAAAAAAexample00000", action="contact", callback="onPass")


class Context:  # weakly referenced, as a browser context is
    pass


class Page:
    def __init__(self, url=URL, frames=()):
        self.context = Context()
        self.url = url
        self.frames = [object(), *frames]
        self.handed: list = []

    def is_closed(self):
        return False

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def evaluate(self, script, arg=None):
        if script == widgets._HAND_JS:
            self.handed.append(arg)
            return {"filled": 1, "called": "called"}
        raise AssertionError(script[:80])


class Ix:
    """The page's reports in turn (the last one stays), and after a token is handed, `after`."""

    def __init__(self, page, *reports, after=OK):
        self.page, self.reports, self.after = page, list(reports), after

    async def report(self, page):
        if page.handed:
            return self.after
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(autocheck, "AUTO_POLL_S", 0.0)
    monkeypatch.setattr(steps, "WAIT_S", 0.05)
    monkeypatch.setattr(steps, "WAIT_OTHER_S", 0.05)
    monkeypatch.setattr(services, "FIRST_POLL_S", 0.0)
    monkeypatch.setattr(services, "POLL_S", 0.01)

    async def found(page):
        return WIDGET, ""

    monkeypatch.setattr(widgets, "find", found)

    async def no_checks(page):
        return []
    monkeypatch.setattr(checks, "find_checks", no_checks)


def env_page(**kw) -> Page:
    page = Page(**kw)
    page.lane = "env/e1-browser"
    return page


def account_page(**kw) -> Page:
    page = Page(**kw)
    page.lane = "acme/linkedin"
    return page


def solver(home, url, *, provider="2captcha"):
    settings.save(home, [SolverEntry(name="main", provider=provider, key=KEY, endpoint=url)])


# ---- the order ------------------------------------------------------------------------------------------------

async def test_an_accounts_browser_goes_through_every_step_as_any_browser(tmp_path):
    page = account_page()
    with FakeService("2captcha", key=KEY) as svc:
        solver(tmp_path, svc.url)
        out = await steps.get_past(Ix(page, PUZZLE), page, browser="acme/linkedin", home=tmp_path)
    assert (out.ended_by, out.passed) == (steps.SOLVER, True) and len(page.handed) == 1
    assert ledger.read(tmp_path)[0].browser == "acme/linkedin"


async def test_no_check_is_nothing_to_do(tmp_path):
    page = env_page()
    out = await steps.get_past(Ix(page, OK), page, browser="env/e1-browser", home=tmp_path)
    assert (out.ended_by, out.passed) == (steps.NOTHING, True)


async def test_a_check_that_passes_by_itself_ends_at_the_wait(tmp_path):
    page = env_page()
    out = await steps.get_past(Ix(page, TURNSTILE, TURNSTILE, OK), page, browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.WAITED and out.passed and "passed by itself" in out.steps[0]


async def test_the_browsers_own_click_comes_before_any_solver(tmp_path, monkeypatch):
    page = env_page()
    check = checks.Check("reCAPTCHA", {"x": 10.0, "y": 10.0, "width": 300.0, "height": 70.0}, True)

    async def one(p):
        return [check]

    async def click(ix, p, c):
        return checks.Click(OK, checks.PASSED, True, "on the machine's screen")
    monkeypatch.setattr(checks, "find_checks", one)
    monkeypatch.setattr(checks, "click_once", click)
    with FakeService("2captcha", key=KEY) as svc:
        solver(tmp_path, svc.url)
        out = await steps.get_past(Ix(page, CHECKBOX), page, browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.CLICK and out.passed and svc.requests == []
    assert out.steps[-1] == "click: clicked the reCAPTCHA check's checkbox once on the machine's screen: it passed"


async def test_with_no_solver_a_check_the_click_did_not_pass_goes_to_the_human(tmp_path, monkeypatch):
    page = env_page()
    check = checks.Check("reCAPTCHA", {"x": 10.0, "y": 10.0, "width": 300.0, "height": 70.0}, True)

    async def one(p):
        return [check]

    async def click(ix, p, c):
        return checks.Click(PUZZLE, checks.PUZZLE, False, "with the browser's pointer")
    monkeypatch.setattr(checks, "find_checks", one)
    monkeypatch.setattr(checks, "click_once", click)
    out = await steps.get_past(Ix(page, CHECKBOX), page, browser="env/e1-browser", home=tmp_path)
    assert (out.ended_by, out.passed) == (steps.HUMAN, False)
    assert out.steps == ["waited 0 s: it did not pass by itself",
                         ("click: clicked the reCAPTCHA check's checkbox once with the browser's pointer: an image "
                          "puzzle appeared"),
                         "solver: the owner has none set up", "handed to your human"]


async def test_the_owners_solver_answers_a_puzzle_the_click_left(tmp_path):
    page = env_page()
    with FakeService("2captcha", key=KEY) as svc:
        solver(tmp_path, svc.url)
        out = await steps.get_past(Ix(page, PUZZLE), page, browser="env/e1-browser", home=tmp_path)
    assert (out.ended_by, out.passed) == (steps.SOLVER, True)
    assert out.steps[1] == "click: no checkbox to click (an image puzzle shows)"
    assert out.steps[2].startswith("solver: main (2Captcha) answered the Cloudflare Turnstile widget for 0.00145 USD: "
                                   "the check passed; token in 1 field(s); the page's callback: called")
    assert page.handed == [["turnstile", DUMMY_TOKEN, "onPass", "https://shop.example.com/contact?order=42"]]
    task = svc.requests[0][1]["task"]
    assert task["websiteURL"] == "https://shop.example.com/contact"  # never the query
    said = json.dumps(out.steps)
    assert KEY not in said and DUMMY_TOKEN not in said
    [line] = ledger.read(tmp_path)
    assert (line.site, line.kind, line.service, line.provider, line.result, line.cost) == \
        ("shop.example.com", "turnstile", "main", "2captcha", ledger.PASSED, 0.00145)
    assert line.browser == "env/e1-browser" and KEY not in ledger.record_path(tmp_path).read_text()
    assert DUMMY_TOKEN not in ledger.record_path(tmp_path).read_text()


async def test_a_service_that_reports_no_cost_is_logged_with_none(tmp_path):
    page = env_page()
    with FakeService("capsolver", key=KEY) as svc:
        solver(tmp_path, svc.url, provider="capsolver")
        out = await steps.get_past(Ix(page, PUZZLE), page, browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.SOLVER and "for a cost it did not report" in out.steps[2]
    assert ledger.read(tmp_path)[0].cost is None


async def test_a_token_that_does_not_take_goes_to_the_human(tmp_path):
    page = env_page()
    with FakeService("2captcha", key=KEY) as svc:
        solver(tmp_path, svc.url)
        out = await steps.get_past(Ix(page, PUZZLE, after=PUZZLE), page, browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.HUMAN and "the check still shows" in out.steps[2]
    assert ledger.read(tmp_path)[0].result == ledger.NOT_PASSED


async def test_a_service_that_gives_no_token_goes_to_the_human(tmp_path):
    page = env_page()
    with FakeService("anticaptcha", key=KEY, unsolvable=True) as svc:
        solver(tmp_path, svc.url, provider="anticaptcha")
        out = await steps.get_past(Ix(page, PUZZLE), page, browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.HUMAN and "gave no token: ERROR_CAPTCHA_UNSOLVABLE" in out.steps[2]
    assert page.handed == [] and ledger.read(tmp_path)[0].result == ledger.FAILED


# ---- what no token answers ---------------------------------------------------------------------------------------------

async def test_cloudflares_full_page_check_is_never_sent(tmp_path):
    page = env_page()
    with FakeService("2captcha", key=KEY) as svc:
        solver(tmp_path, svc.url)
        out = await steps.get_past(Ix(page, CF_PAGE), page, browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.HUMAN and "not for Cloudflare's full-page check" in out.steps[2]
    assert "proxy's credentials" in out.steps[2] and svc.requests == []


async def test_a_widget_the_service_does_not_take_is_refused(tmp_path, monkeypatch):
    page = env_page()

    async def hcaptcha(p):
        return widgets.Widget("hcaptcha", "10000000-ffff-ffff-ffff-000000000001"), ""
    monkeypatch.setattr(widgets, "find", hcaptcha)
    with FakeService("capsolver", key=KEY) as svc:
        solver(tmp_path, svc.url, provider="capsolver")
        out = await steps.get_past(Ix(page, SignalReport(Signal.CAPTCHA, "hCaptcha frame", URL)), page,
                                   browser="env/e1-browser", home=tmp_path)
    assert out.ended_by == steps.HUMAN and "takes hCaptcha" in out.steps[2] and svc.requests == []


# ---- the hold, the snapshot's line, the tool ------------------------------------------------------------------

class Lanes:
    def __init__(self):
        self.held: list = []

    def lane_for_page(self, page):
        return getattr(page, "lane", "env/e1-browser")

    async def report_signal(self, lane_id, signal, detail=None):
        self.held.append((lane_id, signal, detail))


async def test_a_puzzle_holds_any_browser_only_without_a_solver_and_a_checkbox_holds_none(tmp_path):
    lanes = Lanes()
    watch = SignalWatch(SimpleNamespace(), lanes)
    env, account = env_page(), account_page()
    settings.bind_home(tmp_path)
    try:
        for page in (env, account):
            await watch._seen(page, Signal.CAPTCHA, URL, "reCAPTCHA checkbox")  # the agent has its ref
        assert lanes.held == []
        for page in (env, account):
            await watch._seen(page, Signal.CAPTCHA, URL, "reCAPTCHA frame")
        assert [h[0] for h in lanes.held] == ["env/e1-browser", "acme/linkedin"]  # no solver: held, either kind
        lanes.held.clear()
        solver(tmp_path, "https://api.2captcha.com")
        for page in (env, account):
            await watch._seen(page, Signal.CAPTCHA, URL, "reCAPTCHA frame")
            await watch._seen(page, Signal.CAPTCHA, URL, "Cloudflare challenge page")
        assert lanes.held == []  # the owner's solver could take it: browser_challenge first, in either kind
        other = env_page(url="https://other.example.org/")
        await watch._seen(other, Signal.CAPTCHA, other.url, "hCaptcha frame")
        assert lanes.held == []  # any site: there is no site list
    finally:
        settings.bind_home(None)


@pytest.mark.parametrize(("ok", "detail", "solver_on", "held"), [
    (False, "Cloudflare challenge page", False, True),  # the click did not reach it (no X hand): the human's
    (False, "reCAPTCHA frame", False, True),            # a puzzle and no solver: the human's
    (False, "reCAPTCHA frame", True, False),            # a puzzle a solver could take: browser_challenge first
    (True, None, False, False),                          # it passed
])
async def test_a_click_on_a_check_that_did_not_get_past_it_holds_the_browser(tmp_path, ok, detail, solver_on, held):
    from openajar.core.models import ActionKind, ActionResult

    page = account_page()
    result = ActionResult(ok=ok, action=ActionKind.CLICK, ref="c1", url=URL, changed=ok,
                          signal=Signal.OK if ok else Signal.CAPTCHA, signal_detail=detail)

    async def act(p, ref, kind, text=None):
        return result
    lanes = Lanes()
    watch = SignalWatch(SimpleNamespace(act=act), lanes)
    settings.bind_home(tmp_path)
    try:
        if solver_on:
            solver(tmp_path, "https://api.2captcha.com")
        assert await watch.act(page, "c1", ActionKind.CLICK) is result
    finally:
        settings.bind_home(None)
    holds = [h for h in lanes.held if h[1] is Signal.CAPTCHA]  # an ok signal is recorded, and holds nothing
    assert holds == ([("acme/linkedin", Signal.CAPTCHA, "shop.example.com")] if held else [])


async def test_the_snapshot_names_browser_challenge_only_when_the_owner_has_a_solver(tmp_path, monkeypatch):
    page = env_page()
    box = {"x": 10.0, "y": 10.0, "width": 300.0, "height": 70.0}

    async def one(p):
        return [checks.Check("Cloudflare", box, True)]

    async def point(p, c):
        return (30.0, 45.0)
    monkeypatch.setattr(checks, "find_checks", one)
    monkeypatch.setattr(checks, "checkbox_point", point)
    settings.bind_home(tmp_path)
    try:
        lines = await checks.check_lines(page, TURNSTILE)
        assert lines[1] == ("- text: a site's check on this browser. Click c1 once (browser_act); an image puzzle "
                            "after it is for your human")
        assert await checks.check_lines(page, PUZZLE) == []
        solver(tmp_path, "https://api.2captcha.com")
        lines = await checks.check_lines(page, TURNSTILE)
        assert "Call browser_challenge once (toolset browser): it waits, clicks c1 once, then asks the owner's " \
               "solving service, then your human" in lines[1]
        [line] = await checks.check_lines(page, PUZZLE)
        assert "image puzzle (reCAPTCHA frame)" in line and "browser_challenge" in line
        assert await checks.check_lines(account_page(), PUZZLE) == [line]  # an account's browser: the same line
    finally:
        settings.bind_home(None)


class Ctx:
    def __init__(self, runtime):
        self.runtime = runtime
        self.authorized: list = []

    async def authorize(self, effect, **kw):
        self.authorized.append((effect, kw))


async def test_the_tool_runs_on_an_accounts_browser_as_on_any(tmp_path, monkeypatch):
    import contextlib

    from openajar.node.tools import challenge

    page = account_page()

    @contextlib.asynccontextmanager
    async def session(ctx, on, **kw):
        yield page

    @contextlib.asynccontextmanager
    async def unguarded(ctx, on, page):
        yield []
    monkeypatch.setattr(challenge, "driven_session", session)
    monkeypatch.setattr(challenge, "guarded", unguarded)
    lanes = Lanes()
    ctx = Ctx(SimpleNamespace(interaction=Ix(page, CF_PAGE), lanes=lanes, config=SimpleNamespace(home=tmp_path)))
    out = await challenge.browser_challenge(ctx, on="acme/linkedin")
    assert ctx.authorized and ctx.authorized[0][1]["lane_id"] == "acme/linkedin"
    assert out["ended_by"] == "human" and lanes.held == [("acme/linkedin", Signal.CAPTCHA, "shop.example.com")]


async def test_the_tool_says_which_step_ended_it_and_holds_for_the_human(tmp_path, monkeypatch):
    import contextlib

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
    lanes = Lanes()
    ctx = Ctx(SimpleNamespace(interaction=Ix(page, CF_PAGE), lanes=lanes, config=SimpleNamespace(home=tmp_path)))
    out = await challenge.browser_challenge(ctx, on="env/e1-browser")
    assert (out["ended_by"], out["passed"], out["signal"]) == ("human", False, "captcha")
    assert "browser_wait_handback" in out["next"] and out["steps"][-1] == "handed to your human"
    assert lanes.held == [("env/e1-browser", Signal.CAPTCHA, "shop.example.com")]
    with FakeService("2captcha", key=KEY) as svc:
        solver(tmp_path, svc.url)
        lanes.held.clear()
        ctx.runtime.interaction = Ix(page, PUZZLE)
        out = await challenge.browser_challenge(ctx, on="env/e1-browser")
    assert (out["ended_by"], out["passed"]) == ("solver", True) and lanes.held == []
    assert KEY not in json.dumps(out) and DUMMY_TOKEN not in json.dumps(out)



async def test_a_click_counts_for_its_own_page_only():
    page = env_page()
    checks._CLICKED[page] = (asyncio.get_running_loop().time(), page.url)
    assert checks.clicked_within(page, 60)
    page.url = "https://shop.example.com/other"
    assert not checks.clicked_within(page, 60)
