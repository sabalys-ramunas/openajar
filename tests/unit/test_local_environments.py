"""openajar local, the environment side: an environment opens with its browser on its VM, the VM
firewall follows the laptop's public address, and after a sleep (or a restart) the reapers close what
is past its time limit at once and say so. test_environments.py's fixture, with the local node's lane
manager (openajar.local.lanes, routed to the VM as openajar.local.node does it) and VM settings
(follow_ip, no fixed ssh_from). The VM's browser and screen are fakes (test_environments_vmbrowser's)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from test_environments import TITLE, Machine, ok_probe, readable_repo
from test_environments_vmbrowser import FakePlaywright, VmTunnel
from test_policy_support import Clock, make_service

from openajar.core.config import NodeConfig
from openajar.core.models import Effect, utcnow
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.models import ExecResult
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments import bringup, vmbrowser
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.local import wake
from openajar.local.lanes import NO_LOCAL_BROWSER, build_local_lanes
from openajar.local.node import LOCAL_TOOLS, route_browsers
from openajar.policy.wrap import bind_request_token, wrap_tool
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmSettings
from openajar.vms.provider import FakeProvider

HOME_V4, HOME_V6, CAFE_V4 = "203.0.113.5", "2001:db8::5", "198.51.100.20"


class Where:
    """This computer's public addresses, as the lookup finds them: changed by a test."""

    def __init__(self, *ips: str):
        self.ips = list(ips)
        self.asked = 0

    async def __call__(self) -> list[str]:
        self.asked += 1
        return list(self.ips)


class VmMachine(Machine):
    """The fake VM, which also answers the browser's control script (openajar.environments.vmbrowser)."""

    def __init__(self):
        super().__init__()
        self.asked: list[dict] = []

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None, oom_check=False):
        if cmd != vmbrowser.ctl_command():
            return await super().exec(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, grant_id=grant_id)
        req = json.loads(stdin)
        req.pop("script")
        self.asked.append(req)
        return ExecResult(exit_code=0, stdout=json.dumps({"ok": True}) + "\n", stderr="", duration_s=0.01)


@pytest.fixture
async def local(tmp_path, monkeypatch):
    import playwright.async_api

    home = tmp_path / "home"
    home.mkdir()
    policy, rt = make_service(home, clock=Clock())
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(), settings=TermSettings(proxy_port=0))
    machine = VmMachine()
    monkeypatch.setattr(envs, "exec", machine.exec)
    rt.register("envs", envs)
    prov, where = FakeProvider(), Where(HOME_V4, HOME_V6)
    vms = VmManager(home, runtime=rt, provider=prov, settings=VmSettings(), node_name="local", probe=ok_probe,
                    follow_ip=True, ip_lookup=where)
    rt.register("vms", vms)
    lanes = build_local_lanes(NodeConfig(home=home, self_check=False), reaper_interval=0.05)
    rt.register("lanes", lanes)
    from openajar.node.interaction import create_interaction

    rt.register("interaction", lanes.watch(create_interaction()))
    pw = FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    VmTunnel.made, VmTunnel.stopped = [], []
    monkeypatch.setattr(bringup, "Tunnel", VmTunnel)
    # single use, as an owner who chose it: these tests close and reap machines that are deleted
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings(browser="vm", default_keep="none"))
    mgr.repo_probe = readable_repo
    rt.register("environments", mgr)
    route_browsers(mgr, lanes)
    await lanes.start()
    _, token = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                            specials={"vm": {"lanes": ["env/*"]}, "exec": {"lanes": ["env/*"]}})
    from openajar.env import tool_specs as term_specs
    from openajar.mcp.providers import tool_providers

    specs = {s.name: s for provider in tool_providers(LOCAL_TOOLS) for s in provider(rt) if hasattr(s, "handler")}
    calls = {name: wrap_tool(spec, rt) for name, spec in {**specs, **{s.name: s for s in term_specs(rt)}}.items()}

    async def call(name, **kw):
        with bind_request_token(token, session="chat-1"):
            return await calls[name](**kw)

    yield {"rt": rt, "home": home, "mgr": mgr, "vms": vms, "prov": prov, "where": where, "lanes": lanes, "call": call,
           "machine": machine, "pw": pw, "specs": specs}
    await mgr.shutdown()
    await lanes.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    policy.close()


async def opened(local, **kw):
    out = await local["call"]("environment_open", title=kw.pop("title", TITLE), **kw)
    return await local["mgr"].wait_ready(out["id"], 10)


# ------------------------------------------------------------------ the browser, on the VM


async def test_an_environment_opens_with_a_terminal_and_a_browser_on_its_vm(local):
    env = await opened(local, repo="example.test/me/shop")
    assert env.state == "ready", env.error
    assert env.browser == "vm" and VmTunnel.made == [{"vm_browser": True, "browser_port": None}]
    assert [i.spec.id for i in local["lanes"].list()] == [env.browser_lane]  # the lane, routed to the VM
    out = await local["call"]("terminal_exec", cmd="uname -a")
    assert out["exit_code"] == 0
    st = await local["call"]("environment")
    assert st["links"]["browser"].startswith("http://localhost:") and f"/view/env/{env.id}-browser" in \
        st["links"]["browser"]  # the live view, on this computer's own port
    closed = await local["call"]("environment_close")
    assert closed["state"] == "closed" and local["prov"].servers == {}
    assert local["lanes"].list() == []  # its browser lane went with it


async def test_the_browser_opens_on_the_vm_through_the_routed_engine(local):
    env = await opened(local)
    assert env.state == "ready", env.error
    lanes, machine = local["lanes"], local["machine"]
    await asyncio.gather(*local["mgr"]._warming)  # it started during the open (bringup.start_browser)
    # the VM's screen first, then its browser, once. The screen is asked for again (which keeps it) by the
    # environment's own start in the background: before the browser, or after it on a busy computer
    asked = [a["action"] for a in machine.asked]
    assert asked[0] == "display" and asked.count("browser") == 1 and set(asked) == {"display", "browser"}
    out = await local["call"]("browser_open")  # no `on`: the environment's browser
    assert out["state"] == "warm"
    launched = [a for a in machine.asked if a["action"] == "browser"][-1]
    assert local["pw"].connected_to[-1] == f"ws://127.0.0.1:45999{launched['ws_path']}"
    assert lanes.view(env.browser_lane).vnc_socket == "/tmp/agentos-env-fake/vnc"  # the live view: the tunnel
    session = lanes.live_session(env.browser_lane)
    assert session.alive and session.pids == []  # no process on this computer
    view = await local["call"]("browser_view")
    assert view["viewer"]["url"].startswith("http://localhost:")


async def test_the_browser_tools_are_served_on_a_local_node(local):
    names = set(local["specs"])
    assert {"browser_navigate", "browser_snapshot", "browser_act", "browser_wait", "browser_screenshot",
            "browser_tabs", "browser_dialog", "browser_view", "browser_wait_handback"} <= names
    assert local["rt"].get("interaction") is not None


async def test_no_account_lane_is_made_on_this_computer(local):
    from openajar.core.models import LaneSpec

    with pytest.raises(Exception, match="a browser runs only on an environment's VM") as err:
        await local["lanes"].create(LaneSpec(id="personal/reddit"))
    assert getattr(err.value, "code", None) == "unavailable"
    assert "—" not in NO_LOCAL_BROWSER


async def test_a_lane_that_is_not_routed_to_a_vm_never_starts_a_browser_here(local, monkeypatch):
    """An environment's browser lane whose environment is gone (or was "node"): the router sends it to
    the engine and displays behind it, which refuse. No process starts on this computer."""
    import asyncio
    import subprocess

    from openajar.core.models import LaneSpec

    started: list = []

    async def no_exec(*args, **kw):
        started.append(args)
        raise AssertionError(f"a process was started: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_exec)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: started.append(a) or (_ for _ in ()).throw(
        AssertionError(f"a process was started: {a}")))
    lanes = local["lanes"]
    await lanes.create(LaneSpec(id="env/e0000000000-browser"))
    with pytest.raises(Exception, match="nothing starts a browser on this computer") as err:
        await lanes.open("env/e0000000000-browser")
    assert getattr(err.value, "code", None) == "unavailable" and started == []
    assert lanes.get("env/e0000000000-browser").state.value == "cold" and local["machine"].asked == []


# ------------------------------------------------------------------ the firewall follows the laptop


async def test_each_new_vm_lets_ssh_in_from_where_this_computer_is_now(local):
    prov, where = local["prov"], local["where"]
    env = await opened(local)
    assert env.state == "ready"
    (rules,) = prov.firewalls.values()
    assert rules == [f"{HOME_V4}/32", f"{HOME_V6}/128"]  # IPv4 and IPv6, read just now
    await local["call"]("environment_close")
    where.ips = [CAFE_V4]  # the laptop moved to a cafe with IPv4 only
    env = await opened(local, title="second")
    assert env.state == "ready"
    (rules,) = prov.firewalls.values()
    assert rules == [f"{CAFE_V4}/32"]
    assert where.asked == 2  # read again for each VM, never a cached address


async def test_an_address_that_cannot_be_found_fails_the_open_and_says_what_to_do(local):
    local["where"].ips = []
    with pytest.raises(Exception, match="environment_failed: network: ") as err:
        await opened(local)
    words = str(err.value)
    assert "could not find this computer's public IPv4 address" in words and words.lower().count("try again") == 1
    assert "try again in 2 minutes" in words and "4 tries over about 15 s" in words
    assert "ssh_from in vms.toml" in words and "\u2014" not in words
    assert local["mgr"].store.recent(1)[0].error_reason == "network"
    assert local["prov"].servers == {}  # nothing was rented


async def test_ipv6_alone_is_not_enough_since_a_vm_is_reached_at_its_ipv4_address(local):
    vms = local["vms"]
    local["where"].ips = [HOME_V6]
    with pytest.raises(VmError, match="public IPv4 address"):
        await vms._ssh_from()


async def test_refresh_firewall_moves_the_rules_to_the_new_address(local):
    prov, where = local["prov"], local["where"]
    await opened(local)
    where.ips = [CAFE_V4, "2001:db8::77"]
    assert await local["vms"].refresh_firewall() == [f"{CAFE_V4}/32", "2001:db8::77/128"]
    (rules,) = prov.firewalls.values()
    assert rules == [f"{CAFE_V4}/32", "2001:db8::77/128"]


async def test_a_fixed_ssh_from_wins_over_following_the_address(local):
    vms = local["vms"]
    vms.settings = VmSettings(ssh_from=["192.0.2.0/24"])
    assert await vms._ssh_from() == ["192.0.2.0/24"]
    assert await vms.refresh_firewall() is None
    assert local["where"].asked == 0


async def test_a_server_node_keeps_its_one_address(tmp_path):
    vms = VmManager(tmp_path, provider=FakeProvider(), settings=VmSettings(), public_ip=CAFE_V4)
    assert vms.follow_ip is False
    assert await vms._ssh_from() == [f"{CAFE_V4}/32"]
    assert await vms.refresh_firewall() is None


# ------------------------------------------------------------------ the catch-up after a sleep


def _past_its_limit(mgr, env_id):
    def expired(e):
        e.expires_at = utcnow() - timedelta(minutes=5)

    mgr.store.update(env_id, expired)


async def test_after_a_wake_an_environment_past_its_time_limit_closes_at_once(local):
    mgr, prov = local["mgr"], local["prov"]
    env = await opened(local)
    _past_its_limit(mgr, env.id)
    record = await wake.CatchUp(local["rt"], local["home"]).run("woke", slept_s=3 * 3600 + 600)
    assert [c["id"] for c in record["closed"]] == [env.id]
    assert record["closed"][0]["reason"] == "time limit" and record["errors"] == []
    assert mgr.get(env.id).state == "closed" and prov.servers == {}
    saved = wake.read_last(local["home"])
    assert saved == record
    words = wake.note(saved)
    assert words.startswith("The computer woke at ") and "after 3 h 10 min asleep" in words
    assert f"closed 1 environment at once: {env.id} ({TITLE}: time limit)" in words and "—" not in words


async def test_the_catch_up_at_start_says_nothing_was_past_its_limit(local):
    await opened(local)
    record = await wake.CatchUp(local["rt"], local["home"]).run("started")
    assert record["closed"] == []
    assert wake.note(record).endswith("No environment was past its time limit.")
    assert wake.note(record).startswith("The service started at ")


async def test_status_right_after_a_start_names_this_start_not_the_one_before(local):
    """`openajar local status` right after a restart said "The service started at" the start before (seen twice
    on 2026-11-16): the record was written only when the reapers were done, and they can take minutes."""
    import asyncio
    import json

    old = {"at": "2026-11-15T10:00:00+00:00", "reason": "started", "slept_s": None, "closed": [], "saved": [],
           "kept": [], "vms_deleted": [], "errors": []}
    wake.catch_up_path(local["home"]).write_text(json.dumps(old))
    await opened(local)
    vms = local["rt"].get("vms")
    held = asyncio.Event()

    async def slow_firewall():
        await held.wait()

    vms.refresh_firewall = slow_firewall  # the reapers' first step takes a while
    run = asyncio.create_task(wake.CatchUp(local["rt"], local["home"]).run("started"))
    await asyncio.sleep(0.05)
    during = wake.read_last(local["home"])
    assert during["at"] != old["at"] and during["running"] is True
    assert wake.note(during).endswith("and it is checking the environments now (their time limits and the VMs).")
    held.set()
    done = await run
    assert "running" not in wake.read_last(local["home"]) and wake.read_last(local["home"]) == done


async def test_a_wake_moves_the_firewall_before_the_tunnels_come_back(local):
    await opened(local)
    local["where"].ips = [CAFE_V4]
    await wake.CatchUp(local["rt"], local["home"]).run("woke", slept_s=600)
    (rules,) = local["prov"].firewalls.values()
    assert rules == [f"{CAFE_V4}/32"]


async def test_a_failed_address_lookup_is_recorded_and_the_reapers_still_run(local):
    mgr = local["mgr"]
    env = await opened(local)
    _past_its_limit(mgr, env.id)
    local["where"].ips = []
    record = await wake.CatchUp(local["rt"], local["home"]).run("woke", slept_s=600)
    assert [c["id"] for c in record["closed"]] == [env.id]
    assert record["errors"] and record["errors"][0].startswith("firewall: could not find")
    assert "Some of the check failed" in wake.note(record)


async def test_the_reaper_loop_and_a_catch_up_never_close_one_environment_twice(local):
    import asyncio

    mgr = local["mgr"]
    env = await opened(local)
    _past_its_limit(mgr, env.id)
    first, second = await asyncio.gather(mgr.reap_once(), mgr.reap_once())
    assert first["closed"] + second["closed"] == [env.id]


async def test_the_watch_runs_at_start_and_again_when_the_wall_clock_jumps():
    """The awake clock stops while the computer sleeps; the wall clock does not."""
    import asyncio

    clocks = {"wall": 1_000_000.0, "awake": 50.0}
    ran: list[tuple[str, float | None]] = []
    ticks = 0

    async def sleep(s):
        nonlocal ticks
        ticks += 1
        clocks["awake"] += s
        clocks["wall"] += s + (7200 if ticks == 3 else 0)  # the lid closed for two hours before the third tick
        if ticks > 4:
            await asyncio.Event().wait()  # the test ends here

    class Seen(wake.CatchUp):
        async def run(self, reason, *, slept_s=None):
            ran.append((reason, slept_s))
            return {}

    watch = Seen(None, "/nonexistent", wall=lambda: clocks["wall"], awake=lambda: clocks["awake"], sleep=sleep)
    await watch.start()
    for _ in range(50):
        await asyncio.sleep(0)
    await watch.stop()
    assert ran == [("started", None), ("woke", 7200.0)]
