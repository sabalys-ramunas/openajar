"""Human input on the real clock: holds and gaps that reach a far page as planned (human.py uses it).

Every key, button and wheel call is a round trip to the browser, and so is `page.wait_for_timeout`.
On the node a round trip is a millisecond or two. To an environment's browser on its machine
(browser = "vm", Playwright over an ssh tunnel) it is the network's: so a key held 85 ms by the plan
reached the page held 85 ms plus two or three round trips (the keydown's answer coming back, the wait
going out and back, the keyup going out), and every flight and notch gap grew the same way: a floor
under every hold and gap that no hand has, growing with the distance to the machine. Measured on a
Hetzner machine 36 ms away: keys planned 98 ms reached the page held 204 ms, wheel gaps planned 58 ms
reached it 232 ms apart. So a run of input (a word typed, a press, a wheel gesture) keeps its plan on
the real clock: each wait is shortened by the round trips already spent and by what a wait costs over
what it asked for, and a wheel call goes out early by what it takes before its event fires on the
page (`call`), so the page sees the gaps the plan drew. Only the wheel: what a button or key call
takes past its round trip is mostly the page's own handlers, which run after the event, and sending
the release early by that would cut the hold the page sees (a page with a 100 ms click handler saw
holds of 20 ms). A call that hangs moves the rest of the run later; nothing after it is squeezed.
With a browser beside us (a lane on the node, a test's stand-in: a round trip under
ROUND_TRIP_FLOOR_MS) nothing changes: the plan's own waits, as before there was a clock. What stays: two calls in a row are at least one round trip apart, so a gap the plan draws
shorter than that (the quickest wheel notches) reaches the page a round trip long. Paced, on the same
machine: keys planned 95 ms reached the page held 99 ms, notch gaps planned 67 ms came 68 ms apart.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import weakref
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any

Page = Any  # playwright.async_api.Page

_WAIT_COST: weakref.WeakKeyDictionary[Any, list[float]] = weakref.WeakKeyDictionary()  # page -> what recent waits cost
# over their ask, the newest last: the median, so a wait the page was slow to answer does not shorten every gap
WAIT_SAMPLES = 15
# page -> kind of call -> what such a call takes on the page before its event, past the round trip
_CALL_WORK: weakref.WeakKeyDictionary[Any, dict[str, float]] = weakref.WeakKeyDictionary()
MAX_WORK_MS = 200.0  # a call that hung says nothing about the next one
EARLY_KINDS = frozenset({"wheel"})  # the calls whose work on the page comes before their event
# Under this the browser is beside us and a wait's overrun is this process's own lag, not a network: no
# pacing at all then, so a busy node never shortens a hold or a gap.
ROUND_TRIP_FLOOR_MS = 10.0


def _wait_cost(page: Page) -> float:
    try:
        seen = _WAIT_COST.get(page)
    except TypeError:  # a stand-in that cannot be weakly referenced
        return 0.0
    return statistics.median(seen) if seen else 0.0


def _note_wait_cost(page: Page, over_ms: float) -> None:
    """What a wait cost over its ask: a round trip, and the timer's slop."""
    with suppress(TypeError):
        seen = _WAIT_COST.setdefault(page, [])
        seen.append(min(max(over_ms, 0.0), 1000.0))
        del seen[:-WAIT_SAMPLES]


def _ewma(before: float | None, value: float) -> float:
    return value if before is None else 0.7 * before + 0.3 * value


class _Pace:
    """One run of input on a plan in ms (0 when it begins), kept on the real clock. `until(t, kind)`
    returns when a call of that kind should go out for the page to see its event at `t` of the plan;
    `call(work, kind)` then makes it and anchors the plan on its answer."""

    def __init__(self, page: Page):
        self.page = page
        self.start = time.perf_counter()
        self.now = 0.0  # where the plan is

    def _work(self) -> dict[str, float]:
        try:
            return _CALL_WORK.setdefault(self.page, {})
        except TypeError:  # a stand-in that cannot be weakly referenced
            return {}

    async def until(self, t: float, kind: str | None = None) -> None:
        cost = _wait_cost(self.page)
        if cost < ROUND_TRIP_FLOOR_MS:  # a browser beside us: the plan's own wait, measured for the next one
            if t - self.now >= 1:
                asked = round(t - self.now)
                began = time.perf_counter()
                await self.page.wait_for_timeout(asked)
                _note_wait_cost(self.page, (time.perf_counter() - began) * 1000 - asked)
            self.now = max(self.now, t)
            return
        early = self._work().get(kind, 0.0) if kind in EARLY_KINDS else 0.0
        elapsed = (time.perf_counter() - self.start) * 1000  # the plan's time plus the round trips since
        left = t - early - max(elapsed, self.now)
        if left - cost >= 1:
            asked = round(left - cost)
            began = time.perf_counter()
            await self.page.wait_for_timeout(asked)
            _note_wait_cost(self.page, (time.perf_counter() - began) * 1000 - asked)
        elif left >= 1 and cost > 0:  # shorter than the page's wait costs: here, where a wait costs nothing
            await asyncio.sleep(left / 1000)
        self.now = max(self.now, t)

    async def call(self, work: Awaitable[Any], kind: str, t: float | None = None) -> Any:
        """Make the browser call of `kind` whose event the plan puts at `t` (default: where the plan
        is). Its event fired on the page about half a round trip before its answer came back: the plan
        is anchored there. What it took past a round trip is what the next call of its kind goes out
        early by."""
        at = self.now if t is None else t
        began = time.perf_counter()
        out = await work
        answered = time.perf_counter()
        trip = _wait_cost(self.page)
        took = (answered - began) * 1000
        with suppress(TypeError):
            if kind in EARLY_KINDS and self.page in _WAIT_COST:  # past the round trip: unknown until a wait measured one
                works = self._work()
                works[kind] = _ewma(works.get(kind), min(max(took - trip, 0.0), MAX_WORK_MS))
        self.start = answered - (at + trip) / 1000
        return out
