"""A site's "are you human" check that asks for one click on a checkbox, in an environment's browser: the snapshot
shows it with a ref, and browser_act clicks it as a person does.

The checks: Cloudflare's (its "Just a moment..." page and its Turnstile widget), hCaptcha's checkbox and
reCAPTCHA's "I'm not a robot". Each sits in a cross-origin frame, Cloudflare's also in a closed shadow root, so
the page's outline has no node for it. On Cloudflare's own challenge page the frame is out of Playwright's reach
altogether (no URL, no document, and its mouse events never arrive there: measured on a real machine), while the
widget on an ordinary page is not. What a person sees is the same on both, so that is what this reads:

- where: the frame's box when Playwright reaches it, else the box around the widget's token field in the page
  (Turnstile keeps that field outside its shadow root);
- the checkbox in it: a picture of that box, and the square in it (openajar.node.checkbox);
- the click: real input on the machine's X screen (human.x_pointer, the environment's helper): a hand's glide
  from where the page last saw the pointer to a spot just beside the check (where the page says where it saw
  it), a short move onto the checkbox and a press; without that helper, Playwright's pointer (humanize's
  curve) and press, which reach the widget frames but not Cloudflare's challenge page.

After the click it waits for the vendor's answer and says it plainly: passed, an image puzzle (never solved
here: the human takes the browser), or not passed.

Only where an agent may click a check (`allow`): an environment's browser on its machine
(openajar.environments.vmbrowser allows its context). An account's browser never gets a check ref, and its
challenge holds it for the human as it always has (openajar.node.lanes.watch).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import random
import weakref
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from openajar.core.errors import InvalidRequest, RefNotFound
from openajar.core.models import ActionKind, ActionResult, Signal
from openajar.node import agent_pointer, human
from openajar.node.human import quietly
from openajar.node.sites import (
    CHECKBOX_DETAILS,
    HCAPTCHA_CHECKBOX,
    PUZZLE_DETAILS,
    RECAPTCHA_CHECKBOX,
    SignalReport,
    vendor_rule,
)

log = logging.getLogger("openajar.interaction")
Page = Any  # playwright.async_api.Page

NAMES = {"Cloudflare": "Verify you are human (Cloudflare check)",
         "hCaptcha": "I am human (hCaptcha check)",
         "reCAPTCHA": "I'm not a robot (reCAPTCHA check)"}
VENDOR_OF = {HCAPTCHA_CHECKBOX: "hCaptcha", RECAPTCHA_CHECKBOX: "reCAPTCHA"}
CHALLENGE_TOOL = "browser_challenge"  # openajar.node.tools.challenge: wait, click, the owner's solver, the human
OUTCOME_S = 15.0       # how long the vendor gets to answer a click
OUTCOME_POLL_S = 1.0
CORRECT_PX = 3         # the page saw the pointer this far off where it was sent (a moved window): corrected
DEFAULT_OFFSET = (21.0, 0.5)  # in a widget frame's own box: the checkbox's centre, x in px from its left, y share

# Contexts whose agent may click a check (an environment's browser on its machine).
_ALLOWED: weakref.WeakSet[Any] = weakref.WeakSet()
# When a check's checkbox on each tab was last clicked (loop time) and on which address: browser_challenge does not
# click the same page's check twice.
_CLICKED: weakref.WeakKeyDictionary[Any, tuple[float, str]] = weakref.WeakKeyDictionary()
# The checks each page's latest snapshot showed, by ref.
_SHOWN: weakref.WeakKeyDictionary[Any, dict[str, Check]] = weakref.WeakKeyDictionary()

# Cloudflare's token field, outside its closed shadow root: the box of the element around it is the widget's.
_FIELD_BOX_JS = """
(name) => {
  const out = [];
  for (const f of Array.from(document.getElementsByName(name)).slice(0, 10)) {
    if (typeof f.value === 'string' && f.value.length > 0) continue;
    let el = f.parentElement;
    while (el && el !== document.body && el !== document.documentElement) {
      const r = el.getBoundingClientRect();
      if (r.width >= 150 && r.height >= 40) {
        if (r.width <= 1200 && r.height <= 200) out.push({x: r.x, y: r.y, width: r.width, height: r.height});
        break;
      }
      el = el.parentElement;
    }
  }
  return out;
}
"""
_VIEW_JS = "() => [window.mozInnerScreenX || 0, window.mozInnerScreenY || 0, window.devicePixelRatio || 1, " \
           "window.innerWidth, window.innerHeight]"
_LISTEN_JS = """() => { window.__openajarSeen = null; document.addEventListener('mousemove',
  (e) => { window.__openajarSeen = [e.clientX, e.clientY]; }, {capture: true, passive: true}); }"""
_SEEN_JS = "() => window.__openajarSeen || null"


@dataclass(frozen=True, slots=True)
class Check:
    vendor: str                    # "Cloudflare", "hCaptcha" or "reCAPTCHA"
    box: dict[str, float]          # the widget, in the page's viewport
    framed: bool                   # the box is the widget frame's own (else the box around its token field)

    @property
    def name(self) -> str:
        return NAMES[self.vendor]


def allow(context: Any) -> None:
    """This browser's agent may click a check (an environment's browser on its machine)."""
    try:
        _ALLOWED.add(context)
    except TypeError:  # a test's stand-in
        pass


def allowed(page: Any) -> bool:
    try:
        return getattr(page, "context", None) in _ALLOWED
    except TypeError:
        return False


def wants_a_click(report: SignalReport | Any) -> bool:
    """The page's signal is a check that asks for one click (CHECKBOX_DETAILS), not a puzzle."""
    return getattr(report, "signal", None) is Signal.CAPTCHA and getattr(report, "detail", None) in CHECKBOX_DETAILS


async def find_checks(page: Page) -> list[Check]:
    """The checks on the page that wait for a click: their frames Playwright reaches, then Cloudflare's widget by
    its token field. Only reads."""
    found: list[Check] = []
    for frame in list(getattr(page, "frames", []))[1:]:
        rule = vendor_rule("frame", getattr(frame, "url", "") or "")
        if rule is None or rule.detail not in CHECKBOX_DETAILS:
            continue
        element = await quietly(frame.frame_element(), timeout=3.0)
        box = await quietly(element.bounding_box(), timeout=3.0) if element is not None else None
        if box and box["width"] >= 30 and box["height"] >= 30:
            found.append(Check(VENDOR_OF.get(rule.detail, "Cloudflare"), box, True))
    if not any(c.vendor == "Cloudflare" for c in found):
        for box in await quietly(page.evaluate(_FIELD_BOX_JS, "cf-turnstile-response"), [], timeout=3.0) or []:
            found.append(Check("Cloudflare", {k: float(box[k]) for k in ("x", "y", "width", "height")}, False))
    return found


async def check_lines(page: Page, report: SignalReport | Any) -> list[str]:
    """The snapshot's lines for the checks on the page, when this browser's agent may click them: a check that
    shows its checkbox gets a ref (c1, c2...), remembered for act; one still at work by itself ("Verifying...", a
    spinner and no checkbox yet) gets a line that says to wait. When the owner set up a solving service for the
    site (openajar.node.solver), the lines name browser_challenge, and an image puzzle gets a line of its own.
    Empty otherwise."""
    if not allowed(page):
        _forget(page)
        return []
    solver_on = solver_for(page) and await solver_could_take(page, getattr(report, "signal", None),
                                                             getattr(report, "detail", None))
    if not wants_a_click(report):
        _forget(page)
        if solver_on and getattr(report, "signal", None) is Signal.CAPTCHA and \
                getattr(report, "detail", None) in PUZZLE_DETAILS:
            return [(f"- text: an image puzzle ({report.detail}) on this browser. Call {CHALLENGE_TOOL} once (toolset "
                     "browser): it asks the owner's solving service, and hands the browser to your human if that fails")]
        return []
    shown: dict[str, Check] = {}
    lines: list[str] = []
    for check in await find_checks(page):
        if await checkbox_point(page, check) is None:
            lines.append(f"- text: a {check.vendor} check is at work on this browser by itself (no checkbox yet): "
                         "wait a few seconds, then snapshot again" +
                         (f"; if it stays, call {CHALLENGE_TOOL} once (toolset browser)" if solver_on else ""))
            continue
        ref = f"c{len(shown) + 1}"
        shown[ref] = check
        lines.append(f'- checkbox "{check.name}" [ref={ref}] [cursor=pointer]')
        if solver_on:
            lines.append(f"- text: a site's check on this browser. Call {CHALLENGE_TOOL} once (toolset browser): it "
                         f"waits, clicks {ref} once, then asks the owner's solving service, then your human")
        else:
            lines.append(f"- text: a site's check on this browser. Click {ref} once (browser_act); an image puzzle "
                         "after it is for your human")
    try:
        _SHOWN[page] = shown
    except TypeError:
        return []
    return lines


async def solver_could_take(page: Page, signal: Any, detail: str | None) -> bool:
    """A service of the owner's could take this page's check now (openajar.node.solver.steps.could_take): its site
    list, the widget's kind, the sign-up, login and payment guard and its cap. False on any failure to read."""
    if not solver_for(page):
        return False
    from openajar.node.solver.steps import could_take

    try:
        report = SignalReport(Signal(getattr(signal, "value", signal)), detail, getattr(page, "url", "") or "")
        return await could_take(page, report)
    except Exception:  # a page that cannot be read: hold, as without a solver
        log.debug("could not tell whether the owner's solver could take the check", exc_info=True)
        return False


def solver_for(page: Page) -> bool:
    """The owner set up a solving service whose site list covers this page's host (openajar.node.solver)."""
    from openajar.node.solver.settings import on_for

    try:
        return on_for(urlsplit(getattr(page, "url", "") or "").hostname or "")
    except ValueError:
        return False


def _forget(page: Page) -> None:
    try:
        _SHOWN.pop(page, None)
    except TypeError:
        pass


async def checkbox_point(page: Page, check: Check) -> tuple[float, float] | None:
    """The checkbox's centre in the viewport: the square in a picture of the check's box. None when the picture
    shows none (the check is still at work by itself, or it changed). On a node without Pillow, a widget frame's
    own checkbox place, unchecked."""
    try:
        from PIL import Image

        from openajar.node import checkbox
    except ImportError:  # a node without Pillow: only a frame's own box can say where its checkbox is
        checkbox = None  # type: ignore[assignment]
    box = check.box
    if checkbox is not None:
        png = await quietly(page.screenshot(clip=box, type="png", caret="initial", timeout=10_000), timeout=12.0)
        found = await asyncio.to_thread(checkbox.find, Image.open(io.BytesIO(png)).convert("RGB")) if png else None
        return (box["x"] + found[0], box["y"] + found[1]) if found is not None else None
    if check.framed:
        return box["x"] + DEFAULT_OFFSET[0], box["y"] + box["height"] * DEFAULT_OFFSET[1]
    return None


def _waypoint(check: Check, point: tuple[float, float], vw: float, vh: float) -> tuple[float, float]:
    """A spot just outside the check, on the page itself (a frame's events never reach the page), where the hand
    arrives before its last short move onto the checkbox: left of the box when there is room, else below it."""
    box = check.box
    if box["x"] > 40:
        return box["x"] - random.uniform(15, 35), point[1] + random.uniform(-8, 8)
    return point[0] + random.uniform(-8, 8), min(box["y"] + box["height"] + random.uniform(15, 35), vh - 2)


async def _x_click(page: Page, hand: Any, check: Check, point: tuple[float, float]) -> bool:
    """A hand's glide and press on the machine's X screen at `point` (viewport). It arrives first at a spot just
    outside the check, where the page says where it saw the pointer, so the last move lands on the box even
    when the window is not where it claims (a person moved it). False when the machine's hand did not do it
    (the caller then uses Playwright's)."""
    view = await quietly(page.evaluate(_VIEW_JS), timeout=3.0)
    if not view:
        return False
    sx, sy, dpr, vw, vh = (float(v) for v in view)
    dpr = dpr or 1.0
    start = human.POINTERS.get(page) or (vw * random.uniform(0.3, 0.7), vh * random.uniform(0.3, 0.7))
    near = _waypoint(check, point, vw, vh)

    def screen(p: tuple[float, float], off: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
        return (sx + p[0] - off[0]) * dpr, (sy + p[1] - off[1]) * dpr

    await quietly(page.evaluate(_LISTEN_JS), timeout=3.0)
    agent_pointer.told(page, *near)
    ms = human._draw_ms(300, 700) + (abs(near[0] - start[0]) + abs(near[1] - start[1])) * 0.4
    if not await hand.glide(screen(start), screen(near), ms):
        return False
    seen = await quietly(page.evaluate(_SEEN_JS), timeout=3.0)
    off = (seen[0] - near[0], seen[1] - near[1]) if seen and len(seen) == 2 else (0.0, 0.0)
    if abs(off[0]) <= CORRECT_PX and abs(off[1]) <= CORRECT_PX:
        off = (0.0, 0.0)
    agent_pointer.told(page, *point)
    if not await hand.glide(screen(near, off), screen(point, off), human._draw_ms(160, 320)):
        return False
    await asyncio.sleep(human._draw_ms(90, 260) / 1000)
    human.POINTERS[page] = point
    agent_pointer.told_here(page, "press")
    return bool(await hand.press_button(human._draw_ms(*human._locate(page, 50, 230))))


async def _page_click(page: Page, point: tuple[float, float]) -> None:
    """Playwright's pointer: humanize's curve onto the point, a pause, a real press."""
    agent_pointer.told(page, *point)
    await page.mouse.move(*point)
    human.POINTERS[page] = point
    await asyncio.sleep(human._draw_ms(90, 260) / 1000)
    await human._press_release(page)


async def click_check(ix: Any, page: Page, ref: str, kind: ActionKind) -> ActionResult:
    """browser_act on a check's ref: one click on its checkbox, then the vendor's answer."""
    if kind is not ActionKind.CLICK:
        raise InvalidRequest(f"{ref} is a check: the only action on it is click")
    if not allowed(page):
        raise InvalidRequest("a check on this browser is for its human: hand it over (browser_wait_handback)")
    try:
        check = (_SHOWN.get(page) or {}).get(ref)
    except TypeError:
        check = None
    if check is None:
        raise RefNotFound(f"ref {ref} is not a check on the page now. Take a new snapshot.")
    url_before = page.url
    click = await click_once(ix, page, check)
    if click is None:
        raise InvalidRequest(f"the {check.vendor} check shows no checkbox now (it may be at work by itself, or it "
                             "passed): wait a few seconds, then snapshot again")
    if click.how is None:
        raise InvalidRequest(f"the {check.vendor} check is out of view: scroll to it, snapshot again, then click")
    detail = f"clicked the {check.vendor} check's checkbox {click.how}; {OUTCOME_WORDS[click.outcome]}"
    return ActionResult(ok=click.ok, action=ActionKind.CLICK, ref=ref, url=page.url,
                        changed=click.ok or page.url != url_before, detail=detail, **click.report.public())


@dataclass(frozen=True, slots=True)
class Click:
    report: SignalReport
    outcome: str            # PASSED, PUZZLE or NOT_PASSED ("" when no click was made)
    ok: bool
    how: str | None         # "on the machine's screen", "with the browser's pointer"; None: out of view, no click


PASSED, PUZZLE, NOT_PASSED = "passed", "puzzle", "not_passed"
OUTCOME_WORDS = {
    PASSED: "the check passed",
    PUZZLE: ("an image puzzle appeared, which openajar does not solve: tell your human, hand them the browser "
             "(links.browser) and wait with browser_wait_handback"),
    NOT_PASSED: ("the check did not pass: it still asks for a person. Do not click it again in a loop: tell your "
                 "human and wait with browser_wait_handback"),
}


async def click_once(ix: Any, page: Page, check: Check) -> Click | None:
    """One click on `check`'s checkbox (where it is now), then the vendor's answer. None when the check shows no
    checkbox now; a Click with `how` None when it is out of view (nothing was clicked). Only where `allowed`."""
    if not allowed(page):
        raise InvalidRequest("a check on this browser is for its human: hand it over (browser_wait_handback)")
    await human.still(page)
    now = [c for c in await find_checks(page) if c.vendor == check.vendor]
    if now:
        check = min(now, key=lambda c: abs(c.box["x"] - check.box["x"]) + abs(c.box["y"] - check.box["y"]))
    point = await checkbox_point(page, check)
    if point is None:
        return None
    vw, vh = await human._inner_size(page)
    if not (0 <= point[0] < vw and 0 <= point[1] < vh):
        return Click(await ix.report(page), "", False, None)
    await asyncio.sleep(human._draw_ms(500, 1400) / 1000)  # a look before the hand moves
    hand = human.x_pointer(page)
    how = "on the machine's screen"
    if hand is None or not await _x_click(page, hand, check, point):
        how = "with the browser's pointer"
        await _page_click(page, point)
    with contextlib.suppress(TypeError):
        _CLICKED[page] = (asyncio.get_running_loop().time(), str(getattr(page, "url", "") or ""))
    report, outcome = await _outcome(ix, page)
    human.begin_idle(page)
    return Click(report, outcome, outcome == PASSED, how)


def clicked_within(page: Page, seconds: float) -> bool:
    """A check's checkbox on this page (this tab, at this address) was clicked within the last `seconds`
    (browser_act or browser_challenge)."""
    try:
        last = _CLICKED.get(page)
    except TypeError:
        return False
    if last is None or last[1] != str(getattr(page, "url", "") or ""):
        return False
    return asyncio.get_running_loop().time() - last[0] < seconds


async def _outcome(ix: Any, page: Page) -> tuple[SignalReport, str]:
    """Wait for the vendor's answer: (the page's report, PASSED, PUZZLE or NOT_PASSED)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OUTCOME_S
    report = await ix.report(page)
    while loop.time() < deadline and not page.is_closed():
        await asyncio.sleep(OUTCOME_POLL_S)
        report = await ix.report(page)
        if report.signal is Signal.CAPTCHA and report.detail not in CHECKBOX_DETAILS:
            return report, PUZZLE
        if not wants_a_click(report):
            await quietly(page.wait_for_load_state("domcontentloaded", timeout=10_000))
            return await ix.report(page), PASSED
    return report, NOT_PASSED
