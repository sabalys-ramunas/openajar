"""An environment's browser out through this computer's own connection, for real
(openajar.environments.thiscomputer): the docker provider's container with its Camoufox, the node's direct
forwarder, and the SSH -R to the machine's 127.0.0.1:3132.

Skipped as test_environments_vmbrowser_docker is. No internet: the forwarder's lookup answers a made-up name
(home-site.test) with a public address, and its dial takes that address to a server in this process, so a page
with its words came from this computer. home-site.test is also in the machine's /etc/hosts, at a server on the
machine itself that answers other words: a browser that went out by itself would show them. Private addresses
and a name that resolves to one are refused before any dial.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
from test_environments_browserproxy_docker import _load, _open_env, _sh
from test_environments_vmbrowser_docker import _ready, containers, docker_node

from openajar.environments import thiscomputer
from openajar.environments.models import EnvSettings
from openajar.environments.thiscomputer import THIS_COMPUTER, DirectForwarder

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker with the environment image built with its browser")

HOME_EXIT = {"ip": "84.15.180.20", "country": "LT", "city": "Vilnius", "timezone": "Europe/Vilnius",
             "loc": "54.6892,25.2798"}
PUBLIC = "93.184.215.14"   # what home-site.test "resolves" to; the dial takes it to the server here
HOUSEHOLD_V6 = "2a02:2378:1100:aa00::"
FROM_HERE = "arrived at this computer"
DIRECT = "loaded straight from the machine"


class HereServer:
    """A site, served by this process: what reaches it came out of the node's forwarder."""

    def __init__(self) -> None:
        self.heads: list[str] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        async def serve(r, w):
            try:
                self.heads.append((await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)).decode("latin-1"))
                body = f"<title>Here</title><h1>{FROM_HERE}</h1>".encode()
                w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                        % len(body) + body)
                await w.drain()
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                pass
            finally:
                w.close()

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 3)


@pytest.fixture
async def here(tmp_path, monkeypatch):
    site = HereServer()
    await site.start()
    dials: list[str] = []
    names = {"home-site.test": [PUBLIC], "sneaky.test": ["127.0.0.1"], "lan.test": ["192.168.1.1", "10.0.0.1"]}

    async def lookup(self, host, port, family):
        got = [a for a in names.get(host, []) if (":" in a) == (family == socket.AF_INET6)]
        if not got:
            raise socket.gaierror(socket.EAI_NONAME, f"{host}: no address in this test")
        return got

    async def dial(self, addr, port):
        dials.append(addr)
        if addr != PUBLIC:
            raise ConnectionRefusedError(f"{addr} is not in this test")
        return await asyncio.open_connection("127.0.0.1", site.port)

    monkeypatch.setattr(DirectForwarder, "_lookup", lookup)
    monkeypatch.setattr(DirectForwarder, "_dial", dial)
    own = {socket.AF_INET: "192.168.1.23", socket.AF_INET6: HOUSEHOLD_V6 + "5"}  # this computer at home
    monkeypatch.setattr(thiscomputer, "_source_address", lambda family: own.get(family))
    monkeypatch.setattr(thiscomputer, "_OWN", {})
    probed: list = []

    def probe(proxy, timeout):
        probed.append(proxy)
        return dict(HOME_EXIT)

    settings = EnvSettings(browser="vm", browser_proxies=[THIS_COMPUTER])
    async with docker_node(tmp_path, settings) as got:
        got["mgr"].proxy_probe = probe
        got.update(site=site, dials=dials, probed=probed)
        yield got
    await site.stop()


async def test_the_machines_browser_exits_through_this_computer_and_never_reaches_its_network(here):
    node, site, dials = here, here["site"], here["dials"]
    mgr, lanes = node["mgr"], node["lanes"]
    env = await _open_env(node)
    assert env.browser_proxy == THIS_COMPUTER
    # the machine itself would answer home-site.test with other words, from its own network address
    await node["envs"].job_start(env.terminal_lane, f"mkdir -p /srv/d && printf '<h1>{DIRECT}</h1>' > "
                                 "/srv/d/index.html && cd /srv/d && exec python3 -m http.server 80 --bind 0.0.0.0")
    ip = (await _sh(node, env, "hostname -I | awk '{print $1}'")).strip()
    line = f"{ip} home-site.test"
    await _sh(node, env, f"(echo '{line}' >> /etc/hosts) 2>/dev/null || echo '{line}' | sudo -n tee -a /etc/hosts")
    await asyncio.sleep(1.0)
    assert DIRECT in await _sh(node, env, "curl -s --noproxy '*' --retry 10 --retry-connrefused --retry-delay 1 "
                                          "http://home-site.test/")

    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    assert node["probed"] == [None]  # the launch's check went out straight from here
    assert mgr.network_view(mgr.get(env.id))["browser_proxy"] == {"label": THIS_COMPUTER, "exit_ip": HOME_EXIT["ip"],
                                                                   "exit_country": "LT"}

    text = await _load(page, "http://home-site.test/hello?x=1")
    assert FROM_HERE in text and DIRECT not in text, text
    assert set(dials) == {PUBLIC}  # the page and its icon
    # it arrived from the node, in origin form, with no proxy headers
    head = next(h for h in site.heads if h.startswith("GET /hello?x=1 "))
    assert "Host: home-site.test" in head and "proxy-" not in head.lower()
    tz = await page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert tz == HOME_EXIT["timezone"]

    # what must never be reached from this computer: nothing is dialled, and the machine's own server stays out
    seen = len(site.heads)
    for url in ("http://192.168.1.1/", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                "http://sneaky.test/", "http://lan.test/", "http://[::ffff:192.168.1.1]/", "http://100.64.0.1/",
                "http://printer.local/", f"http://{HOME_EXIT['ip']}/", f"http://[{HOUSEHOLD_V6}1]/"):
        text = await _load(page, url)
        assert FROM_HERE not in text and DIRECT not in text, (url, text)
    assert set(dials) == {PUBLIC} and len(site.heads) == seen
    blocked = {row["host"] for row in mgr.network_view(mgr.get(env.id))["blocked"]}
    assert {"192.168.1.1", "10.0.0.1", "sneaky.test", "lan.test"} <= blocked, blocked
    # this computer's loopback: the page's 127.0.0.1 is the machine's own, never the node's server
    assert FROM_HERE not in await _load(page, f"http://127.0.0.1:{site.port}/")
    assert FROM_HERE not in await _load(page, f"http://[::1]:{site.port}/")
    assert len(site.heads) == seen
    # straight to the forwarder from the machine (any program there can): refused the same way
    got = await _sh(node, env, f"curl -s -o /dev/null -w '%{{http_code}}' -x http://127.0.0.1:3132 "
                               f"http://127.0.0.1:{site.port}/; echo; curl -s -x http://127.0.0.1:3132 "
                               "http://home-site.test/")
    assert got.splitlines()[0] == "403" and FROM_HERE in got, got
    print(json.dumps(sorted(blocked)))

    await lanes.close(env.browser_lane)
    closed = await mgr.close(env.id, discard=True, reason="test")
    assert closed.state == "closed"
    assert await asyncio.to_thread(containers, node["node_name"]) == []
