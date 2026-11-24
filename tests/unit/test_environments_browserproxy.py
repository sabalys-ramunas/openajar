"""An environment's browser through the owner's proxy (openajar.environments.browserproxy): which label an
environment takes, the forwarder on the node that holds the credentials, the prefs and the location the
machine's browser gets, fail closed, save and resume, what the agent sees, and `openajar proxy`.
No real proxy: the upstream is a small server here, the exit check a fake."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from test_environments import node  # noqa: F401 - the fixture
from test_environments_vmbrowser import FakePlaywright, VmTunnel, vm_node  # noqa: F401 - the fixture
from typer.testing import CliRunner

from openajar.core.config import NodeConfig, load_config, load_proxies, save_config, save_proxies
from openajar.core.errors import EngineError
from openajar.core.models import LaneSpec, ProxyConfig, utcnow
from openajar.env.models import ExecResult, RemoteHost
from openajar.env.netproxy import Upstream
from openajar.environments import browserproxy, network, tunnels, vmbrowser
from openajar.environments.browserproxy import OpenPolicy, location_config, pick
from openajar.environments.envproxy import Blocked, EnvForwarder, EnvPolicy
from openajar.environments.models import Environment, EnvSettings, load_settings

# Made-up credentials, put together at run time so the source holds nothing a secret scanner takes for real.
FAKE_USER = "oa" + "-user-" + "x7k"
FAKE_PASS = "Pw" + "-made-up-" + "9q4z"
EXIT = {"ip": "203.0.113.7", "country": "LT", "city": "Vilnius", "timezone": "Europe/Vilnius", "loc": "54.6892,25.2798"}


def _secrets(label: str) -> tuple[str, str]:
    return FAKE_USER + label, FAKE_PASS + label


def _proxies(home, *labels: str) -> None:
    save_proxies(NodeConfig(home=home), {
        label: ProxyConfig(label=label, host=f"{label}.proxy.example", port=9000 + i, username=_secrets(label)[0],
                           password=_secrets(label)[1]) for i, label in enumerate(labels)})


def _no_secret(text: str, *labels: str) -> None:
    for label in labels:
        user, password = _secrets(label)
        assert password not in text and user not in text, label
        assert f"{label}.proxy.example" not in text  # nor the proxy's host


# ------------------------------------------------------------------ which proxy


def test_a_new_environment_takes_the_label_fewest_open_environments_use_ties_in_list_order():
    pool, known = ["lt1", "lt2", "lt3"], {"lt1", "lt2", "lt3"}
    assert pick(pool, known, []) == "lt1"
    assert pick(pool, known, ["lt1"]) == "lt2"
    assert pick(pool, known, ["lt1", "lt2"]) == "lt3"
    assert pick(pool, known, ["lt1", "lt2", "lt3", "lt2"]) == "lt1"
    assert pick(pool, {"lt3"}, []) == "lt3"  # a label proxies.json does not have is skipped
    assert pick([], known, []) is None and pick(["gone"], known, []) is None


def test_the_pool_is_checked_like_allow_hosts():
    assert EnvSettings(browser_proxies=["lt1", " lt2", "lt1"]).browser_proxies == ["lt1", "lt2"]
    assert EnvSettings().browser_proxies == []
    for bad in ("LT1", "lt 1", "", "lt1:pass"):
        with pytest.raises(ValueError):
            EnvSettings(browser_proxies=[bad])


# ------------------------------------------------------------------ what the machine's browser gets


def test_the_prefs_point_only_the_browser_at_the_node_forwarder_on_full_and_closed_networks():
    assert network.vm_browser_prefs("full") == {}
    closed = network.vm_browser_prefs("limited")
    assert closed["network.proxy.ssl_port"] == network.PROXY_PORT_ON_VM == 3130
    for level in ("full", "limited", "none"):
        prefs = network.vm_browser_prefs(level, proxied=True)
        assert prefs["network.proxy.type"] == 1
        assert prefs["network.proxy.http"] == prefs["network.proxy.ssl"] == "127.0.0.1"
        assert prefs["network.proxy.http_port"] == prefs["network.proxy.ssl_port"] == 3132
        assert "localhost" in prefs["network.proxy.no_proxies_on"]  # the machine's dev server stays direct
        assert prefs["media.peerconnection.ice.proxy_only"] is True  # WebRTC only through the proxy
        assert prefs["network.http.http3.enable"] is False and prefs["network.dns.disablePrefetch"] is True
        # nothing of the browser's own leaves past the proxy, or marks the exit with Mozilla's traffic
        assert prefs["network.proxy.failover_direct"] is False and prefs["network.trr.mode"] == 5
        assert prefs["network.connectivity-service.enabled"] is False
        assert prefs["network.captive-portal-service.enabled"] is False
        assert prefs["toolkit.telemetry.enabled"] is False and prefs["app.normandy.enabled"] is False
        assert prefs["browser.safebrowsing.malware.enabled"] is False
    assert network.BROWSER_PROXY_PORT_ON_VM == 3132
    # the machine's other programs keep their own way out: the variables name 3130 or nothing
    assert "3132" not in json.dumps(network.proxy_env("limited", {}, ca=False))
    assert network.proxy_env("full", {}, ca=False) == {}


def test_the_exit_becomes_camoufox_config_keys():
    got = location_config(EXIT)
    assert 1500 <= got.pop("geolocation:accuracy") < 6000  # a city's, the same for this exit IP every launch
    assert got == {"timezone": "Europe/Vilnius", "geolocation:latitude": 54.6892, "geolocation:longitude": 25.2798,
                   "webrtc:ipv4": "203.0.113.7"}
    assert location_config({"ip": "2001:db8::1", "timezone": "Not/AZone", "loc": "x,y"}) == {"webrtc:ipv6": "2001:db8::1"}
    assert location_config({"ip": "203.0.113.7", "loc": "95,10"}) == {"webrtc:ipv4": "203.0.113.7"}
    assert location_config({}) == {}


def test_the_machines_browser_claims_linux_when_the_engine_names_no_os():
    spec = LaneSpec(id="env/e1234567890-browser")
    assert vmbrowser.browser_options(spec)["os"] == ["linux"]  # openajar local: NoLocalEngine.identity_os is None
    assert vmbrowser.browser_options(spec, identity_os=["windows"])["os"] == ["windows"]
    from openajar.local.lanes import NoLocalEngine

    assert vmbrowser.browser_options(spec, identity_os=NoLocalEngine.identity_os)["os"] == ["linux"]


def test_the_open_policy_lets_any_public_host_and_port_through_and_nothing_local():
    p = OpenPolicy()
    for host, port in (("example.com", 443), ("example.com", 8443), ("93.184.216.34", 80), ("stun.example.org", 3478)):
        assert p.check(host, port)[0], (host, port)
    for host in ("localhost", "app.localhost", "printer.local", "127.0.0.1", "10.1.2.3", "192.168.1.1",
                 "169.254.169.254", "::1", "fd00::1", "a b"):
        assert not p.check(host, 443)[0], host


# ------------------------------------------------------------------ the forwarder on the node


async def _upstream(seen: list[str], password: str):
    """An HTTP proxy that wants Basic credentials: a CONNECT is answered and then echoed."""

    async def serve(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        seen.append(head)
        token = base64.b64encode(f"{FAKE_USER}:{password}".encode()).decode()
        if f"Proxy-Authorization: Basic {token}" not in head:
            w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()
            return
        w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await w.drain()
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        w.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _browser_forwarder(policy, upstream_port: int, password: str):
    fwd = EnvForwarder("e1", policy, Blocked(clock=utcnow),
                       upstream=Upstream("127.0.0.1", upstream_port, FAKE_USER, password))
    server = await asyncio.start_server(fwd._client, "127.0.0.1", 0)
    return fwd, server, server.sockets[0].getsockname()[1]


async def _connect(port: int, target: str) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await w.drain()
    return await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5), r, w


async def test_the_browser_forwarder_chains_to_the_proxy_with_its_credentials_on_any_port():
    seen: list[str] = []
    password = FAKE_PASS
    up, uport = await _upstream(seen, password)
    _fwd, server, port = await _browser_forwarder(OpenPolicy(), uport, password)
    try:
        head, r, w = await _connect(port, "example.com:8443")
        assert head.startswith(b"HTTP/1.1 200")
        w.write(b"hello")
        assert await asyncio.wait_for(r.readexactly(5), 5) == b"hello"
        w.close()
        assert seen[0].startswith("CONNECT example.com:8443")
        head, _r, w = await _connect(port, "10.0.0.5:443")  # a private address never reaches the upstream
        assert head.startswith(b"HTTP/1.1 403") and len(seen) == 1
        w.close()
    finally:
        server.close()
        up.close()


async def test_a_proxy_that_refuses_the_credentials_fails_closed():
    seen: list[str] = []
    up, uport = await _upstream(seen, FAKE_PASS)
    _fwd, server, port = await _browser_forwarder(OpenPolicy(), uport, "wrong" + FAKE_PASS)
    try:
        head, _r, w = await _connect(port, "example.com:443")
        assert head.startswith(b"HTTP/1.1 502") and b"407" in head + await _r.read(200)
        w.close()
    finally:
        server.close()
        up.close()


async def test_on_a_closed_network_the_browser_forwarder_keeps_to_the_list():
    seen: list[str] = []
    up, uport = await _upstream(seen, FAKE_PASS)
    _fwd, server, port = await _browser_forwarder(EnvPolicy(["allowed.example"]), uport, FAKE_PASS)
    try:
        head, _r, w = await _connect(port, "other.example:443")
        assert head.startswith(b"HTTP/1.1 403") and seen == []
        w.close()
        head, _r, w = await _connect(port, "allowed.example:443")
        assert head.startswith(b"HTTP/1.1 200")
        w.close()
    finally:
        server.close()
        up.close()


# ------------------------------------------------------------------ an environment, end to end (fakes)


def _ctl_recorder(node_):
    """The machine's control script, as a fake: what each request carried, and an ok."""
    asked: list[dict] = []
    plain = node_["machine"].exec

    async def exec_(lane_id, cmd, *, stdin=None, **kw):
        if cmd == vmbrowser.ctl_command():
            req = json.loads(stdin)
            req.pop("script")
            asked.append(req)
            return ExecResult(exit_code=0, stdout=json.dumps({"ok": True}) + "\n", stderr="", duration_s=0.01)
        kw.pop("use", None)
        return await plain(lane_id, cmd, stdin=stdin, **kw)

    node_["envs"].exec = exec_
    return asked


@pytest.fixture
def proxied(vm_node, monkeypatch):  # noqa: F811
    import playwright.async_api

    mgr = vm_node["mgr"]
    _proxies(mgr.home, "lt1", "lt2")
    mgr.settings = EnvSettings(browser="vm", desktop=False, browser_proxies=["lt1", "lt2"])
    mgr.proxy_probe = lambda proxy, timeout: dict(EXIT)
    pw = FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    vm_node["asked"] = _ctl_recorder(vm_node)
    return vm_node


async def _launch(node_, env, tmp_path):
    return await node_["mgr"]._vm_browsers.launch(LaneSpec(id=env.browser_lane), profile_dir=tmp_path / "profile")


async def test_the_browser_exits_through_the_node_and_nothing_secret_reaches_the_machine(proxied, tmp_path):
    from test_environments import opened

    mgr = proxied["mgr"]
    one = await opened(proxied, title="one")
    two = await opened(proxied, title="two", session="chat-2")
    env, other = mgr.get(one["id"]), mgr.get(two["id"])
    assert (env.browser_proxy, other.browser_proxy) == ("lt1", "lt2")  # the fewest open, in list order

    net = mgr._nets[env.id]
    assert (3132, net.browser_proxy_port) in net.forwards()
    assert mgr._tunnels[env.id].net_forwards == net.forwards()  # the tunnel carries it to the machine
    remote = RemoteHost(vm_id="vm_1", host="127.0.0.1", port=22, key_path="/k", known_hosts="/kh",
                        host_alias="openajar-vm_1")
    argv = tunnels.net_tunnel_argv(remote, net.forwards())
    assert f"127.0.0.1:3132:127.0.0.1:{net.browser_proxy_port}" in argv
    _no_secret(" ".join(argv), "lt1", "lt2")

    session = await _launch(proxied, env, tmp_path)
    try:
        [browser] = [a for a in proxied["asked"] if a["action"] == "browser"]
        opts = browser["options"]
        assert "proxy" not in opts and opts["os"] == ["linux"] and opts["geoip"] is False
        assert opts["firefox_user_prefs"]["network.proxy.ssl_port"] == 3132
        place = {k: v for k, v in browserproxy.exit_options(EXIT)["config"].items() if not k.startswith("geolocation:")}
        assert {k: v for k, v in opts["config"].items() if not k.startswith("window.")} == place
        assert opts["firefox_user_prefs"]["permissions.default.geo"] == 0  # no position: the page's ask waits
        assert opts["config"]["timezone"] == "Europe/Vilnius"
        assert opts["locale"] == ["lt-LT", "lt", "en-US", "en"]  # the exit country's (browser_language "exit")
    finally:
        await session.close()

    sent = json.dumps(proxied["asked"]) + json.dumps([[c, s] for c, _g, s in proxied["machine"].ran_in])
    _no_secret(sent, "lt1", "lt2")

    view = mgr.network_view(mgr.get(env.id))
    assert view["browser_proxy"] == {"label": "lt1", "exit_ip": "203.0.113.7", "exit_country": "LT"}
    shown = await proxied["call"]("environment", on=one["on"])
    assert shown["network"]["browser_proxy"]["label"] == "lt1"
    _no_secret(json.dumps(shown, default=str) + json.dumps(view), "lt1", "lt2")
    assert "9000" not in json.dumps(view)  # nor the proxy's port


async def test_a_proxy_that_fails_its_check_stops_the_launch_with_what_to_run(proxied, tmp_path):
    from test_environments import opened

    mgr = proxied["mgr"]
    env = mgr.get((await opened(proxied))["id"])

    def refused(proxy, timeout):
        raise OSError("the proxy rejected its credentials (HTTP 407)")

    mgr.proxy_probe = refused
    with pytest.raises(EngineError) as e:
        await _launch(proxied, env, tmp_path)
    assert "lt1" in e.value.message and "openajar proxy test lt1" in e.value.message and "407" in e.value.message
    assert "\u2014" not in e.value.message
    assert not [a for a in proxied["asked"] if a["action"] == "browser"]  # no browser started without it


async def test_a_label_gone_from_proxies_json_starts_no_forwarder_and_no_browser(proxied, tmp_path):
    from test_environments import opened

    mgr = proxied["mgr"]
    env = mgr.get((await opened(proxied))["id"])
    await mgr.net_down(env.id, forget=False)
    _proxies(mgr.home, "lt2")  # lt1 is gone; the node restarts its side of the network
    await mgr.net_forwards(env)
    assert mgr._nets[env.id].browser_proxy_port is None and all(p != 3132 for p, _ in mgr._nets[env.id].forwards())
    with pytest.raises(EngineError) as e:
        await _launch(proxied, env, tmp_path)
    assert "lt1" in e.value.message and not [a for a in proxied["asked"] if a["action"] == "browser"]


async def test_a_node_browser_environment_takes_no_proxy(node):  # noqa: F811
    from test_environments import opened

    _proxies(node["mgr"].home, "lt1")
    node["mgr"].settings = EnvSettings(browser_proxies=["lt1"])  # browser = "node": not covered
    env = node["mgr"].get((await opened(node))["id"])
    assert env.browser_proxy is None and "browser_proxy" not in node["mgr"].network_view(env)


# ------------------------------------------------------------------ save and resume


def _resuming(mgr, saved: str | None) -> Environment:
    now = utcnow()
    return Environment(id="e1234567890", title="t", grant_id="g", binding="session:x", created_at=now,
                       expires_at=now, browser="vm", resumed={"save_id": "s1", "missing": [], "browser_proxy": saved})


def test_a_resume_keeps_its_saves_proxy(proxied):
    mgr = proxied["mgr"]
    env = _resuming(mgr, "lt2")
    mgr.assign_browser_proxy(env)
    assert env.browser_proxy == "lt2" and "browser_proxy" not in env.resumed and "browser_proxy_note" not in env.resumed


async def test_a_resume_whose_proxy_is_gone_picks_again_and_says_so(proxied, monkeypatch):
    mgr = proxied["mgr"]
    mgr.settings.browser_proxies = ["lt1"]
    env = _resuming(mgr, "lt2")
    mgr.assign_browser_proxy(env)
    assert env.browser_proxy == "lt1"
    mgr.store.put(env)

    async def restored(e):
        return e

    monkeypatch.setattr(mgr, "_restore", restored)
    env = await mgr._restore_any(mgr.get(env.id))
    [said] = env.resumed["missing"]
    assert "lt2" in said and "lt1" in said and "browser_proxy_note" not in env.resumed

    mgr.settings.browser_proxies = []
    none = _resuming(mgr, "lt2")
    mgr.assign_browser_proxy(none)
    assert none.browser_proxy is None and "from the machine itself" in none.resumed["browser_proxy_note"]


def test_a_save_carries_the_label_and_its_resume_brings_it_back(proxied):
    from openajar.environments.saves import SaveManifest

    m = SaveManifest(id="s1", family="g", title="t", env_id="e1234567890", saved_at=utcnow(), browser_proxy="lt2")
    plan = {"restore": {"from": "files"}}
    assert proxied["mgr"].resuming(m, plan)["browser_proxy"] == "lt2"
    assert SaveManifest.model_validate_json(m.model_dump_json()).browser_proxy == "lt2"


def _new(mgr, title: str, *, family: str = "g", repo: str | None = None, state: str = "starting",
         label: str | None = None, eid: str | None = None) -> Environment:
    now = utcnow()
    return Environment(id=eid or f"e{abs(hash((title, family, now))) % 10**10:010d}", title=title, grant_id=family,
                       binding="session:x", created_at=now, expires_at=now, browser="vm", repo=repo, state=state,
                       browser_proxy=label)


def test_a_new_environment_of_the_same_title_or_repo_takes_the_label_it_had_last_time(proxied):
    mgr = proxied["mgr"]
    mgr.settings.browser_proxies = ["lt1", "lt2"]
    mgr.store.put(_new(mgr, "Shop", state="closed", label="lt2"))  # closed, no save: keep "none"
    env = _new(mgr, "shop ")
    mgr.assign_browser_proxy(env)
    assert env.browser_proxy == "lt2"  # a fresh pick would be lt1
    mgr.store.put(_new(mgr, "other", repo="acme/app", state="closed", label="lt2"))
    by_repo = _new(mgr, "new title", repo="acme/app")
    mgr.assign_browser_proxy(by_repo)
    assert by_repo.browser_proxy == "lt2"
    other_family = _new(mgr, "shop", family="g2")
    mgr.assign_browser_proxy(other_family)
    assert other_family.browser_proxy == "lt1"  # another family's history is not this one's
    mgr.store.put(_new(mgr, "busy", state="ready", label="lt2"))  # lt2 is in use now: the fewest-used instead
    taken = _new(mgr, "shop")
    mgr.assign_browser_proxy(taken)
    assert taken.browser_proxy == "lt1"


async def test_a_resume_whose_proxy_now_exits_elsewhere_says_so(proxied, tmp_path):
    from openajar.environments.saves import SaveManifest

    mgr = proxied["mgr"]
    m = SaveManifest(id="s1", family="g", title="t", env_id="e1234567890", saved_at=utcnow(), browser_proxy="lt1",
                     browser_exit_ip="198.51.100.9")
    assert SaveManifest.model_validate_json(m.model_dump_json()).browser_exit_ip == "198.51.100.9"
    mgr.saves = lambda: type("S", (), {"find": staticmethod(lambda sid: (m, tmp_path))})()
    env = _resuming(mgr, "lt1")
    env.restore_from, env.browser_proxy = "s1", "lt1"
    mgr.store.put(env)
    mgr._nets[env.id] = type("N", (), {"browser_upstream": object(), "browser_exit": None})()
    await mgr.browser_proxy_options(mgr.get(env.id))  # the label's proxy now exits from 203.0.113.7
    said = mgr.get(env.id).resumed["missing"]
    assert len(said) == 1 and "198.51.100.9" in said[0] and "203.0.113.7" in said[0] and "lt1" in said[0]
    await mgr.browser_proxy_options(mgr.get(env.id))  # said once
    assert len(mgr.get(env.id).resumed["missing"]) == 1
    assert mgr.browser_exit_ip(mgr.get(env.id)) == "203.0.113.7"  # what its next save keeps
    m.browser_exit_ip = "203.0.113.7"
    mgr._nets[env.id].browser_exit = None
    mgr.store.update(env.id, lambda e: setattr(e, "resumed", {"missing": []}))
    await mgr.browser_proxy_options(mgr.get(env.id))
    assert mgr.get(env.id).resumed["missing"] == []  # the same address: nothing to say
    del mgr._nets[env.id]  # the fake network node: nothing for the shutdown to stop


# ------------------------------------------------------------------ openajar proxy


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    from openajar.cli import app

    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    return home


def _cli(*args: str, input: str | None = None):
    from openajar.cli import app

    return CliRunner().invoke(app, list(args), input=input)


def _local(home) -> None:
    save_config(load_config(home).model_copy(update={"name": "local"}))


def test_proxy_add_joins_the_environments_list_by_default_only_in_local_mode(home):
    user, password = _secrets("lt1")
    out = _cli("proxy", "add", "lt1", f"5.6.7.8:8000:{user}:{password}")
    assert out.exit_code == 0 and "will not use it" in out.output and load_settings(home).browser_proxies == []
    _local(home)
    out = _cli("proxy", "add", "lt2", f"5.6.7.9:8000:{user}:{password}")
    assert out.exit_code == 0, out.output
    assert "Environments' browsers will exit through it: environments opened from now on." in out.output
    assert load_settings(home).browser_proxies == ["lt2"]
    out = _cli("proxy", "add", "lt1", f"5.6.7.8:8000:{user}:{password}", "--environments")
    assert load_settings(home).browser_proxies == ["lt2", "lt1"]
    out = _cli("proxy", "add", "lt1", f"5.6.7.8:8000:{user}:{password}", "--no-environments")
    assert load_settings(home).browser_proxies == ["lt2"]
    from openajar.environments.store import EnvironmentStore

    now = utcnow()
    EnvironmentStore(home / "environments.db").put(Environment(
        id="e1234567890", title="shop", grant_id="g", binding="session:x", created_at=now, expires_at=now,
        state="ready", browser="vm", browser_proxy="lt2"))
    out = _cli("proxy", "add", "lt2", f"5.6.7.10:8000:{user}:{password}")  # another host: refused, and says who
    assert out.exit_code == 1 and "e1234567890 (shop)" in out.output
    out = _cli("proxy", "add", "lt2", f"5.6.7.10:8000:{user}:{password}", "--yes")
    assert out.exit_code == 0 and "get its new address" in out.output and "e1234567890 (shop)" in out.output
    assert password not in _cli("proxy", "list").output


def test_proxy_add_on_a_node_browser_says_it_is_not_covered(home):
    (home / "environments.toml").write_text('browser = "node"\n')
    user, password = _secrets("lt1")
    out = _cli("proxy", "add", "lt1", f"5.6.7.8:8000:{user}:{password}", "--environments")
    assert out.exit_code == 0 and 'browser = "vm"' in out.output and password not in out.output


def test_proxy_list_marks_the_environments_proxies(home):
    _proxies(home, "lt1", "lt2")
    (home / "environments.toml").write_text('browser_proxies = ["lt2", "gone"]\n')
    out = _cli("proxy", "list")
    lines = out.output.splitlines()
    assert lines[0].startswith("lt1") and not lines[0].endswith("environments")
    assert lines[1].startswith("lt2") and lines[1].endswith("environments")
    assert "not here: gone" in out.output
    _no_secret(out.output.replace("lt1.proxy.example", "").replace("lt2.proxy.example", ""), "lt1", "lt2")


def test_proxy_remove_takes_it_out_of_the_list_and_names_the_open_environments(home, monkeypatch):
    _proxies(home, "lt1", "lt2")
    (home / "environments.toml").write_text('browser_proxies = ["lt1", "lt2"]\n')
    from openajar.environments.store import EnvironmentStore

    now = utcnow()
    EnvironmentStore(home / "environments.db").put(Environment(
        id="e1234567890", title="shop", grant_id="g", binding="session:x", created_at=now, expires_at=now,
        state="ready", browser="vm", browser_proxy="lt1"))
    from openajar.environments.saves import SaveStore

    monkeypatch.setattr(SaveStore, "all", lambda self, family=None: [
        type("M", (), {"title": "shop", "id": "s7", "browser_proxy": "lt1"})()])
    out = _cli("proxy", "remove", "lt1")
    assert out.exit_code == 0, out.output
    assert "Removed proxy lt1." in out.output and "e1234567890 (shop)" in out.output
    assert "Saves that exited through it" in out.output and "shop (s7)" in out.output
    assert load_settings(home).browser_proxies == ["lt2"] and "lt1" not in load_proxies(load_config(home))
    assert _cli("proxy", "remove", "lt1").exit_code == 1

    from openajar.core import layout

    monkeypatch.setattr(layout, "proxy_users", lambda home, label: ["personal/reddit"])
    out = _cli("proxy", "remove", "lt2")
    assert out.exit_code == 1 and "personal/reddit" in out.output and "lt2" in load_proxies(load_config(home))


def test_proxy_test_says_each_exit_or_why_it_failed(home, monkeypatch):
    from openajar.core import system

    _proxies(home, "lt1", "lt2")

    def exit_info(self, proxy, *, timeout):
        if proxy.label == "lt2":
            raise OSError(f"cannot connect for {proxy.password}")  # redacted before it is shown
        return dict(EXIT)

    monkeypatch.setattr(system.Host, "proxy_exit_info", exit_info)
    out = _cli("proxy", "test", "lt1")
    assert out.exit_code == 0 and "lt1  exit 203.0.113.7  Vilnius, LT  Europe/Vilnius" in out.output
    out = _cli("proxy", "test")
    assert out.exit_code == 1 and "lt2  failed: cannot connect" in out.output
    _no_secret(out.output, "lt1", "lt2")
    assert _cli("proxy", "test", "nope").exit_code == 1


def test_browserproxy_words_carry_no_em_dash():
    for words in (browserproxy.gone_words("lt1"), browserproxy.failed_words("lt1", "x")):
        assert "\u2014" not in words


# ------------------------------------------------------------------ where the exit is, as the browser shows it


def test_a_thin_exit_answer_takes_the_timezone_and_place_from_its_country():
    from openajar.environments.exitplace import COUNTRY_ACCURACY_M

    # no timezone: the country's own zone (one zone), or the one nearest the position (several)
    assert location_config({"ip": "203.0.113.7", "country": "LT", "loc": "54.69,25.28"})["timezone"] == "Europe/Vilnius"
    assert location_config({"country": "US", "loc": "41.88,-87.63"})["timezone"] == "America/Chicago"
    assert location_config({"country": "US", "loc": "34.05,-118.24"})["timezone"] == "America/Los_Angeles"
    assert location_config({"country": "BR", "timezone": "Not/AZone"})["timezone"] == "America/Sao_Paulo"
    # no position: the main zone's city, and an accuracy that says so
    got = location_config({"ip": "203.0.113.7", "country": "JP", "timezone": "Asia/Tokyo"})
    assert got["geolocation:accuracy"] == COUNTRY_ACCURACY_M and 35 < got["geolocation:latitude"] < 36
    # a zone this computer may not know yet but tzdata's table has, and nothing at all from a place unknown
    assert location_config({"country": "MX", "timezone": "America/Ciudad_Juarez"})["timezone"] == "America/Ciudad_Juarez"
    assert location_config({"ip": "203.0.113.7", "country": "ZZ"}) == {"webrtc:ipv4": "203.0.113.7"}
    assert location_config({"country": "LT", "timezone": "UTC"})["timezone"] == "Europe/Vilnius"  # no place has UTC
    assert location_config({"country": "LT", "loc": "0,0"})["geolocation:accuracy"] == COUNTRY_ACCURACY_M


def test_the_browser_speaks_the_exit_countrys_language_then_english():
    langs = browserproxy.browser_languages
    assert langs(EXIT) == ["lt-LT", "lt", "en-US", "en"]  # the official language, not the most drawn (ru)
    assert langs({"country": "JP"}) == ["ja-JP", "ja", "en-US", "en"]
    assert langs({"country": "BR"}) == ["pt-BR", "pt", "en-US", "en"]
    assert langs({"country": "GB"}) == ["en-GB", "en"] and langs({"country": "US"}) == ["en-US", "en"]
    assert langs({"country": "IN"}) == ["en-IN", "en"]  # English is official and widely used there
    assert langs({"country": "CH", "region": "Zurich"})[0] == "de-CH"
    assert langs({"country": "CH", "region": "Geneva"})[0] == "fr-CH"
    assert langs({"country": "CH", "region": "Ticino"})[0] == "it-CH"
    assert langs({"country": "BE", "region": "Flanders"})[0] == "nl-BE"
    assert langs({"country": "BE", "region": "Wallonia"})[0] == "fr-BE"
    assert langs({"country": "CA", "region": "Quebec"})[0] == "fr-CA" and langs({"country": "CA"})[0] == "en-CA"
    assert langs({"country": "TW"}) == ["zh-TW", "zh", "en-US", "en"]
    assert langs({}) is None and langs({"country": "ZZ"}) is None  # the lane's own locale stays
    # a fixed language: the owner's, whatever the exit
    assert langs(EXIT, "en-US") == ["en-US", "en"]
    assert langs(EXIT, "de-DE, de, en") == ["de-DE", "de", "en"]


def test_the_exit_options_carry_the_locale_and_the_header_firefox_sends():
    got = browserproxy.exit_options(EXIT)
    assert got["locale"] == ["lt-LT", "lt", "en-US", "en"]
    config = got["config"]
    assert config["headers.Accept-Language"] == "lt-LT,lt;q=0.8,en-US;q=0.5,en;q=0.3"
    assert {k: config[k] for k in config if k.startswith("locale:")} == {
        "locale:language": "lt", "locale:region": "LT", "locale:script": "Latn", "locale:all": "lt-LT, lt, en-US, en"}
    assert browserproxy.exit_options(EXIT, "en-US")["config"]["headers.Accept-Language"] == "en-US,en;q=0.5"
    assert set(config) <= set(browserproxy.EXIT_CONFIG_KEYS)  # what a pinned fingerprint must not hold
    assert set(got) - {"config"} <= set(browserproxy.EXIT_OPTION_KEYS)
    assert browserproxy.exit_options({"ip": "203.0.113.7"}) == {"config": {"webrtc:ipv4": "203.0.113.7"}}
    from openajar.environments.exitplace import accept_language

    assert accept_language(["de", "en-US", "en"]) == "de,en-US;q=0.7,en;q=0.3"  # Firefox's German default


def test_the_locale_keys_are_the_ones_camoufox_makes_for_every_country():
    locales = pytest.importorskip("camoufox.locales")  # the node has none; the machine's Camoufox does this
    from openajar.environments.exitplace import exit_languages, locale_config, places

    for cc in places()["languages"]:
        for region in ("", "Quebec", "Geneva", "Ticino", "Wallonia"):
            tags = exit_languages({"country": cc, "region": region})
            made: dict = {}
            locales.handle_locales(tags, made)  # raises for a tag Camoufox refuses: the launch would fail
            assert made == locale_config(tags), (cc, region)


def test_browser_language_is_exit_or_tags_with_a_country_first():
    assert EnvSettings().browser_language == "exit"
    assert EnvSettings(browser_language=" de-DE,de , en").browser_language == "de-DE, de, en"
    assert EnvSettings(browser_language="es-419, es").browser_language == "es-419, es"
    for bad in ("en", "english", "EN-us", "", "zh-Hant-TW", "en-US; rm"):
        with pytest.raises(ValueError):
            EnvSettings(browser_language=bad)


def _seen(**over):
    seen = {"timezone": "Europe/Vilnius", "offset_minutes": _offset("Europe/Vilnius"),
            "languages": ["lt-LT", "lt", "en-US", "en"], "accept_language": "lt-LT,lt;q=0.8,en-US;q=0.5,en;q=0.3",
            "intl_locale": "lt-LT", "latitude": 54.69, "longitude": 25.28,
            "ips": ["3e1c0b2a-1111-2222-3333-444455556666.local", "192.168.1.5", "203.0.113.7"]}
    seen.update(over)
    return seen


def _offset(tz: str) -> int:
    import datetime
    import zoneinfo

    now = datetime.datetime.now(zoneinfo.ZoneInfo(tz)).utcoffset()
    return -int(now.total_seconds() // 60)


def test_the_self_check_names_each_way_a_page_disagrees_with_the_exit():
    check = browserproxy.exit_mismatches
    assert check(_seen(), EXIT) == []
    assert check(_seen(languages=["en-US", "en"], accept_language="en-US,en;q=0.5", intl_locale="en-US"),
                 EXIT, "en-US") == []
    got = check(_seen(timezone="Europe/Berlin", offset_minutes=_offset("Europe/Vilnius") + 60), EXIT)
    assert any("timezone is Europe/Berlin" in w for w in got) and any("minutes off UTC" in w for w in got)
    got = check(_seen(languages=["en-US", "en"], accept_language="en-US,en;q=0.9", intl_locale="en-US"), EXIT)
    assert any("languages are en-US, en" in w for w in got) and any("Intl's locale is en-US" in w for w in got)
    assert any("not the one Firefox sends" in w  # Chrome's q values under a Firefox user agent
               for w in check(_seen(accept_language="lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7"), EXIT))
    assert any("does not list the page's languages" in w
               for w in check(_seen(accept_language="en-US,en;q=0.5"), EXIT))  # a header and JS that disagree
    assert any("km from the exit" in w for w in check(_seen(latitude=50.11, longitude=8.68), EXIT))
    assert check(_seen(ips=["95.217.0.10"]), EXIT) == ["WebRTC shows 95.217.0.10, not the exit 203.0.113.7"]
    assert all("—" not in w for w in got)


class _ProbeHost:
    def __init__(self, info=None, geo=None):
        self.info, self.geo, self.asked = info, geo, []

    def proxy_exit_info(self, proxy, *, timeout):
        self.asked.append("ipinfo")
        if isinstance(self.info, Exception):
            raise self.info
        return dict(self.info)

    def proxy_exit_geo(self, proxy, *, timeout):
        self.asked.append("geojs")
        if isinstance(self.geo, Exception):
            raise self.geo
        return dict(self.geo)


def _probe_with(monkeypatch, host):
    from openajar.core import system

    monkeypatch.setattr(system, "Host", lambda: host)
    monkeypatch.setattr(browserproxy, "_MORE", {})
    return browserproxy._probe(ProxyConfig(label="lt1", host="lt1.proxy.example", port=9000), 1.0)


def test_a_thin_ipinfo_answer_is_filled_by_the_second_source_through_the_proxy(monkeypatch):
    geo = {"ip": "203.0.113.7", "country": "LT", "city": "Vilnius", "timezone": "Europe/Vilnius", "loc": "54.6,25.2"}
    host = _ProbeHost(info=EXIT, geo=geo)
    assert _probe_with(monkeypatch, host) == EXIT and host.asked == ["ipinfo"]  # a full answer: one request
    host = _ProbeHost(info={"ip": "203.0.113.7", "country": "LT", "timezone": "Bad/Zone"}, geo=geo)
    got = _probe_with(monkeypatch, host)
    assert got["timezone"] == "Europe/Vilnius" and got["loc"] == "54.6,25.2" and host.asked == ["ipinfo", "geojs"]
    host.asked.clear()
    assert browserproxy._probe(ProxyConfig(label="lt1", host="x", port=1), 1.0)["loc"] == "54.6,25.2"
    assert host.asked == ["ipinfo"]  # the second answer is kept for that exit IP
    # ipinfo answered with an error, though the proxy carried it: the second source alone
    host = _ProbeHost(info=OSError("HTTP 429 from ipinfo.io"), geo=geo)
    assert _probe_with(monkeypatch, host)["ip"] == "203.0.113.7"
    # another exit answered the second time (a rotating proxy): not this exit's place
    host = _ProbeHost(info={"ip": "203.0.113.7", "country": "LT"}, geo={**geo, "ip": "198.51.100.4"})
    assert _probe_with(monkeypatch, host) == {"ip": "203.0.113.7", "country": "LT"}
    # the proxy itself failed: no second request, the launch fails closed
    host = _ProbeHost(info=OSError("the proxy rejected its credentials (HTTP 407)"), geo=geo)
    with pytest.raises(OSError, match="407"):
        _probe_with(monkeypatch, host)
    assert host.asked == ["ipinfo"]
    host = _ProbeHost(info=OSError("HTTP 503 from ipinfo.io"), geo=OSError("timed out"))
    with pytest.raises(OSError, match="503"):
        _probe_with(monkeypatch, host)


def test_the_second_source_answer_is_read_in_ipinfos_keys(monkeypatch):
    from openajar.core import system

    body = json.dumps({"ip": "203.0.113.7", "country_code": "LT", "city": "Vilnius", "region": "Vilnius",
                       "timezone": "Europe/Vilnius", "latitude": "54.6892", "longitude": "25.2798"})
    monkeypatch.setattr(system.Host, "_through_proxy", lambda self, proxy, url, **kw: body)
    got = system.Host().proxy_exit_geo(ProxyConfig(label="lt1", host="x", port=1), timeout=1)
    assert got == {"ip": "203.0.113.7", "country": "LT", "city": "Vilnius", "region": "Vilnius",
                   "timezone": "Europe/Vilnius", "loc": "54.6892,25.2798"}


async def test_a_fixed_browser_language_in_environments_toml_keeps_the_place_of_the_exit(proxied, tmp_path):
    from test_environments import opened

    from openajar.environments.models import update_settings

    mgr = proxied["mgr"]
    update_settings(mgr.home, {"browser": "vm", "desktop": False, "browser_proxies": ["lt1", "lt2"],
                               "browser_language": "en-US"})
    env = mgr.get((await opened(proxied))["id"])
    session = await _launch(proxied, env, tmp_path)
    try:
        [browser] = [a for a in proxied["asked"] if a["action"] == "browser"]
        opts = browser["options"]
        assert opts["locale"] == ["en-US", "en"] and opts["config"]["timezone"] == "Europe/Vilnius"
        assert opts["config"]["headers.Accept-Language"] == "en-US,en;q=0.5"
    finally:
        await session.close()


def test_proxy_test_says_what_the_browser_claims_there(home, monkeypatch):
    from openajar.core import system

    _proxies(home, "lt1")
    monkeypatch.setattr(system.Host, "proxy_exit_info", lambda self, proxy, *, timeout: dict(EXIT))
    out = _cli("proxy", "test", "lt1")
    assert out.exit_code == 0, out.output
    assert "browser: Europe/Vilnius, lt-LT, lt, en-US, en (exit near 54.69,25.28)" in out.output
    assert "—" not in out.output


def test_every_key_the_exit_sets_follows_each_launch_and_is_never_pinned():
    from openajar.environments import vmpin

    for key in browserproxy.EXIT_CONFIG_KEYS:
        assert vmpin.per_launch(key), key
    for key in browserproxy.EXIT_PREFS:
        assert key in vmpin.LAUNCH_PREFS, key
    for info in (EXIT, {"country": "CH", "region": "Geneva"}, {"ip": "2001:db8::1", "country": "JP"}):
        for language in ("exit", "en-US"):
            got = browserproxy.exit_options(info, language)
            assert all(vmpin.per_launch(k) for k in got["config"]), got
            assert set(got) - {"config"} <= set(browserproxy.EXIT_OPTION_KEYS)
