"""An environment's network: what its machine may reach, and how that is set up on the machine.

Three levels (environments.toml `network`, the node's default; `allow_hosts`, the owner's own hosts):

- `full` (the default): the machine reaches the internet as it always did.
- `limited`: only package registries and code hosts (PRESET), the repository's own git host, the
  owner's `allow_hosts`, and the hosts the environment's app secrets are bound to.
- `none`: nothing but the git gateway and the hosts of bound secrets.

Limited and none are closed outside the machine (openajar.vms.netwall: a Hetzner firewall with no
way out, or the docker entrypoint's closed wall), since the agent is root on its VM. The machine's
way out is then the node's allowlist proxy (openajar.environments.envproxy), forwarded to
127.0.0.1:PROXY_PORT_ON_VM over its SSH connection, and every program finds it through the usual
proxy variables, apt's and Docker's settings. The node resolves names itself and connects to public
addresses only.

An agent may pass `network` to environment_open only to narrow it (full, then limited, then none),
never to widen it; nothing an agent or a repository says widens it. The owner widens it at the node:
`openajar environments allow --network limited --host api.example.com`.

This module is pure: the levels, the lists and the shell the machine runs. The node side is
openajar.environments.networking.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

from openajar.core.errors import OpenajarError

NetworkLevel = Literal["full", "limited", "none"]
LEVELS: tuple[str, ...] = ("full", "limited", "none")   # widest first
PROXY_PORT_ON_VM = 3130    # the node's allowlist proxy, on the machine's loopback (a -R of its tunnel)
SWAP_PORT_ON_VM = 3131     # the node's secret swap, where the machine's relay sends bound hosts
# The browser's own proxy (openajar.environments.browserproxy): a forwarder on the node that chains to the
# owner's proxy, so the credentials stay on the node. Only the browser on the machine is pointed at it.
BROWSER_PROXY_PORT_ON_VM = 3132
RELAY_ADDR = "127.0.10.1"  # bound hosts in the machine's /etc/hosts: its relay listens here on 443
CA_PATH = "/usr/local/share/ca-certificates/agentos-environment.crt"
BLOCKED_KEPT = 50          # refused hosts kept per environment
BLOCKED_IN_PR = 10         # and shown in a pull request

# What `limited` reaches: package registries and code hosts, the ones Claude Code's "limited" and
# Codex's "common dependencies" lists share for the languages a dev box runs, without the broad ones
# (no google.com or microsoft.com). An entry with a leading dot is the domain and every host under
# it; any other is that host alone. Ports 80 and 443 only.
PRESET: tuple[str, ...] = (
    # JavaScript: npm, yarn, Node, JSR
    "registry.npmjs.org", "registry.yarnpkg.com", "repo.yarnpkg.com", "nodejs.org", "jsr.io",
    # Python: PyPI, uv's Python builds come from GitHub (below)
    "pypi.org", "files.pythonhosted.org", "pypi.python.org",
    # Rust: crates and rustup
    "crates.io", "index.crates.io", "static.crates.io", "static.rust-lang.org", "sh.rustup.rs",
    # Go: the module proxy and checksum database
    "proxy.golang.org", "sum.golang.org",
    # Ruby, PHP, .NET
    "rubygems.org", "index.rubygems.org", "repo.packagist.org", "packagist.org", "api.nuget.org",
    # Java: Maven Central and Gradle
    "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "services.gradle.org",
    # Ubuntu's packages (country mirrors under archive.ubuntu.com, and Hetzner's own mirror)
    "archive.ubuntu.com", ".archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com",
    "mirror.hetzner.com",
    # Containers: Docker Hub and GitHub's registry
    "registry-1.docker.io", "auth.docker.io", "index.docker.io", "production.cloudflare.docker.com",
    "download.docker.com", "ghcr.io", "pkg-containers.githubusercontent.com",
    # GitHub: code, releases and raw files (no credential reaches the machine: it pushes through the gateway)
    "github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com",
    "raw.githubusercontent.com", "release-assets.githubusercontent.com",
    # Playwright's browsers, for the tests a web app runs
    "cdn.playwright.dev", "playwright.download.prss.microsoft.com", "playwright.azureedge.net",
)

HOST_RE = re.compile(r"^(\*\.|\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}(:[0-9]{1,5})?$")


class NetworkNotAllowed(OpenajarError):
    """An agent asked for a wider network than the node allows, or one the provider cannot hold."""
    code = "network_not_allowed"


def rank(level: str) -> int:
    return LEVELS.index(level)


def narrowed(default: str, asked: str | None) -> str:
    """The level an environment opens with: the node's default, or a narrower one the agent asked for.
    A wider one is refused (never widened, whatever the agent read)."""
    if asked is None or str(asked).strip() == "":
        return default
    want = str(asked).strip().lower()
    if want not in LEVELS:
        raise NetworkNotAllowed(f"network is full, limited or none, not {asked!r}")
    if rank(want) < rank(default):
        raise NetworkNotAllowed(
            f"this node's environments have network {default}, and an agent may only narrow it (full, then "
            f"limited, then none), not widen it to {want}. Open with network=\"{default}\" or narrower, or tell the "
            f"user: the owner widens it with `openajar environments allow --network {want}`")
    return want


def check_host(entry: str) -> str:
    """An allow_hosts entry: a host (`api.example.com`), a domain and its hosts (`.example.com` or
    `*.example.com`), with a port when it is not 80 or 443. Lower case, no scheme or path."""
    e = str(entry).strip().lower().rstrip(".")
    if e.startswith(("http://", "https://")):
        e = e.split("://", 1)[1].split("/", 1)[0]
    if not HOST_RE.match(e):
        raise ValueError(f"not a host name: {entry!r} (like api.example.com, .example.com or host:8443)")
    return e


def allowlist(level: str, *, allow_hosts: list[str] | tuple[str, ...] = (), repo_host: str | None = None,
              secret_hosts: list[str] | tuple[str, ...] = ()) -> list[str]:
    """What the node's proxy lets the machine reach. Full: nothing to list (the machine has no proxy)."""
    if level == "full":
        return []
    out: list[str] = list(secret_hosts)
    if level == "limited":
        out += [*PRESET, *allow_hosts]
        if repo_host:
            out.append(repo_host)
    return list(dict.fromkeys(out))


def short_list(level: str, allow: list[str], secret_hosts: list[str]) -> dict:
    """The allowed list in short, for environment() and the web app."""
    if level == "full":
        return {"all": True}
    extra = [h for h in allow if h not in PRESET and h not in secret_hosts]
    return {"preset": "package registries and code hosts" if level == "limited" else None,
            "hosts": extra[:20], "secret_hosts": secret_hosts}


def refusal(host: str) -> str:
    return (f"{host} is not on this environment's network list. The owner can allow it: "
            f"openajar environments allow --host {host}")


def blocked_section(blocked: list[dict]) -> str:
    """The pull request's "Blocked during this work", or "" when nothing was blocked."""
    if not blocked:
        return ""
    rows = sorted(blocked, key=lambda b: -int(b.get("count") or 0))[:BLOCKED_IN_PR]
    lines = [f"- `{b['host']}` ({b.get('count', 1)}x)" + (f": {b['why']}" if b.get("why") else "") for b in rows]
    more = len(blocked) - len(rows)
    if more > 0:
        lines.append(f"- and {more} more")
    return ("\n\n### Blocked during this work\n\nThe environment's network refused these hosts. The owner allows one "
            "with `openajar environments allow --host <host>`.\n\n" + "\n".join(lines) + "\n")


# ---------------------------------------------------------------- on the machine

# legacy-name: the block in a machine's /etc/environment keeps 0.4.0's markers, so a resumed save's block is replaced
BEGIN, END = "# agentos network: begin", "# agentos network: end"  # legacy-name

# Relays RELAY_ADDR:443 to the node's swap (SWAP_PORT_ON_VM), byte for byte. Standard library only.
RELAY_PY = f"""import socket, threading
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
def serve(c):
    try:
        u = socket.create_connection(("127.0.0.1", {SWAP_PORT_ON_VM}), timeout=10)
        u.settimeout(None)
    except OSError:
        c.close()
        return
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    pipe(u, c)
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("{RELAY_ADDR}", 443))
s.listen(64)
while True:
    c, _ = s.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
"""

RELAY_UNIT = """[Unit]
Description=openajar: bound hosts to the node's secret swap
[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/agentos-env/secret-relay.py
Restart=always
RestartSec=1
[Install]
WantedBy=multi-user.target
"""


def _block(path: str, body: str) -> str:
    """Put `body` between our markers in `path`, replacing what an earlier run put there. Written in
    place (`cat >`), never renamed over: a docker machine's /etc/hosts is a bind mount."""
    q = shlex.quote
    return (f"t=$(mktemp) && {{ sed '/^{BEGIN}$/,/^{END}$/d' {q(path)} 2>/dev/null || true; }} > \"$t\" && "
            f"printf '%s\\n' {q(BEGIN)} {' '.join(q(line) for line in body.splitlines())} {q(END)} >> \"$t\" && "
            f"cat \"$t\" > {q(path)} && rm -f \"$t\"")


def proxy_env(level: str, variables: dict[str, str], *, ca: bool) -> dict[str, str]:
    """The environment every process on the machine gets (/etc/environment): the proxy for a closed
    network, the placeholders of bound secrets, and the CA bundle the runtimes read."""
    out: dict[str, str] = {}
    if level != "full":
        url = f"http://127.0.0.1:{PROXY_PORT_ON_VM}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            out[name] = url
        out["NO_PROXY"] = out["no_proxy"] = "localhost,127.0.0.1,::1"
        # Node's own fetch (and, from 24, http.request) reads the variables above only with this: without it
        # an app's fetch() tried the closed network itself and got ENOTFOUND, not the 403. Node 22.21+ and 24+
        out["NODE_USE_ENV_PROXY"] = "1"
    if ca:
        out["NODE_EXTRA_CA_CERTS"] = CA_PATH
        out["REQUESTS_CA_BUNDLE"] = out["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"
    out.update(variables)
    return out


def vm_script(level: str, *, variables: dict[str, str], ca_pem: str | None, secret_hosts: list[str]) -> str:
    """The shell the node runs on the machine (as root, from stdin) after the tunnels are up. It can run
    again: each file it writes has its own marked block or is written whole."""
    lines = ["set -eu"]
    env = proxy_env(level, variables, ca=bool(ca_pem))
    lines.append(_block("/etc/environment", "\n".join(f'{k}="{v}"' for k, v in env.items())))
    if level != "full":
        url = f"http://127.0.0.1:{PROXY_PORT_ON_VM}/"
        lines.append(f"mkdir -p /etc/apt/apt.conf.d && printf '%s\\n' 'Acquire::http::Proxy \"{url}\";' "
                     f"'Acquire::https::Proxy \"{url}\";' > /etc/apt/apt.conf.d/95agentos-proxy")
        dropin = (f"[Service]\nEnvironment=\"HTTP_PROXY={url}\" \"HTTPS_PROXY={url}\" "
                  "\"NO_PROXY=localhost,127.0.0.1,::1\"\n")
        lines.append("if [ -d /etc/systemd/system ] && command -v dockerd >/dev/null 2>&1; then "
                     "mkdir -p /etc/systemd/system/docker.service.d && "
                     f"printf '%s' {shlex.quote(dropin)} > /etc/systemd/system/docker.service.d/agentos-proxy.conf && "
                     "if systemctl is-active --quiet docker 2>/dev/null; then systemctl daemon-reload && "
                     "systemctl restart docker; fi; fi")
    else:
        lines.append("rm -f /etc/apt/apt.conf.d/95agentos-proxy /etc/systemd/system/docker.service.d/agentos-proxy.conf")
    if ca_pem:
        lines.append(f"mkdir -p /usr/local/share/ca-certificates && printf '%s' {shlex.quote(ca_pem)} > {CA_PATH} && "
                     "update-ca-certificates >/dev/null 2>&1")
    if secret_hosts:
        lines.append(_block("/etc/hosts", "\n".join(f"{RELAY_ADDR} {h}" for h in secret_hosts)))
        lines.append("mkdir -p /usr/local/lib/agentos-env && "
                     f"printf '%s' {shlex.quote(RELAY_PY)} > /usr/local/lib/agentos-env/secret-relay.py")
        unit = shlex.quote(RELAY_UNIT)
        lines.append(  # systemd where it runs (a cloud VM), else a process of its own (a docker machine)
            "if [ -d /run/systemd/system ]; then "
            f"printf '%s' {unit} > /etc/systemd/system/agentos-secret-relay.service && systemctl daemon-reload && "
            "systemctl enable --now agentos-secret-relay >/dev/null 2>&1 && systemctl restart agentos-secret-relay; "
            "else pkill -f secret-relay.py 2>/dev/null || true; "
            "setsid -f python3 /usr/local/lib/agentos-env/secret-relay.py >/dev/null 2>&1 </dev/null; fi")
    else:  # none bound (now): no names sent to the relay, and no relay
        lines.append(_block("/etc/hosts", ""))
        lines.append("if [ -d /run/systemd/system ]; then systemctl disable --now agentos-secret-relay >/dev/null 2>&1 || true; "
                     "fi; pkill -f secret-relay.py 2>/dev/null || true")
    lines.append("echo network-ready")
    return "\n".join(lines) + "\n"


def vm_browser_prefs(level: str, *, proxied: bool = False) -> dict[str, object]:
    """Firefox's settings for a browser on the machine (browser = "vm"): with an owner's proxy
    (`proxied`), the node's browser forwarder on the machine's loopback (BROWSER_PROXY_PORT_ON_VM); else in a
    closed network the node's allowlist proxy. localhost stays the machine's own either way.

    Proxied, nothing of the browser's leaves past the proxy: WebRTC gathers only through it (no UDP from
    the machine's own address), HTTP/3 is off (QUIC is UDP, which an HTTP proxy does not carry) and DNS
    prefetch is off (names are looked up by the proxy, never by the machine's datacenter resolver)."""
    if not proxied and level == "full":
        return {}
    port = BROWSER_PROXY_PORT_ON_VM if proxied else PROXY_PORT_ON_VM
    prefs: dict[str, object] = {
        "network.proxy.type": 1, "network.proxy.http": "127.0.0.1", "network.proxy.http_port": port,
        "network.proxy.ssl": "127.0.0.1", "network.proxy.ssl_port": port,
        "network.proxy.no_proxies_on": "localhost, 127.0.0.1, ::1", "network.proxy.share_proxy_settings": True}
    if proxied:
        from openajar.node.camoufox.prefs import WEBRTC_PROXY_PREFS

        prefs.update({**WEBRTC_PROXY_PREFS, "network.http.http3.enable": False, "network.dns.disablePrefetch": True,
                      "network.dns.disablePrefetchFromHTTPS": True, "network.predictor.enabled": False})
        prefs.update(PROXIED_QUIET_PREFS)
    return prefs


# A proxied browser makes no request of its own, beside the pages': each of these would either leave without
# the proxy or show the exit as a browser whose background traffic no person's browsing makes. Camoufox's build
# sets most of them as its defaults (camoufox.cfg); as prefs of the launch they hold whatever a build ships.
# Not in its defaults, and Firefox's own are the risky way: failover_direct (true) and the DoH mode.
PROXIED_QUIET_PREFS: dict[str, object] = {
    # a proxy that fails is never skipped for a direct connection from the machine's datacenter address
    "network.proxy.failover_direct": False,
    # the connectivity and captive portal checks (detectportal.firefox.com on a timer): a failed one marks
    # the browser offline or behind a portal, and they run whatever the page does
    "network.connectivity-service.enabled": False, "network.captive-portal-service.enabled": False,
    # DNS over HTTPS off by choice (5): names are the proxy's to look up, never a resolver of the browser's
    "network.trr.mode": 5,
    # telemetry, health reports, studies (Normandy) and the region lookup: uploads and polls to Mozilla
    "toolkit.telemetry.enabled": False, "datareporting.healthreport.uploadEnabled": False,
    "datareporting.policy.dataSubmissionEnabled": False, "app.normandy.enabled": False,
    "app.shield.optoutstudies.enabled": False, "browser.region.update.enabled": False,
    "browser.region.network.url": "",
    # Safe Browsing: its lists are polled from Google every half hour while it is on
    "browser.safebrowsing.malware.enabled": False, "browser.safebrowsing.phishing.enabled": False,
}
