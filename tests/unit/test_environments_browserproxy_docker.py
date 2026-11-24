"""An environment's browser through the owner's proxy, for real (openajar.environments.browserproxy): the
docker provider's container with its Camoufox, the node's browser forwarder, the SSH -R to the machine's
127.0.0.1:3132, and a fake owner's proxy in this process (Basic auth, CONNECT and plain HTTP).

Skipped as test_environments_vmbrowser_docker is. No internet: the fake proxy answers made-up names itself
(proxied.test, and ipinfo.test for the launch's exit check, which goes through it like the real one), so a
page with its words came through it. fallback.test is also in the machine's /etc/hosts, at a server on the
machine's own network address that answers other words: a browser that went out by itself would show them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import uuid
from dataclasses import dataclass, field

import pytest
from test_environments_vmbrowser_docker import _ready, containers, docker_node

from openajar.core.config import NodeConfig, save_proxies
from openajar.core.errors import OpenajarError
from openajar.core.models import ProxyConfig
from openajar.environments.models import EnvSettings

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker with the environment image built with its browser")

# Made-up credentials, put together at run time so the source holds nothing a secret scanner takes for real.
FAKE_USER = "oa" + "-dock-" + uuid.uuid4().hex[:6]
FAKE_PASS = "Pw" + "-made-up-" + uuid.uuid4().hex[:10]
LABEL = "fk1"
EXIT = {"ip": "203.0.113.9", "country": "LT", "city": "Vilnius", "timezone": "Europe/Vilnius",
        "loc": "54.6892,25.2798"}
VIA_PROXY = "came through the fake proxy"
DIRECT = "loaded straight from the machine"


@dataclass
class FakeUpstream:
    """The owner's proxy: Basic auth required (else 407); a name of `answers` answered here, asked in plain
    HTTP (the launch's check) or through a CONNECT to its port 80 (the browser's forwarder tunnels plain http);
    any other CONNECT recorded and refused (502), since no made-up name has a server behind it. `seen` holds
    every (method, host:port, credentials were right) it was asked for, `inner` the head of each request sent
    inside a tunnel."""

    answers: dict[str, str] = field(default_factory=dict)
    seen: list[tuple[str, str, bool]] = field(default_factory=list)
    inner: list[str] = field(default_factory=list)
    reject: bool = False
    server: asyncio.Server | None = None
    port: int = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 3)
            self.server = None

    def hosts(self) -> set[str]:
        return {target.rsplit(":", 1)[0] for _, target, _ in self.seen}

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode("latin-1")
            method, target, _ = head.split("\r\n", 1)[0].split(" ", 2)
            auth = [ln.split(":", 1)[1].strip() for ln in head.split("\r\n")[1:]
                    if ln.lower().startswith("proxy-authorization:")]
            good = auth == [f"Basic {_token()}"]
            host = target.split("/")[2] if target.startswith("http://") else target
            self.seen.append((method, host if ":" in host else f"{host}:80", good))
            if method == "CONNECT" and good and not self.reject and host.endswith(":80") \
                    and host.split(":")[0] in self.answers:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                self.inner.append((await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode("latin-1"))
                method = "GET"
            writer.write(self._answer(method, host, good))
            await writer.drain()
        except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            writer.close()

    def _answer(self, method: str, host: str, good: bool) -> bytes:
        if self.reject or not good:
            return (b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"fake\"\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        body = self.answers.get(host.split(":")[0]) if method != "CONNECT" else None
        if body is None:
            return b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        data = body.encode()
        kind = "application/json" if body.startswith("{") else "text/html"
        return (f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {len(data)}\r\nConnection: close"
                "\r\n\r\n").encode() + data


def _token() -> str:
    return base64.b64encode(f"{FAKE_USER}:{FAKE_PASS}".encode()).decode()


def probe_through(proxy: ProxyConfig, timeout: float) -> dict[str, str]:
    """The launch's exit check through the proxy, as Host.proxy_exit_info does but in plain HTTP to a name
    only the fake proxy answers: a stopped or refusing proxy fails it, like the real one."""
    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=timeout)
    try:
        conn.request("GET", "http://ipinfo.test/json", headers={"Proxy-Authorization": f"Basic {_token()}"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status} from ipinfo.test")
        return json.loads(resp.read())
    except (http.client.HTTPException, ValueError) as e:
        raise OSError(str(e)) from None
    finally:
        conn.close()


@pytest.fixture
async def proxied(tmp_path):
    fake = FakeUpstream(answers={"proxied.test": f"<title>Proxied</title><h1>{VIA_PROXY}</h1>",
                                 "fallback.test": f"<title>Fallback</title><h1>{VIA_PROXY}</h1>",
                                 "ipinfo.test": json.dumps(EXIT)})
    await fake.start()

    def write_proxy(home) -> None:
        save_proxies(NodeConfig(home=home), {LABEL: ProxyConfig(label=LABEL, host="127.0.0.1", port=fake.port,
                                                                username=FAKE_USER, password=FAKE_PASS)})

    settings = EnvSettings(browser="vm", browser_proxies=[LABEL])
    async with docker_node(tmp_path, settings, home_setup=write_proxy) as got:
        got["mgr"].proxy_probe = probe_through
        got["fake"] = fake
        yield got
    await fake.stop()


async def _open_env(node) -> object:
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id("session:s1", "Proxied page")
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding="session:s1", title="Proxied page", grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
    env = await mgr.wait_ready(env.id, 600)
    assert env.state == "ready", env.error
    return env


async def _sh(node, env, cmd: str) -> str:
    got = await node["envs"].exec(env.terminal_lane, cmd, timeout_s=60)
    return got.stdout


async def _load(page, url: str) -> str:
    """The page's text after a navigation, or "" when the navigation failed."""
    try:
        await page.goto(url, timeout=20_000)
    except Exception:  # noqa: BLE001 - a refused load is what some checks expect
        return ""
    return await page.evaluate("() => document.body ? document.body.innerText : ''")


async def _serve_on_machine(node, env) -> None:
    """localhost:3000 (the machine's loopback) and fallback.test:3001 (its own network address, in its
    /etc/hosts), each with words of its own."""
    await node["envs"].job_start(env.terminal_lane, "mkdir -p /srv/l && printf '<h1>on the machine itself</h1>' > "
                                 "/srv/l/index.html && cd /srv/l && exec python3 -m http.server 3000 --bind 127.0.0.1")
    await node["envs"].job_start(env.terminal_lane, f"mkdir -p /srv/d && printf '<h1>{DIRECT}</h1>' > /srv/d/index.html"
                                 " && cd /srv/d && exec python3 -m http.server 80 --bind 0.0.0.0")
    ip = (await _sh(node, env, "hostname -I | awk '{print $1}'")).strip()
    assert ip, "the machine has no network address"
    line = f"{ip} fallback.test"
    await _sh(node, env, f"(echo '{line}' >> /etc/hosts) 2>/dev/null || echo '{line}' | sudo -n tee -a /etc/hosts")
    await asyncio.sleep(1.0)
    assert DIRECT in await _sh(node, env, "curl -s --noproxy '*' http://fallback.test/")  # it would load directly
    # the loopback server answers before a page asks it (a busy machine starts it late)
    assert "on the machine itself" in await _sh(node, env, "curl -s --noproxy '*' --retry 15 --retry-connrefused "
                                                "--retry-delay 1 http://127.0.0.1:3000/")


async def _no_credentials_on_machine(node, env) -> None:
    # the patterns go in a file outside what is searched (as base64), so no command line here holds them
    packed = base64.b64encode(f"{FAKE_USER}\n{FAKE_PASS}\n{_token()}".encode()).decode()
    pattern = "-f /dev/shm/oa-patterns"
    found = await _sh(node, env, f"echo {packed} | base64 -d > /dev/shm/oa-patterns; "
                      "S=; [ \"$(id -u)\" = 0 ] || S='sudo -n'; "
                      "for p in /proc/[0-9]*; do $S cat $p/cmdline $p/environ 2>/dev/null | tr '\\0' '\\n'; done "
                      f"| grep -c -F {pattern}; "
                      f"$S grep -rIl -F {pattern} /run/agentos-browser /tmp /root /home /etc 2>/dev/null; "
                      "$S find / -xdev \\( -name prefs.js -o -name user.js \\) 2>/dev/null | "
                      f"$S xargs -r grep -l -F {pattern}; rm -f /dev/shm/oa-patterns; true")
    lines = [ln for ln in found.splitlines() if ln.strip()]
    assert lines and lines[0].strip() == "0", f"credentials in a process's args or environment: {found}"
    assert lines[1:] == [], f"credentials in files on the machine: {lines[1:]}"
    prefs = await _sh(node, env, "S=; [ \"$(id -u)\" = 0 ] || S='sudo -n'; $S find / -xdev \\( -name prefs.js -o "
                      "-name user.js \\) 2>/dev/null | $S xargs -r grep -h 'network.proxy.http\"' ; true")
    assert "127.0.0.1" in prefs or prefs == "", prefs  # where Firefox wrote one: the forwarder on the loopback


async def test_the_machines_browser_exits_through_the_owners_proxy_and_never_without_it(proxied):
    node, fake = proxied, proxied["fake"]
    mgr, lanes = node["mgr"], node["lanes"]
    env = await _open_env(node)
    assert env.browser_proxy == LABEL
    await _serve_on_machine(node, env)

    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    assert mgr.network_view(mgr.get(env.id))["browser_proxy"] == {"label": LABEL, "exit_ip": EXIT["ip"],
                                                                   "exit_country": "LT"}

    # through the proxy: the words only it has, asked for with the owner's credentials
    assert VIA_PROXY in await _load(page, "http://proxied.test/")
    assert ("CONNECT", "proxied.test:80", True) in fake.seen
    # plain http rides in a tunnel: the credentials are never in a request whose target the page chose
    assert fake.inner and all(head.startswith("GET /") for head in fake.inner)  # origin form: the page, its icon
    assert not any("proxy-authorization" in head.lower() for head in fake.inner)
    text = await _load(page, "http://fallback.test/")
    assert VIA_PROXY in text and DIRECT not in text
    await _load(page, "https://secure.test/")
    assert ("CONNECT", "secure.test:443", True) in fake.seen  # HTTPS too
    assert all(good for _, _, good in fake.seen)

    # the machine's own loopback: straight there, never past the proxy
    assert "on the machine itself" in await _load(page, "http://localhost:3000/")
    assert "on the machine itself" in await _load(page, "http://127.0.0.1:3000/")
    assert not fake.hosts() & {"localhost", "127.0.0.1"}

    # where the exit is: the page's clock, and the OS it claims
    await _load(page, "http://proxied.test/")
    seen = await page.evaluate("() => [Intl.DateTimeFormat().resolvedOptions().timeZone, navigator.platform, "
                               "navigator.userAgent]")
    assert seen[0] == EXIT["timezone"], seen
    assert "Linux" in seen[1] and "Linux" in seen[2], seen

    await _no_credentials_on_machine(node, env)

    # fail closed: the proxy refuses, then is gone, and nothing loads by the machine's own way out
    fake.reject = True
    assert DIRECT not in await _load(page, "http://fallback.test/")
    assert DIRECT not in await _load(page, "https://fallback.test/")
    await fake.stop()
    for url in ("http://fallback.test/", "http://proxied.test/"):
        text = await _load(page, url)
        assert DIRECT not in text and VIA_PROXY not in text, (url, text)

    await lanes.close(env.browser_lane)
    with pytest.raises(OpenajarError, match=f"proxy {LABEL} did not work"):
        await lanes.open(env.browser_lane)

    closed = await mgr.close(env.id, discard=True, reason="test")
    assert closed.state == "closed"
    assert await asyncio.to_thread(containers, node["node_name"]) == []


# Pretend exits in other countries and scripts (the fake proxy's ipinfo.test answers what each one says), and
# what a page reads there: everything that depends on where the browser is must be where the exit is.
EXITS = {
    "LT": ({"ip": "203.0.113.21", "country": "LT", "region": "Vilnius", "city": "Vilnius",
            "timezone": "Europe/Vilnius", "loc": "54.6892,25.2798"},
           ["lt-LT", "lt", "en-US", "en"], "1 234 567,891", "2026 m. sausio 15 d."),
    "JP": ({"ip": "203.0.113.22", "country": "JP", "region": "Tokyo", "city": "Tokyo", "timezone": "Asia/Tokyo",
            "loc": "35.6895,139.6917"},
           ["ja-JP", "ja", "en-US", "en"], "1,234,567.891", "2026年1月15日"),
    "BR": ({"ip": "203.0.113.23", "country": "BR", "region": "Sao Paulo", "city": "São Paulo",
            "timezone": "America/Sao_Paulo", "loc": "-23.5475,-46.6361"},
           ["pt-BR", "pt", "en-US", "en"], "1.234.567,891", "15 de janeiro de 2026"),
    "CH": ({"ip": "203.0.113.24", "country": "CH", "region": "Geneva", "city": "Geneva",
            "timezone": "Europe/Zurich", "loc": "46.2022,6.1457"},
           ["fr-CH", "fr", "en-US", "en"], "1'234'567,891", "15 janvier 2026"),
}

ECHO = ("import http.server as h\n"
        "class E(h.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        b = (self.headers.get('Accept-Language') or '').encode()\n"
        "        self.send_response(200); self.send_header('Content-Type', 'text/plain')\n"
        "        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)\n"
        "h.HTTPServer(('127.0.0.1', 3005), E).serve_forever()\n")

READ_PAGE = """async () => {
  const o = Intl.DateTimeFormat().resolvedOptions();
  const r = {timezone: o.timeZone, intl_locale: o.locale, offset_minutes: new Date().getTimezoneOffset(),
    languages: [...navigator.languages], language: navigator.language, ua: navigator.userAgent,
    number: (1234567.891).toLocaleString(),
    date: new Date(Date.UTC(2026, 0, 15, 12)).toLocaleDateString(undefined, {year: 'numeric', month: 'long',
                                                                             day: 'numeric'})};
  const w = new Worker(URL.createObjectURL(new Blob(["postMessage([[...navigator.languages], " +
    "Intl.DateTimeFormat().resolvedOptions().timeZone, Intl.DateTimeFormat().resolvedOptions().locale])"])));
  r.worker = await new Promise(res => { w.onmessage = e => res(e.data); setTimeout(() => res(null), 5000); });
  const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
  const cands = []; pc.onicecandidate = e => { if (e.candidate) cands.push(e.candidate.candidate); };
  pc.createDataChannel('x'); await pc.setLocalDescription(await pc.createOffer());
  await new Promise(res => setTimeout(res, 3000)); pc.close();
  r.candidates = cands; r.ips = cands.map(c => c.split(' ')[4]);
  r.voices = speechSynthesis.getVoices().map(v => v.lang);
  return r;
}"""


READ_POSITION = """async () => {
  const r = {permission: (await navigator.permissions.query({name: 'geolocation'})).state};
  // the timeout option counts from a granted permission: a prompt nobody answers never calls back
  const pos = await new Promise(res => { setTimeout(() => res('no answer'), 6000);
    navigator.geolocation.getCurrentPosition(
      p => res([p.coords.latitude, p.coords.longitude, p.coords.accuracy]), e => res('error ' + e.code)); });
  if (Array.isArray(pos)) { [r.latitude, r.longitude, r.accuracy] = pos; } else { r.position_error = pos; }
  return r;
}"""


def _spaces(text: str) -> str:
    return " ".join(str(text).replace(" ", " ").replace(" ", " ").split())


async def _read_where(lanes, env, fake) -> dict:
    page = await lanes.live_session(env.browser_lane).page()
    fake.inner.clear()
    assert VIA_PROXY in await _load(page, "http://proxied.test/")
    seen = await page.evaluate(READ_PAGE)
    await page.evaluate("() => { const f = document.createElement('iframe'); f.srcdoc = '<p>in a frame</p>'; "
                        "document.body.appendChild(f); return new Promise(r => f.onload = r); }")
    seen["iframe"] = await page.frames[-1].evaluate(
        "() => [[...navigator.languages], Intl.DateTimeFormat().resolvedOptions().timeZone]")
    sent = [ln.split(":", 1)[1].strip() for head in fake.inner for ln in head.split("\r\n")
            if ln.lower().startswith("accept-language:")]
    seen["accept_language_via_proxy"] = sent[0] if sent else None
    seen["accept_language"] = await _load(page, "http://localhost:3005/")  # the machine's loopback
    seen.update(await page.evaluate(READ_POSITION))  # a secure context (localhost): geolocation answers there
    return seen


async def test_the_browser_claims_the_exits_place_and_language_in_each_country(proxied):
    """One environment, so one pinned device (vmpin): its first launch (LT) pins it, and every later launch,
    at an exit that moved to another country, shows that country's place and languages on the same device."""
    from openajar.environments import vmpin
    from openajar.environments.browserproxy import exit_mismatches

    node, fake = proxied, proxied["fake"]
    mgr, lanes = node["mgr"], node["lanes"]
    env = await _open_env(node)
    await node["envs"].job_start(env.terminal_lane, f"cat > /tmp/echo.py <<'EOF'\n{ECHO}EOF\nexec python3 /tmp/echo.py")
    await asyncio.sleep(1.0)
    results, devices = {}, set()
    for cc, (exit_info, langs, number, date) in EXITS.items():
        fake.answers["ipinfo.test"] = json.dumps(exit_info)
        await lanes.open(env.browser_lane)
        try:
            seen = await _read_where(lanes, env, fake)
        finally:
            await lanes.close(env.browser_lane)
        results[cc] = seen
        pinned = vmpin.pin_path(node["home"], env.id).read_bytes()
        devices.add(pinned)  # the pin is written once, at the first launch, and never again
        print(cc, json.dumps(seen, ensure_ascii=False))
        assert exit_mismatches(seen, exit_info) == [], (cc, seen)
        assert seen["languages"] == langs and seen["intl_locale"] == langs[0], (cc, seen)
        assert seen["accept_language_via_proxy"] == seen["accept_language"], (cc, seen)
        assert seen["worker"] == [langs, exit_info["timezone"], langs[0]], (cc, seen)
        assert seen["iframe"] == [langs, exit_info["timezone"]], (cc, seen)
        assert (_spaces(seen["number"]), _spaces(seen["date"])) == (number, date), (cc, seen)
        # no position: the permission is "prompt" and a page's ask waits, as in an untouched Firefox (vmharden)
        assert seen["permission"] == "prompt" and seen.get("position_error") == "no answer", (cc, seen)
    print(json.dumps(results, ensure_ascii=False, indent=1))
    assert len(devices) == 1  # the same pinned device in every country
    assert len({r["ua"] for r in results.values()}) == 1, results

    # a fixed language: the place still follows the exit, the language stays the owner's
    mgr.settings = mgr.settings.model_copy(update={"browser_language": "en-US"})
    exit_info = EXITS["JP"][0]
    fake.answers["ipinfo.test"] = json.dumps(exit_info)
    await lanes.open(env.browser_lane)
    try:
        seen = await _read_where(lanes, env, fake)
    finally:
        await lanes.close(env.browser_lane)
    print(json.dumps(seen, ensure_ascii=False, indent=1))
    assert exit_mismatches(seen, exit_info, "en-US") == [], seen
    assert seen["languages"][0] == "en-US" and seen["timezone"] == "Asia/Tokyo", seen
    assert _spaces(seen["number"]) == "1,234,567.891", seen
