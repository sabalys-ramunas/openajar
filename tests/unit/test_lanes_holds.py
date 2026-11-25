"""Holds after bad signals, quarantine and the exit IP self-check (AUD-07, AUD-14).

The real LaneManagerImpl over the fakes of test_lanes.py (no browser, no X).
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeInteraction
from test_lanes import PROXY, Clock, FakeDisplays, FakeEngine

from openajar.core.config import NodeConfig
from openajar.core.errors import AgentPaused, InvalidRequest, LaneQuarantined
from openajar.core.models import ControlState, LaneSpec, LaneState, ProxyConfig, Signal
from openajar.node import lanes as lanes_mod
from openajar.node.flow_driver import LaneAgentPaused, control_gate
from openajar.node.lanes import HOLD_EXPIRED_BY, LaneManagerImpl, _as_hold
from openajar.node.lanes import quarantine as lanes_quarantine
from openajar.node.store import LaneStore

GATEWAY = ProxyConfig(label="gw", host="gate.proxy.test", port=7000, username="u", password="p")
NODE_IP = "192.0.2.50"


class Wall:
    """A wall clock the test moves by hand (hold_until and control times use it)."""

    def __init__(self):
        self.now = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def build(tmp_path: Path, *, engine: FakeEngine | None = None, proxies: dict | None = None, policy=None,
          wall: Wall | None = None, **kw) -> SimpleNamespace:
    cfg = {k: kw.pop(k) for k in list(kw) if k in NodeConfig.model_fields}
    config = NodeConfig(home=tmp_path, **{"max_warm_lanes": 3, "busy_timeout": 1.0, "self_check": False,
                                          "idle_seconds": 600, "display_base": 110, "vnc_base": 5810,
                                          "viewer_base": 6110, **cfg})
    engine = engine or FakeEngine()
    displays = FakeDisplays(config)
    clock, wall = Clock(), wall or Wall()
    loader = (lambda: dict(proxies)) if proxies is not None else (lambda: {"lt1": PROXY, "gw": GATEWAY})
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), engine, displays, loader, clock=clock, now=wall,
                          reaper_interval=60, hold_policy=policy, **kw)
    return SimpleNamespace(mgr=mgr, engine=engine, displays=displays, clock=clock, wall=wall, config=config)


def again(t: SimpleNamespace, **kw) -> LaneManagerImpl:
    """A second manager on the same home: what agentosd sees after a restart."""
    return LaneManagerImpl(t.config, LaneStore(t.config.db_path), t.engine, FakeDisplays(t.config),
                           lambda: {"lt1": PROXY, "gw": GATEWAY}, clock=t.clock, now=t.wall, reaper_interval=60,
                           **kw)


def policy(signal: Signal, *, now: datetime):
    if signal in (Signal.CAPTCHA, Signal.CHECKPOINT, Signal.BLOCKED):
        return (f"needs_human:{signal.value}", None)
    if signal == Signal.RATE_LIMITED:
        return {"reason": "rate_limited", "until": now + timedelta(hours=6)}
    return None


async def agent_blocked(mgr: LaneManagerImpl, lane_id: str, kind: str = "agent") -> str:
    with pytest.raises(AgentPaused) as e:
        async with mgr.using(lane_id, principal_kind=kind):
            pass
    return e.value.message


# ---- holds ------------------------------------------------------------------------------


async def test_a_hold_stops_agents_and_flows_at_the_gate_and_says_why(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    seen = []
    t.mgr.add_control_listener(lambda lane, old, new: seen.append((lane, new.state, new.reason)))
    info = await t.mgr.hold("a/one", reason="needs_human:captcha on www.reddit.com")
    assert info.control.state == ControlState.PAUSED and info.hold == "needs_human:captcha on www.reddit.com"
    assert info.hold_until is None
    assert seen == [("a/one", ControlState.PAUSED, "needs_human:captcha on www.reddit.com")]
    for kind in ("agent", "flow", "brain"):
        message = await agent_blocked(t.mgr, "a/one", kind)
        assert "openajar paused a/one itself (no person stopped you)" in message and "needs_human:captcha" in message
        assert "until the human solves it in the live view and hands the browser back" in message
        assert "\u2014" not in message
    with pytest.raises(LaneAgentPaused):
        control_gate(t.mgr, "a/one")()  # a flow run checks the same control before every step
    async with t.mgr.using("a/one", principal_kind="human"):
        pass
    assert t.engine.launches and t.mgr.events("a/one")[0].kind == "held"


async def test_a_hold_survives_a_restart(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.hold("a/one", reason="needs_human:checkpoint")
    await t.mgr.shutdown()
    mgr = again(t)
    await mgr.start()
    try:
        assert mgr.get("a/one").hold == "needs_human:checkpoint"
        assert "needs_human:checkpoint" in await agent_blocked(mgr, "a/one")
    finally:
        await mgr.shutdown()


async def test_only_a_human_hand_back_clears_a_hold_never_the_idle_hand_back(tmp_path):
    t = build(tmp_path, control_idle_seconds=60)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.hold("a/one", reason="needs_human:captcha")
    await t.mgr.take_control("a/one", by="owner")  # the human comes to solve it ...
    assert t.mgr.get("a/one").hold == "needs_human:captcha"
    t.wall.advance(minutes=5)  # ... and walks away without handing back
    assert await t.mgr.reap_idle_control() == ["a/one"]
    control = t.mgr.control("a/one")
    assert control.state == ControlState.PAUSED and control.reason == "needs_human:captcha"
    await agent_blocked(t.mgr, "a/one")
    info = await t.mgr.hand_back("a/one", by="owner")
    assert info.control.state == ControlState.AGENT and info.hold is None and info.control.reason is None
    async with t.mgr.using("a/one"):
        pass


async def test_a_self_clearing_hold_ends_at_hold_until_and_never_sooner(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    until = t.wall.now + timedelta(hours=6)
    info = await t.mgr.hold("a/one", reason="rate_limited", until=until)
    assert info.hold_until == until
    message = await agent_blocked(t.mgr, "a/one")
    assert "until 2026-11-04 09:00 UTC" in message and "Do not retry sooner" in message
    t.wall.advance(hours=5, minutes=59)
    await agent_blocked(t.mgr, "a/one")
    assert await t.mgr.reap_holds() == []
    t.wall.advance(minutes=1)
    async with t.mgr.using("a/one"):
        pass
    control = t.mgr.control("a/one")
    assert control.state == ControlState.AGENT and control.by == HOLD_EXPIRED_BY
    assert [e.kind for e in t.mgr.events("a/one")][:2] == ["hold_cleared", "held"]


async def test_the_reaper_ends_expired_holds(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.hold("a/one", reason="rate_limited", until=t.wall.now + timedelta(minutes=10))
    t.wall.advance(minutes=11)
    assert await t.mgr.reap_holds() == ["a/one"]
    assert t.mgr.control("a/one").state == ControlState.AGENT


async def test_waiting_agents_wake_when_a_self_clearing_hold_ends(tmp_path):
    t = build(tmp_path, wall=None)
    t.mgr.now = lambda: datetime.now(UTC)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.hold("a/one", reason="rate_limited", until=datetime.now(UTC) + timedelta(seconds=0.3))
    loop = asyncio.get_running_loop()
    started = loop.time()
    info = await t.mgr.wait_for_agent_control("a/one", timeout=5)
    assert info.control.state == ControlState.AGENT and loop.time() - started < 2


async def test_a_second_hold_never_weakens_the_first(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    changes = []
    t.mgr.add_control_listener(lambda *a: changes.append(a))
    await t.mgr.hold("a/one", reason="needs_human:captcha")
    await t.mgr.hold("a/one", reason="rate_limited", until=t.wall.now + timedelta(hours=6))
    control = t.mgr.control("a/one")
    assert control.reason == "needs_human:captcha" and control.hold_until is None
    assert len(changes) == 1, "the same hold twice is one notification"

    await t.mgr.create(LaneSpec(id="a/two"))
    late = t.wall.now + timedelta(hours=6)
    await t.mgr.hold("a/two", reason="rate_limited", until=late)
    await t.mgr.hold("a/two", reason="rate_limited", until=t.wall.now + timedelta(hours=1))
    assert t.mgr.control("a/two").hold_until == late
    await t.mgr.hold("a/two", reason="needs_human:blocked")
    assert t.mgr.control("a/two").hold_until is None and t.mgr.control("a/two").reason == "needs_human:blocked"


async def test_a_hold_on_a_human_stop_still_needs_the_human(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.stop_agent("a/one", by="owner")
    await t.mgr.hold("a/one", reason="rate_limited", until=t.wall.now + timedelta(minutes=5))
    control = t.mgr.control("a/one")
    assert control.by == "owner" and control.hold_until is None
    t.wall.advance(hours=1)
    await t.mgr.reap_holds()
    assert t.mgr.control("a/one").state == ControlState.PAUSED


async def test_a_human_stop_on_a_self_clearing_hold_makes_it_permanent(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.hold("a/one", reason="rate_limited", until=t.wall.now + timedelta(minutes=5))
    await t.mgr.stop_agent("a/one", by="owner")
    t.wall.advance(hours=1)
    await t.mgr.reap_holds()
    control = t.mgr.control("a/one")
    assert control.state == ControlState.PAUSED and control.by == "owner" and control.hold_until is None


async def test_hold_needs_a_reason_and_ignores_a_time_already_past(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    with pytest.raises(InvalidRequest):
        await t.mgr.hold("a/one", reason="  ")
    info = await t.mgr.hold("a/one", reason="rate_limited", until=t.wall.now - timedelta(seconds=1))
    assert info.control.state == ControlState.AGENT


async def test_a_hold_while_a_human_is_in_control_comes_back_after_they_leave(tmp_path):
    t = build(tmp_path, control_idle_seconds=60)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.take_control("a/one", by="owner")
    await t.mgr.hold("a/one", reason="needs_human:checkpoint")
    assert t.mgr.control("a/one").state == ControlState.HUMAN
    t.wall.advance(minutes=2)
    await t.mgr.reap_idle_control()
    assert t.mgr.control("a/one").state == ControlState.PAUSED


# ---- signals --------------------------------------------------------------------------


async def test_report_signal_records_it_and_holds_through_the_policy(tmp_path):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    info = await t.mgr.report_signal("a/one", Signal.OK)
    assert info.signal == Signal.OK and info.signal_at == t.wall.now and info.hold is None
    info = await t.mgr.report_signal("a/one", "captcha", detail="www.reddit.com")
    assert info.signal == Signal.CAPTCHA and info.hold == "needs_human:captcha on www.reddit.com"
    assert "needs_human:captcha" in await agent_blocked(t.mgr, "a/one")
    with pytest.raises(InvalidRequest):
        await t.mgr.report_signal("a/one", "martian")

    await t.mgr.create(LaneSpec(id="a/two"))
    info = await t.mgr.report_signal("a/two", Signal.RATE_LIMITED)
    assert info.hold == "rate_limited" and info.hold_until == t.wall.now + timedelta(hours=6)


async def test_without_a_policy_signals_are_recorded_and_hold_nothing(tmp_path):
    t = build(tmp_path, policy=None)
    await t.mgr.create(LaneSpec(id="a/one"))
    info = await t.mgr.report_signal("a/one", Signal.CAPTCHA)
    assert info.signal == Signal.CAPTCHA and info.control.state == ControlState.AGENT
    async with t.mgr.using("a/one"):
        pass


async def test_a_policy_that_fails_holds_the_lane_for_a_human(tmp_path):
    def broken(signal, *, now):
        raise RuntimeError("boom")

    t = build(tmp_path, policy=broken)
    await t.mgr.create(LaneSpec(id="a/one"))
    assert (await t.mgr.report_signal("a/one", Signal.BLOCKED)).hold == "needs_human:blocked"


async def test_the_default_policy_is_openajar_node_holds_when_it_exists(tmp_path, monkeypatch):
    t = build(tmp_path)
    assert lanes_mod.default_hold_policy() is None or "openajar.node.holds" in sys.modules
    module = types.ModuleType("openajar.node.holds")
    calls = []

    def hold_for(signal, *, now):
        calls.append(signal)
        return SimpleNamespace(reason=f"needs_human:{signal.value}", until=None)

    module.hold_for = hold_for
    monkeypatch.setitem(sys.modules, "openajar.node.holds", module)
    mgr = again(t)
    assert mgr.hold_policy is hold_for
    await mgr.create(LaneSpec(id="a/one"))
    assert (await mgr.report_signal("a/one", Signal.CHECKPOINT)).hold == "needs_human:checkpoint"
    assert calls == [Signal.CHECKPOINT]


def test_hold_results_in_every_shape():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    assert _as_hold(None, now) is None and _as_hold(False, now) is None
    assert _as_hold("needs_human:captcha", now) == ("needs_human:captcha", None)
    assert _as_hold(("rate_limited", timedelta(hours=6)), now) == ("rate_limited", now + timedelta(hours=6))
    assert _as_hold({"reason": "x", "hold_until": now}, now) == ("x", now)
    assert _as_hold(SimpleNamespace(reason="x", until=60), now) == ("x", now + timedelta(seconds=60))
    assert _as_hold(SimpleNamespace(reason="x", hold_until=datetime(2026, 11, 5)), now) == \
        ("x", datetime(2026, 11, 5, tzinfo=UTC))
    assert _as_hold({"reason": ""}, now) is None


async def test_watch_turns_signals_from_tool_calls_into_holds(tmp_path):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    interaction = FakeInteraction()
    watched = t.mgr.watch(interaction)
    assert t.mgr.watch(watched) is watched
    async with t.mgr.using("a/one") as page:
        await watched.navigate(page, "https://www.reddit.com/r/x?token=secret")
    assert t.mgr.get("a/one").signal == Signal.OK
    interaction.signal_value = Signal.CAPTCHA
    async with t.mgr.using("a/one", principal_kind="flow") as page:
        result = await watched.act(page, "e2", "click")
        assert result.signal == Signal.CAPTCHA  # the step that saw it finishes
    info = t.mgr.get("a/one")
    assert info.hold == "needs_human:captcha on www.reddit.com"  # only the host, never the query
    assert "secret" not in info.model_dump_json()
    await agent_blocked(t.mgr, "a/one")
    await agent_blocked(t.mgr, "a/one", "flow")
    assert watched.fail_with is None  # everything else passes through


class ChallengeReads(FakeInteraction):
    """Snapshots and waits that report the page's signal, as the real interaction's do."""

    async def snapshot(self, page):
        from openajar.core.models import SnapshotResult

        return SnapshotResult(url=page.url, title="t", text="- button [ref=e1]", ref_count=1, signal=self.signal_value)


async def test_every_page_read_that_sees_a_challenge_holds_the_lane(tmp_path):
    from openajar.node.interaction import WaitFor

    for read in ("snapshot", "wait", "screenshot"):
        t = build(tmp_path / read, policy=policy)
        await t.mgr.create(LaneSpec(id="a/one"))
        interaction = ChallengeReads()
        watched = t.mgr.watch(interaction)
        interaction.signal_value = Signal.CAPTCHA
        async with t.mgr.using("a/one") as page:
            if read == "wait":
                await watched.wait(page, WaitFor(for_text=["x"], timeout_s=0))
            else:
                await getattr(watched, read)(page)
        assert t.mgr.get("a/one").hold == "needs_human:captcha", read
        await agent_blocked(t.mgr, "a/one")
        await t.mgr.shutdown()


async def test_a_hand_back_with_the_challenge_still_on_screen_holds_again_at_once(tmp_path):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    interaction = FakeInteraction()
    watched = t.mgr.watch(interaction)
    interaction.signal_value = Signal.CAPTCHA
    async with t.mgr.using("a/one") as page:
        await watched.navigate(page, "https://www.reddit.com/challenge")
    changes: list[tuple[str, str]] = []
    t.mgr.add_control_listener(lambda lane_id, old, new: changes.append((old.state.value, new.state.value)))

    info = await t.mgr.hand_back("a/one", by="owner")
    assert info.control.state is ControlState.PAUSED
    assert info.control.reason == "needs_human:captcha on www.reddit.com" and info.control.by == "node"
    assert ("paused", "agent") not in changes  # agents never had it, not even for a moment
    await agent_blocked(t.mgr, "a/one")

    interaction.signal_value = Signal.OK  # the person solved it
    assert (await t.mgr.hand_back("a/one", by="owner")).control.state is ControlState.AGENT
    async with t.mgr.using("a/one"):
        pass
    await t.mgr.shutdown()


async def test_a_hand_back_reads_the_page_even_when_agents_have_the_lane(tmp_path):
    """A challenge no read has seen yet, on a lane agents still have: the hand-back holds it."""
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    interaction = FakeInteraction()
    watched = t.mgr.watch(interaction)
    async with t.mgr.using("a/one") as page:
        await watched.navigate(page, "https://www.reddit.com/")
    interaction.signal_value = Signal.CAPTCHA  # the page changed by itself; nothing read it
    info = await t.mgr.hand_back("a/one", by="owner")
    assert info.control.state is ControlState.PAUSED and info.control.reason.startswith("needs_human:captcha")
    await agent_blocked(t.mgr, "a/one")
    await t.mgr.shutdown()


async def test_a_hand_back_of_a_rate_limit_backoff_is_the_persons_choice(tmp_path):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    interaction = FakeInteraction()
    watched = t.mgr.watch(interaction)
    interaction.signal_value = Signal.RATE_LIMITED
    async with t.mgr.using("a/one") as page:
        await watched.navigate(page, "https://www.reddit.com/")
    assert (await t.mgr.hand_back("a/one", by="owner")).control.state is ControlState.AGENT
    await t.mgr.shutdown()


async def test_watch_ignores_pages_that_belong_to_no_lane(tmp_path):
    t = build(tmp_path, policy=policy)
    interaction = FakeInteraction()
    interaction.signal_value = Signal.CAPTCHA
    stray = SimpleNamespace(url="https://x.test/")
    assert await t.mgr.watch(interaction).signal(stray) == Signal.CAPTCHA


# ---- quarantine -----------------------------------------------------------------------


async def test_quarantine_closes_the_browser_persists_and_only_a_human_clears_it(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.open("a/one")
    info = await t.mgr.quarantine("a/one", "exit IP 203.0.113.9 does not match proxy lt1; found by the health check")
    assert info.state == LaneState.QUARANTINED and "does not match" in info.quarantine_reason
    assert not t.engine.running and not t.displays.slots
    assert info.control.state == ControlState.PAUSED and info.hold.startswith("quarantined: ")
    for kind in ("agent", "human"):
        with pytest.raises(LaneQuarantined, match="hands it back"):
            await t.mgr.open("a/one", principal_kind=kind)
        with pytest.raises(LaneQuarantined):
            async with t.mgr.using("a/one", principal_kind=kind):
                pass
    await t.mgr.shutdown()

    mgr = again(t)
    await mgr.start()
    try:
        assert mgr.get("a/one").state == LaneState.QUARANTINED
        info = await mgr.hand_back("a/one", by="owner")
        assert info.state == LaneState.COLD and info.quarantine_reason is None and info.hold is None
        async with mgr.using("a/one"):
            pass
        assert [e.kind for e in mgr.events("a/one")][:2] == ["hold_cleared", "quarantine_cleared"]
    finally:
        await mgr.shutdown()


async def test_quarantine_lets_a_tool_call_in_flight_finish_its_step(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    entered, step_done = asyncio.Event(), []

    async def call():
        async with t.mgr.using("a/one"):
            entered.set()
            await asyncio.sleep(0.2)
            step_done.append(t.engine.sessions[0].closed)

    task = asyncio.create_task(call())
    await entered.wait()
    await t.mgr.quarantine("a/one", "manual check")
    await task
    assert step_done == [False] and t.engine.sessions[0].closed


async def test_hand_back_of_a_self_check_quarantine_resets_the_lane(tmp_path):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="192.0.2.99"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    assert (await t.mgr.open("a/one")).state == LaneState.QUARANTINED
    assert (await t.mgr.hand_back("a/one", by="owner")).state == LaneState.COLD


# ---- exit IP --------------------------------------------------------------------------


@pytest.fixture
def no_dns(monkeypatch):
    async def proxy_ips(host):
        return {host}

    monkeypatch.setattr(lanes_quarantine, "_proxy_ips", proxy_ips)


async def test_a_gateway_proxy_pins_its_first_exit_and_quarantines_on_a_change(tmp_path, no_dns):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    await t.mgr.create(LaneSpec(id="a/two", proxy_label="gw"))
    assert (await t.mgr.open("a/one")).state == LaneState.WARM  # a host name: any exit, pinned
    assert t.mgr.store.proxy_exit("gw").exit_ip == "203.0.113.7"
    assert (await t.mgr.open("a/two")).state == LaneState.WARM
    await t.mgr.close("a/two")

    t.engine.exit_ip = "203.0.113.99"  # the provider moved the exit
    info = await t.mgr.open("a/two")
    assert info.state == LaneState.QUARANTINED
    assert "used to exit from 203.0.113.7 and now exits from 203.0.113.99" in info.quarantine_reason
    with pytest.raises(LaneQuarantined):
        await t.mgr.open("a/two")  # reopening does not clear it: a human has to look
    await t.mgr.hand_back("a/two", by="owner")  # the human accepts the new exit
    assert t.mgr.store.proxy_exit("gw").exit_ip == "203.0.113.99"
    assert (await t.mgr.open("a/two")).state == LaneState.WARM


async def test_probe_exit_ip_is_the_self_check_on_demand(tmp_path, no_dns):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    assert await t.mgr.probe_exit_ip("a/one") is None  # cold
    await t.mgr.open("a/one")
    assert await t.mgr.probe_exit_ip("a/one") == "203.0.113.7"
    assert t.mgr.get("a/one").state == LaneState.WARM

    async with t.mgr.using("a/one"):  # busy: the probe skips the lane rather than wait
        assert await t.mgr.probe_exit_ip("a/one") is None

    t.engine.exit_ip = "203.0.113.42"
    assert await t.mgr.probe_exit_ip("a/one") == "203.0.113.42"
    info = t.mgr.get("a/one")
    assert info.state == LaneState.QUARANTINED and not t.engine.running

    t.engine.exit_ip = RuntimeError("proxy down")
    await t.mgr.hand_back("a/one", by="owner")
    t.engine.exit_ip = "203.0.113.42"
    await t.mgr.open("a/one")
    t.engine.exit_ip = RuntimeError("proxy down")
    assert await t.mgr.probe_exit_ip("a/one") is None  # unreadable: never quarantines a warm lane
    assert t.mgr.get("a/one").state == LaneState.WARM


async def test_probe_exit_ip_never_runs_while_a_live_view_is_open(tmp_path, no_dns):
    """The read opens a scratch window over the lane's whole screen: whoever watches would see it
    flash (and lose keyboard focus mid sign-in). The health monitor checks the proxy instead."""
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    await t.mgr.open("a/one")
    t.mgr.view_opened("a/one")
    t.mgr.view_opened("a/one")  # two people watch
    t.engine.exit_ip = "203.0.113.42"  # would quarantine, if it were read
    assert await t.mgr.probe_exit_ip("a/one") is None
    t.mgr.view_closed("a/one")
    assert await t.mgr.probe_exit_ip("a/one") is None  # one still watches
    t.mgr.view_closed("a/one")
    t.mgr.view_closed("a/one")  # an extra close never goes below zero
    t.engine.exit_ip = "203.0.113.7"
    assert await t.mgr.probe_exit_ip("a/one") == "203.0.113.7"
    assert t.mgr.get("a/one").state == LaneState.WARM


async def test_a_static_proxy_must_exit_from_its_own_address(tmp_path):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="192.0.2.99"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    info = await t.mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and "does not match proxy lt1" in info.quarantine_reason
    assert t.mgr.store.proxy_exit("lt1") is None  # never pinned from a mismatch


async def test_an_environment_tunnel_exits_from_its_vm_and_is_never_quarantined_for_it(tmp_path, no_dns):
    """An environment's browser goes through a SOCKS socket on the node (127.0.0.1) and leaves from
    the VM, whose IP is neither the socket's nor fixed. It carries no account, so the exit is not
    judged (a Hetzner VM's lane was quarantined for exactly this, 2026-11-13)."""
    from openajar.core.models import ProxyConfig

    async def node_ip():
        return NODE_IP

    tunnel = ProxyConfig(label="env-e1", host="127.0.0.1", port=1080, scheme="socks5", check_exit=False)
    # a cloud VM; a docker machine on the node itself; a closed network, where the read is refused (2026-11-18)
    for i, exit_ip in enumerate(("203.0.113.66", NODE_IP, OSError("NetworkError when attempting to fetch"))):
        t = build(tmp_path / str(i), self_check=True, engine=FakeEngine(exit_ip=exit_ip),
                  proxies={"env-e1": tunnel}, node_ip_probe=node_ip)
        await t.mgr.create(LaneSpec(id="env/e1-browser", proxy_label="env-e1"))
        info = await t.mgr.open("env/e1-browser")
        assert info.state == LaneState.WARM, info.quarantine_reason
        assert t.mgr.store.proxy_exit("env-e1") is None  # nothing pinned
        assert t.mgr._lanes["env/e1-browser"].exit_ip is None  # and nothing read: the exit is not judged


async def test_a_proxied_lane_leaving_from_the_node_ip_is_quarantined_until_a_human_looks(tmp_path, no_dns):
    async def node_ip():
        return NODE_IP

    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip=NODE_IP), node_ip_probe=node_ip)
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    info = await t.mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and "node's own IP" in info.quarantine_reason
    assert t.mgr.store.get("a/one").quarantine.kind == "exit_ip_is_node"
    assert t.mgr.store.proxy_exit("gw") is None


async def test_a_lane_without_a_proxy_must_leave_from_the_node_ip(tmp_path):
    reads = []

    async def node_ip():
        reads.append(1)
        return NODE_IP

    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"), node_ip_probe=node_ip)
    await t.mgr.create(LaneSpec(id="a/one"))
    info = await t.mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and f"not this node's own IP {NODE_IP}" in info.quarantine_reason
    t.engine.exit_ip = NODE_IP
    assert (await t.mgr.open("a/one")).state == LaneState.WARM
    assert len(reads) == 1  # the node's IP is cached


async def test_an_unreadable_node_ip_skips_the_node_checks(tmp_path):
    async def node_ip():
        raise OSError("offline")

    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"), node_ip_probe=node_ip)
    await t.mgr.create(LaneSpec(id="a/one"))
    assert (await t.mgr.open("a/one")).state == LaneState.WARM


# ---- races (each was a review finding) ----------------------------------------------------


def slow_close(session) -> asyncio.Event:
    """Make the session's close wait until the returned event is set; started is set on entry."""
    gate = asyncio.Event()
    session.started = asyncio.Event()
    original = session.close

    async def close():
        session.started.set()
        await gate.wait()
        await original()

    session.close = close
    return gate


async def test_the_idle_hand_back_looks_again_before_each_lane(tmp_path):
    t = build(tmp_path, control_idle_seconds=60)
    for name in ("a/one", "a/two"):
        await t.mgr.create(LaneSpec(id=name))
        await t.mgr.take_control(name, by="owner")
    t.wall.advance(minutes=2)

    async def listener(lane_id, old, new):
        if lane_id == "a/one":
            t.mgr.note_human_input("a/two")  # the human types on the other lane meanwhile
            await asyncio.sleep(0)

    t.mgr.add_control_listener(listener)
    assert await t.mgr.reap_idle_control() == ["a/one"]
    assert t.mgr.control("a/two").state == ControlState.HUMAN


async def test_an_open_queued_behind_a_quarantining_probe_does_not_relaunch(tmp_path, no_dns):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    await t.mgr.open("a/one")
    t.engine.exit_ip = "203.0.113.99"
    gate = slow_close(t.engine.sessions[-1])
    probe = asyncio.create_task(t.mgr.probe_exit_ip("a/one"))
    await t.engine.sessions[-1].started.wait()  # the probe holds the lane and is closing it
    opening = asyncio.create_task(t.mgr.open("a/one"))
    await asyncio.sleep(0.05)
    gate.set()
    assert await probe == "203.0.113.99"
    with pytest.raises(LaneQuarantined):
        await opening
    assert len(t.engine.launches) == 1 and not t.engine.running


async def test_a_quarantine_while_the_open_waits_for_a_slot_ends_with_no_browser(tmp_path):
    t = build(tmp_path, max_warm_lanes=1, busy_timeout=5)
    await t.mgr.create(LaneSpec(id="a/busy"))
    await t.mgr.create(LaneSpec(id="a/two"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def call():
        async with t.mgr.using("a/busy"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(call())
    await entered.wait()
    opening = asyncio.create_task(t.mgr.open("a/two"))  # waits in _reserve for the only slot
    await asyncio.sleep(0.05)
    quarantining = asyncio.create_task(t.mgr.quarantine("a/two", "manual check"))
    await asyncio.sleep(0.05)
    release.set()
    await holder
    with pytest.raises(LaneQuarantined):
        await opening
    info = await quarantining
    assert info.state == LaneState.QUARANTINED and "a/two" not in t.engine.running


async def test_an_agent_close_that_waited_loses_to_a_take_over(tmp_path):
    from openajar.core.errors import HumanInControl

    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def call():
        async with t.mgr.using("a/one"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(call())
    await entered.wait()
    closing = asyncio.create_task(t.mgr.close("a/one"))  # waits for the call
    await asyncio.sleep(0.05)
    await t.mgr.take_control("a/one", by="owner")
    release.set()
    await holder
    with pytest.raises(HumanInControl):
        await closing
    assert t.engine.running == {"a/one"}, "the human's browser was closed by an agent"


async def test_a_static_proxy_never_accepts_an_exit_other_than_its_host(tmp_path):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip=PROXY.host))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    await t.mgr.open("a/one")
    await t.mgr.close("a/one")
    t.engine.exit_ip = "192.0.2.99"
    info = await t.mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and "not from its own address" in info.quarantine_reason
    await t.mgr.hand_back("a/one", by="owner")  # handing back does not make 192.0.2.99 valid
    assert t.mgr.store.proxy_exit("lt1").exit_ip == PROXY.host
    assert (await t.mgr.open("a/one")).state == LaneState.QUARANTINED
    t.engine.exit_ip = PROXY.host
    await t.mgr.hand_back("a/one", by="owner")
    assert (await t.mgr.open("a/one")).state == LaneState.WARM


async def test_a_gateway_is_not_pinned_while_the_node_ip_is_unknown(tmp_path, no_dns):
    node = [None]

    async def node_ip():
        if node[0] is None:
            raise OSError("rate limited")
        return node[0]

    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"), node_ip_probe=node_ip)
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    info = await t.mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and "could not read this node's own IP" in info.quarantine_reason
    assert t.mgr.store.proxy_exit("gw") is None
    node[0], t.mgr._node_ip_at = NODE_IP, None  # readable again (the cache would retry after a minute)
    assert (await t.mgr.open("a/one")).state == LaneState.WARM
    assert t.mgr.store.proxy_exit("gw").exit_ip == "203.0.113.7"


async def test_a_cancelled_probe_never_loses_its_quarantine(tmp_path, no_dns):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    await t.mgr.open("a/one")
    t.engine.exit_ip = "203.0.113.99"
    session = t.engine.sessions[-1]
    gate = slow_close(session)
    probe = asyncio.create_task(t.mgr.probe_exit_ip("a/one"))
    await session.started.wait()
    probe.cancel()  # for example the health service stopping
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert t.mgr.store.get("a/one").quarantine is not None and session.closed
    with pytest.raises(LaneQuarantined):
        await t.mgr.open("a/one")


async def test_exit_and_node_ips_of_different_versions_are_not_compared(tmp_path, no_dns):
    async def node_ip():
        return NODE_IP  # IPv4, read by the daemon

    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="2001:db8::7"), node_ip_probe=node_ip)
    await t.mgr.create(LaneSpec(id="a/plain"))
    assert (await t.mgr.open("a/plain")).state == LaneState.WARM  # no proxy, the browser read over IPv6
    await t.mgr.create(LaneSpec(id="a/gw", proxy_label="gw"))
    info = await t.mgr.open("a/gw")  # a gateway's first exit cannot be checked against the node: not pinned
    assert info.state == LaneState.QUARANTINED and t.mgr.store.proxy_exit("gw") is None


async def test_two_lanes_pinning_one_gateway_at_once_never_overwrite_the_first_exit(tmp_path, no_dns, monkeypatch):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    proxy = ProxyConfig(label="gw", host="gw.example.test", port=8000)

    async def node_ip():
        return "192.0.2.1"

    monkeypatch.setattr(t.mgr, "_read_node_ip", node_ip)
    # Lane one pins first, but lane two read the pin before that write landed: it sees none.
    real = t.mgr.store.proxy_exit
    reads = []

    def proxy_exit(label):
        reads.append(label)
        return None if len(reads) == 1 else real(label)

    monkeypatch.setattr(t.mgr.store, "proxy_exit", proxy_exit)
    t.mgr.store.pin_proxy_exit("gw", "203.0.113.7", by="self-check (gateway)")

    verdict = await t.mgr._judge_exit("a/two", proxy, "203.0.113.99")
    assert verdict is not None
    reason, record = verdict
    assert record.kind == "exit_ip_changed" and record.exit_ip == "203.0.113.99"
    assert "pinned to exit from 203.0.113.7" in reason
    assert real("gw").exit_ip == "203.0.113.7"  # the first pin holds


def test_pin_proxy_exit_if_absent_keeps_the_first(tmp_path):
    store = LaneStore(tmp_path / "lanes.db")
    assert store.pin_proxy_exit_if_absent("gw", "203.0.113.7").exit_ip == "203.0.113.7"
    assert store.pin_proxy_exit_if_absent("gw", "203.0.113.99").exit_ip == "203.0.113.7"
    assert store.proxy_exit("gw").exit_ip == "203.0.113.7"


# ---- a proxy change, a cleared quarantine -----------------


async def _exit_changed(t: SimpleNamespace, lane_id: str = "a/one") -> None:
    """Quarantine `lane_id` the way a moved gateway exit does (kind exit_ip_changed, label gw)."""
    await t.mgr.create(LaneSpec(id=lane_id, proxy_label="gw"))
    assert (await t.mgr.open(lane_id)).state == LaneState.WARM
    await t.mgr.close(lane_id)
    t.engine.exit_ip = "203.0.113.99"
    assert (await t.mgr.open(lane_id)).state == LaneState.QUARANTINED
    assert t.mgr.store.get(lane_id).quarantine.kind == "exit_ip_changed"


async def test_a_proxy_change_ends_an_exit_ip_quarantine_in_memory_store_and_hold(tmp_path, no_dns):
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await _exit_changed(t)
    info = await t.mgr.set_proxy("a/one", "lt1", confirm=True, by="owner")
    assert info.state == LaneState.COLD and info.quarantine_reason is None
    assert info.hold is None and info.control.state == ControlState.AGENT
    assert t.mgr.store.get("a/one").quarantine is None
    assert t.mgr.store.proxy_exit("gw").exit_ip == "203.0.113.7", "the old proxy's new exit was accepted"
    kinds = [e.kind for e in t.mgr.events("a/one")]
    assert kinds[:3] == ["hold_cleared", "quarantine_cleared", "proxy_changed"]
    await t.mgr.shutdown()

    mgr = again(t)
    await mgr.start()
    try:
        info = mgr.get("a/one")
        assert info.state == LaneState.COLD and info.hold is None  # the store agrees after a restart
        t.engine.exit_ip = "192.0.2.99"  # the new proxy leaks: the self-check still runs first
        assert (await mgr.open("a/one")).state == LaneState.QUARANTINED
    finally:
        await mgr.shutdown()


async def test_a_proxy_change_keeps_a_manual_quarantine_and_a_challenge_hold(tmp_path):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.create(LaneSpec(id="a/two"))
    await t.mgr.quarantine("a/one", "the owner wants to look at it", by="owner")
    await t.mgr.report_signal("a/two", Signal.CHECKPOINT)
    one = await t.mgr.set_proxy("a/one", "lt1", by="owner")
    two = await t.mgr.set_proxy("a/two", "lt1", by="owner")
    assert one.state == LaneState.QUARANTINED and t.mgr.store.get("a/one").quarantine is not None
    assert one.hold.startswith("quarantined: ")
    assert two.control.state == ControlState.PAUSED and two.hold == "needs_human:checkpoint"


async def test_hand_back_after_a_proxy_change_never_accepts_the_old_proxys_exit(tmp_path, no_dns):
    """A lane stuck before the fix: proxy already moved, the old exit_ip_changed record still there."""
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await _exit_changed(t)
    rec = t.mgr.store.get("a/one")
    t.mgr.store.update_spec(rec.spec.model_copy(update={"proxy_label": "lt1"}))  # what the old set_proxy left
    info = await t.mgr.hand_back("a/one", by="owner")
    assert info.state == LaneState.COLD and info.hold is None
    assert t.mgr.store.proxy_exit("gw").exit_ip == "203.0.113.7"


async def test_clearing_a_quarantine_lifts_its_own_hold_and_the_store_agrees(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.quarantine("a/one", "exit IP 203.0.113.9 does not match proxy lt1; found by the health check")
    info = await t.mgr.clear_quarantine("a/one", by="owner")
    assert info.state == LaneState.COLD and info.hold is None and info.control.state == ControlState.AGENT
    async with t.mgr.using("a/one"):
        pass
    await t.mgr.shutdown()
    mgr = again(t)
    await mgr.start()
    try:
        assert mgr.control("a/one").state == ControlState.AGENT and mgr.get("a/one").hold is None
    finally:
        await mgr.shutdown()


async def test_clearing_a_quarantine_keeps_a_challenge_hold_until_hand_back(tmp_path):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.report_signal("a/one", Signal.CHECKPOINT)
    await t.mgr.quarantine("a/one", "manual check")
    info = await t.mgr.clear_quarantine("a/one", by="owner")
    assert info.state == LaneState.COLD and info.hold == "needs_human:checkpoint"
    assert "needs_human:checkpoint" in await agent_blocked(t.mgr, "a/one")
    assert (await t.mgr.hand_back("a/one", by="owner")).hold is None


async def test_clearing_a_quarantine_keeps_a_persons_own_stop(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.stop_agent("a/one", by="owner")
    await t.mgr.quarantine("a/one", "manual check")
    info = await t.mgr.clear_quarantine("a/one", by="owner")
    assert info.control.state == ControlState.PAUSED and info.hold is None
    await agent_blocked(t.mgr, "a/one")


async def test_hand_back_of_a_quarantine_still_hands_back_with_the_note(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.quarantine("a/one", "manual check")
    await t.mgr.hand_back("a/one", by="owner", note="checked the proxy")
    control = t.mgr.control("a/one")
    assert control.state == ControlState.AGENT and control.handback_note == "checked the proxy"
    assert [e.kind for e in t.mgr.events("a/one")][:2] == ["hold_cleared", "quarantine_cleared"]


async def test_clearing_a_quarantine_whose_record_never_landed_lifts_its_hold(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.hold("a/one", reason="quarantined: the write of its record failed")
    info = await t.mgr.clear_quarantine("a/one", by="owner")
    assert info.hold is None and info.control.state == ControlState.AGENT


async def test_a_quarantine_never_hides_another_hold_from_its_release(tmp_path):
    """A backoff under a quarantine, or a challenge on top of one, outlives the quarantine's
    release: clearing or a proxy change lifts only the quarantine's own hold."""
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.create(LaneSpec(id="a/two"))
    await t.mgr.report_signal("a/one", Signal.RATE_LIMITED)
    await t.mgr.quarantine("a/one", "manual check")
    await t.mgr.quarantine("a/two", "manual check")
    await t.mgr.report_signal("a/two", Signal.CHECKPOINT)
    for lane_id, reason in (("a/one", "rate_limited"), ("a/two", "needs_human:checkpoint")):
        info = await t.mgr.clear_quarantine(lane_id, by="owner")
        assert info.state == LaneState.COLD and info.hold == reason
        assert info.control.hold_until is None  # stacked on a quarantine: a person hands it back
        t.wall.advance(hours=7)
        assert reason in await agent_blocked(t.mgr, lane_id)
        assert (await t.mgr.hand_back(lane_id, by="owner")).hold is None


# ---- failing closed: an error never lets agents past a challenge or a quarantine ----------------


async def test_a_bad_signal_the_lane_cannot_be_held_for_stops_the_call_that_read_it(tmp_path, monkeypatch):
    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="a/one"))
    interaction = FakeInteraction()
    watched = t.mgr.watch(interaction)

    async def broken(lane_id, signal, detail=None):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(t.mgr, "report_signal", broken)
    async with t.mgr.using("a/one", principal_kind="flow") as page:
        await watched.navigate(page, "https://www.reddit.com/")  # a quiet signal: nothing to hold
        interaction.signal_value = Signal.CAPTCHA
        with pytest.raises(AgentPaused, match="could not hold the browser") as e:
            await watched.act(page, "e2", "click")
        assert "—" not in e.value.message
        with pytest.raises(AgentPaused):  # a signal that is no Signal is never taken for a quiet one
            await watched._seen(page, "something-new", page.url)
    await t.mgr.shutdown()


def test_a_broken_holds_module_holds_every_bad_signal_for_a_human(monkeypatch):
    from openajar.node.lanes import control as lanes_control

    def importer(error):
        def import_module(name):
            raise error
        return SimpleNamespace(import_module=import_module)

    monkeypatch.setattr(lanes_control, "importlib", importer(RuntimeError("bad holds module")))
    fallback = lanes_control.default_hold_policy()
    assert fallback is lanes_mod.hold_for_a_human and fallback is not None
    assert fallback(Signal.CAPTCHA, now=datetime.now(UTC)) == "needs_human:captcha"
    monkeypatch.setattr(lanes_control, "importlib", importer(ModuleNotFoundError("x", name="some_dependency")))
    assert lanes_control.default_hold_policy() is lanes_mod.hold_for_a_human
    monkeypatch.setattr(lanes_control, "importlib", SimpleNamespace(import_module=lambda name: types.ModuleType(name)))
    assert lanes_control.default_hold_policy() is lanes_mod.hold_for_a_human  # there, but no hold_for
    # a build without the module at all holds nothing, as documented
    monkeypatch.setattr(lanes_control, "importlib",
                        importer(ModuleNotFoundError("x", name="openajar.node.holds")))
    assert lanes_control.default_hold_policy() is None


async def test_the_broken_holds_fallback_holds_for_a_human_and_lets_quiet_signals_pass(tmp_path):
    t = build(tmp_path, policy=lanes_mod.hold_for_a_human)
    await t.mgr.create(LaneSpec(id="a/one"))
    assert (await t.mgr.report_signal("a/one", Signal.OK)).hold is None
    info = await t.mgr.report_signal("a/one", Signal.RATE_LIMITED)
    assert info.hold == "needs_human:rate_limited" and info.hold_until is None
    await agent_blocked(t.mgr, "a/one")


async def test_a_probe_never_vouches_for_an_exit_it_could_not_judge(tmp_path, no_dns):
    """The health monitor takes an IP the manager returns as judged (judges_exit_ip): when the lane's
    proxy cannot be told, the probe returns None so the monitor checks the proxy itself."""
    t = build(tmp_path, self_check=True, engine=FakeEngine(exit_ip="203.0.113.7"))
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    await t.mgr.open("a/one")
    t.mgr.proxies_loader = dict  # its proxy is no longer configured on this node
    t.engine.exit_ip = "203.0.113.99"
    assert await t.mgr.probe_exit_ip("a/one") is None
    await t.mgr.shutdown()


async def test_a_quarantine_that_could_not_be_saved_still_closes_and_refuses_everyone(tmp_path, monkeypatch):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await t.mgr.open("a/one")
    real = t.mgr.store.set_quarantine

    def failing(lane_id, record):
        if record is not None:
            raise RuntimeError("database is locked")
        return real(lane_id, record)

    monkeypatch.setattr(t.mgr.store, "set_quarantine", failing)
    with pytest.raises(RuntimeError, match="database is locked"):
        await t.mgr.quarantine("a/one", "exit IP leaked")
    assert not t.engine.running and not t.displays.slots  # the browser closed all the same
    info = t.mgr.get("a/one")
    assert info.state == LaneState.QUARANTINED and info.quarantine_reason == "exit IP leaked"
    for kind in ("agent", "human"):
        with pytest.raises(LaneQuarantined, match="hands it back"):
            async with t.mgr.using("a/one", principal_kind=kind):
                pass
    info = await t.mgr.hand_back("a/one", by="owner")  # a human clears it, saved or not
    assert info.state == LaneState.COLD and info.hold is None
    async with t.mgr.using("a/one"):
        pass
    await t.mgr.shutdown()


class CheckReads(ChallengeReads):
    """A page whose signal names the rule that matched, as the real interaction's results do."""

    detail = "Cloudflare challenge page"

    async def navigate(self, page, url, **kw):
        result = await super().navigate(page, url, **kw)
        return result.model_copy(update={"signal_detail": self.detail})

    async def report(self, page):
        from openajar.node.sites import SignalReport

        return SignalReport(self.signal_value, self.detail, page.url)


class BrowserContext:
    pass


async def test_a_checkbox_check_holds_an_account_browser_but_not_an_environments(tmp_path):
    """The boundary of the agent's click on a check (openajar.node.interaction.checks): an environment's browser on
    its machine is allowed, an account's never is, and an image puzzle holds both."""
    from openajar.node.interaction import checks

    t = build(tmp_path, policy=policy)
    await t.mgr.create(LaneSpec(id="biz/glassdoor"))
    await t.mgr.create(LaneSpec(id="env/e1-browser"))
    interaction = CheckReads()
    watched = t.mgr.watch(interaction)
    interaction.signal_value = Signal.CAPTCHA
    async with t.mgr.using("biz/glassdoor") as page:
        page.context = BrowserContext()  # an account's browser: never allowed
        await watched.navigate(page, "https://www.glassdoor.com/")
    assert t.mgr.get("biz/glassdoor").hold == "needs_human:captcha on www.glassdoor.com"
    async with t.mgr.using("env/e1-browser") as page:
        page.context = BrowserContext()
        checks.allow(page.context)  # what openajar.environments.vmkeys.attach does for its browser
        await watched.navigate(page, "https://www.glassdoor.com/")
        await watched.screenshot(page)
        assert t.mgr.get("env/e1-browser").hold is None  # the agent has the check's ref
        interaction.detail = "hCaptcha frame"  # an image puzzle: the human's, here too
        await watched.navigate(page, "https://www.glassdoor.com/")
    assert t.mgr.get("env/e1-browser").hold == "needs_human:captcha on www.glassdoor.com"
    await t.mgr.shutdown()
