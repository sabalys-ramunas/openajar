"""Human-like input on top of Camoufox's humanize cursor (async port of poc/human.py).

Camoufox (humanize=True) already turns every pointer move into a curved, human-speed
path, and every event is real browser input (isTrusted). These helpers cover what it
does not: scrolling with the mouse wheel instead of jumping to an element, a
look-before-you-act pause, a random point inside the target instead of its exact
center, a real press duration, and typing key by key with a hand's timing (held keys, rollover, Shift,
the odd typo fixed with Backspace).

Every helper that takes a `target` accepts a Playwright Locator or an ElementHandle
(both have `bounding_box()` and `evaluate()`). Nothing here mutates the page: the only
scripts we run read layout and focus state.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import random
import string
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.node import agent_pointer
from openajar.node.human_pace import _Pace

Page = Any    # playwright.async_api.Page
Target = Any  # playwright.async_api.Locator | playwright.async_api.ElementHandle

MAX_WHEEL_STEPS = 80

# The nearest ancestor that scrolls vertically, clipped to the viewport, so the pointer rests
# over the area the wheel must move. Falls back to the whole viewport (document scroll).
_SCROLL_AREA_JS = """
(el) => {
  const vw = window.innerWidth, vh = window.innerHeight;
  let node = el.parentElement;
  while (node && node !== document.body && node !== document.documentElement) {
    const style = getComputedStyle(node);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) {
      const r = node.getBoundingClientRect();
      const left = Math.max(r.left, 0), top = Math.max(r.top, 0);
      const right = Math.min(r.right, vw), bottom = Math.min(r.bottom, vh);
      if (right - left > 20 && bottom - top > 20) return {x: left, y: top, width: right - left, height: bottom - top};
    }
    node = node.parentElement;
  }
  return {x: 0, y: 0, width: vw, height: vh};
}
"""

# Would a press at (x, y) reach the target? Follows open shadow roots, and accepts the target's own
# <label>. (x, y) are the top page's; inside a frame the answer is "frame" unless `local` says the
# point was already carried into this frame's own coordinates (_hits_through_frames).
_HITS_TARGET_JS = """
(el, [x, y, local]) => {
  if (window !== window.top && !local) return 'frame';
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit) return false;
  for (let n = hit; n; n = n.parentNode || n.host) if (n === el) return true;
  for (const label of el.labels || []) if (label === hit || label.contains(hit)) return true;
  return false;
}
"""

# Run on a frame element in its parent document: does a press at (x, y) of the parent's viewport
# reach this frame (and not an element laid over it)? The point in the frame's own viewport, or null.
_FRAME_POINT_JS = """
(fe, [x, y]) => {
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (hit !== fe) return null;
  const r = fe.getBoundingClientRect(), cs = getComputedStyle(fe);
  return [x - r.left - fe.clientLeft - parseFloat(cs.paddingLeft || '0'),
          y - r.top - fe.clientTop - parseFloat(cs.paddingTop || '0')];
}
"""


class TargetNotVisible(InvalidRequest):
    """The element has no box on the page (hidden, detached or zero size)."""
    code = "target_not_visible"


class TargetCovered(InvalidRequest):
    """Another element (a banner, a dialog, a sticky header) sits on top of the target."""
    code = "target_covered"


async def quietly(awaitable: Any, default: Any = None, *, timeout: float | None = None) -> Any:
    """Await a read that may fail while the page changes under us (navigation, detach). With a
    `timeout` (seconds), a page that never answers gives `default` too, once the time is up."""
    try:
        async with asyncio.timeout(timeout):
            return await awaitable
    except Exception:  # noqa: BLE001 - any browser-side failure (or no answer in time) means "could not read"
        return default


def _draw_ms(lo: int, hi: int) -> int:
    """A time between `lo` and `hi`, skewed like a person's: log-normal, most near the geometric
    middle and a long tail upwards. A flat range is what `random.randint` leaves and people do not
    (the red team's pooled checks)."""
    if hi <= lo:
        return lo
    low, ratio = max(lo, 1), hi / max(lo, 1)
    # the peak sits low in the range, so the tail is real: centred, the cut range reads as flat
    middle, spread = low * ratio ** 0.3, math.log(ratio) / 3
    for _ in range(12):
        value = random.lognormvariate(math.log(middle), spread)
        if lo <= value <= hi:
            return round(value)
    return round(middle)


def _locate(page: Page, lo: int, hi: int) -> tuple[int, int]:
    """A call site's bounds as this lane draws them. Only scale-like times are shifted: a pause, a
    flight, a hold, an idle stretch. A pixel distance or a share of the page is not a duration, and
    _shift would read one as milliseconds (30 px to 3e4 px), so those keep their own offset."""
    offset = lane_offset(page)
    return int(_shift(lo, offset)), int(_shift(max(hi, _shift(lo, offset)), offset))


def _share(page: Page, share: float) -> float:
    """A rate or a share, scaled by the lane's own offset and kept inside 0 to 1."""
    return min(1.0, share * math.exp(lane_offset(page)))


async def _pause(page: Page, lo: int, hi: int) -> None:
    lo, hi = _locate(page, lo, hi)
    await page.wait_for_timeout(_draw_ms(lo, hi))


# Input on the real clock (holds and gaps that reach a far page as planned): human_pace.py.


# ---- a lane's own draw --------------------------------------------------------------------------
# Every call site keeps its bounds, but a lane draws a small offset once and every later draw is
# shifted by it: one lane rests longer and moves its pointer in a tighter band over the page, the
# next is quicker and sweeps wider. So the shape of a draw stays log-normal inside its band, while
# two lanes no longer share the same numbers (the red team's pooled comparisons, which set the band
# once for every lane). The offset is in log space, so it scales a draw instead of moving it, and it
# is the same size whichever band the draw is in: a lane that hesitates a fifth longer before a
# press hesitates a fifth longer before a wheel too.

SECONDS_SHIFT = 0.18   # how far a lane's own draw sits from the code's bound, in log space
SECONDS_FLOOR = 30     # below about 30 ms a draw reads as a constant whatever its shape
# Field position: the same offset on both axes, drawn once, as a hand settles in one part of the
# page. Kept below the no-overshoot distance, so a short reach stays short.
POINTER_SHIFT = 0.05
_OFFSETS: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()


def lane_offset(page: Page) -> float:
    """This lane's own offset in log space, drawn once per browser context and kept for its life.
    A page without a context (a test's stand-in) draws one each time, which is the old behaviour."""
    try:
        context = page.context
        offset = _OFFSETS.get(context)
        if offset is None:
            offset = _OFFSETS[context] = random.gauss(0.0, SECONDS_SHIFT)
        return offset
    except (AttributeError, TypeError):
        return random.gauss(0.0, SECONDS_SHIFT)


def _shift(value: float, offset: float) -> float:
    return max(value, SECONDS_FLOOR) * math.exp(offset)


def rest_span(offset: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Where a hand may rest across the page, as two (low, high) shares of the area: x then y. The
    span is the lane's, so one lane keeps to the middle and another uses the edges. Centred on 0.5
    (x) and 0.55 (y), so the middle stays the middle."""
    cx, cy = 0.25 * math.exp(offset), 0.3 * math.exp(offset)
    return ((0.5 - cx, 0.5 + cx), (0.55 - cy, 0.55 + cy))


async def _rest(page: Page, area: dict) -> None:
    """Wait out a drift, then move the pointer to a spot in the lane's own band of `area`."""
    await still(page)
    (xlo, xhi), (ylo, yhi) = rest_span(random.gauss(0.0, POINTER_SHIFT))
    x = area["x"] + area["width"] * random.uniform(xlo, xhi)
    y = area["y"] + area["height"] * random.uniform(ylo, yhi)
    agent_pointer.told(page, x, y)
    await page.mouse.move(x, y)


# A wheel call is not a distance, it is whole notches. Camoufox's humanize re-snaps every call to
# the browser's own notch, so a page sees notch-sized steps and never the delta asked for: at the
# browser's stock setting that is 48 px, a size no mouse of ours sends. `WHEEL_PREFS` (engine) sets
# the notch to 132 px, the size the owner's mouse sends, so what a page records is one real mouse's
# step and only the number of notches follows the distance a gesture wants. A page sees the count
# in the calls and in how close together they are, which is how a real wheel reads too.
NOTCH_PX = 132      # what a page scrolls for one notch, once the multiplier is set
ASK_PX = 110        # what to ask for one notch; a call snaps up to the next notch past about 150
NOTCH_JITTER = 0.12  # around a call's ask, so the browser sees 98 and 124 rather than one number
MAX_JITTER = 0.2     # the widest a gesture's spread may draw

# Down is reading: a notch, then a pause to take the lines in, and longer pauses between skims. Up
# is going back over content already seen, so it is a flick: notches much closer together and no
# reading pause in it at all. The human reference does the same (its gaps inside a return are 42 to
# 106 ms against 44 to 120 going down, and its returns have almost no pause in them).
# (gap band ms, typical gap ms, notches asked per call.)
WHEEL_PACE = {
    "read": ((16, 420), 68, 1),
    "return": ((14, 110), 38, 1),
}


def _notch_gap_ms(page: Page, pace: str = "read") -> int:
    (lo, hi), typical, _ = WHEEL_PACE[pace]
    lo, hi = _locate(page, lo, hi)
    return round(min(max(random.lognormvariate(math.log(typical), 0.55), lo), hi))


def _call_notches(notches: int, per_call: int, jitter: float = NOTCH_JITTER) -> list[int]:
    """How many notches each call of one gesture asks for: no call under one, the calls together no
    more than `notches`, and each within `jitter` of `per_call`."""
    if notches <= 1:
        return [1]
    calls, used = [], 0
    while used < notches:
        want = max(1, round(per_call * random.uniform(1 - jitter, 1 + jitter)))
        calls.append(min(want, notches - used))
        used = sum(calls)
    return calls


async def _wheel(page: Page, dy: int, *, pace: str = "read") -> None:
    """One wheel gesture of about `dy` pixels, a call at a time, whole notches per call. `pace` is
    "read" going down into content, or "return" going back up over content already seen, a flick."""
    await still(page)
    _, _, per_call = WHEEL_PACE[pace]
    # a lane's wheel turns in its own notch count: one that dwells longer between notches turns
    # more, smaller ones, so the two move together
    notches = max(1, round(abs(dy) / NOTCH_PX * math.exp(lane_offset(page))))
    # at most 20% either way, so no call is a sliver of a notch (about 1 gesture in 75 drew a wider
    # spread before, and its smallest call fell under 0.8 of an ask)
    jitter = min(NOTCH_JITTER * math.exp(random.gauss(0.0, 0.3)), MAX_JITTER)
    clock, at = _Pace(page), 0.0
    for i, count in enumerate(_call_notches(notches, per_call, jitter)):
        if i:
            at += _notch_gap_ms(page, pace)
            await clock.until(at, "wheel")
        step = round(count * ASK_PX * random.uniform(1 - jitter, 1 + jitter))
        agent_pointer.told_here(page, "scroll")
        await clock.call(page.mouse.wheel(0, step if dy > 0 else -step), "wheel", at)


async def _inner_size(page: Page) -> tuple[float, float]:
    vw, vh = await page.evaluate("[window.innerWidth, window.innerHeight]")
    agent_pointer.saw_viewport(page, vw, vh)  # what the live view needs to place the agent's pointer
    return float(vw), float(vh)



def _comfortable(box: dict, vh: float) -> bool:
    """Sits well inside the viewport, or (if taller than the comfort band) starts near the top."""
    top, bottom = box["y"], box["y"] + box["height"]
    if box["height"] > vh * 0.65:
        return 0 <= top <= vh * 0.3
    return vh * 0.2 <= top and bottom <= vh * 0.85


def _fully_visible(box: dict, vh: float) -> bool:
    return 0 <= box["y"] and box["y"] + box["height"] <= vh


async def dwell(page: Page, lo: int = 1500, hi: int = 4000) -> None:
    """A person looks at a page for a moment before doing anything."""
    await _pause(page, lo, hi)


async def _rest_pointer(page: Page, area: dict | None = None) -> None:
    """Move the pointer somewhere natural inside `area` (default: the middle of the viewport)."""
    if area is None:
        vw, vh = await _inner_size(page)
        area = {"x": 0, "y": 0, "width": vw, "height": vh}
    await _rest(page, area)


# Shapes and paces that are the same for every lane unless drawn otherwise: how much pauses and
# flights spread, how often a long reach overshoots, how far past the target it goes, and how far a
# wheel gesture or a run of reading steps goes. Drawn per lane, the same size at every call site.
HESITATION = {"spread": 0.35, "after_space": (120, 380), "pause": (200, 500)}
OVERSHOOT = {"chance": 0.35, "past": (0.03, 0.09)}
READ_STEPS = {"skim": (2, 4), "wander": (2, 4)}
IDLE = {"far": 0.2, "sigma": (70, 45)}

# Going back over ground already covered, a person mostly keeps going back: they went up to look
# at something again. So after a return, a reading gesture dips down once to see what is below and
# goes straight back up, rather than starting a fresh read further down.
_GROUND: dict[Any, str] = {}       # context -> "below" | "covered"
GROUND = {"dip": (2, 4)}


# Pages whose pointer has been placed (place_pointer), so it happens once per page.
_PLACED: weakref.WeakSet[Any] = weakref.WeakSet()


async def place_pointer(page: Page) -> None:
    """Put a fresh page's pointer where a hand might have left it, near the middle of the viewport,
    while the page is still blank. Playwright's pointer starts at the window's top-left corner, so
    the first move a site sees would come from (0, 0), which no real mouse does; on about:blank no
    page sees this move. A page already showing a site is left alone (moving now would be that
    very move from the corner). Once per page; never raises."""
    try:
        if page in _PLACED:
            return
        _PLACED.add(page)
        if getattr(page, "url", None) != "about:blank":
            return
        size = await quietly(_inner_size(page), None, timeout=3.0)
        vw, vh = size or (1280.0, 720.0)
        x, y = vw * random.uniform(0.3, 0.7), vh * random.uniform(0.35, 0.65)
        await quietly(page.mouse.move(x, y), timeout=5.0)
    except Exception:  # noqa: BLE001 - a page that cannot be placed (a stand-in, a closed tab) keeps its corner
        return


def on_covered(page: Page) -> bool:
    """Whether this lane has gone back up and is still over ground it has already read."""
    try:
        return _GROUND.get(page.context) == "covered"
    except (AttributeError, TypeError):
        return False


def _mark(page: Page, ground: str) -> None:
    try:
        _GROUND[page.context] = ground
    except (AttributeError, TypeError):
        pass


async def glance(page: Page) -> None:
    """Skim a little: a few wheel steps down, a pause, then partly back up in one flick. Over
    ground already covered it is the other way round: one small dip to see what follows, then
    straight back up to where the person was, and it stays covered."""
    await _rest_pointer(page)
    if on_covered(page):
        await _wheel(page, random.randint(160, 320))
        await _pause(page, 400, 1000)
        await _wheel(page, -random.randint(480, 1120), pace="return")
        await _pause(page, 300, 800)
        return
    for _ in range(random.randint(*READ_STEPS["skim"])):
        await _wheel(page, random.randint(160, 400))
        await _pause(page, 500, 1400)
    await _pause(page, 600, 1500)
    await _wheel(page, -random.randint(1440, 2560), pace="return")
    await _pause(page, 300, 800)
    _mark(page, "covered")


async def wheel_down(page: Page) -> None:
    """Keep reading: a few wheel steps further down the page, which leaves covered ground behind."""
    await _rest_pointer(page)
    for _ in range(random.randint(*READ_STEPS["wander"])):
        await _wheel(page, random.randint(160, 400))
        await _pause(page, 400, 1100)
    await _pause(page, 700, 1500)
    _mark(page, "below")


async def scroll_into_view(page: Page, target: Target) -> bool:
    """Wheel-scroll until the element sits comfortably in view. No-op if it already is fully
    visible. Stops when the page cannot scroll further. Returns True if the element ends up
    fully inside the viewport."""
    _, vh = await _inner_size(page)
    box = await target.bounding_box()
    if box is None:
        return False
    if _fully_visible(box, vh):
        return True
    area = await quietly(target.evaluate(_SCROLL_AREA_JS))
    await _rest_pointer(page, area)
    last_y, stuck = None, 0
    for _ in range(MAX_WHEEL_STEPS):
        box = await target.bounding_box()
        if box is None:
            return False
        if _comfortable(box, vh):
            break
        stuck = stuck + 1 if last_y is not None and abs(box["y"] - last_y) < 1 else 0
        if stuck >= 2:
            break  # the page (or its scroll container) cannot scroll further
        last_y = box["y"]
        center = box["y"] + box["height"] / 2
        direction = 1 if center > vh * 0.5 else -1
        far = abs(center - vh * 0.5) > vh * 1.5
        # up is back over what was just passed, so it flicks; down covers new ground in steps
        if direction < 0:
            step, pace = random.randint(500, 900), "return"
        else:
            step, pace = (random.randint(350, 650) if far else random.randint(160, 380)), "read"
        await _wheel(page, direction * step, pace=pace)
        await _pause(page, *((60, 180) if direction < 0 else (140, 420)))
    box = await target.bounding_box()
    return box is not None and (_fully_visible(box, vh) or _comfortable(box, vh))


def _visible_part(box: dict | None, vw: float, vh: float) -> dict | None:
    """The part of the element's box inside the viewport, or None if (almost) nothing shows."""
    if box is None:
        return None
    left, top = max(box["x"], 0.0), max(box["y"], 0.0)
    right, bottom = min(box["x"] + box["width"], vw), min(box["y"] + box["height"], vh)
    if right - left < 2 or bottom - top < 2:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def _inside(box: dict | None, x: float, y: float) -> bool:
    return box is not None and box["x"] <= x <= box["x"] + box["width"] and box["y"] <= y <= box["y"] + box["height"]


async def _aim(page: Page, target: Target, attempts: int = 5) -> tuple[float, float]:
    """A random point in the visible middle of the element that a press would really reach. A
    target a sticky bar covers gets one small wheel away from the edge it sits under, as a person
    nudges the page; a dialog stays a dialog, and the error goes back to the agent."""
    try:
        return await _aim_once(page, target, attempts)
    except TargetCovered:
        _, vh = await _inner_size(page)
        box = await target.bounding_box()
        if box is None:
            raise
        middle = box["y"] + box["height"] / 2
        if vh * 0.3 <= middle <= vh * 0.7:
            raise
        await _wheel(page, -240 if middle < vh * 0.3 else 240)
        await _pause(page, 200, 600)
        return await _aim_once(page, target, attempts)


async def _aim_once(page: Page, target: Target, attempts: int) -> tuple[float, float]:
    vw, vh = await _inner_size(page)
    for _ in range(attempts):
        part = _visible_part(await target.bounding_box(), vw, vh)
        if part is None:
            raise TargetNotVisible("the target is not visible in the viewport (hidden, zero size or off screen)")
        x = part["x"] + part["width"] * random.uniform(0.3, 0.7)
        y = part["y"] + part["height"] * random.uniform(0.3, 0.7)
        if await _hits(target, x, y):
            return x, y
    raise TargetCovered("another element covers the target (a banner, dialog or sticky header?); "
                        "deal with it first, then snapshot again")


async def _hits(target: Target, x: float, y: float) -> bool:
    hit = await quietly(target.evaluate(_HITS_TARGET_JS, [x, y, False]), False)
    if hit != "frame":
        return bool(hit)
    return bool(await quietly(_hits_through_frames(target, x, y), False, timeout=5.0))


async def _hits_through_frames(target: Target, x: float, y: float) -> bool:
    """For a target inside a frame: the point must reach each frame on the way down (a parent page
    can lay a link over the frame), and then the target in its own frame's coordinates."""
    handle = target if hasattr(target, "owner_frame") else await target.element_handle()
    frame = await handle.owner_frame()
    frames = []
    while frame is not None and frame.parent_frame is not None:
        frames.append(frame)
        frame = frame.parent_frame
    for child in reversed(frames):
        point = await (await child.frame_element()).evaluate(_FRAME_POINT_JS, [x, y])
        if point is None:
            return False
        x, y = point
    return await handle.evaluate(_HITS_TARGET_JS, [x, y, True]) is True


async def _overshoot(page: Page, x: float, y: float) -> None:
    """A long reach now and then goes a little past the target and comes back."""
    start = POINTERS.get(page)
    if start is None or math.dist(start, (x, y)) < 240 or random.random() > _share(page, OVERSHOOT["chance"]):
        return
    past = 1 + random.uniform(*OVERSHOOT["past"])
    over = (start[0] + (x - start[0]) * past, start[1] + (y - start[1]) * past)
    width, height = await _inner_size(page)
    if 0 < over[0] < width and 0 < over[1] < height:
        agent_pointer.told(page, *over)
        await page.mouse.move(*over)
        await _pause(page, 60, 190)


async def _glide_onto(page: Page, target: Target) -> None:
    """Glide the pointer onto the target. If the page shifted under the pointer during the move
    (late images, a banner), aim again rather than press on whatever is there now."""
    await still(page)
    for _ in range(3):
        x, y = await _aim(page, target)
        await _overshoot(page, x, y)
        agent_pointer.told(page, x, y)
        await page.mouse.move(x, y)  # Camoufox humanize makes this a curved, human-speed path
        POINTERS[page] = (x, y)
        await _pause(page, 90, 260)
        if _inside(await target.bounding_box(), x, y):
            return
    raise TargetCovered("the target kept moving under the pointer; snapshot again")


# A check run right before every mouse down or key press inside the block (browser_act's form
# re-check: openajar.node.tools.form_check.check_form_unchanged). It raises to stop the press.
_before_press: contextvars.ContextVar[Callable[[], Awaitable[None]] | None] = contextvars.ContextVar(
    "openajar_before_press", default=None)


@contextmanager
def before_press(check: Callable[[], Awaitable[None]] | None) -> Iterator[None]:
    """Run `check` just before each press in the block, after the scroll, the pause and the glide,
    so nothing the page does during them reaches a press unchecked. None: no check."""
    token = _before_press.set(check)
    try:
        yield
    finally:
        _before_press.reset(token)


async def _check_before_press() -> None:
    check = _before_press.get()
    if check is not None:
        await check()


async def click(page: Page, target: Target) -> None:
    """Scroll by wheel, pause, glide to a random point inside the element, press for real."""
    await scroll_into_view(page, target)
    await _pause(page, 500, 1400)
    await _glide_onto(page, target)
    await _check_before_press()
    # atomic: a freeze that interrupts the call here still lets the button come up (a click is
    # never cut in half); the caller sees the interrupt as soon as the press is done
    await asyncio.shield(_press_release(page))


async def _press_release(page: Page) -> None:
    agent_pointer.told_here(page, "press")
    clock = _Pace(page)
    await clock.call(page.mouse.down(), "button", 0)
    try:
        await clock.until(_draw_ms(*_locate(page, 50, 230)), "release")  # held this long as the page sees it
    finally:
        await clock.call(page.mouse.up(), "release")


async def hover(page: Page, target: Target) -> None:
    """Scroll by wheel, glide onto a random point inside the element and rest there."""
    await scroll_into_view(page, target)
    await _pause(page, 300, 900)
    await _glide_onto(page, target)
    await _pause(page, 300, 800)


# Keys a US layout presses without and with Shift. Anything else in the Basic Multilingual Plane
# goes through `keyboard.type`, which inserts it as text when the layout has no key for it.
_SHIFTED = frozenset(string.ascii_uppercase + '~!@#$%^&*()_+{}|:"<>?')
_PRESSABLE = frozenset(string.ascii_lowercase + string.digits + "`-=[]\\;',./ ") | _SHIFTED
# Line breaks press Enter, as `keyboard.type` did. A tab is not here: pressing Tab would move focus
# and send the rest of the text to another field, so it goes in as text, as `keyboard.type` put it.
_CONTROL_KEYS = {"\n": "Enter", "\r": "Enter"}

# The letters next to each letter on a US QWERTY keyboard: where a slipping finger lands.
_NEIGHBOURS = {
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg", "y": "tugh", "u": "yihj", "i": "uojk",
    "o": "ipkl", "p": "ol", "a": "qwsz", "s": "weadzx", "d": "erfsxc", "f": "rtgdcv", "g": "tyhfvb",
    "h": "yujgbn", "j": "uikhnm", "k": "iojlm", "l": "opk", "z": "asx", "x": "zsdc", "c": "xdfv",
    "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}

TYPO_RATE = 1 / 40     # letters that slip onto a neighbouring key
ROLLOVER_RATE = 0.27   # letter pairs where the next key goes down before the last comes up


def _hold_ms(offset: float = 0.0) -> float:
    """How long a key stays down: log-normal around 85 ms (the browser's round trip adds a few, so a
    page measures about 90), as people hold keys 50 to 150 ms. `offset` is the lane's, in log space:
    a quick typist keeps its keys down a little less and a heavy one a little more."""
    scale = math.exp(offset)
    return min(max(random.lognormvariate(math.log(85 * scale), 0.3), 40.0 * scale), 220.0 * scale)


def _flight_ms(offset: float = 0.0, spread: float = 0.35) -> float:
    """Keydown to the next keydown in a run of typing: log-normal around 150 ms, spread by the
    lane's own shape."""
    scale = math.exp(offset)
    return min(max(random.lognormvariate(math.log(150 * scale), spread), 80.0 * scale), 600.0 * scale)


def _rolls(prev: str, key: str) -> bool:
    """Fluent typists press the next letter before the last one is up; never the same key twice
    (a second keydown of a held key is an auto-repeat) and never around Shift."""
    return prev != key and prev in _NEIGHBOURS and key in _NEIGHBOURS


def _keystrokes(text: str, typos: bool) -> list[tuple[str, str]]:
    """What the hands do for `text`: ("key", ch) presses a key, ("insert", ch) and ("type", ch) go
    in as Playwright inserts them, ("notice", "") and ("resume", "") are the pauses around fixing a
    typo. A typo is a neighbouring key, noticed after 0 to 2 more letters or digits (never across a
    space, punctuation or Enter, which can act on what was typed: a tag field turns a word into a
    chip), then erased with Backspace and typed again."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if typos and ch in _NEIGHBOURS and random.random() < TYPO_RATE:
            run = [ch]
            more = random.randint(0, 2)
            while len(run) <= more and i + len(run) < len(text) and text[i + len(run)].isascii() \
                    and text[i + len(run)].isalnum():
                run.append(text[i + len(run)])
            out.append(("key", random.choice(_NEIGHBOURS[ch])))
            out += [("key", c) for c in run[1:]]
            out.append(("notice", ""))
            out += [("key", "Backspace")] * len(run)
            out.append(("resume", ""))
            out += [("key", c) for c in run]
            i += len(run)
        elif ord(ch) > 0xFFFF:
            out.append(("insert", ch))
            i += 1
        else:
            out.append(("key", ch) if ch in _PRESSABLE or ch in _CONTROL_KEYS else ("type", ch))
            i += 1
    return out


# ---- hands at rest ------------------------------------------------------------------------------
# While the model thinks, a lane's pointer would otherwise sit dead still and then jump into action,
# which no hand does (the red team's `think_then_burst`). So after an agent's action the pointer drifts
# a little, for 3 to IDLE_FOR_S seconds, a different span each time: then it rests, as a hand does
# during a long read. 12 s covers 82% of the pauses real agents left (the canary log). Only moves: no
# press, no wheel, no key. Every input path calls `still` first, which waits for a move in flight, so
# a press can never land where a drift left the pointer mid-way.

IDLE_FOR_S = 12.0


def _idle_span_s() -> float:
    return _draw_ms(3000, round(IDLE_FOR_S * 1000)) / 1000


# A lane that may drift, by context, and what says agents still hold it (lanes.py): a human's lane
# never moves under them.
IDLE_LANES: weakref.WeakKeyDictionary[Any, Callable[[], bool]] = weakref.WeakKeyDictionary()
_IDLE: weakref.WeakKeyDictionary[Any, _Idle] = weakref.WeakKeyDictionary()
# Where we last left the pointer, by page.
POINTERS: weakref.WeakKeyDictionary[Any, tuple[float, float]] = weakref.WeakKeyDictionary()


class _Idle:
    def __init__(self, page: Page, allowed: Callable[[], bool]):
        self.page, self.allowed = page, allowed
        self.stop = asyncio.Event()
        self.shift = 0.0  # this lane's, for the span and the drift's own pace
        self.task = asyncio.create_task(self._drift())

    async def _drift(self) -> None:
        loop = asyncio.get_running_loop()
        self.shift = math.exp(lane_offset(self.page))
        deadline = loop.time() + _draw_ms(round(3000 * self.shift), round(IDLE_FOR_S * 1000)) / 1000
        with suppress(Exception):  # a page that closes or navigates ends the drift, and nothing else
            while loop.time() < deadline:
                try:
                    await asyncio.wait_for(self.stop.wait(), _draw_ms(round(700 * self.shift),
                                                                      round(4200 * self.shift)) / 1000)
                    return
                except TimeoutError:
                    pass
                if not self.allowed():
                    return
                width, height = await _inner_size(self.page)
                x, y = POINTERS.get(self.page, (width * 0.5, height * 0.5))
                if random.random() < IDLE["far"]:  # the hand goes somewhere else on the page
                    x, y = width * random.uniform(0.15, 0.85), height * random.uniform(0.2, 0.8)
                else:
                    x, y = x + random.gauss(0, IDLE["sigma"][0]), y + random.gauss(0, IDLE["sigma"][1])
                x, y = min(max(x, width * 0.05), width * 0.95), min(max(y, height * 0.08), height * 0.95)
                await self.page.mouse.move(x, y)
                POINTERS[self.page] = (x, y)


def allow_idle(session: Any, allowed: Callable[[], bool]) -> None:
    context = getattr(session, "context", None)
    if context is not None:
        IDLE_LANES[context] = allowed


def begin_idle(page: Page) -> None:
    """After an agent's action: the hand rests on the mouse until the next one."""
    try:
        allowed = IDLE_LANES.get(page.context)
    except (AttributeError, TypeError):  # a test's stand-in page has no context
        return
    if allowed is not None and page not in _IDLE:
        _IDLE[page] = _Idle(page, allowed)


async def still(page: Page) -> None:
    """Stop any drift, waiting out a move in flight. Costs nothing when there is none."""
    try:
        idle = _IDLE.pop(page, None)
    except TypeError:
        return
    if idle is not None:
        idle.stop.set()
        await idle.task


# A lane's X keyboard by its browser context (engine_camoufox sets it, forget_context drops it).
X_KEYBOARDS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


def x_keyboard(page: Page) -> Any:
    try:
        return X_KEYBOARDS.get(page.context)
    except (AttributeError, TypeError):  # a test's stand-in page has no context
        return None


class _Hands:
    """Key events on one timeline (ms since typing began). Each key goes down at a planned time and
    comes up after its own hold, so the next key can go down before it is up (rollover). Shift goes
    down before a capital and comes up after it, as it does under a hand. `before_key` (when given)
    runs right before each key goes down, Shift included, and raises to stop the typing."""

    def __init__(self, page: Page, before_key: Callable[[], Awaitable[None]] | None = None):
        self.page = page
        self.before_key = before_key
        self.clock = _Pace(page)  # the plan below, kept on the real clock
        self.held: list[tuple[float, str]] = []  # (time it comes up, key)
        self.shift = False
        self.last: tuple[str, float, float] | None = None  # the last key: what, when it went down, hold
        self.extra = 0.0  # a pause owed before the next key (noticing a typo)
        # the lane's own typist, in log space and as a spread: one types evenly, one in fits
        self.offset = lane_offset(page)
        self.spread = HESITATION["spread"] * math.exp(random.gauss(0.0, 0.3))

    @property
    def now(self) -> float:
        """Where the plan is, in ms since typing began."""
        return self.clock.now

    @now.setter
    def now(self, value: float) -> None:
        self.clock.now = value

    async def _until(self, t: float) -> None:
        await self.clock.until(t, "key")

    async def _lift(self, before: float = math.inf) -> None:
        """Let up every held key due to come up before `before`, each at its time."""
        for up_at, key in sorted(self.held):
            if up_at > before:
                break
            await self._until(up_at)
            self.held.remove((up_at, key))
            await self.clock.call(self.page.keyboard.up(key), "key")

    async def _down(self, key: str) -> None:
        if self.before_key is not None:
            await self.before_key()
        # held before the await: once the call is made the browser may have the key down, and a cancel
        # during the round trip must still find it in `held` for let_go
        hold = _hold_ms(self.offset)
        self.held.append((self.now + hold, key))
        self.last = (key, self.now, hold)
        await self.clock.call(self.page.keyboard.down(key), "key")

    async def _shift_up(self) -> None:
        if self.shift:
            await self._lift()
            await self._until(self.now + random.uniform(15, 60))
            self.shift = False
            await self.clock.call(self.page.keyboard.up("Shift"), "key")

    def _next_down(self, key: str) -> float:
        """When `key` goes down: a flight after the last key (longer after a space, now and then a
        hesitation), or, for a fluent pair of letters, while the last key is still down."""
        extra, self.extra = self.extra, 0.0
        if self.last is None:
            return self.now + extra
        prev, down_at, hold = self.last
        if not extra and _rolls(prev, key) and random.random() < ROLLOVER_RATE:
            return down_at + max(35.0, hold * random.uniform(0.45, 0.85))
        gap = _flight_ms(self.offset, self.spread) + extra
        if prev == " ":
            gap += random.randint(*HESITATION["after_space"])
        elif not extra and random.random() < _share(self.page, 0.08):
            gap += random.randint(*HESITATION["pause"])  # an occasional hesitation
        return down_at + max(gap, hold + random.uniform(15, 45))

    async def press(self, ch: str) -> None:
        key = _CONTROL_KEYS.get(ch, ch)
        at = self._next_down(key)
        if ch in _SHIFTED:
            if not self.shift:
                await self._lift()
                lead = random.uniform(50, 120)
                await self._until(max(at - lead, self.now + 10))
                if self.before_key is not None:
                    await self.before_key()
                self.shift = True
                await self.clock.call(self.page.keyboard.down("Shift"), "key")
                at = max(at, self.now + lead)
            else:
                await self._lift()  # a run of capitals under one Shift: one key at a time
        elif self.shift:
            await self._shift_up()
            at = max(at, self.now + random.uniform(20, 60))
        for up_at, other in self.held:
            if other == key:
                # a key still down cannot go down again (the browser would send an auto-repeat and
                # then one keyup for two keydowns): it comes up first ("aba", "banana")
                at = max(at, up_at + random.uniform(15, 45))
        await self._lift(at)
        await self._until(at)
        await self._down(key)

    async def put(self, ch: str, *, insert: bool) -> None:
        """A character with no key on the layout, at the rhythm of a key press."""
        at = self._next_down(ch)
        await self._shift_up()
        await self._lift()
        await self._until(at)
        if self.before_key is not None:
            await self.before_key()
        keyboard = x_keyboard(self.page)
        if not insert and keyboard is not None and keyboard.has(ch):
            hold = _hold_ms(self.offset)
            # its event is the key coming up, a hold after the plan put it down
            if await self.clock.call(keyboard.press(ch, hold), "xkey", self.now + hold):
                self.now += hold
                self.last = (ch, self.now - hold, hold)
                return
        if insert:
            await self.clock.call(self.page.keyboard.insert_text(ch), "key")
        else:
            await self.clock.call(self.page.keyboard.type(ch), "key")
        self.last = (ch, self.now, 0.0)

    async def finish(self) -> None:
        await self._shift_up()
        await self._lift()

    async def let_go(self) -> None:
        """Interrupted: let every held key up at once, so none stays down for the next caller."""
        held, self.held = self.held, []
        for _, key in held:
            await quietly(self.page.keyboard.up(key), timeout=2.0)
        if self.shift:
            self.shift = False
            await quietly(self.page.keyboard.up("Shift"), timeout=2.0)


async def type_text(page: Page, text: str, *, typos: bool = False,
                    before_key: Callable[[], Awaitable[None]] | None = None) -> None:
    """Type one key at a time with a human hand's timing: each key held a log-normal time around
    90 ms, uneven flights between keys, the next letter now and then pressed before the last is up,
    Shift held around capitals and shifted symbols, and (with `typos`) now and then a neighbouring
    key, noticed and fixed with Backspace. The text that ends up in the field is `text` either way.
    Typos are off unless asked for, because a wrong key can do harm before it is fixed (a secret, a
    field that acts on each key): pass typos=typos_fit(field) to allow them where they are safe.
    `before_key` runs right before each key goes down (Shift included) and raises to stop: the
    typist's focus probe, so a check still holds when a capital lands after Shift's lead.

    A character outside the Basic Multilingual Plane (most emoji) goes in as one text insertion, as
    an emoji picker does: Firefox turns `keyboard.type` of such a character into two (seen on
    Instagram's caption). A Lithuanian letter is a real key press on a lane's X keyboard
    (xkeyboard.py). Any other character the US layout has no key for goes through `keyboard.type`,
    which inserts it as text."""
    await still(page)
    agent_pointer.told_here(page, "type")
    hands = _Hands(page, before_key)
    try:
        for kind, ch in _keystrokes(text, typos):
            if kind == "key":
                await hands.press(ch)
            elif kind == "notice":
                hands.extra = random.uniform(250, 700)
            elif kind == "resume":
                hands.extra = random.uniform(120, 350)
            else:
                await hands.put(ch, insert=kind == "insert")
        await hands.finish()
    except BaseException:
        await asyncio.shield(hands.let_go())
        raise
    await page.wait_for_timeout(random.randint(70, 190))


async def press(page: Page, key: str) -> None:
    """A short look, then one real key press held for a human length of time."""
    await still(page)
    await _pause(page, 120, 380)
    await _check_before_press()
    await page.keyboard.press(key, delay=random.randint(50, 130))
