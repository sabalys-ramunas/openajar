"""A site's "are you human" checkbox in any browser (openajar.node.interaction.checks): a ref in the snapshot, one
click by a hand on the machine's screen (or the browser's pointer), and the vendor's answer said plainly."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from openajar.core.errors import InvalidRequest, RefNotFound
from openajar.core.models import ActionKind, Signal
from openajar.node import human
from openajar.node.interaction import checks
from openajar.node.interaction.urls import normalize_ref
from openajar.node.sites import SignalReport
from openajar.node.snapshot import is_check_ref

CF_PAGE = SignalReport(Signal.CAPTCHA, "Cloudflare challenge page", "https://www.glassdoor.com/")
PUZZLE = SignalReport(Signal.CAPTCHA, "hCaptcha frame", "https://www.glassdoor.com/")
OK = SignalReport(Signal.OK, None, "https://www.glassdoor.com/")
BOX = {"x": 643.0, "y": 360.0, "width": 480.0, "height": 69.0}  # glassdoor's: the widget centred in a wider box


def widget_png(size=(480, 69), at=(99, 22)) -> bytes:
    image = Image.new("RGB", size, (250, 250, 250))
    draw = ImageDraw.Draw(image)
    if at is None:
        draw.ellipse([100, 22, 124, 46], outline=(30, 140, 60), width=2)  # Turnstile's spinner
        draw.text((134, 27), "Verifying...", fill=(30, 30, 30))
    else:
        draw.rectangle([at[0], at[1], at[0] + 23, at[1] + 23], outline=(102, 102, 102), width=2)
        draw.text((at[0] + 34, at[1] + 5), "Verify you are human", fill=(30, 30, 30))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class Context:  # weakly referenced, as a browser context is
    pass


class Mouse:
    def __init__(self, log):
        self.log = log

    async def move(self, x, y):
        self.log.append(("page move", round(x), round(y)))


class Page:
    """A Cloudflare challenge page: its frame out of Playwright's reach (no URL), its token field in the page."""

    def __init__(self, *, window_off=(0, 0)):
        self.context = Context()
        self.url = "https://www.glassdoor.com/"
        self.frames = [object(), SimpleNamespace(url="")]
        self.log: list[tuple] = []
        self.mouse = Mouse(self.log)
        self.closed = False
        self.window_off = window_off  # where the window really is, against where it says
        self.last_glide_end = None
        self.picture = widget_png()

    def is_closed(self):
        return self.closed

    async def evaluate(self, script, arg=None):
        if script == checks._FIELD_BOX_JS:
            assert arg == "cf-turnstile-response"
            return [BOX]
        if script == checks._VIEW_JS:
            return [77, 190, 1, 1766, 795]
        if script == checks._LISTEN_JS:
            return None
        if script == checks._SEEN_JS:  # the page saw the pointer where the glide ended, less the window's offset
            x, y = self.last_glide_end
            return [x - 77 - self.window_off[0], y - 190 - self.window_off[1]]
        if "innerWidth" in script:
            return [1766, 795]
        raise AssertionError(script)

    async def screenshot(self, clip=None, **kw):
        assert clip == BOX
        return self.picture

    async def wait_for_load_state(self, *a, **kw):
        return None


class Hand:
    def __init__(self, page, ok=True):
        self.page, self.ok = page, ok
        self.calls: list[tuple] = []

    async def glide(self, start, end, ms):
        self.calls.append(("glide", tuple(round(v) for v in start), tuple(round(v) for v in end)))
        self.page.last_glide_end = end
        return self.ok

    async def press_button(self, hold_ms):
        self.calls.append(("press", hold_ms))
        return self.ok


class Ix:
    def __init__(self, *reports):
        self.reports = list(reports)

    async def report(self, page):
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(checks, "OUTCOME_POLL_S", 0.0)
    monkeypatch.setattr(human, "_draw_ms", lambda lo, hi: 0)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(human, "still", nothing)
    pressed = []

    async def press(page):
        page.log.append(("page press",))
    monkeypatch.setattr(human, "_press_release", press)
    return pressed


def test_check_refs_are_refs():
    assert normalize_ref("c1") == "c1" and normalize_ref("[ref=c12]") == "c12"
    assert is_check_ref("c3") and not is_check_ref("e3") and not is_check_ref("f1e3")
    with pytest.raises(InvalidRequest):
        normalize_ref("cx")


async def test_the_snapshot_shows_the_check_with_a_ref_in_any_browser():
    page = Page()  # an account's browser as much as an environment's
    assert await checks.check_lines(page, CF_PAGE) == [
        '- checkbox "Verify you are human (Cloudflare check)" [ref=c1] [cursor=pointer]',
        "- text: a site's check on this browser. Click c1 once (browser_act); an image puzzle after it is for your human"]
    assert await checks.check_lines(page, PUZZLE) == []  # a puzzle is not a click: it goes to the human
    assert await checks.check_lines(page, OK) == []


async def test_a_check_at_work_by_itself_gets_no_ref_but_a_word_to_wait():
    page = Page()
    page.picture = widget_png(at=None)  # "Verifying...": a spinner, no checkbox
    lines = await checks.check_lines(page, CF_PAGE)
    assert lines == [("- text: a Cloudflare check is at work on this browser by itself (no checkbox yet): wait a "
                      "few seconds, then snapshot again")]
    with pytest.raises(RefNotFound):
        await checks.click_check(Ix(CF_PAGE), page, "c1", ActionKind.CLICK)


async def test_the_click_is_a_hand_on_the_machines_screen_and_the_answer_is_said():
    page = Page()
    hand = Hand(page)
    human.X_POINTERS[page.context] = hand
    await checks.check_lines(page, CF_PAGE)
    result = await checks.click_check(Ix(CF_PAGE, OK), page, "c1", ActionKind.CLICK)
    assert result.ok and result.signal is Signal.OK
    assert "on the machine's screen" in result.detail and result.detail.endswith("the check passed")
    (g1, _, near), (g2, start2, end), (press, _) = hand.calls
    assert g1 == g2 == "glide" and press == "press"
    assert near[0] < 77 + 643 and start2 == near  # beside the widget first, on the page itself
    assert end == (77 + 754, 190 + 394)  # then the checkbox the picture shows (not the wide box's middle)
    assert page.log == []  # nothing through the browser's own pointer


async def test_a_window_that_is_not_where_it_says_is_corrected_on_the_way():
    page = Page(window_off=(6, -11))
    hand = Hand(page)
    human.X_POINTERS[page.context] = hand
    await checks.check_lines(page, CF_PAGE)
    await checks.click_check(Ix(CF_PAGE, OK), page, "c1", ActionKind.CLICK)
    assert hand.calls[1][2] == (77 + 754 + 6, 190 + 394 - 11)  # where the page sees the checkbox


async def test_without_the_machines_hand_the_browsers_pointer_clicks():
    page = Page()
    human.X_POINTERS.pop(page.context, None)
    await checks.check_lines(page, CF_PAGE)
    result = await checks.click_check(Ix(CF_PAGE, OK), page, "c1", ActionKind.CLICK)
    assert result.ok and "with the browser's pointer" in result.detail
    assert page.log == [("page move", 754, 394), ("page press",)]


async def test_an_image_puzzle_is_said_plainly_and_goes_to_the_human():
    page = Page()
    human.X_POINTERS[page.context] = Hand(page)
    await checks.check_lines(page, CF_PAGE)
    puzzle = await checks.click_check(Ix(CF_PAGE, PUZZLE), page, "c1", ActionKind.CLICK)
    assert not puzzle.ok and "image puzzle" in puzzle.detail and "browser_wait_handback" in puzzle.detail
    assert puzzle.signal is Signal.CAPTCHA and puzzle.signal_detail == "hCaptcha frame"  # held for the human


async def test_a_check_that_stays_says_so(monkeypatch):
    monkeypatch.setattr(checks, "OUTCOME_S", 0.05)
    page = Page()
    human.X_POINTERS[page.context] = Hand(page)
    await checks.check_lines(page, CF_PAGE)
    result = await checks.click_check(Ix(CF_PAGE), page, "c1", ActionKind.CLICK)
    assert not result.ok and "did not pass" in result.detail and "Do not click it again" in result.detail


async def test_only_a_click_on_a_check_ref_the_page_shows():
    page = Page()
    with pytest.raises(InvalidRequest, match="only action"):
        await checks.click_check(Ix(CF_PAGE), page, "c1", ActionKind.TYPE)
    with pytest.raises(RefNotFound):
        await checks.click_check(Ix(CF_PAGE), page, "c1", ActionKind.CLICK)
