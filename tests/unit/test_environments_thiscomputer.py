"""An environment's browser out through this computer's own connection (openajar.environments.thiscomputer,
the reserved label `this-computer` of browser_proxies).

The security heart first: the direct forwarder connects from this computer, so it must never let the machine
reach this computer's own loopback services, the owner's home network, the router, or anything else that is
not the public internet, whatever way a target is written (a name, a literal, an IPv4-mapped IPv6, a name that
resolves to a mix, a name that answers differently the second time). No real network: the forwarder's lookup
and dial are replaced where a test needs a public address, and a real server on this computer's loopback stands
for the services that must stay out of reach.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest
from test_environments import node  # noqa: F401 - the fixture
from test_environments_browserproxy import EXIT, _cli, _launch, _local, _proxies, home, proxied  # noqa: F401
from test_environments_vmbrowser import vm_node  # noqa: F401 - the fixture

from openajar.core.errors import EngineError
from openajar.core.models import utcnow
from openajar.environments import browserproxy, thiscomputer
from openajar.environments.browserproxy import OpenPolicy, pick
from openajar.environments.envproxy import Blocked, EnvPolicy
from openajar.environments.models import Environment, EnvSettings, load_settings
from openajar.environments.thiscomputer import THIS_COMPUTER, DirectForwarder, refused_address

PUBLIC = "93.184.215.14"        # stands for a site: a global address no test connects to for real
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
HOME_EXIT = {"ip": "84.15.180.20", "country": "LT", "city": "Vilnius", "timezone": "Europe/Vilnius",
             "loc": "54.6892,25.2798"}


# ------------------------------------------------------------------ what is never reached from this computer


REFUSED = [
    "127.0.0.1", "127.8.9.10", "0.0.0.0", "10.0.0.1", "172.16.5.4", "192.168.1.1", "192.168.0.254",
    "169.254.169.254", "100.64.0.1", "100.127.255.254",  # link-local (a cloud's metadata), CGNAT
    "192.0.0.170", "198.18.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "240.0.0.1", "255.255.255.255",
    "224.0.0.1", "239.255.255.250", "224.0.1.1",  # multicast (224.0.1.1 counts as global in Python)
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "ff0e::1",
    "::ffff:127.0.0.1", "::ffff:192.168.1.1", "::ffff:10.0.0.1", "::ffff:0.0.0.0",  # IPv4-mapped
    "::127.0.0.1", "::192.168.1.1",  # IPv4-compatible (deprecated, still parsed)
    "64:ff9b::c0a8:101", "64:ff9b::7f00:1",  # NAT64 of a private or loopback address
    "2002:c0a8:101::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # 6to4 of 192.168.1.1, Teredo
    "2001:db8::1", "100::1",
]


@pytest.mark.parametrize("addr", REFUSED)
def test_no_private_local_or_special_address_is_ever_reached_from_this_computer(addr):
    assert refused_address(addr), addr


@pytest.mark.parametrize("addr", [PUBLIC, "8.8.8.8", "1.1.1.1", PUBLIC6, "2a00:1450:4001:80b::200e",
                                  "::ffff:8.8.8.8", "64:ff9b::808:808"])
def test_a_public_address_is_reached(addr):
    assert refused_address(addr) is None, addr


def test_this_computers_own_public_addresses_and_their_home_prefix_are_refused(monkeypatch):
    """A home network has no NAT for IPv6: the printer and this computer itself have global addresses in the
    household's prefix, and connecting to the household's own public IPv4 reaches the router from inside."""
    own = {socket.AF_INET: "192.168.1.23", socket.AF_INET6: "2a02:2378:1100:aa00:1c2e:5f7a:9b0c:d1e2"}
    monkeypatch.setattr(thiscomputer, "_source_address", lambda family: own.get(family))
    monkeypatch.setattr(thiscomputer, "_OWN", {})
    nets = thiscomputer.own_networks({HOME_EXIT["ip"]})
    assert refused_address(HOME_EXIT["ip"], nets)  # the router, from inside
    assert refused_address("2a02:2378:1100:aa00:1c2e:5f7a:9b0c:d1e2", nets)  # this computer
    assert refused_address("2a02:2378:1100:aa00::1", nets)  # the printer, the router: same /64
    assert refused_address("2a02:2378:1100:aa42::7", nets)  # another subnet of the household's /56
    assert refused_address(PUBLIC6, nets) is None and refused_address(PUBLIC, nets) is None
    # a computer with a public IPv4 on its own interface: its neighbours there are refused too
    own[socket.AF_INET] = "84.15.181.9"
    monkeypatch.setattr(thiscomputer, "_OWN", {})
    nets = thiscomputer.own_networks(set())
    assert refused_address("84.15.181.1", nets) and refused_address("84.15.181.9", nets)
    assert refused_address("84.15.182.1", nets) is None


# ------------------------------------------------------------------ the direct forwarder, over a real socket


class Recorder:
    """The forwarder's lookup and dial, faked: `names` answers a lookup (by family), `routes` sends a dial of
    a public address to a server on this computer's loopback. Every lookup and dial is recorded."""

    def __init__(self, names: dict | None = None, routes: dict | None = None):
        self.names = names or {}
        self.routes = routes or {}
        self.lookups: list[tuple[str, int]] = []
        self.dials: list[str] = []

    def install(self, fwd: DirectForwarder) -> None:
        async def lookup(host, port, family):
            self.lookups.append((host, family))
            got = self.names.get(host)
            if callable(got):
                got = got()
            return [a for a in got or [] if (":" in a) == (family == socket.AF_INET6)]

        async def dial(addr, port):
            self.dials.append(addr)
            where = self.routes.get(addr)
            if where is None:
                raise ConnectionRefusedError(f"no route to {addr} in this test")
            return await asyncio.open_connection("127.0.0.1", where)

        fwd._lookup = lookup
        fwd._dial = dial


class LoopbackService:
    """A service on this computer's loopback (the openajar web app, a database): it must never see the machine."""

    def __init__(self):
        self.heads: list[str] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self):
        async def serve(r, w):
            try:
                self.heads.append((await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)).decode("latin-1"))
                body = b"from this computer"
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
                await w.drain()
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                self.heads.append("")
            finally:
                w.close()

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def close(self):
        self.server.close()


async def _direct(policy=None, *, names=None, routes=None, own=None):
    fwd = DirectForwarder("e1", policy or OpenPolicy(), Blocked(clock=utcnow), max_conns=16)
    rec = Recorder(names, routes)
    rec.install(fwd)
    fwd._own = lambda: [ipaddress.ip_network(n) for n in (own or [])]
    server = await asyncio.start_server(fwd._client, "127.0.0.1", 0)
    return fwd, rec, server, server.sockets[0].getsockname()[1]


async def _ask(port: int, request: str) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(request.encode())
    await w.drain()
    try:
        return await asyncio.wait_for(r.read(4096), 5)
    finally:
        w.close()


def _connect(target: str) -> str:
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n"


def _get(url: str) -> str:
    return f"GET {url} HTTP/1.1\r\nHost: {url.split('/')[2]}\r\nProxy-Authorization: Basic eDp5\r\n\r\n"


async def test_this_computers_loopback_is_refused_however_it_is_written():
    svc = await LoopbackService().start()
    p = svc.port
    targets = [f"127.0.0.1:{p}", f"[::1]:{p}", f"localhost:{p}", f"0.0.0.0:{p}", f"[::ffff:127.0.0.1]:{p}",
               f"[::]:{p}", f"127.1.2.3:{p}", f"app.localhost:{p}", f"[::127.0.0.1]:{p}"]
    for policy in (OpenPolicy(), EnvPolicy([f"127.0.0.1:{p}", f"localhost:{p}", f"0.0.0.0:{p}", f"::1:{p}"])):
        _fwd, rec, server, port = await _direct(policy)
        try:
            for target in targets:
                head = await _ask(port, _connect(target))
                assert head.startswith(b"HTTP/1.1 403"), (target, head)
                url = f"http://{target}/admin"
                head = await _ask(port, _get(url))
                assert head.startswith(b"HTTP/1.1 403"), (url, head)
            assert rec.dials == [] and svc.heads == []  # nothing ever connected
        finally:
            server.close()
    svc.close()


async def test_the_home_network_and_the_router_are_refused_on_a_full_and_a_closed_network():
    targets = ["192.168.1.1:80", "192.168.0.1:443", "10.0.0.1:80", "172.16.0.1:8080", "169.254.169.254:80",
               "100.64.0.1:443", "[fe80::1]:80", "[fd00::1]:443", "[::ffff:192.168.1.1]:80", "[64:ff9b::c0a8:101]:80",
               "printer.local:631", "nas.local:443", "router.home.arpa:80", "db.internal:5432", "fritz.box.local:80"]
    # a closed network whose list names private addresses: they stay refused from this computer
    allow = ["192.168.1.1", "10.0.0.1", "172.16.0.1:8080", "169.254.169.254", "printer.local:631", "nas.local",
             "db.internal:5432", "100.64.0.1"]
    for policy in (OpenPolicy(), EnvPolicy(allow)):
        _fwd, rec, server, port = await _direct(policy, names={"printer.local": ["192.168.1.50"],
                                                               "nas.local": ["192.168.1.60"]})
        try:
            for target in targets:
                assert (await _ask(port, _connect(target))).startswith(b"HTTP/1.1 403"), target
                assert (await _ask(port, _get(f"http://{target}/"))).startswith(b"HTTP/1.1 403"), target
            assert rec.dials == []
            assert not any(h in ("printer.local", "nas.local") for h, _ in rec.lookups)  # refused by name
        finally:
            server.close()


async def test_a_name_that_resolves_to_a_private_address_is_refused_and_a_mix_keeps_only_the_public_one():
    svc = await LoopbackService().start()
    names = {"rebind.example": ["127.0.0.1"], "home.example": ["192.168.1.1", "10.0.0.1"],
             "mixed.example": ["127.0.0.1", "192.168.1.1", PUBLIC], "mapped.example": ["::ffff:192.168.1.1"],
             "cgnat.example": ["100.64.1.1"], "zero.example": ["0.0.0.0"]}
    _fwd, rec, server, port = await _direct(names=names, routes={PUBLIC: svc.port})
    try:
        for host in ("rebind.example", "home.example", "mapped.example", "cgnat.example", "zero.example"):
            head = await _ask(port, _connect(f"{host}:443"))
            assert head.startswith(b"HTTP/1.1 403") and b"private or local" in head, (host, head)
            head = await _ask(port, _get(f"http://{host}:{svc.port}/"))
            assert head.startswith(b"HTTP/1.1 403"), host
        assert rec.dials == [] and svc.heads == []
        head = await _ask(port, _get("http://mixed.example/x"))
        assert head.startswith(b"HTTP/1.1 200") and rec.dials == [PUBLIC]  # the public one only
    finally:
        server.close()
        svc.close()


async def test_a_name_is_looked_up_once_and_the_address_checked_is_the_one_connected():
    """DNS rebinding: a name that answers a public address and then a private one gets one lookup per
    connection, and the connection goes to the address that was checked, never to a second answer."""
    svc = await LoopbackService().start()
    answers = iter([[PUBLIC], ["127.0.0.1"], [PUBLIC], ["127.0.0.1"], ["192.168.1.1"]])
    _fwd, rec, server, port = await _direct(names={"flip.example": lambda: next(answers)}, routes={PUBLIC: svc.port})
    try:
        assert (await _ask(port, _get("http://flip.example/1"))).startswith(b"HTTP/1.1 200")
        assert (await _ask(port, _get("http://flip.example/2"))).startswith(b"HTTP/1.1 403")
        assert (await _ask(port, _connect("flip.example:80"))).startswith(b"HTTP/1.1 200")
        assert (await _ask(port, _connect("flip.example:80"))).startswith(b"HTTP/1.1 403")
        assert rec.dials == [PUBLIC, PUBLIC]
        assert [h for h, _ in rec.lookups] == ["flip.example"] * 4  # one each (IPv4 found, no IPv6 asked)
    finally:
        server.close()
        svc.close()


async def test_the_exit_address_and_the_households_prefix_are_refused():
    own = ["2a02:2378:1100:aa00::/56", f"{HOME_EXIT['ip']}/32"]
    names = {"me.example": ["2a02:2378:1100:aa00::1"], "router.example": [HOME_EXIT["ip"]]}
    _fwd, rec, server, port = await _direct(names=names, own=own)
    try:
        for target in (f"{HOME_EXIT['ip']}:80", "[2a02:2378:1100:aa00::1]:22", "[2a02:2378:1100:aa17::5]:443",
                       "me.example:443", "router.example:80"):
            assert (await _ask(port, _connect(target))).startswith(b"HTTP/1.1 403"), target
        assert rec.dials == []
    finally:
        server.close()


async def test_mail_is_never_sent_from_the_households_address():
    _fwd, rec, server, port = await _direct(names={"mx.example": [PUBLIC]})
    try:
        head = await _ask(port, _connect("mx.example:25"))
        assert head.startswith(b"HTTP/1.1 403") and b"25" in head and rec.dials == []
    finally:
        server.close()


async def test_plain_http_goes_straight_to_the_site_in_origin_form_with_no_proxy_headers():
    svc = await LoopbackService().start()
    _fwd, _rec, server, port = await _direct(names={"site.example": [PUBLIC]}, routes={PUBLIC: svc.port})
    try:
        body = await _ask(port, _get("http://site.example/a?b=1"))
        assert body.startswith(b"HTTP/1.1 200") and body.endswith(b"from this computer")
        [head] = svc.heads
        assert head.startswith("GET /a?b=1 HTTP/1.1\r\n") and "Host: site.example" in head
        assert "proxy-authorization" not in head.lower()
        head = await _ask(port, _connect("site.example:8443"))  # any public port, as with a proxy
        assert head.startswith(b"HTTP/1.1 200")
    finally:
        server.close()
        svc.close()


# ------------------------------------------------------------------ one address: the probe's family


async def test_the_forwarder_connects_over_the_family_the_probe_saw_and_the_other_only_when_a_name_has_none():
    svc = await LoopbackService().start()
    names = {"both.example": [PUBLIC, PUBLIC6], "six.example": [PUBLIC6], "four.example": [PUBLIC]}
    routes = {PUBLIC: svc.port, PUBLIC6: svc.port}
    fwd, rec, server, port = await _direct(names=names, routes=routes)
    try:
        assert fwd.family == socket.AF_INET  # before a probe: IPv4, the address most sites see
        fwd.follow(HOME_EXIT)
        await _ask(port, _connect("both.example:443"))
        await _ask(port, _connect("six.example:443"))
        assert rec.dials == [PUBLIC, PUBLIC6]
        assert rec.lookups == [("both.example", socket.AF_INET), ("six.example", socket.AF_INET),
                               ("six.example", socket.AF_INET6)]
        rec.dials.clear()
        rec.lookups.clear()
        fwd.follow({"ip": "2a02:2378:1100:aa00::9", "country": "LT"})  # an IPv6-only network answered
        await _ask(port, _connect("both.example:443"))
        await _ask(port, _connect("four.example:443"))
        assert rec.dials == [PUBLIC6, PUBLIC]
        assert rec.lookups[0] == ("both.example", socket.AF_INET6)
    finally:
        server.close()
        svc.close()


async def test_a_name_with_no_address_at_all_is_a_502_not_a_refusal():
    fwd = DirectForwarder("e1", OpenPolicy(), Blocked(clock=utcnow), max_conns=4)

    async def lookup(host, port, family):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    fwd._lookup = lookup
    server = await asyncio.start_server(fwd._client, "127.0.0.1", 0)
    try:
        head = await _ask(server.sockets[0].getsockname()[1], _connect("nothing.example:443"))
        assert head.startswith(b"HTTP/1.1 502")
    finally:
        server.close()


async def test_the_real_lookup_asks_for_one_family():
    fwd = DirectForwarder("e1", OpenPolicy(), Blocked(clock=utcnow))
    assert await fwd._lookup("localhost", 80, socket.AF_INET) == ["127.0.0.1"]
    assert all(":" in a for a in await _or_none(fwd._lookup("localhost", 80, socket.AF_INET6)))


async def _or_none(coro):
    try:
        return await coro
    except OSError:
        return []


def test_the_direct_probe_reuses_the_second_source_and_asks_without_a_proxy(monkeypatch):
    from test_environments_browserproxy import _ProbeHost

    from openajar.core import system

    thin = {"ip": HOME_EXIT["ip"], "country": "LT"}
    host = _ProbeHost(info=thin, geo=HOME_EXIT)
    asked = []
    real_info, real_geo = host.proxy_exit_info, host.proxy_exit_geo
    host.proxy_exit_info = lambda proxy, *, timeout: asked.append(proxy) or real_info(proxy, timeout=timeout)
    host.proxy_exit_geo = lambda proxy, *, timeout: asked.append(proxy) or real_geo(proxy, timeout=timeout)
    monkeypatch.setattr(system, "Host", lambda: host)
    monkeypatch.setattr(browserproxy, "_MORE", {})
    got = browserproxy._probe(None, 1.0)
    assert got["timezone"] == "Europe/Vilnius" and host.asked == ["ipinfo", "geojs"] and asked == [None, None]


def test_the_direct_request_prefers_ipv4_and_falls_back(monkeypatch):
    from openajar.core import system

    tried = []

    def fake_gai(host, port, family=0, type=0, *a):
        if family == socket.AF_INET:
            tried.append("v4")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", port))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::10", port, 0, 0))]

    made = []

    def fake_conn(address, timeout=None, source_address=None, **kw):
        made.append(address)
        if address[0] == "192.0.2.10" and "v4-down" in tried:
            raise OSError("no route")
        return "a socket"

    monkeypatch.setattr(system.socket, "getaddrinfo", fake_gai)
    monkeypatch.setattr(system.socket, "create_connection", fake_conn)
    assert system.ipv4_first(("ipinfo.io", 443), 5) == "a socket" and made == [("192.0.2.10", 443)]
    tried.append("v4-down")
    made.clear()
    system.ipv4_first(("ipinfo.io", 443), 5)
    assert made == [("192.0.2.10", 443), ("ipinfo.io", 443)]  # then the system's own order (IPv6 here)


# ------------------------------------------------------------------ which environments take it


def test_this_computer_needs_no_entry_in_proxies_json_and_is_picked_like_any_label():
    known = browserproxy.known_labels({"lt1": object()})
    assert THIS_COMPUTER in known
    assert pick([THIS_COMPUTER], known, []) == THIS_COMPUTER
    assert pick(["lt1", THIS_COMPUTER], known, ["lt1"]) == THIS_COMPUTER
    assert pick([THIS_COMPUTER, "lt1"], known, [THIS_COMPUTER]) == "lt1"
    assert EnvSettings(browser_proxies=[THIS_COMPUTER, "lt1"]).browser_proxies == [THIS_COMPUTER, "lt1"]


@pytest.fixture
def here(proxied):  # noqa: F811
    mgr = proxied["mgr"]
    mgr.settings = EnvSettings(browser="vm", desktop=False, browser_proxies=[THIS_COMPUTER])
    probed = []

    def probe(proxy, timeout):
        probed.append(proxy)
        return dict(HOME_EXIT)

    mgr.proxy_probe = probe
    proxied["probed"] = probed
    return proxied


async def test_the_browser_exits_through_this_computer_with_a_direct_forwarder_and_claims_its_exit(here, tmp_path):
    from test_environments import opened

    mgr = here["mgr"]
    one = await opened(here, title="one")
    env = mgr.get(one["id"])
    assert env.browser_proxy == THIS_COMPUTER
    net = mgr._nets[env.id]
    assert isinstance(net.browser_forwarder, DirectForwarder) and net.browser_forwarder.upstream is None
    assert net.browser_upstream is None and net.browser_direct
    assert (3132, net.browser_proxy_port) in net.forwards()
    assert net.browser_forwarder._sem._value == browserproxy.BROWSER_MAX_CONNS
    session = await _launch(here, env, tmp_path)
    try:
        [browser] = [a for a in here["asked"] if a["action"] == "browser"]
        opts = browser["options"]
        assert opts["firefox_user_prefs"]["network.proxy.ssl_port"] == 3132
        assert opts["config"]["timezone"] == "Europe/Vilnius" and opts["config"]["webrtc:ipv4"] == HOME_EXIT["ip"]
        assert opts["locale"] == ["lt-LT", "lt", "en-US", "en"]
    finally:
        await session.close()
    assert here["probed"] == [None]  # the direct check, no proxy
    assert net.browser_forwarder.family == socket.AF_INET and HOME_EXIT["ip"] in net.browser_forwarder.exits
    view = mgr.network_view(mgr.get(env.id))
    assert view["browser_proxy"] == {"label": THIS_COMPUTER, "exit_ip": HOME_EXIT["ip"], "exit_country": "LT"}
    assert mgr.browser_exit_ip(mgr.get(env.id)) == HOME_EXIT["ip"]  # what a save keeps


async def test_this_computer_beside_a_proxy_takes_turns_like_any_label(here):
    from test_environments import opened

    mgr = here["mgr"]
    mgr.settings = EnvSettings(browser="vm", desktop=False, browser_proxies=["lt1", THIS_COMPUTER])
    a = mgr.get((await opened(here, title="a"))["id"])
    b = mgr.get((await opened(here, title="b", session="chat-2"))["id"])
    assert (a.browser_proxy, b.browser_proxy) == ("lt1", THIS_COMPUTER)
    assert not mgr._nets[a.id].browser_direct and mgr._nets[b.id].browser_direct


async def test_when_the_direct_check_fails_the_browser_does_not_start_and_the_words_say_what_to_do(here, tmp_path):
    from test_environments import opened

    mgr = here["mgr"]
    env = mgr.get((await opened(here))["id"])

    def offline(proxy, timeout):
        raise OSError("[Errno 51] Network is unreachable")

    mgr.proxy_probe = offline
    with pytest.raises(EngineError) as e:
        await _launch(here, env, tmp_path)
    words = e.value.message.lower()
    assert "this computer" in words and "openajar proxy test this-computer" in words and "unreachable" in words
    assert "proxy add" not in words and "\u2014" not in words
    assert not [a for a in here["asked"] if a["action"] == "browser"]


async def test_turned_off_while_open_the_next_start_of_its_network_fails_closed(here, tmp_path):
    from test_environments import opened

    from openajar.environments.models import update_settings

    mgr = here["mgr"]
    update_settings(mgr.home, {"browser": "vm", "desktop": False, "browser_proxies": [THIS_COMPUTER]})
    env = mgr.get((await opened(here))["id"])
    await mgr.net_down(env.id, forget=False)
    update_settings(mgr.home, {"browser_proxies": []})  # `openajar proxy this-computer --off`, then a restart
    await mgr.net_forwards(env)
    assert mgr._nets[env.id].browser_proxy_port is None
    with pytest.raises(EngineError) as e:
        await _launch(here, env, tmp_path)
    assert "this computer" in e.value.message.lower() and "proxy add" not in e.value.message
    assert not [a for a in here["asked"] if a["action"] == "browser"]


async def test_a_resume_from_another_network_says_the_address_moved(here, tmp_path):
    from openajar.environments.saves import SaveManifest

    mgr = here["mgr"]
    m = SaveManifest(id="s1", family="g", title="t", env_id="e1234567890", saved_at=utcnow(),
                     browser_proxy=THIS_COMPUTER, browser_exit_ip="198.51.100.9")
    mgr.saves = lambda: type("S", (), {"find": staticmethod(lambda sid: (m, tmp_path))})()
    now = utcnow()
    env = Environment(id="e1234567890", title="t", grant_id="g", binding="session:x", created_at=now, expires_at=now,
                      browser="vm", resumed={"save_id": "s1", "missing": [], "browser_proxy": THIS_COMPUTER})
    mgr.assign_browser_proxy(env)
    assert env.browser_proxy == THIS_COMPUTER and "browser_proxy_note" not in env.resumed
    env.restore_from = "s1"
    mgr.store.put(env)
    mgr._nets[env.id] = type("N", (), {"browser_upstream": None, "browser_direct": True, "browser_exit": None,
                                       "browser_forwarder": None})()
    await mgr.browser_proxy_options(mgr.get(env.id))
    [said] = mgr.get(env.id).resumed["missing"]
    assert "198.51.100.9" in said and HOME_EXIT["ip"] in said and "this computer" in said
    assert "another network" in said and "proxy" not in said
    del mgr._nets[env.id]


def test_a_resume_whose_this_computer_was_turned_off_says_so_plainly(here):
    mgr = here["mgr"]
    _proxies(mgr.home, "lt1")
    mgr.settings = EnvSettings(browser="vm", desktop=False, browser_proxies=["lt1"])
    now = utcnow()
    env = Environment(id="e1234567890", title="t", grant_id="g", binding="session:x", created_at=now, expires_at=now,
                      browser="vm", resumed={"save_id": "s1", "missing": [], "browser_proxy": THIS_COMPUTER})
    mgr.assign_browser_proxy(env)
    note = env.resumed["browser_proxy_note"]
    assert env.browser_proxy == "lt1" and "this computer's own connection" in note and "the proxy lt1" in note


def test_the_words_for_this_computer_name_no_proxy_to_add():
    for words in (browserproxy.gone_words(THIS_COMPUTER), browserproxy.failed_words(THIS_COMPUTER, "x")):
        assert "this computer" in words.lower() and "proxy add" not in words and "\u2014" not in words
        assert "openajar proxy" in words


# ------------------------------------------------------------------ openajar proxy this-computer


def _probe_direct(monkeypatch, info=None, error=None):
    from openajar.core import system

    asked = []

    def exit_info(self, proxy, *, timeout):
        asked.append(proxy)
        if error is not None:
            raise OSError(error)
        return dict(info or HOME_EXIT)

    monkeypatch.setattr(system.Host, "proxy_exit_info", exit_info)
    monkeypatch.setattr(system.Host, "proxy_exit_geo",  # the second source agrees (not counted in `asked`)
                        lambda self, proxy, *, timeout: dict(info or HOME_EXIT))
    return asked


def test_proxy_this_computer_turns_it_on_says_the_exit_and_the_costs_and_off_again(home, monkeypatch):  # noqa: F811
    _local(home)
    asked = _probe_direct(monkeypatch)
    out = _cli("proxy", "this-computer")
    assert out.exit_code == 0, out.output
    assert load_settings(home).browser_proxies == [THIS_COMPUTER] and asked == [None]
    assert f"exits from {HOME_EXIT['ip']}" in out.output and "Vilnius, LT" in out.output
    assert "upload" in out.output and "on and online" in out.output and "your household" in out.output
    assert "datacenter" not in out.output and "\u2014" not in out.output
    assert len(out.output.splitlines()) <= 6
    _proxies(home, "lt1")
    assert _cli("proxy", "this-computer").exit_code == 0 and load_settings(home).browser_proxies == [THIS_COMPUTER]
    listed = _cli("proxy", "list").output
    assert any(ln.startswith(THIS_COMPUTER) and ln.endswith("environments") for ln in listed.splitlines()), listed
    assert "not here" not in listed
    out = _cli("proxy", "this-computer", "--off")
    assert out.exit_code == 0 and load_settings(home).browser_proxies == [] and "no longer" in out.output
    out = _cli("proxy", "this-computer", "--off")
    assert out.exit_code == 0 and "was not" in out.output
    assert _cli("proxy", "this-computer").exit_code == 0
    out = _cli("proxy", "remove", THIS_COMPUTER)
    assert out.exit_code == 0 and load_settings(home).browser_proxies == [], out.output
    assert "lt1" in load_proxies_labels(home)


def load_proxies_labels(home) -> set[str]:  # noqa: F811
    from openajar.core.config import load_config, load_proxies

    return set(load_proxies(load_config(home)))


def test_proxy_this_computer_on_a_server_says_its_address_is_a_datacenters(home, monkeypatch):  # noqa: F811
    _probe_direct(monkeypatch)
    out = _cli("proxy", "this-computer")
    assert out.exit_code == 0 and "datacenter" in out.output and "gains nothing" in out.output
    assert load_settings(home).browser_proxies == [THIS_COMPUTER]


def test_proxy_this_computer_turns_on_even_when_the_check_fails_and_says_so(home, monkeypatch):  # noqa: F811
    _local(home)
    _probe_direct(monkeypatch, error="timed out")
    out = _cli("proxy", "this-computer")
    assert out.exit_code == 0 and "timed out" in out.output and "does not start" in out.output
    assert load_settings(home).browser_proxies == [THIS_COMPUTER]


def test_this_computer_is_a_reserved_label(home):  # noqa: F811
    out = _cli("proxy", "add", THIS_COMPUTER, "5.6.7.8:8000:u:p")
    assert out.exit_code != 0 and "reserved" in out.output and "openajar proxy this-computer" in out.output
    assert THIS_COMPUTER not in load_proxies_labels(home)
    from openajar.commands import proxy

    assert proxy.THIS_COMPUTER == THIS_COMPUTER  # the CLI's copy (it does not import the forwarder at start)


def test_proxy_test_this_computer_checks_the_direct_exit(home, monkeypatch):  # noqa: F811
    _local(home)
    asked = _probe_direct(monkeypatch)
    out = _cli("proxy", "test", THIS_COMPUTER)
    assert out.exit_code == 0, out.output
    assert f"{THIS_COMPUTER}  exit {HOME_EXIT['ip']}  Vilnius, LT  Europe/Vilnius" in out.output
    assert "browser: Europe/Vilnius, lt-LT" in out.output and asked == [None]
    _cli("proxy", "this-computer")
    asked.clear()
    assert _cli("proxy", "test").exit_code == 0 and asked == [None]  # every exit: this computer is one
    _probe_direct(monkeypatch, error="Network is unreachable")
    out = _cli("proxy", "test", THIS_COMPUTER)
    assert out.exit_code == 1 and "failed: Network is unreachable" in out.output
