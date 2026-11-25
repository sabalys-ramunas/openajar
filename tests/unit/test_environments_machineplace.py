"""A browser on its machine with no proxy claims the machine's own place (openajar.environments.machineplace): its
timezone and WebRTC address, from ipinfo.io's answer for the machine's address, else the provider's location of
the server. Its languages stay en-US, and its launch never waits on this or fails for it."""

from __future__ import annotations

import logging

import pytest
from test_environments import node, opened  # noqa: F401 - the fixture
from test_environments_browserproxy import _ctl_recorder
from test_environments_vmbrowser import FakePlaywright, vm_node  # noqa: F401 - the fixture

from openajar.core.models import LaneSpec
from openajar.environments import machineplace, vmpin

BERLIN = {"ip": "203.0.113.2", "country": "DE", "region": "Bavaria", "city": "Nuremberg",
          "timezone": "Europe/Berlin", "loc": "49.4478,11.0683"}


@pytest.fixture
def bare(vm_node, monkeypatch):  # noqa: F811
    import playwright.async_api

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: FakePlaywright())
    machineplace._KNOWN.clear()
    vm_node["asked"] = _ctl_recorder(vm_node)
    return vm_node


async def _launch(node_, env, tmp_path):
    return await node_["mgr"]._vm_browsers.launch(LaneSpec(id=env.browser_lane), profile_dir=tmp_path / "profile")


def _browser_options(node_):
    [req] = [a for a in node_["asked"] if a["action"] == "browser"][-1:]
    return req["options"]


async def test_no_proxy_claims_the_machines_timezone_and_address_and_keeps_its_languages(bare, tmp_path,
                                                                                         monkeypatch):
    """The 2026-11-24 sites run: with no proxy the browser said UTC from a German datacenter address, and
    Fingerprint's playground flagged "privacy settings"."""
    asked: list[str] = []

    def lookup(ip, timeout=6.0):
        asked.append(ip)
        return {**BERLIN, "ip": ip}

    monkeypatch.setattr(machineplace, "ipinfo_of", lookup)
    monkeypatch.setattr(machineplace, "_lookable", lambda ip: True)  # the fake machine's address is a test one
    out = await opened(bare)
    env = bare["mgr"].get(out["id"])
    host = bare["mgr"].vms().store.get(env.vm_id).host
    assert env.browser_proxy is None
    session = await _launch(bare, env, tmp_path)
    await session.close()
    opts = _browser_options(bare)
    assert opts["config"]["timezone"] == "Europe/Berlin" and opts["config"]["webrtc:ipv4"] == host == asked[0]
    assert opts["locale"] == "en-US" and not any(k.startswith(("locale:", "geolocation:", "headers.Accept-Language"))
                                            for k in opts["config"])
    assert "network.proxy.type" not in opts["firefox_user_prefs"]  # still no proxy
    assert all(vmpin.per_launch(k) for k in ("timezone", "webrtc:ipv4"))  # never pinned: a resume elsewhere follows
    session = await _launch(bare, env, tmp_path)  # a relaunch asks no one again
    await session.close()
    assert asked == [host] and _browser_options(bare)["config"]["timezone"] == "Europe/Berlin"


async def test_without_an_answer_the_servers_location_gives_the_timezone(bare, tmp_path, monkeypatch, caplog):
    def down(ip, timeout=6.0):
        raise OSError("timed out")

    monkeypatch.setattr(machineplace, "ipinfo_of", down)
    out = await opened(bare)
    env = bare["mgr"].get(out["id"])
    rec = bare["mgr"].vms().store.get(env.vm_id)
    assert rec.location == "nbg1"
    with caplog.at_level(logging.INFO, logger="openajar.environments"):
        await (await _launch(bare, env, tmp_path)).close()
    assert _browser_options(bare)["config"]["timezone"] == "Europe/Berlin"
    assert "takes the timezone of its location, nbg1" in caplog.text


async def test_a_place_nobody_knows_starts_the_browser_as_before_and_says_so(bare, tmp_path, monkeypatch, caplog):
    def down(ip, timeout=6.0):
        raise OSError("timed out")

    monkeypatch.setattr(machineplace, "ipinfo_of", down)
    monkeypatch.setattr(machineplace, "PROVIDER_PLACES", {})
    out = await opened(bare)
    env = bare["mgr"].get(out["id"])
    with caplog.at_level(logging.INFO, logger="openajar.environments"):
        await (await _launch(bare, env, tmp_path)).close()
    assert "timezone" not in (_browser_options(bare).get("config") or {})
    assert "could not be learned" in caplog.text and "its browser claims no timezone" in caplog.text


async def test_a_machine_on_this_computers_loopback_has_no_place_to_claim(bare):
    out = await opened(bare)
    env = bare["mgr"].get(out["id"])
    store = bare["mgr"].vms().store
    rec = store.get(env.vm_id)
    rec.host = "127.0.0.1"
    store.put(rec)
    assert await machineplace.place_options(bare["mgr"], env) == {}


def test_ipinfo_answering_for_another_address_is_not_taken(monkeypatch):
    from openajar.core.system import Host

    monkeypatch.setattr(Host, "_through_proxy", lambda self, proxy, url, **kw: '{"ip": "198.51.100.9"}')
    with pytest.raises(OSError, match="another address"):
        machineplace.ipinfo_of("203.0.113.2")
