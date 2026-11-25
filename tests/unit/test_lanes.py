"""Lane manager behaviour with a fake engine and fake displays (no browser, no X)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest
from testkit.timing import until

from openajar.core.config import NodeConfig
from openajar.core.errors import (
    EngineError,
    InvalidRequest,
    LaneBusy,
    LaneExists,
    LaneNotFound,
    LaneQuarantined,
)
from openajar.core.models import LaneSpec, LaneState, ProxyConfig, ViewerInfo
from openajar.node.lanes import LaneManagerImpl
from openajar.node.store import LaneStore

# ---- fakes ---------------------------------------------------------------------------


class FakePage:
    def __init__(self, session: FakeSession):
        self.session = session
        self.url = "about:blank"

    async def evaluate(self, js: str, arg=None):
        self.session.engine.evaluations.append(arg)
        ip = self.session.engine.exit_ip
        if isinstance(ip, Exception):
            raise ip
        return ip

    def locator(self, selector: str):
        return FormlessLocator()


class FormlessLocator:
    """What browser_act reads before a click (openajar.node.fields.read_form_diff): no form here."""

    async def evaluate(self, js: str, arg=None):
        return None


class FakeSession:
    _next_pid = 1000

    def __init__(self, engine: FakeEngine, lane_id: str):
        FakeSession._next_pid += 1
        self.engine = engine
        self.lane_id = lane_id
        self.pid = FakeSession._next_pid
        self.alive = True
        self.closed = False
        self._page = FakePage(self)

    async def page(self):
        if not self.alive:
            raise EngineError("not running")
        return self._page

    async def close(self):
        await asyncio.sleep(0)
        self.closed = True
        self.alive = False
        self.engine.running.discard(self.lane_id)


class FakeEngine:
    name = "camoufox"

    def __init__(self, *, exit_ip: str | Exception = "203.0.113.7", delay: float = 0.0):
        self.exit_ip = exit_ip
        self.delay = delay
        self.launches: list[tuple[str, str, ProxyConfig | None]] = []
        self.sessions: list[FakeSession] = []
        self.running: set[str] = set()
        self.max_running = 0
        self.fail_next: Exception | None = None
        self.evaluations: list = []
        self.swept: list[Path] = []

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_next:
            e, self.fail_next = self.fail_next, None
            raise e
        assert profile_dir.is_dir()
        if not fingerprint_path.exists():
            fingerprint_path.write_text(json.dumps({"navigator.platform": "Win32"}))
        self.launches.append((spec.id, display, proxy))
        session = FakeSession(self, spec.id)
        self.sessions.append(session)
        self.running.add(spec.id)
        self.max_running = max(self.max_running, len(self.running))
        return session

    async def sweep_orphans(self, root: Path):
        self.swept.append(root)
        return []


class FakeDisplays:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.slots: dict[str, int] = {}
        self.released: list[str] = []
        self.swept = 0

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        if lane_id not in self.slots:
            free = next(i for i in range(self.config.max_slots) if i not in self.slots.values())
            self.slots[lane_id] = free
        return self.get(lane_id)

    async def release(self, lane_id: str) -> None:
        if self.slots.pop(lane_id, None) is not None:
            self.released.append(lane_id)

    def get(self, lane_id: str) -> ViewerInfo | None:
        if lane_id not in self.slots:
            return None
        i = self.slots[lane_id]
        c = self.config
        return ViewerInfo(display=f":{c.display_base + i}", vnc_port=c.vnc_base + i, viewer_port=c.viewer_base + i)

    async def sweep(self):
        self.swept += 1
        return []


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


PROXY = ProxyConfig(label="lt1", host="198.51.100.10", port=12323, username="u", password="p")


def make(tmp_path: Path, *, engine: FakeEngine | None = None, proxies: dict | None = None, **cfg) -> tuple:
    defaults = {"max_warm_lanes": 3, "busy_timeout": 1.0, "self_check": True, "idle_seconds": 600,
                "display_base": 110, "vnc_base": 5810, "viewer_base": 6110}
    config = NodeConfig(home=tmp_path, **{**defaults, **cfg})
    engine = engine or FakeEngine()
    displays = FakeDisplays(config)
    clock = Clock()
    mgr = LaneManagerImpl(config, LaneStore(config.db_path), engine, displays,
                          lambda: dict(proxies if proxies is not None else {"lt1": PROXY}),
                          clock=clock, reaper_interval=0.01)
    return mgr, engine, displays, clock


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# ---- create, list, get ---------------------------------------------------------------


async def test_create_makes_private_dirs_and_lists(tmp_path):
    mgr, *_ = make(tmp_path)
    info = await mgr.create(LaneSpec(id="personal/reddit"))
    assert info.state == LaneState.COLD and info.last_used is None
    assert mode(mgr.config.lane_dir("personal/reddit")) == 0o700
    assert mode(mgr.config.profile_dir("personal/reddit")) == 0o700
    assert mode(tmp_path / "lanes") == 0o700
    assert [i.spec.id for i in mgr.list()] == ["personal/reddit"]
    with pytest.raises(LaneExists):
        await mgr.create(LaneSpec(id="personal/reddit"))
    with pytest.raises(LaneNotFound):
        mgr.get("personal/nope")
    with pytest.raises(LaneNotFound):
        await mgr.open("personal/nope")


async def test_create_rejects_unknown_proxy_and_engine(tmp_path):
    mgr, *_ = make(tmp_path)
    with pytest.raises(InvalidRequest, match="unknown proxy"):
        await mgr.create(LaneSpec(id="a/b", proxy_label="nope"))
    with pytest.raises(InvalidRequest, match="unknown engine"):
        await mgr.create(LaneSpec(id="a/b", engine="chromium"))
    assert mgr.list() == []


async def test_store_is_shared_across_managers(tmp_path):
    """The CLI creates lanes from another process while the daemon runs."""
    daemon, *_ = make(tmp_path)
    cli, *_ = make(tmp_path)
    assert daemon.list() == []
    await cli.create(LaneSpec(id="personal/reddit", proxy_label="lt1"))
    await cli.create(LaneSpec(id="biz/insta"))
    assert [i.spec.id for i in daemon.list()] == ["biz/insta", "personal/reddit"]
    assert daemon.get("personal/reddit").spec.proxy_label == "lt1"
    info = await daemon.open("biz/insta")
    assert info.state == LaneState.WARM
    # runtime state is the daemon's; the CLI only sees what the store holds
    assert cli.get("biz/insta").state == LaneState.COLD
    assert cli.get("biz/insta").last_used is not None
    await daemon.shutdown()


async def test_restart_leaves_every_lane_cold(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.open("a/one")
    again, *_ = make(tmp_path)
    assert again.get("a/one").state == LaneState.COLD
    assert again.get("a/one").exit_ip == "203.0.113.7"  # persisted from the self-check
    await mgr.shutdown()


# ---- open, close, self-check ---------------------------------------------------------


async def test_open_warms_with_viewer_and_close_goes_cold(tmp_path):
    mgr, engine, displays, _ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    info = await mgr.open("a/one")
    assert info.state == LaneState.WARM
    assert info.viewer and info.viewer.display == ":110" and info.viewer.viewer_port == 6110
    assert mgr.view("a/one") == info.viewer
    assert engine.launches == [("a/one", ":110", None)]
    assert info.exit_ip == "203.0.113.7" and info.last_used is not None
    again = await mgr.open("a/one")  # already warm: no second launch
    assert again.state == LaneState.WARM and len(engine.launches) == 1
    closed = await mgr.close("a/one")
    assert closed.state == LaneState.COLD and closed.viewer is None
    assert engine.sessions[0].closed and displays.released == ["a/one"]
    assert mgr.view("a/one") is None


async def test_self_check_with_matching_proxy(tmp_path):
    mgr, engine, *_ = make(tmp_path, engine=FakeEngine(exit_ip=PROXY.host))
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    info = await mgr.open("a/one")
    assert info.state == LaneState.WARM and info.exit_ip == PROXY.host
    assert engine.launches[0][2] == PROXY
    assert engine.evaluations == ["https://ipinfo.io/ip"]


async def test_self_check_mismatch_quarantines(tmp_path):
    mgr, engine, displays, _ = make(tmp_path, engine=FakeEngine(exit_ip="192.0.2.99"))
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    info = await mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED
    assert "192.0.2.99" in info.quarantine_reason and "lt1" in info.quarantine_reason
    assert "—" not in info.quarantine_reason
    assert engine.sessions[0].closed and displays.released == ["a/one"] and not displays.slots
    with pytest.raises(LaneQuarantined):
        async with mgr.using("a/one"):
            pass
    # a quarantined lane does not hold a warm slot, and reopening retries the check
    engine.exit_ip = PROXY.host
    assert (await mgr.open("a/one")).state == LaneState.WARM
    assert mgr.get("a/one").quarantine_reason is None


async def test_self_check_fetch_failure_with_proxy_quarantines(tmp_path):
    mgr, *_ = make(tmp_path, engine=FakeEngine(exit_ip=RuntimeError("NetworkError")))
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    info = await mgr.open("a/one")
    assert info.state == LaneState.QUARANTINED and "could not read the exit IP" in info.quarantine_reason


async def test_self_check_failure_without_proxy_is_not_fatal(tmp_path):
    mgr, *_ = make(tmp_path, engine=FakeEngine(exit_ip=RuntimeError("offline")))
    await mgr.create(LaneSpec(id="a/one"))
    info = await mgr.open("a/one")
    assert info.state == LaneState.WARM and info.exit_ip is None


async def test_self_check_off_skips_fetch(tmp_path):
    mgr, engine, *_ = make(tmp_path, self_check=False, engine=FakeEngine(exit_ip="192.0.2.99"))
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    assert (await mgr.open("a/one")).state == LaneState.WARM
    assert engine.evaluations == []


async def test_engine_failure_leaves_lane_cold_and_frees_display(tmp_path):
    mgr, engine, displays, _ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    engine.fail_next = RuntimeError("boom")
    with pytest.raises(EngineError, match="boom"):
        await mgr.open("a/one")
    assert mgr.get("a/one").state == LaneState.COLD and not displays.slots
    assert (await mgr.open("a/one")).state == LaneState.WARM


async def test_open_with_missing_proxy_config_fails_before_warming(tmp_path):
    mgr, engine, displays, _ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one", proxy_label="lt1"))
    mgr.proxies_loader = dict
    with pytest.raises(InvalidRequest, match="not configured"):
        await mgr.open("a/one")
    assert engine.launches == [] and not displays.slots


async def test_crashed_browser_is_restarted_on_use(tmp_path):
    mgr, engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.open("a/one")
    engine.sessions[0].alive = False  # the browser died on its own
    async with mgr.using("a/one") as page:
        assert page.session is engine.sessions[1]
    assert len(engine.launches) == 2


# ---- max_warm_lanes, LRU and busy ----------------------------------------------------


async def test_cap_closes_least_recently_used_idle_lane(tmp_path):
    mgr, engine, displays, clock = make(tmp_path, max_warm_lanes=2)
    for name in ("a/one", "a/two", "a/three"):
        await mgr.create(LaneSpec(id=name))
    await mgr.open("a/one")
    clock.advance(1)
    await mgr.open("a/two")
    clock.advance(1)
    async with mgr.using("a/one"):  # one is now the most recently used
        pass
    clock.advance(1)
    await mgr.open("a/three")
    states = {i.spec.id: i.state for i in mgr.list()}
    assert states == {"a/one": LaneState.WARM, "a/two": LaneState.COLD, "a/three": LaneState.WARM}
    assert engine.max_running == 2 and displays.released == ["a/two"]
    # the freed display slot is reused
    assert mgr.view("a/three").display == ":111"


async def test_cap_waits_for_an_active_lane_then_evicts_it(tmp_path):
    mgr, engine, _, _ = make(tmp_path, max_warm_lanes=1, busy_timeout=2.0)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.create(LaneSpec(id="a/two"))
    entered = asyncio.Event()

    async def hold():
        async with mgr.using("a/one"):
            entered.set()
            await asyncio.sleep(0.2)

    holder = asyncio.create_task(hold())
    await entered.wait()
    info = await mgr.open("a/two")
    await holder
    assert info.state == LaneState.WARM
    assert mgr.get("a/one").state == LaneState.COLD and engine.max_running == 1


async def test_cap_all_active_raises_busy_after_timeout(tmp_path):
    mgr, *_ = make(tmp_path, max_warm_lanes=1, busy_timeout=0.2)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.create(LaneSpec(id="a/two"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with mgr.using("a/one"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(LaneBusy) as err:
        await mgr.open("a/two")
    assert 0.15 <= loop.time() - t0 < 1.5
    assert err.value.code == "busy" and "—" not in err.value.message
    assert mgr.get("a/two").state == LaneState.COLD
    assert mgr.get("a/one").state == LaneState.ACTIVE
    release.set()
    await holder


async def test_lane_waiting_for_its_own_lock_is_not_evicted(tmp_path):
    mgr, _engine, _, _ = make(tmp_path, max_warm_lanes=1, busy_timeout=0.2)
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.create(LaneSpec(id="a/two"))
    await mgr.open("a/one")
    lane = mgr._lanes["a/one"]
    lane.users += 1  # a tool call is queued on this lane
    with pytest.raises(LaneBusy):
        await mgr.open("a/two")
    lane.users -= 1
    assert (await mgr.open("a/two")).state == LaneState.WARM


async def test_concurrent_opens_never_exceed_cap(tmp_path):
    mgr, engine, *_ = make(tmp_path, max_warm_lanes=2, busy_timeout=5.0, engine=FakeEngine(delay=0.02))
    names = [f"a/l{i}" for i in range(5)]
    for name in names:
        await mgr.create(LaneSpec(id=name))
    results = await asyncio.gather(*(mgr.open(n) for n in names))
    assert all(r.state == LaneState.WARM for r in results)
    assert engine.max_running <= 2
    assert sum(i.state == LaneState.WARM for i in mgr.list()) == 2


async def test_concurrent_open_of_one_lane_launches_once(tmp_path):
    mgr, engine, *_ = make(tmp_path, engine=FakeEngine(delay=0.05))
    await mgr.create(LaneSpec(id="a/one"))
    await asyncio.gather(*(mgr.open("a/one") for _ in range(4)))
    assert len(engine.launches) == 1


# ---- using() ----------------------------------------------------------------------


async def test_using_serializes_calls_and_tracks_state(tmp_path):
    mgr, engine, _, _clock = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    events: list[str] = []
    inside = 0

    async def call(tag: str):
        nonlocal inside
        async with mgr.using("a/one") as page:
            inside += 1
            assert inside == 1, "two calls held the same lane at once"
            assert mgr.get("a/one").state == LaneState.ACTIVE
            assert page is engine.sessions[0]._page
            events.append(f"enter {tag}")
            await asyncio.sleep(0.02)
            events.append(f"exit {tag}")
            inside -= 1

    await asyncio.gather(call("1"), call("2"), call("3"))
    assert len(engine.launches) == 1  # the first call warmed the lane
    assert len(events) == 6
    for k in range(0, 6, 2):  # every call ran start to finish before the next one entered
        tag = events[k].split()[1]
        assert events[k : k + 2] == [f"enter {tag}", f"exit {tag}"]
    info = mgr.get("a/one")
    assert info.state == LaneState.WARM and info.last_used is not None


async def test_using_touches_last_used_and_survives_errors(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    with pytest.raises(ValueError):
        async with mgr.using("a/one"):
            raise ValueError("tool failed")
    first = mgr.get("a/one")
    assert first.state == LaneState.WARM and first.last_used is not None
    await asyncio.sleep(0.01)
    async with mgr.using("a/one"):
        pass
    assert mgr.get("a/one").last_used > first.last_used


async def test_close_waits_for_the_active_call(tmp_path):
    mgr, _engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="a/one"))
    order: list[str] = []
    entered = asyncio.Event()

    async def call():
        async with mgr.using("a/one"):
            entered.set()
            await asyncio.sleep(0.05)
            order.append("call done")

    task = asyncio.create_task(call())
    await entered.wait()
    await mgr.close("a/one")
    order.append("closed")
    await task
    assert order == ["call done", "closed"]
    assert mgr.get("a/one").state == LaneState.COLD


# ---- idle reaper, start, shutdown -----------------------------------------------------


async def test_reap_idle_closes_only_idle_lanes(tmp_path):
    mgr, _, _, clock = make(tmp_path, idle_seconds=600)
    await mgr.create(LaneSpec(id="a/old"))
    await mgr.create(LaneSpec(id="a/new"))
    await mgr.open("a/old")
    clock.advance(500)
    await mgr.open("a/new")
    clock.advance(200)
    assert await mgr.reap_idle() == ["a/old"]
    assert mgr.get("a/old").state == LaneState.COLD
    assert mgr.get("a/new").state == LaneState.WARM


async def test_reaper_never_closes_an_active_lane(tmp_path):
    mgr, _, _, clock = make(tmp_path, idle_seconds=10)
    await mgr.create(LaneSpec(id="a/one"))
    async with mgr.using("a/one"):
        clock.advance(60)
        assert await mgr.reap_idle() == []
        assert mgr.get("a/one").state == LaneState.ACTIVE
    # using() exit counts as activity, so it is not idle yet
    assert await mgr.reap_idle() == []
    clock.advance(11)
    assert await mgr.reap_idle() == ["a/one"]


async def test_start_sweeps_and_runs_the_reaper(tmp_path):
    mgr, engine, displays, clock = make(tmp_path, idle_seconds=10)
    await mgr.start()
    assert engine.swept == [tmp_path / "lanes"] and displays.swept == 1
    await mgr.create(LaneSpec(id="a/one"))
    await mgr.open("a/one")
    clock.advance(11)
    await until(lambda: mgr.get("a/one").state == LaneState.COLD)
    assert mgr.get("a/one").state == LaneState.COLD
    await mgr.shutdown()
    assert mgr._reaper is None


async def test_shutdown_closes_every_lane(tmp_path):
    mgr, engine, displays, _ = make(tmp_path)
    await mgr.start()
    for name in ("a/one", "a/two"):
        await mgr.create(LaneSpec(id=name))
        await mgr.open(name)
    await mgr.shutdown()
    assert all(s.closed for s in engine.sessions) and not engine.running and not displays.slots
    assert {i.state for i in mgr.list()} == {LaneState.COLD}


# ---- import ------------------------------------------------------------------------


DEAD_PID = 2**22 + 7


def _fake_profile(root: Path) -> Path:
    profile = root / "src-profile"
    (profile / "storage" / "default").mkdir(parents=True)
    (profile / "prefs.js").write_text('user_pref("x", 1);')
    (profile / "cookies.sqlite").write_bytes(b"SQLite format 3\0")
    (profile / "storage" / "default" / "data.bin").write_bytes(b"\1\2")
    (profile / ".parentlock").write_text("")
    # Firefox's lock is a dangling symlink. Its pid can be no process: above Linux's highest pid_max (2**22) and
    # macOS's (99998). 4242 was once a live process on a CI runner.
    os.symlink(f"127.0.0.1:+{DEAD_PID}", profile / "lock")
    for p in profile.rglob("*"):
        if not p.is_symlink():
            p.chmod(0o755 if p.is_dir() else 0o644)
    return profile


async def test_import_strips_locks_and_sets_permissions(tmp_path):
    mgr, *_ = make(tmp_path / "home")
    profile = _fake_profile(tmp_path)
    fp = tmp_path / "fp.json"
    fp.write_text(json.dumps({"navigator.platform": "MacIntel", "navigator.userAgent": "UA"}))
    fp.chmod(0o644)
    info = await mgr.import_profile(LaneSpec(id="personal/reddit", proxy_label="lt1"), profile, fp)
    assert info.state == LaneState.COLD and info.spec.proxy_label == "lt1"
    dest = mgr.config.profile_dir("personal/reddit")
    assert (dest / "prefs.js").read_text() == 'user_pref("x", 1);'
    assert (dest / "storage" / "default" / "data.bin").read_bytes() == b"\1\2"
    assert not (dest / "lock").is_symlink() and not (dest / "lock").exists()
    assert not (dest / ".parentlock").exists()
    assert mode(dest) == 0o700 and mode(mgr.config.lane_dir("personal/reddit")) == 0o700
    assert all(mode(p) & 0o077 == 0 for p in dest.rglob("*"))
    pinned = mgr.config.fingerprint_path("personal/reddit")
    assert mode(pinned) == 0o600
    assert json.loads(pinned.read_text()) == json.loads(fp.read_text())
    assert (profile / "lock").is_symlink(), "the source profile is left untouched"


async def test_import_without_fingerprint(tmp_path):
    mgr, *_ = make(tmp_path / "home")
    await mgr.import_profile(LaneSpec(id="a/b"), _fake_profile(tmp_path), None)
    assert not mgr.config.fingerprint_path("a/b").exists()
    assert (mgr.config.profile_dir("a/b") / "prefs.js").exists()


async def test_import_rejects_bad_input(tmp_path):
    mgr, *_ = make(tmp_path / "home")
    profile = _fake_profile(tmp_path)
    with pytest.raises(InvalidRequest, match="not found"):
        await mgr.import_profile(LaneSpec(id="a/b"), tmp_path / "missing", None)
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(InvalidRequest, match="not valid JSON"):
        await mgr.import_profile(LaneSpec(id="a/b"), profile, bad)
    assert mgr.list() == [] and not mgr.config.profile_dir("a/b").exists()
    await mgr.create(LaneSpec(id="a/b"))
    with pytest.raises(LaneExists):
        await mgr.import_profile(LaneSpec(id="a/b"), profile, None)
    # a profile dir that already holds data is never overwritten
    dest = mgr.config.profile_dir("c/d")
    dest.mkdir(parents=True)
    (dest / "prefs.js").write_text("keep")
    with pytest.raises(InvalidRequest, match="refusing to overwrite"):
        await mgr.import_profile(LaneSpec(id="c/d"), profile, None)
    assert (dest / "prefs.js").read_text() == "keep"


async def test_imported_lane_opens_with_its_fingerprint(tmp_path):
    mgr, _engine, *_ = make(tmp_path / "home")
    fp = tmp_path / "fp.json"
    fp.write_text(json.dumps({"navigator.platform": "MacIntel"}))
    await mgr.import_profile(LaneSpec(id="a/b"), _fake_profile(tmp_path), fp)
    await mgr.open("a/b")
    # the fake engine only writes a fingerprint when none is pinned
    assert json.loads(mgr.config.fingerprint_path("a/b").read_text()) == {"navigator.platform": "MacIntel"}
