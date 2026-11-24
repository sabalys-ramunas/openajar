"""human: input keeps its plan on the real clock when every browser call is a round trip away (an
environment's browser on its machine, driven over an ssh tunnel). The page must see the holds and gaps
the plan drew, not the plan plus two round trips."""

from __future__ import annotations

import asyncio
import itertools
import statistics
import time

from openajar.node import human, human_pace

RTT_S = 0.03  # a round trip to the machine's browser


WORK_S = 0.04  # what a press or a wheel does on the page before its event fires (it waits for a frame)


async def _trip(at: list[float], what: tuple, work: float = 0.0) -> None:
    """A browser call: it reaches the page half a round trip after it is sent, does its `work` there,
    fires its event, and its answer is back half a round trip later."""
    await asyncio.sleep(RTT_S / 2 + work)
    at.append((time.perf_counter() * 1000, *what))
    await asyncio.sleep(RTT_S / 2)


class Keyboard:
    def __init__(self, seen: list) -> None:
        self.seen = seen

    async def down(self, key: str) -> None:
        await _trip(self.seen, ("down", key))

    async def up(self, key: str) -> None:
        await _trip(self.seen, ("up", key))


class Mouse:
    def __init__(self, seen: list) -> None:
        self.seen = seen

    async def down(self) -> None:
        await _trip(self.seen, ("down", "mouse"), WORK_S)

    async def up(self) -> None:
        await _trip(self.seen, ("up", "mouse"))

    async def wheel(self, dx: float, dy: float) -> None:
        await _trip(self.seen, ("wheel", dy), WORK_S)


class FarPage:
    """A page a round trip away; `wait_for_timeout` is a round trip too, as Playwright's is."""

    def __init__(self) -> None:
        self.seen: list[tuple] = []
        self.keyboard = Keyboard(self.seen)
        self.mouse = Mouse(self.seen)

    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(RTT_S + ms / 1000)


def holds(seen: list[tuple]) -> list[float]:
    down: dict[str, float] = {}
    out = []
    for t, kind, key in seen:
        if kind == "down":
            down[key] = t
        elif kind == "up" and key in down:
            out.append(t - down.pop(key))
    return out


async def test_keys_reach_a_far_page_held_as_long_as_planned(monkeypatch):
    monkeypatch.setattr(human, "_hold_ms", lambda offset=0.0: 90.0)
    monkeypatch.setattr(human, "ROLLOVER_RATE", 0.0)
    page = FarPage()
    await human.type_text(page, "hello there", typos=False)
    held = holds(page.seen)
    assert len(held) == 11
    # unpaced, each hold was 90 ms plus two round trips (150 ms here); the first carries the wait's
    # cost before it is known
    assert 80 <= statistics.median(held) <= 105, held


async def test_a_hold_shorter_than_a_wait_costs_is_kept_too(monkeypatch):
    """A 50 ms hold over 30 ms round trips: a wait on the page would cost more than what is left after
    the keydown's answer, so the rest is waited here; the page sees about 50 ms, not one round trip."""
    monkeypatch.setattr(human, "_hold_ms", lambda offset=0.0: 50.0)
    monkeypatch.setattr(human, "ROLLOVER_RATE", 0.0)
    page = FarPage()
    await human.type_text(page, "hello there", typos=False)
    held = holds(page.seen)
    assert 42 <= statistics.median(held) <= 62, held


async def test_a_press_reaches_a_far_page_held_as_long_as_planned(monkeypatch):
    monkeypatch.setattr(human, "_draw_ms", lambda lo, hi: 120)
    page = FarPage()
    for _ in range(3):  # the second and later presses know what a wait costs
        await human._press_release(page)
    held = holds(page.seen)
    assert len(held) == 3
    # unpaced, 120 ms plus two round trips; paced from the send alone, 120 ms less the press's own work
    assert all(105 <= h <= 140 for h in held[1:]), held


async def test_wheel_notches_reach_a_far_page_at_the_planned_gaps(monkeypatch):
    monkeypatch.setattr(human, "_notch_gap_ms", lambda page, pace="read": 70)
    monkeypatch.setattr(human, "lane_offset", lambda page: 0.0)
    page = FarPage()
    await human._wheel(page, 6 * human.NOTCH_PX)
    times = [t for t, kind, _ in page.seen if kind == "wheel"]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert len(gaps) >= 4
    # unpaced, each gap was 70 ms plus the wheel call's work, its round trip and the wait's
    assert 60 <= statistics.median(gaps) <= 85, gaps


async def test_a_call_that_hangs_moves_the_rest_instead_of_squeezing_it():
    """A call that took far longer than its round trip (a busy page, an X key sent over ssh) moves the
    rest of the run later: the next gap is still about what the plan says, not made up by bunching."""
    page = FarPage()
    await human_pace._Pace(page).until(20)  # one wait measures the round trip: a far page, so the run is paced
    clock = human_pace._Pace(page)
    await clock.call(asyncio.sleep(0.4), "slow", 100)  # the call for plan time 100 took 400 ms
    began = time.perf_counter()
    await clock.until(200)
    waited = (time.perf_counter() - began) * 1000
    # the plan's 100 ms between the two events, less the round trip the call's answer already spent coming back
    assert 100 - RTT_S * 1000 - 10 <= waited <= 100 + 20, waited


def test_a_page_that_answers_at_once_waits_exactly_the_plan():
    """A stand-in page (no round trips) gets the plan's waits unchanged."""
    waits: list[float] = []

    class Page:
        async def wait_for_timeout(self, ms: float) -> None:
            waits.append(ms)

    async def run() -> None:
        clock = human_pace._Pace(Page())
        for t in (50, 120, 120, 400):
            await clock.until(t)

    asyncio.run(run())
    assert waits == [50, 70, 280]


class SlowHandlerPage(FarPage):
    """A far page whose click handler runs for 100 ms after the button comes up: the answer to mouse.up
    is late by the handler, not by anything before the event."""

    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, float]] = []

    async def down(self):
        await asyncio.sleep(RTT_S / 2)
        self.events.append(("down", time.perf_counter()))
        await asyncio.sleep(RTT_S / 2)

    async def up(self):
        await asyncio.sleep(RTT_S / 2)
        self.events.append(("up", time.perf_counter()))
        await asyncio.sleep(0.1 + RTT_S / 2)


async def test_a_slow_handler_after_the_release_never_shortens_the_next_holds():
    """What a release takes past its round trip is the page's handler, after the event: the next release
    is not sent early by it (it was, and a page with a 100 ms click handler saw 20 ms holds)."""
    page = SlowHandlerPage()
    await human_pace._Pace(page).until(20)
    holds = []
    for _ in range(4):
        clock = human_pace._Pace(page)
        await clock.call(page.down(), "button", 0)
        await clock.until(120, "release")
        await clock.call(page.up(), "release")
        holds.append((page.events[-1][1] - page.events[-2][1]) * 1000)
    assert all(100 <= h <= 160 for h in holds), holds


async def test_a_browser_beside_us_keeps_the_plans_own_waits():
    """Under the round trip floor (a lane on the node) nothing is paced: each wait is the plan's gap, whatever
    the calls between took, as before there was a clock."""
    asked: list[int] = []

    class Near:
        async def wait_for_timeout(self, ms):
            asked.append(ms)

    page = Near()
    clock = human_pace._Pace(page)
    await clock.until(100, "key")
    await clock.call(asyncio.sleep(0.05), "key")  # a slow call: a busy node, not a network
    await clock.until(180, "key")
    assert asked == [100, 80]
