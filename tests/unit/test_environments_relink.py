"""An environment's link to its machine, kept up while it is open (openajar.environments.relink): a tunnel that
dropped is stopped and started again on the same ports with the same forwards, and in local mode the VM firewall
follows this computer's address while it is awake."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest
from test_environments import node, opened  # noqa: F401 - the fixture
from test_environments_vmbrowser import VmTunnel
from test_local_environments import CAFE_V4, HOME_V4, local  # noqa: F401 - the fixture

from openajar.environments import bringup
from openajar.local import wake
from openajar.mcp.registry import with_catalog_lines
from openajar.policy.wrap import ToolCallError
from openajar.vms.models import VmError


class PortTunnel:
    """bringup's Tunnel as a fake whose ports are this computer's: a second one on the same ports fails while the
    first still holds them, as the real guards do."""

    held: ClassVar[set[int]] = set()
    made: ClassVar[list[PortTunnel]] = []

    def __init__(self, remote, *, gateway_port, socks_port=None, vm_browser=False, browser_port=None):
        self.remote, self.gateway_port = remote, gateway_port
        self.socks_port = socks_port or 41000 + len(PortTunnel.made)
        self.vm_browser = vm_browser
        self.browser_port = (browser_port or 45000 + len(PortTunnel.made)) if vm_browser else None
        self.net_forwards: tuple = ()
        self.alive = False
        self.on_exit = None
        PortTunnel.made.append(self)

    def _ports(self) -> set[int]:
        return {p for p in (self.socks_port, self.browser_port) if p}

    async def start(self):
        if self._ports() & PortTunnel.held:
            raise OSError(f"the tunnel's port {min(self._ports() & PortTunnel.held)} on this computer is taken")
        PortTunnel.held |= self._ports()
        self.alive = True

    def drop(self):
        """The link dropped: ssh is gone, the guards still hold the ports."""
        self.alive = False

    async def stop(self):
        PortTunnel.held -= self._ports()
        self.alive = False


@pytest.fixture
def ports(node, monkeypatch):  # noqa: F811
    PortTunnel.held, PortTunnel.made = set(), []
    monkeypatch.setattr(bringup, "Tunnel", PortTunnel)
    return node


async def test_the_reaper_starts_a_dropped_tunnel_again_on_its_ports_after_stopping_it(ports):
    """The reaper's restart reused the dead tunnel's ports while its guards held them: "port is taken" on every
    pass (the 2026-11-24 sites run). It stops the dead one first now, and the new one has the same ports."""
    mgr = ports["mgr"]
    out = await opened(ports)
    first = mgr._tunnels[out["id"]]
    first.drop()
    got = await mgr.reap_once()
    second = mgr._tunnels[out["id"]]
    assert out["id"] in got["tunnels"] and second is not first and second.alive
    assert second.socks_port == first.socks_port and second.net_forwards == first.net_forwards
    second.drop()
    await mgr.reap_once()
    assert mgr._tunnels[out["id"]].alive and len(PortTunnel.made) == 3


# ------------------------------------------------------------------ the firewall follows this computer while awake

PHONE_V4 = "192.0.2.44"


class GatedTunnel(VmTunnel):
    """The tunnel as a firewall sees it: its ssh connects only while the VM firewall names the address this computer
    has now (`where`), as Hetzner's does. Each start that fails says what ssh said."""

    prov = None
    where = None
    tries: ClassVar[list[bool]] = []

    async def start(self):
        rules = next(iter(GatedTunnel.prov.firewalls.values()), [])
        ok = f"{GatedTunnel.where.ips[0]}/32" in rules
        GatedTunnel.tries.append(ok)
        if not ok:
            raise OSError("the tunnel to the environment's machine did not start: ssh: connect to host 203.0.113.9 "
                          "port 22: Operation timed out")
        self.alive = True


@pytest.fixture
def gated(local, monkeypatch):  # noqa: F811
    GatedTunnel.prov, GatedTunnel.where, GatedTunnel.tries = local["prov"], local["where"], []
    monkeypatch.setattr(bringup, "Tunnel", GatedTunnel)
    from openajar.environments import relink

    monkeypatch.setattr(relink, "RELINK_WAITS_S", (0.0, 0.01, 0.01))
    return local


async def _ready(node_):
    out = await node_["call"]("environment_open", title="Link")
    env = await node_["mgr"].wait_ready(out["id"], 10)
    assert env.state == "ready", env.error
    return env, out["on"]


async def _settled(mgr, env_id):
    for _ in range(200):
        task = mgr._relinking().get(env_id)
        if task is None or task.done():
            return
        await asyncio.sleep(0.01)


async def test_an_address_that_moves_while_awake_moves_the_firewall_and_the_link_comes_back(gated):
    """The 2026-11-24 sites run: this Mac's address moved three times in an hour while awake, and every
    environment's link died for good, since the firewall was set only at start and after a sleep. Now the drop of
    a tunnel is the signal: the firewall follows the address, and the tunnel and the browser come back."""
    mgr, prov, where = gated["mgr"], gated["prov"], gated["where"]
    env, _ = await _ready(gated)
    assert prov.firewalls and next(iter(prov.firewalls.values()))[0] == f"{HOME_V4}/32"
    where.ips = [CAFE_V4]  # the address moved: the tunnel's ssh dies, the firewall still names the old one
    mgr._tunnels[env.id].alive = False
    mgr.tunnel_dropped(env.id)
    await _settled(mgr, env.id)
    assert mgr._tunnels[env.id].alive and mgr.link_down(env.id) is None
    (rules,) = prov.firewalls.values()
    assert rules == [f"{CAFE_V4}/32"]  # only the new address: the old one left in the same call
    assert GatedTunnel.tries[-1] is True and mgr.vms().addresses.changes == 1
    view = mgr.link_view(mgr.get(env.id))
    assert view["state"] == "back" and "address changed" in view["note"] and "—" not in view["note"]


async def test_during_the_gap_a_call_runs_nothing_and_says_the_link_is_coming_back(gated):
    """While the link is down a call gets environment_not_ready (retryable, with retry_after) rather than an
    ssh timeout or a dead browser, and environment() waits for the link."""
    mgr, where = gated["mgr"], gated["where"]
    env, on = await _ready(gated)
    where.ips = [CAFE_V4]
    mgr._tunnels[env.id].alive = False
    vms = mgr.vms()
    held = asyncio.Event()
    follow = vms.follow_address

    async def slow_follow(why, **kw):  # the firewall's change takes a moment (Hetzner's action)
        await held.wait()
        return await follow(why, **kw)

    vms.follow_address = slow_follow
    mgr.tunnel_dropped(env.id)
    await asyncio.sleep(0.02)
    with pytest.raises(ToolCallError) as err:
        await gated["call"]("terminal_exec", on=on, cmd="echo hi")
    words = with_catalog_lines(str(err.value))  # as the agent reads it
    assert words.startswith("environment_not_ready:") and "link to its machine dropped" in words
    assert "Nothing ran" in words and "(try 1)" in words and "\nretry_after: 30" in words and "\u2014" not in words
    waiting = asyncio.create_task(gated["call"]("environment", on=on))
    await asyncio.sleep(0.02)
    held.set()
    out = await waiting
    assert out["link"]["state"] == "back" and mgr._tunnels[env.id].alive
    assert (await gated["call"]("terminal_exec", on=on, cmd="echo hi"))["exit_code"] == 0


async def test_the_minute_check_moves_the_firewall_and_brings_back_a_tunnel_nothing_noticed(gated):
    mgr, prov, where = gated["mgr"], gated["prov"], gated["where"]
    env, _ = await _ready(gated)
    catch = wake.CatchUp(gated["rt"], gated["home"])
    assert await catch.follow() is False  # the same address: no change, and the provider is not asked
    calls = len(prov.firewalls)
    where.ips = [CAFE_V4]
    mgr._tunnels[env.id].alive = False
    mgr.vms().addresses._looked_at = None  # a minute later
    assert await catch.follow() is True
    await _settled(mgr, env.id)
    assert mgr._tunnels[env.id].alive and list(prov.firewalls.values()) == [[f"{CAFE_V4}/32"]] and calls == 1


async def test_the_catch_up_loop_looks_once_a_minute_of_awake_time(local):  # noqa: F811
    looks: list[float] = []
    now = {"t": 0.0}

    async def sleep(s):
        now["t"] += s
        if now["t"] > 200:
            raise asyncio.CancelledError

    catch = wake.CatchUp(local["rt"], local["home"], tick_s=5, wall=lambda: now["t"], awake=lambda: now["t"],
                         sleep=sleep)

    async def follow():
        looks.append(now["t"])
        return False

    catch.follow = follow
    with pytest.raises(asyncio.CancelledError):
        await catch._loop()
    assert looks == [60.0, 120.0, 180.0]


async def test_two_sources_that_disagree_change_nothing(local):  # noqa: F811
    """The address comes from a third-party service and opens a firewall: a change needs a second one not to
    disagree."""
    vms, prov, where = local["vms"], local["prov"], local["where"]
    await vms.refresh_firewall()
    where.ips = [CAFE_V4]

    async def other():
        return PHONE_V4

    vms.addresses._second = other
    vms.addresses._looked_at = None
    out = await vms.follow_address("a test")
    assert out == {"changed": False, "nets": [f"{HOME_V4}/32", "2001:db8::5/128"], "disagreed": PHONE_V4}
    assert list(prov.firewalls.values()) == [[f"{HOME_V4}/32", "2001:db8::5/128"]]

    async def same():
        return CAFE_V4

    vms.addresses._second = same
    vms.addresses._looked_at = None
    assert (await vms.follow_address("a test"))["changed"] is True
    assert list(prov.firewalls.values()) == [[f"{CAFE_V4}/32"]]


@pytest.mark.parametrize("addr", ["10.1.2.3", "192.168.1.20", "100.64.0.9", "127.0.0.1", "169.254.1.1", "0.0.0.0",
                                  "224.0.0.1"])
async def test_a_private_or_reserved_answer_never_opens_the_firewall(local, addr):  # noqa: F811
    vms, prov, where = local["vms"], local["prov"], local["where"]
    vms.preflight_waits = ()
    where.ips = [addr]
    with pytest.raises(VmError, match="public IPv4 address"):
        await vms.refresh_firewall()
    assert prov.firewalls == {}


async def test_looks_close_together_share_one_answer(local):  # noqa: F811
    vms, where = local["vms"], local["where"]
    await vms.refresh_firewall()
    vms.addresses._looked_at = None
    asked = where.asked
    for _ in range(5):  # five tunnels dropped at once
        await vms.follow_address("a drop")
    assert where.asked == asked + 1
