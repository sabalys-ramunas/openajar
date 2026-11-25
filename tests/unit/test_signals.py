"""Page signals: vendor challenge rules, rule order, the interaction's report and what tools show."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from openajar.core.models import AccountRef, LaneSpec, NavigateResult, Signal, SnapshotResult
from openajar.node import interaction as ia_mod
from openajar.node.interaction import (
    PlaywrightInteraction,
    bind_lane,
    is_challenge_url,
    lane_expectation,
    last_report,
    take_report,
)
from openajar.node.interaction.readback import _MARKERS_JS
from openajar.node.sites import (
    MARKER_SELECTORS,
    PASSED_FIELDS,
    Frame,
    LaneExpectation,
    PageFacts,
    classify_page,
    classify_signal,
    vendor_rule,
)
from openajar.node.tools import INSTRUCTIONS, page_result, signal_lines

EM_DASH = "—"
A, B, OK, U = Signal.CAPTCHA, Signal.BLOCKED, Signal.OK, Signal.UNKNOWN
SHOP = "https://shop.example/checkout"


def facts(*, frames=(), scripts=(), elements=None, status=200, url=SHOP, cookies=None) -> PageFacts:
    return PageFacts(url, status=status, frames=tuple(frames), scripts=tuple(scripts), elements=elements or {},
                     cookies=cookies)


# ---- vendor frames, scripts and markers --------------------------------------------------------

CHALLENGE_FRAMES = [
    ("https://www.google.com/recaptcha/api2/anchor?ar=1&k=key&co=x&hl=lt&v=1&size=normal", "reCAPTCHA checkbox"),
    ("https://www.google.com/recaptcha/api2/bframe?hl=lt&v=1&k=key", "reCAPTCHA frame"),
    ("https://www.google.com/recaptcha/enterprise/anchor?k=key&size=normal", "reCAPTCHA checkbox"),
    ("https://www.recaptcha.net/recaptcha/api2/anchor?k=key", "reCAPTCHA checkbox"),
    ("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&id=1", "hCaptcha checkbox"),
    ("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge&id=1", "hCaptcha frame"),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/x/normal",
     "Cloudflare Turnstile frame"),
    ("https://client-api.arkoselabs.com/fc/gc/?token=abc", "Arkose (FunCaptcha) frame"),
    ("https://iframe.arkoselabs.com/B7D8911C-5CC8-A9A3-35B0-554ACEE604DA/index.html", "Arkose (FunCaptcha) frame"),
    ("https://linkedin-api.arkoselabs.com/v2/1.5.5/enforcement.abc.html", "Arkose (FunCaptcha) frame"),
    ("https://api.funcaptcha.com/fc/gc/?token=abc", "Arkose (FunCaptcha) frame"),
    ("https://geo.captcha-delivery.com/captcha/?initialCid=abc&hash=h&cid=c&t=fe&referer=x", "DataDome frame"),
    ("https://geo.captcha-delivery.com/interstitial/?initialCid=abc&hash=h", "DataDome frame"),
    ("https://captcha.px-cdn.net/PXabc123/captcha.html", "HUMAN (PerimeterX) frame"),
]


@pytest.mark.parametrize("src, detail", CHALLENGE_FRAMES)
def test_a_visible_challenge_frame_is_a_captcha(src, detail):
    got = classify_page(facts(frames=[Frame(src, True)]))
    assert (got.signal, got.detail) == (A, detail)
    assert is_challenge_url(src)


@pytest.mark.parametrize("src, _detail", CHALLENGE_FRAMES)
def test_a_hidden_or_tiny_challenge_frame_is_not(src, _detail):
    assert classify_page(facts(frames=[Frame(src, False)])).signal is OK


def test_datadome_blocked_visitor_frame_is_blocked_not_a_puzzle():
    src = "https://geo.captcha-delivery.com/captcha/?initialCid=abc&hash=h&cid=c&t=bv&referer=x"
    got = classify_page(facts(frames=[Frame(src, True)], status=403))
    assert (got.signal, got.detail) == (B, "DataDome block frame")
    tail = classify_page(facts(frames=[Frame("https://geo.captcha-delivery.com/captcha/?cid=c&t=bv", True)]))
    assert tail.signal is B


@pytest.mark.parametrize("src", [
    # loaded on normal pages: badges, sensors, pre-checks, API scripts
    "https://www.google.com/recaptcha/api2/anchor?ar=1&k=key&co=x&hl=en&v=1&size=invisible&cb=1",
    "https://www.google.com/recaptcha/enterprise/anchor?k=key&size=invisible",
    "https://www.recaptcha.net/recaptcha/api2/anchor?k=key&size=invisible",
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox-invisible",
])
def test_invisible_widgets_never_count_even_at_badge_size(src):
    assert classify_page(facts(frames=[Frame(src, True)])).signal is OK
    assert not is_challenge_url(src)


@pytest.mark.parametrize("src", [
    "https://www.google.com/recaptcha/api.js?render=key",
    "https://www.gstatic.com/recaptcha/releases/abc/recaptcha__lt.js",
    "https://js.hcaptcha.com/1/api.js",
    "https://challenges.cloudflare.com/turnstile/v0/api.js",
    "https://shop.example/cdn-cgi/challenge-platform/scripts/jsd/main.js",
    "https://client-api.arkoselabs.com/v2/B7D8911C/api.js",
    "https://js.datadome.co/tags.js",
    "https://ct.captcha-delivery.com/c.js",
    "https://client.px-cloud.net/PXabc123/main.min.js",
    "https://collector-pxabc123.perimeterx.net/api/v2/collector",
    "https://client.perimeterx.net/PXabc123/main.min.js",
    "https://captcha.px-cdn.net/PXabc123/captcha.js",
])
def test_a_page_that_merely_loads_a_captcha_or_sensor_script_is_ok(src):
    got = classify_page(facts(scripts=[src]))
    assert got.signal is OK and got.detail is None
    rule = vendor_rule("script", src)
    assert rule is not None and rule.signal is None


def test_cloudflare_challenge_page_by_its_orchestrate_script_or_markers():
    script = "https://shop.example/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=8abc"
    got = classify_page(facts(scripts=[script], status=403))
    assert (got.signal, got.detail) == (A, "Cloudflare challenge page")
    managed = "https://shop.example/cdn-cgi/challenge-platform/h/g/orchestrate/managed/v1?ray=8abc"
    assert classify_page(facts(scripts=[managed], status=503)).signal is A
    for marker in ("#challenge-form", "#challenge-stage", "#cf-challenge-running"):
        # presence is enough: the stage is often hidden while the check runs
        got = classify_page(facts(elements={marker: False}, status=503))
        assert (got.signal, got.detail) == (A, "Cloudflare challenge page")


def test_human_challenge_counts_only_when_the_container_shows():
    assert classify_page(facts(elements={"#px-captcha": True})).detail == "HUMAN (PerimeterX) challenge"
    assert classify_page(facts(elements={"#px-captcha": False})).signal is OK
    assert set(MARKER_SELECTORS) == {"#challenge-form", "#challenge-stage", "#cf-challenge-running", "#px-captcha"}


@pytest.mark.parametrize("src", [
    "https://www.google.com/search?q=recaptcha", "https://example.com/www.google.com/recaptcha/",
    "https://notgoogle.com/recaptcha/api2/anchor", "https://evil-hcaptcha.com.example.net/",
    "https://arkoselabs.com.evil.example/fc/", "https://notcaptcha-delivery.com/captcha/", "about:srcdoc", "",
])
def test_look_alike_frames_are_not_challenges(src):
    assert classify_page(facts(frames=[Frame(src, True)])).signal is OK


# ---- order and generic rules ------------------------------------------------------------------

def test_a_challenge_wins_over_the_status_and_the_site_rules():
    cf = [Frame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/x", True)]
    assert classify_page(facts(frames=cf, status=403)).signal is A
    assert classify_page(facts(frames=cf, status=429)).signal is A
    on_login = facts(frames=cf, url="https://www.instagram.com/accounts/login/", cookies=[])
    assert classify_page(on_login, LaneExpectation("a/ig", frozenset({"instagram"}))).signal is A


@pytest.mark.parametrize("status, signal, detail", [
    (429, Signal.RATE_LIMITED, "HTTP 429"), (403, B, "HTTP 403"), (200, OK, None), (None, OK, None),
    (500, OK, None), (503, OK, None), (404, OK, None), (999, OK, None),
])
def test_generic_statuses(status, signal, detail):
    got = classify_page(facts(status=status))
    assert (got.signal, got.detail) == (signal, detail)


def test_nothing_loaded_or_unreadable_is_unknown():
    assert classify_page(PageFacts("")).signal is U
    assert classify_page(PageFacts("about:blank")).detail == "nothing is loaded"
    got = classify_page(PageFacts(SHOP, readable=False))
    assert (got.signal, got.detail) == (U, "the page could not be read")
    assert classify_page(PageFacts(SHOP, status=429, readable=False)).signal is Signal.RATE_LIMITED


def test_classify_signal_keeps_its_old_contract():
    assert classify_signal(loaded=False, status=429, challenge=True) is U
    assert classify_signal(loaded=True, status=403, challenge=True) is A
    assert classify_signal(loaded=True, status=429, challenge=False) is Signal.RATE_LIMITED
    assert classify_signal(loaded=True, status=403, challenge=False) is B
    assert classify_signal(loaded=True, status=500, challenge=False) is OK


# ---- the interaction's report, on a page double -------------------------------------------------

class FakeContext:
    def __init__(self, cookies=None):
        self.jar = list(cookies or [])
        self.asked: list[list[str]] = []

    async def cookies(self, urls):
        self.asked.append(list(urls))
        return self.jar


class ReadOnlyPage:
    """A page whose only readable surface is what the signal is allowed to use. Every way to read
    text or change the page fails the test."""

    def __init__(self, url, context, markers=None, frames=()):
        self.url, self.context = url, context
        self.markers = markers if markers is not None else {"frames": [], "scripts": [], "elements": {}}
        self.frames = [object(), *frames]
        self.scripts: list[tuple[str, object]] = []

    def on(self, event, handler):
        pass

    async def evaluate(self, script, arg=None):
        self.scripts.append((script, arg))
        return self.markers

    def __getattr__(self, name):
        if name in ("content", "inner_text", "text_content", "inner_html", "locator", "query_selector",
                    "add_init_script", "set_content", "title", "get_by_text", "fill", "click", "dispatch_event"):
            raise AssertionError(f"the signal must not call page.{name}")
        raise AttributeError(name)


async def test_report_reads_cookies_through_the_context_for_the_page_url():
    ctx = FakeContext()
    page = ReadOnlyPage("https://www.instagram.com/explore/", ctx)
    bind_lane(page, LaneSpec(id="biz-a/instagram"))
    got = await PlaywrightInteraction().report(page)
    assert (got.signal, got.detail) == (Signal.LOGGED_OUT, "instagram session cookie sessionid missing")
    assert ctx.asked == [["https://www.instagram.com/explore/"]]
    (script, arg), = page.scripts
    assert script == _MARKERS_JS and arg == [list(MARKER_SELECTORS), 30, list(PASSED_FIELDS)]


async def test_report_without_a_lane_binding_never_says_logged_out():
    page = ReadOnlyPage("https://www.instagram.com/", FakeContext())
    assert lane_expectation(page) is None
    assert (await PlaywrightInteraction().report(page)).signal is OK


async def test_report_names_the_account_and_a_wrong_one():
    ctx = FakeContext([{"name": "sessionid", "value": "s"}, {"name": "ds_user_id", "value": "555"}])
    page = ReadOnlyPage("https://www.instagram.com/", ctx)
    bind_lane(ctx, LaneSpec(id="biz-a/instagram", account=AccountRef(site="instagram", user_id="444")))
    got = await PlaywrightInteraction().report(page)
    assert got.signal is OK and got.wrong_account
    assert got.public()["account"]["status"] == "wrong_account"
    bind_lane(ctx, LaneSpec(id="biz-a/instagram", account=AccountRef(site="instagram", user_id="555")))
    assert (await PlaywrightInteraction().report(page)).account.status == "match"
    bind_lane(ctx, None)
    assert lane_expectation(ctx) is None


async def test_report_sees_challenge_frames_and_reads_no_text():
    markers = {"frames": [{"src": "https://client-api.arkoselabs.com/fc/gc/?token=x", "visible": True}],
               "scripts": ["https://js.datadome.co/tags.js"], "elements": {}}
    page = ReadOnlyPage("https://www.linkedin.com/checkpoint/challenge/x", FakeContext(), markers)
    got = await PlaywrightInteraction().report(page)
    assert (got.signal, got.detail) == (A, "Arkose (FunCaptcha) frame")


async def test_a_passed_widget_is_no_challenge_and_its_token_is_never_read():
    # Turnstile's non-interactive check passed by itself: its frame stays on the page as "Success!"
    frame = {"src": "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/av0/rch/x/0x4AA/auto",
             "visible": True}
    waiting = ReadOnlyPage("https://peet.ws/turnstile-test/non-interactive.html", FakeContext(),
                           {"frames": [frame], "scripts": [], "elements": {}})
    got = await PlaywrightInteraction().report(waiting)
    assert (got.signal, got.detail) == (A, "Cloudflare Turnstile frame")
    passed = ReadOnlyPage("https://peet.ws/turnstile-test/non-interactive.html", FakeContext(),
                          {"frames": [frame], "scripts": [], "elements": {}, "passed": ["cf-turnstile-response"]})
    assert (await PlaywrightInteraction().report(passed)).signal is OK
    assert set(PASSED_FIELDS) == {"cf-turnstile-response", "h-captcha-response", "g-recaptcha-response"}
    assert ".value.length > 0" in _MARKERS_JS and "return {frames, scripts, elements, passed}" in _MARKERS_JS


def test_a_passed_widget_does_not_hide_another_vendors_challenge():
    hcaptcha = Frame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge", True)
    turnstile = Frame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/x", True)
    facts = PageFacts(url="https://example.com/", frames=(turnstile, hcaptcha),
                      passed=frozenset({"cf-turnstile-response"}))
    assert classify_page(facts).detail == "hCaptcha frame"
    assert classify_page(replace(facts, passed=frozenset(PASSED_FIELDS))).signal is OK
    # a Cloudflare challenge page is one by its markers, whatever its widget holds
    page = PageFacts(url="https://www.glassdoor.com/", elements={"#challenge-form": False}, frames=(turnstile,),
                     passed=frozenset({"cf-turnstile-response"}))
    assert classify_page(page).detail == "Cloudflare challenge page"


def test_the_marker_script_reads_structure_only():
    banned = ["innerText", "textContent", "innerHTML", "outerHTML", "document.title", "body.", "setAttribute",
              "dataset", "appendChild", "remove(", "style.", "cookie", "addEventListener", "window.__"]
    for word in banned:
        assert word not in _MARKERS_JS, word
    assert re.search(r"getBoundingClientRect|getComputedStyle", _MARKERS_JS)


class _Frame:
    def __init__(self, url, shown):
        self.url, self.shown, self.asked = url, shown, []

    async def frame_element(self):
        frame = self

        class _El:
            async def evaluate(self, script, arg=None):
                frame.asked.append((script, arg))
                return frame.shown
        return _El()


async def test_challenge_frames_inside_closed_shadow_roots_count_when_they_show():
    turnstile = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/x"
    frame = _Frame(turnstile, True)
    assert (await PlaywrightInteraction().report(ReadOnlyPage(SHOP, FakeContext(), frames=[frame]))).signal is A
    assert frame.asked == [(ia_mod._SHOWN_JS, 30)]
    hidden = ReadOnlyPage(SHOP, FakeContext(), frames=[_Frame(turnstile, False)])
    assert (await PlaywrightInteraction().report(hidden)).signal is OK
    other = ReadOnlyPage(SHOP, FakeContext(), frames=[_Frame("https://cdn.example/ad.html", True)])
    assert (await PlaywrightInteraction().report(other)).signal is OK
    assert "SHOWN" not in _MARKERS_JS and ia_mod._SHOWN_JS in _MARKERS_JS


async def test_take_report_returns_the_last_signal_of_a_lane_use_once():
    ctx = FakeContext()
    ia = PlaywrightInteraction()
    page = ReadOnlyPage("https://www.facebook.com/checkpoint/?next=x", ctx)
    await ia.report(page)
    assert last_report(ctx).signal is Signal.CHECKPOINT
    page.url = "https://www.facebook.com/"
    await ia.report(page)
    assert take_report(page).signal is OK      # the state the use left behind
    assert take_report(page) is None


async def test_an_unknown_read_does_not_hide_a_bad_signal_seen_before():
    ctx = FakeContext()
    ia = PlaywrightInteraction()
    page = ReadOnlyPage("https://www.instagram.com/challenge/", ctx)
    await ia.report(page)
    page.url = "https://shop.example/"
    page.evaluate = _broken                     # the page could not be read
    got = await ia.report(page)
    assert (got.signal, got.detail) == (U, "the page could not be read")
    assert take_report(ctx).signal is Signal.CHECKPOINT


async def test_url_and_cookie_rules_hold_while_the_page_cannot_be_read():
    page = ReadOnlyPage("https://www.instagram.com/challenge/", FakeContext())
    page.evaluate = _broken
    assert (await PlaywrightInteraction().report(page)).signal is Signal.CHECKPOINT
    page = ReadOnlyPage("https://www.instagram.com/", FakeContext())
    page.evaluate = _broken
    bind_lane(page, LaneSpec(id="biz-a/ig"))
    assert (await PlaywrightInteraction().report(page)).signal is Signal.LOGGED_OUT


async def _broken(script, arg=None):
    raise RuntimeError("Execution context was destroyed")


async def test_nothing_loaded_is_unknown_and_remembered_only_when_nothing_better_is_known():
    ctx = FakeContext()
    page = ReadOnlyPage("about:blank", ctx)
    got = await PlaywrightInteraction().report(page)
    assert (got.signal, got.detail) == (U, "nothing is loaded")
    assert take_report(ctx).signal is U


async def test_a_page_double_without_a_cookie_jar_still_gets_a_signal():
    class NoJar:
        pass
    page = ReadOnlyPage("https://www.reddit.com/", NoJar())
    bind_lane(page, LaneSpec(id="personal/reddit"))  # not weak-referenceable is fine too
    assert (await PlaywrightInteraction().report(page)).signal is OK


# ---- what agents see ----------------------------------------------------------------------------

def test_instructions_say_what_to_do_on_every_signal():
    for value in [s.value for s in Signal] + ["signal_detail", "wrong_account", "unpinned", "browser_view",
                                                "needs_human:checkpoint", "agent_paused"]:
        assert value in INSTRUCTIONS, value
    for rule in ("do not retry", "refresh", "never log in again on your own", "must not be tried sooner"):
        assert rule in INSTRUCTIONS
    assert EM_DASH not in INSTRUCTIONS


def test_page_results_show_detail_and_account_only_when_there_is_something_to_say():
    plain = NavigateResult(url="https://x.example/", title="t", status=200, signal=Signal.OK)
    assert page_result("a/b", plain) == {"on": "a/b", "url": "https://x.example/", "title": "t", "status": 200,
                                         "signal": "ok"}
    bad = plain.model_copy(update={"signal": Signal.CHECKPOINT, "signal_detail": "instagram challenge URL",
                                   "account": {"site": "instagram", "user_id": "1", "status": "unpinned"}})
    out = page_result("a/b", bad)
    assert out["signal"] == "checkpoint" and out["signal_detail"] == "instagram challenge URL"
    assert out["account"]["user_id"] == "1"


def test_snapshot_header_lines():
    snap = SnapshotResult(url="u", title="t", text="", ref_count=0, signal=Signal.CAPTCHA,
                          signal_detail="DataDome frame")
    assert signal_lines(snap) == ["signal: captcha (DataDome frame)"]
    assert signal_lines(SnapshotResult(url="u", title="t", text="", ref_count=0)) == ["signal: unknown"]
    wrong = snap.model_copy(update={"signal": Signal.OK, "signal_detail": None, "account": {
        "site": "instagram", "user_id": "555", "status": "wrong_account",
        "expected": {"handle": "@acme.coffee", "user_id": "444"}}})
    assert signal_lines(wrong) == ["signal: ok", ("account: instagram user 555 (wrong_account; the browser expects "
                                                  "@acme.coffee (444). Stop and tell the human)")]
    for line in signal_lines(wrong):
        assert EM_DASH not in line


def test_interaction_module_keeps_its_old_names():
    assert ia_mod.classify_signal is classify_signal


def test_a_puzzle_wins_over_the_checkbox_it_came_from():
    checkbox = Frame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&id=1", True)
    puzzle = Frame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge&id=1", True)
    assert classify_page(PageFacts(url="https://example.com/", frames=(checkbox,))).detail == "hCaptcha checkbox"
    assert classify_page(PageFacts(url="https://example.com/", frames=(checkbox, puzzle))).detail == "hCaptcha frame"
    anchor = Frame("https://www.google.com/recaptcha/api2/anchor?k=key&size=normal", True)
    idle = Frame("https://www.google.com/recaptcha/api2/bframe?k=key", False)  # parked off the page until a puzzle
    assert classify_page(PageFacts(url="https://example.com/", frames=(anchor, idle))).detail == "reCAPTCHA checkbox"
    assert classify_page(PageFacts(url="https://example.com/",
                                   frames=(anchor, replace(idle, visible=True)))).detail == "reCAPTCHA frame"
