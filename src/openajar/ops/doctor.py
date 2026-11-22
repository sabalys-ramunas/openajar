"""`openajar doctor`: one checklist that says what is wrong with this node and how to fix it.

Every check returns results with a status (pass, warn, fail, skip), a one-line detail and,
when something is wrong, the exact command or edit that fixes it. Checks read the machine
only through `openajar.core.system.Host`, so each one runs against a fake in unit tests. The
doctor is read-only: it never changes a file, a lane or a service, and it never prints a
secret (proxy credentials, the MCP token and vault keys stay out of every line and of --json).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import itertools
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar import __version__
from openajar.core.config import LANE_MEMORY_DEFAULT_MB, NodeConfig, config_path, load_config, load_proxies
from openajar.core.layout import BROWSER_BIN, connect_ro, default_backup_dir, lane_dirs, vault_socket
from openajar.core.models import ProxyConfig
from openajar.core.system import Host, redact
from openajar.node import fingerprint as fpmod
from openajar.ops.doctor_web import check_web

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
ORDER = {FAIL: 0, WARN: 1, PASS: 2, SKIP: 3}

REQUIRED_PACKAGES = ["xvfb", "x11vnc", "fonts-liberation", "fonts-noto-color-emoji"]
OPTIONAL_PACKAGES = {"bubblewrap": "terminal and service lanes isolate programs with it",
                     "xclip": "the live view copies what a lane copied to your computer with it",
                     "ffmpeg": "video proofs of an environment's browser (browser_record) record its screen with it"}
PACKAGE_BINARIES = {"xvfb": "Xvfb", "x11vnc": "x11vnc", "bubblewrap": "bwrap", "xclip": "xclip", "ffmpeg": "ffmpeg"}

LANE_RAM = 700 * 1024**2      # what one warm Camoufox lane takes, measured on the 4 GB node
BASE_RAM = 512 * 1024**2      # agentosd, Xvfb overhead and the OS
DISK_FAIL = (1 * 1024**3, 5)  # (bytes, percent): below either is a failure
DISK_WARN = (5 * 1024**3, 10)
TMPFS_FAIL = (64 * 1024**2, 2)  # a tmpfs /tmp: a few hundred MB free is plenty
TMPFS_WARN = (256 * 1024**2, 10)
BACKUP_MAX_AGE_DAYS = 7


@dataclass
class CheckResult:
    id: str
    title: str
    status: str
    detail: str
    fix: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail,
                "fix": self.fix, "data": self.data}


@dataclass
class DoctorContext:
    home: Path
    host: Host = field(default_factory=Host)
    config: NodeConfig | None = None
    unit: str = "agentosd"
    network: bool = True
    timeout: float = 15.0
    prefix: Path | None = None   # the install prefix (where .venv lives)
    client_home: Path | None = None  # whose `openajar connect` connections to check (default: $HOME)
    # how they are read (openajar.client.state.connections_at): the CLI passes it in, since ops is a
    # layer below the client; without it this computer has none to check
    read_connections: Callable[[Path | None], list[Any]] | None = None
    claude_profiles: Check | None = None  # the Claude Code profiles' entries (openajar.ops.cli passes it in)

    def __post_init__(self) -> None:
        self.home = Path(self.home)
        if self.config is None and config_path(self.home).exists():
            try:
                self.config = load_config(self.home)
            except Exception:  # noqa: BLE001 - the config check reports it
                self.config = None
        if self.prefix is None:
            prefix = Path(sys.prefix)
            self.prefix = prefix.parent if prefix.name == ".venv" else prefix


Check = Callable[[DoctorContext], list[CheckResult]]


def _size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _systemctl_show(ctx: DoctorContext, props: list[str]) -> dict[str, str] | None:
    if not ctx.host.which("systemctl"):
        return None
    rc, out = ctx.host.run(["systemctl", "show", ctx.unit, "--no-pager", *(f"-p{p}" for p in props)])
    if rc != 0 and not out:
        return None
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


# ---- checks -------------------------------------------------------------------------

def check_config(ctx: DoctorContext) -> list[CheckResult]:
    path = config_path(ctx.home)
    if not path.exists():
        return [CheckResult("config", "node.toml", FAIL, f"no node.toml in {ctx.home}",
                            f"OPENAJAR_HOME={ctx.home} openajar node init")]
    if ctx.config is None:
        return [CheckResult("config", "node.toml", FAIL, f"{path} does not parse",
                            f"fix the TOML in {path}, or restore it from a backup")]
    c = ctx.config
    if not c.mcp_token:
        return [CheckResult("config", "node.toml", FAIL, "node.toml has no mcp_token",
                            f"OPENAJAR_HOME={ctx.home} openajar node init")]
    return [CheckResult("config", "node.toml", PASS,
                        f"node {c.name}, MCP on {c.mcp_host}:{c.mcp_port}, max {c.max_warm_lanes} warm lanes",
                        data={"name": c.name, "mcp_port": c.mcp_port, "max_warm_lanes": c.max_warm_lanes})]


def check_os(ctx: DoctorContext) -> list[CheckResult]:
    p = ctx.host.platform()
    arch = p.get("machine", "")
    if p.get("system") != "Linux":
        return [CheckResult("os", "OS and arch", WARN, f"{p.get('system')} {arch}: nodes run on Linux",
                            "run the node on Ubuntu 24.04 (x86_64 or arm64)", data=p)]
    pretty = p.get("pretty_name") or f"{p.get('id', 'linux')} {p.get('version_id', '')}".strip()
    if arch not in ("x86_64", "aarch64"):
        return [CheckResult("os", "OS and arch", WARN, f"{pretty}, {arch}: Camoufox ships for x86_64 and arm64",
                            "use an x86_64 or arm64 machine", data=p)]
    if p.get("id") != "ubuntu" or p.get("version_id") != "24.04":
        return [CheckResult("os", "OS and arch", WARN, f"{pretty}, {arch}: only Ubuntu 24.04 is tested",
                            "Ubuntu 24.04 is the supported node OS", data=p)]
    return [CheckResult("os", "OS and arch", PASS, f"{pretty}, {arch}", data=p)]


def check_python(ctx: DoctorContext) -> list[CheckResult]:
    py = ctx.host.python()
    version = ".".join(map(str, py["version"]))
    out = []
    if tuple(py["version"][:2]) < (3, 12):
        out.append(CheckResult("python", "Python", FAIL, f"Python {version}; openajar needs 3.12 or newer",
                               "rerun deploy/install-node.sh (it builds a 3.12 venv with uv)"))
    elif py["prefix"] == py["base_prefix"]:
        out.append(CheckResult("python", "Python", WARN, f"Python {version}, not in a virtualenv ({py['prefix']})",
                               "run openajar from the venv deploy/install-node.sh makes (<prefix>/.venv/bin/openajar)"))
    else:
        out.append(CheckResult("python", "Python", PASS, f"Python {version} in {py['prefix']}",
                               data={"version": version, "prefix": py["prefix"]}))
    return out


def check_packages(ctx: DoctorContext) -> list[CheckResult]:
    names = REQUIRED_PACKAGES + list(OPTIONAL_PACKAGES)
    status = ctx.host.dpkg_installed(names)
    if status is None:  # no dpkg: look for the programs instead
        status = {}
        for pkg in names:
            binary = PACKAGE_BINARIES.get(pkg)
            if pkg == "novnc":
                status[pkg] = ctx.host.exists(Path("/usr/share/novnc"))
            elif binary:
                status[pkg] = ctx.host.which(binary) is not None
            else:
                status[pkg] = True  # fonts: cannot tell without dpkg
    missing = [p for p in REQUIRED_PACKAGES if not status.get(p)]
    optional = [p for p in OPTIONAL_PACKAGES if not status.get(p)]
    out = []
    if missing:
        out.append(CheckResult("packages", "System packages", FAIL, f"missing: {', '.join(missing)}",
                               f"sudo apt-get install -y --no-install-recommends {' '.join(missing)}",
                               data={"missing": missing}))
    else:
        out.append(CheckResult("packages", "System packages", PASS, f"{', '.join(REQUIRED_PACKAGES)} installed"))
    for pkg in optional:
        out.append(CheckResult(f"packages.{pkg}", f"Package {pkg}", WARN,
                               f"{pkg} is missing ({OPTIONAL_PACKAGES[pkg]})",
                               f"sudo apt-get install -y {pkg}"))
    return out


def _service_cache(ctx: DoctorContext) -> Path | None:
    """The service user's browser cache when agentosd runs as its own user (HOME=OPENAJAR_HOME)."""
    cache = ctx.home / ".cache"
    return cache if ctx.host.exists(cache / "camoufox") else None


def check_camoufox(ctx: DoctorContext) -> list[CheckResult]:
    info = ctx.host.camoufox(_service_cache(ctx))
    fetch = f"cd {ctx.prefix} && .venv/bin/python -m camoufox fetch"
    if "verstr" not in info:
        return [CheckResult("camoufox", "Camoufox browser", FAIL,
                            f"no Camoufox browser installed ({info.get('error', 'unknown error')})", fetch)]
    installed = fpmod.browser_major(info["verstr"])
    out = [CheckResult("camoufox", "Camoufox browser", PASS, f"Camoufox {info['verstr']} (Firefox {installed})",
                       data={"version": info["verstr"], "firefox": installed, "path": info.get("path")})]
    out.append(_fingerprint_model(ctx))
    lanes = [lane.id for lane in lane_dirs(ctx.home)]
    if lanes:
        # Each lane's pin, browser version, proxy, session and holds are its own checklist.
        out.append(CheckResult("lanes", "Lanes", PASS,
                               f"{len(lanes)} lane(s); check each one with: openajar lane check --all",
                               data={"lanes": lanes, "command": "openajar lane check --all"}))
    return out


def _fingerprint_model(ctx: DoctorContext) -> CheckResult:
    """Camoufox draws every fingerprint from fpgen's model inside the venv. Without it (a venv rebuilt
    while the browser stayed cached) the first lane fails to start; under the hardened unit the venv
    is read-only, so it must be in place and readable by the user agentosd runs as."""
    user = None
    if ctx.host.euid() == 0:
        with contextlib.suppress(OSError):
            owner = ctx.host.user_name(os.stat(ctx.home).st_uid)
            user = owner if owner not in ("root", "0") else None
    info = ctx.host.fpgen_model(user)
    who = f" by {user}" if user else ""
    fix = (f"sudo {ctx.prefix}/.venv/bin/python -c 'from camoufox.fpgen_model import ensure_fpgen_model; "
           "ensure_fpgen_model()' (as root: the code is read-only for the service user), then run doctor again")
    if info.get("ok"):
        return CheckResult("camoufox.model", "Fingerprint model", PASS, f"in place and readable{who}",
                           data={"dir": info.get("dir")})
    where = f" in {info['dir']}" if info.get("dir") else ""
    return CheckResult("camoufox.model", "Fingerprint model", FAIL,
                       f"Camoufox's fingerprint model is missing or unreadable{who}{where} ({info.get('error')}): "
                       "no lane can start", fix, data={"dir": info.get("dir")})


def _openajar_slot_process(cfg: NodeConfig, argv: list[str]) -> bool:
    name = os.path.basename(argv[0]) if argv else ""
    displays = {f":{cfg.display_base + i}" for i in range(cfg.max_slots)}
    ports = {f"127.0.0.1:{cfg.viewer_base + i}" for i in range(cfg.max_slots)}
    if name == "Xvfb":
        return len(argv) > 1 and argv[1] in displays
    if name == "x11vnc":
        return any(a in displays for a in argv)
    if any("websockify" in os.path.basename(a) for a in argv[:2]):
        return any(a in ports for a in argv)
    return False


def _x11vnc_on_tcp(cfg: NodeConfig, processes: list[tuple[int, list[str]]]) -> list[str]:
    """Our displays' x11vnc processes that listen on TCP: started by a version before the private
    unix socket (any local user, the brain user included, can drive those browsers)."""
    displays = {f":{cfg.display_base + i}" for i in range(cfg.max_slots)}
    found = []
    for pid, argv in processes:
        if not argv or os.path.basename(argv[0]) != "x11vnc":
            continue
        args = dict(itertools.pairwise(argv))
        # both listeners must be off: -rfbport 0 alone still leaves IPv6 [::]:5900 open
        if args.get("-display") in displays and (args.get("-rfbport") != "0" or args.get("-rfbportv6") != "-1"):
            found.append(f"{args['-display']} (pid {pid})")
    return found


def check_displays(ctx: DoctorContext) -> list[CheckResult]:
    cfg = ctx.config
    if cfg is None:
        return [CheckResult("displays", "Displays and ports", SKIP, "no node.toml")]
    processes = ctx.host.processes()
    ours = any(_openajar_slot_process(cfg, argv) for _, argv in processes)
    on_tcp = _x11vnc_on_tcp(cfg, processes)
    if on_tcp:
        return [CheckResult("displays", "Displays and ports", FAIL,
                            f"x11vnc listens on TCP for {', '.join(on_tcp)}: any local user can drive those lanes",
                            "restart agentosd (`sudo openajar node restart`): it stops them, and lanes open "
                            "again on private unix sockets", data={"x11vnc_tcp": on_tcp})]
    free, taken = 0, []
    for i in range(cfg.max_slots):
        busy = []
        if ctx.host.display_in_use(cfg.display_base + i):
            busy.append(f":{cfg.display_base + i}")
        # x11vnc has no port any more (a unix socket under OPENAJAR_HOME/vnc); the viewer range is
        # still checked because an old websockify may hold it.
        if ctx.host.port_in_use(cfg.viewer_base + i):
            busy.append(str(cfg.viewer_base + i))
        if busy:
            taken.append(f"slot {i} ({', '.join(busy)})")
        else:
            free += 1
    ranges = (f"displays :{cfg.display_base}-:{cfg.display_base + cfg.max_slots - 1}, "
              f"viewer {cfg.viewer_base}-{cfg.viewer_base + cfg.max_slots - 1}")
    data = {"free": free, "slots": cfg.max_slots, "taken": taken}
    note = " (some by agentosd's own warm lanes)" if taken and ours else ""
    if free + (len(taken) if ours else 0) < cfg.max_warm_lanes:
        return [CheckResult("displays", "Displays and ports", FAIL,
                            f"{free} of {cfg.max_slots} slots free, fewer than max_warm_lanes "
                            f"{cfg.max_warm_lanes}; taken: {', '.join(taken)}",
                            "stop what holds those displays and ports, or move the ranges with "
                            "`openajar node init --display-base N --viewer-base N`", data=data)]
    if taken:
        return [CheckResult("displays", "Displays and ports", WARN if not ours else PASS,
                            f"{free} of {cfg.max_slots} slots free; taken: {', '.join(taken)}{note}",
                            None if ours else "a foreign process holds part of the range; lanes skip those slots",
                            data=data)]
    return [CheckResult("displays", "Displays and ports", PASS, f"all {cfg.max_slots} slots free ({ranges})",
                        data=data)]


def check_service(ctx: DoctorContext) -> list[CheckResult]:
    props = _systemctl_show(ctx, ["LoadState", "ActiveState", "SubState", "UnitFileState", "User", "MainPID",
                                  "NRestarts", "ExecMainStartTimestamp"])
    if props is None:
        return [CheckResult("service", "agentosd service", SKIP, "systemd is not available here")]
    if props.get("LoadState") == "not-found":
        return [CheckResult("service", "agentosd service", WARN, f"{ctx.unit}.service is not installed",
                            "sudo deploy/install-node.sh --systemd (a dev install runs `openajar node serve`)",
                            data=props)]
    active, sub = props.get("ActiveState", "?"), props.get("SubState", "?")
    user = props.get("User") or "root"
    since = props.get("ExecMainStartTimestamp") or ""
    restarts = props.get("NRestarts", "0")
    data = {k: props.get(k) for k in ("ActiveState", "SubState", "UnitFileState", "User", "NRestarts")}
    out = []
    if active != "active":
        out.append(CheckResult("service", "agentosd service", FAIL, f"{ctx.unit} is {active} ({sub})",
                               f"sudo systemctl restart {ctx.unit}; journalctl -u {ctx.unit} -n 50 --no-pager",
                               data=data))
    else:
        extra = f", restarted {restarts} time(s)" if restarts not in ("", "0") else ""
        out.append(CheckResult("service", "agentosd service", PASS,
                               f"{ctx.unit} active ({sub}) as {user}{f' since {since}' if since else ''}{extra}",
                               data=data))
    if props.get("UnitFileState") not in ("enabled", "enabled-runtime", "static", None, ""):
        out.append(CheckResult("service.enabled", "agentosd at boot", WARN,
                               f"{ctx.unit} is {props.get('UnitFileState')}, so it will not start at boot",
                               f"sudo systemctl enable {ctx.unit}"))
    if user == "root":
        out.append(CheckResult("service.user", "agentosd user", WARN, f"{ctx.unit} runs as root",
                               "sudo openajar migrate-service-user (reversible: --rollback)"))
    return out


MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "openajar-doctor", "version": __version__}}}


def check_mcp(ctx: DoctorContext) -> list[CheckResult]:
    cfg = ctx.config
    if cfg is None or not cfg.mcp_token:
        return [CheckResult("mcp", "MCP endpoint", SKIP, "no node.toml or no token")]
    where = f"127.0.0.1:{cfg.mcp_port}"
    if not ctx.host.port_in_use(cfg.mcp_port):
        props = _systemctl_show(ctx, ["ActiveState"]) or {}
        status = FAIL if props.get("ActiveState") == "active" else WARN
        return [CheckResult("mcp", "MCP endpoint", status, f"nothing listens on {where}",
                            f"sudo systemctl restart {ctx.unit} (or run `openajar node serve`)")]
    url = f"http://127.0.0.1:{cfg.mcp_port}/mcp"
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Host": where}
    body = json.dumps(MCP_INIT).encode()
    try:
        anon, _, _ = ctx.host.http_request("POST", url, headers=base, body=body, timeout=ctx.timeout)
        auth = {**base, "Authorization": f"Bearer {cfg.mcp_token}"}
        authed, headers, _ = ctx.host.http_request("POST", url, body=body, timeout=ctx.timeout, headers=auth)
        session = headers.get("mcp-session-id")
        if session:  # end the session this probe opened
            with contextlib.suppress(OSError):
                ctx.host.http_request("DELETE", url, timeout=ctx.timeout,
                                      headers={**auth, "Mcp-Session-Id": session})
    except OSError as e:
        return [CheckResult("mcp", "MCP endpoint", FAIL,
                            f"{where} accepts connections but the request failed: "
                            f"{redact(str(e) or type(e).__name__, cfg.mcp_token)}",
                            f"journalctl -u {ctx.unit} -n 50 --no-pager")]
    if anon != 401:
        return [CheckResult("mcp", "MCP endpoint", FAIL,
                            f"{where} answered HTTP {anon} without a token (expected 401): it is not agentosd, "
                            "or auth is off", f"find what listens on port {cfg.mcp_port} and stop it")]
    if authed == 401:
        return [CheckResult("mcp", "MCP endpoint", FAIL,
                            f"{where} rejects the node.toml token (HTTP 401): the daemon runs with another token",
                            f"sudo systemctl restart {ctx.unit}")]
    if not 200 <= authed < 300:
        return [CheckResult("mcp", "MCP endpoint", FAIL, f"{where} answered HTTP {authed} to an initialize request",
                            f"journalctl -u {ctx.unit} -n 50 --no-pager")]
    return [CheckResult("mcp", "MCP endpoint", PASS,
                        f"MCP answers on {where} with the node.toml token; requests without it get 401")]


def _want(path: Path, mode: int, kind: str) -> tuple[Path, int, str]:
    return path, mode, kind


def check_permissions(ctx: DoctorContext) -> list[CheckResult]:
    home = ctx.home
    expected = [_want(home, 0o700, "dir"), _want(home / "node.toml", 0o600, "file"),
                _want(home / "secrets", 0o700, "dir"), _want(home / "openajar.db", 0o600, "file"),
                _want(home / "vault.db", 0o600, "file"), _want(home / "lanes", 0o700, "dir"),
                _want(home / "run", 0o700, "dir"), _want(home / "backups", 0o700, "dir")]
    secrets_dir = home / "secrets"
    if secrets_dir.is_dir() and not secrets_dir.is_symlink():
        expected += [_want(p, 0o600, "file") for p in sorted(secrets_dir.iterdir()) if not p.name.startswith(".")]
    for lane in lane_dirs(home):
        expected += [_want(lane.path.parent, 0o700, "dir"), _want(lane.path, 0o700, "dir"),
                     _want(lane.profile, 0o700, "dir"), _want(lane.fingerprint, 0o600, "file")]
    home_st = ctx.host.lstat(home)
    if home_st is None:
        return [CheckResult("permissions", "File permissions", FAIL, f"{home} does not exist",
                            f"OPENAJAR_HOME={home} openajar node init")]
    owner = home_st.st_uid
    problems: list[str] = []
    fixes: list[str] = []
    others: set[int] = set()
    seen: set[Path] = set()
    for path, mode, kind in expected:
        if path in seen:
            continue
        seen.add(path)
        st = ctx.host.lstat(path)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            problems.append(f"{path} is a symlink")
            fixes.append(f"replace the symlink {path} with the real {kind}")
            continue
        public = kind == "file" and path.parent == secrets_dir and path.suffix == ".pub"
        if st.st_mode & (0o022 if public else 0o077):  # a public key may be readable, never writable
            problems.append(f"{path} is {stat.S_IMODE(st.st_mode):o} (want {'644 or ' if public else ''}{mode:o})")
            fixes.append(f"chmod {mode:o} {path}")
        if st.st_uid != owner:
            others.add(st.st_uid)
            problems.append(f"{path} is owned by {ctx.host.user_name(st.st_uid)}, the home by "
                            f"{ctx.host.user_name(owner)}")
            fixes.append(f"chown {ctx.host.user_name(owner)}: {path}")
    if problems:
        fix = "; ".join(fixes[:4]) + (f"; or for everything: chmod -R go-rwx {home}" if len(fixes) > 4 else "")
        return [CheckResult("permissions", "File permissions", FAIL,
                            f"{len(problems)} problem(s): {'; '.join(problems[:5])}"
                            + (" ..." if len(problems) > 5 else ""), fix, data={"problems": problems})]
    return [CheckResult("permissions", "File permissions", PASS,
                        f"home, node.toml, secrets, databases and lanes are private to "
                        f"{ctx.host.user_name(owner)} (700/600)")]


def check_vault(ctx: DoctorContext) -> list[CheckResult]:
    db = ctx.home / "vault.db"
    if not ctx.host.exists(db):
        return [CheckResult("vault", "Vault", WARN, "no vault yet, so login() and vault_fill cannot work",
                            "openajar vault init")]
    try:
        conn = connect_ro(db)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return [CheckResult("vault", "Vault", FAIL, f"{db} cannot be read ({type(e).__name__})",
                            "restore vault.db from a backup")]
    if "wrapped_key" not in meta:
        return [CheckResult("vault", "Vault", WARN, "vault.db exists but holds no vault", "openajar vault init")]
    mode = meta.get("mode", "?")
    data: dict[str, Any] = {"mode": mode, "entries": entries}
    out = []
    if mode == "keyfile":
        key = ctx.home / "secrets" / "vault.key"
        if ctx.host.lstat(key) is None:
            out.append(CheckResult("vault.key", "Vault key file", FAIL, f"{key} is missing; the vault cannot unseal",
                                   "restore secrets/vault.key from a backup"))
    resp = ctx.host.unix_request(vault_socket(ctx.home), {"op": "status"}, timeout=5)
    state = (resp or {}).get("status", {}).get("state") if resp and resp.get("ok") else None
    data["daemon_state"] = state
    if state == "unsealed":
        out.insert(0, CheckResult("vault", "Vault", PASS, f"unsealed ({mode} mode, {entries} entries)", data=data))
    elif state == "sealed":
        out.insert(0, CheckResult("vault", "Vault", WARN, f"sealed ({mode} mode, {entries} entries): logins wait",
                                  "openajar vault unseal", data=data))
    else:
        out.insert(0, CheckResult("vault", "Vault", PASS,
                                  f"initialized ({mode} mode, {entries} entries); the daemon's vault socket does "
                                  "not answer, so the seal state is unknown", data=data))
    return out


def check_disk(ctx: DoctorContext) -> list[CheckResult]:
    out = []
    targets = [("disk.home", "Disk (OPENAJAR_HOME)", ctx.home), ("disk.tmp", "Disk (/tmp)", Path("/tmp"))]
    if ctx.host.same_filesystem(ctx.home, Path("/tmp")):
        targets = [("disk", "Disk (OPENAJAR_HOME and /tmp)", ctx.home)]
    for cid, title, path in targets:
        usage = ctx.host.disk_usage(path)
        if usage is None:
            out.append(CheckResult(cid, title, SKIP, f"cannot read the filesystem of {path}"))
            continue
        total, _, free = usage
        pct = 100.0 * free / total if total else 0.0
        detail = f"{_size(free)} free of {_size(total)} ({pct:.0f}%) under {path}"
        data = {"free": free, "total": total, "percent_free": round(pct, 1)}
        fix = (f"free space under {path}: openajar backup prune --keep 3, remove old lanes/*/*/shots, "
               "journalctl --vacuum-size=200M")
        fail, warn = DISK_FAIL, DISK_WARN
        if cid == "disk.tmp" and ctx.host.fstype(path) == "tmpfs":
            # /tmp in memory: small by design, no backups go there; only a nearly full one matters
            fail, warn = TMPFS_FAIL, TMPFS_WARN
            detail += " (tmpfs, in memory)"
            fix = "free space in /tmp (it is in memory): remove what fills it, or restart the service that does"
        if free < fail[0] or pct < fail[1]:
            out.append(CheckResult(cid, title, FAIL, detail, fix, data=data))
        elif free < warn[0] or pct < warn[1]:
            out.append(CheckResult(cid, title, WARN, detail, fix, data=data))
        else:
            out.append(CheckResult(cid, title, PASS, detail, data=data))
    return out


def signing_lanes(home: Path) -> list[str]:
    """Lanes with hosts to sign Web Bot Auth requests for (secrets/webbotauth/<ws>/<name>.json).
    Read-only, and only the host count: an unreadable file counts as not signing."""
    root = Path(home) / "secrets" / "webbotauth"
    out = []
    with contextlib.suppress(OSError):
        for path in sorted(root.glob("*/*.json")):
            with contextlib.suppress(OSError, ValueError, AttributeError):
                if json.loads(path.read_text()).get("hosts"):
                    out.append(f"{path.parent.name}/{path.stem}")
    return out


def check_clock(ctx: DoctorContext) -> list[CheckResult]:
    if not ctx.host.which("timedatectl"):
        return [CheckResult("clock", "Clock sync", SKIP, "timedatectl is not available here")]
    rc, out = ctx.host.run(["timedatectl", "show", "-p", "NTP", "-p", "NTPSynchronized", "-p", "Timezone"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if rc != 0 or not props:
        return [CheckResult("clock", "Clock sync", SKIP, "timedatectl gave no answer")]
    if props.get("NTPSynchronized") == "yes":
        return [CheckResult("clock", "Clock sync", PASS, f"synchronized by NTP (timezone {props.get('Timezone', '?')})")]
    # Web Bot Auth verifiers allow no future skew: a clock one second fast breaks every signature.
    signing = signing_lanes(ctx.home)
    also = (f"; {len(signing)} lane(s) sign requests, and verifiers reject a signature from a clock that runs "
            "ahead") if signing else ""
    data = {"signing_lanes": signing} if signing else {}
    if props.get("NTP") != "yes":
        return [CheckResult("clock", "Clock sync", WARN, f"NTP is off; TOTP codes and TLS need a correct clock{also}",
                            "sudo timedatectl set-ntp true", data=data)]
    return [CheckResult("clock", "Clock sync", WARN, f"NTP is on but the clock is not synchronized yet{also}",
                        "systemctl status systemd-timesyncd --no-pager", data=data)]


def check_memory(ctx: DoctorContext) -> list[CheckResult]:
    info = ctx.host.meminfo()
    if not info or "MemTotal" not in info:
        return [CheckResult("memory", "RAM and swap", SKIP, "no /proc/meminfo here")]
    total, avail = info["MemTotal"], info.get("MemAvailable", 0)
    swap, swap_free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    warm = ctx.config.max_warm_lanes if ctx.config else 3
    fits = max(1, int((total - BASE_RAM) // LANE_RAM))
    detail = (f"{_size(total)} RAM ({_size(avail)} available), swap {_size(swap) if swap else 'none'}; "
              f"max_warm_lanes {warm}, room for about {fits}")
    data = {"total": total, "available": avail, "swap": swap, "max_warm_lanes": warm, "recommended": fits}
    out = []
    if warm > fits:
        out.append(CheckResult("memory", "RAM and swap", WARN, detail,
                               f"set max_warm_lanes = {fits} in {config_path(ctx.home)} (each warm lane needs "
                               f"about {_size(LANE_RAM)}), then restart agentosd", data=data))
    elif avail < total * 0.10:
        out.append(CheckResult("memory", "RAM and swap", WARN, detail + "; less than 10% is available now",
                               "close lanes (browser_close) or lower max_warm_lanes", data=data))
    else:
        out.append(CheckResult("memory", "RAM and swap", PASS, detail, data=data))
    if swap and swap_free < swap * 0.5:
        out.append(CheckResult("memory.swap", "Swap use", WARN, f"{_size(swap - swap_free)} of swap in use",
                               "lower max_warm_lanes so lanes do not swap"))
    return out


def _service_memory_capped(props: dict[str, str]) -> bool:
    return any(props.get(name, "infinity") not in ("", "infinity") for name in ("MemoryMax", "MemoryHigh"))


def check_lane_limits(ctx: DoctorContext) -> list[CheckResult]:
    """The per-lane caps in node.toml are only in force where systemd made the lane's scope: it
    refuses transient scopes on the system manager to an unprivileged agentosd (the hardened unit's
    user), and the lane then runs without them (agentosd logs it once). The memory watchdog
    (openajar.node.memwatch) holds lanes to the same memory cap everywhere, scope or not."""
    title = "Per-lane caps"
    cfg = ctx.config
    # The default memory cap rides on the default scopes, which a capped or unprivileged service
    # never gets: there the service's own cap holds the browsers, and that is not a problem to report.
    default_cap = cfg is not None and cfg.lane_memory_max_mb is None and cfg.lane_scopes
    if cfg is None or not (cfg.lane_memory_max_mb or cfg.lane_cpu_max_pct or default_cap):
        return [CheckResult("lane_limits", title, SKIP, "no lane caps: lane_memory_max_mb is 0 or lane_scopes is off, "
                            "and lane_cpu_max_pct is unset")]
    memory = f"memory {cfg.lane_memory_max_mb} MB" if cfg.lane_memory_max_mb else ""
    if default_cap:
        memory = f"memory {LANE_MEMORY_DEFAULT_MB} MB (the default)"
    asked = ", ".join(p for p in (memory, f"CPU {cfg.lane_cpu_max_pct}% of a core" if cfg.lane_cpu_max_pct else "") if p)
    lanes_root = str(cfg.home / "lanes").rstrip("/") + "/"
    browsers = [pid for pid, argv in ctx.host.processes()
                if argv and os.path.basename(argv[0]) == BROWSER_BIN and "-contentproc" not in argv
                and any(a.startswith(lanes_root) for a in argv[1:])]
    shown = _systemctl_show(ctx, ["User", "MemoryMax", "MemoryHigh"])
    if default_cap and not cfg.lane_cpu_max_pct and shown is None:
        return [CheckResult("lane_limits", title, SKIP, "systemd is not available here, so lanes get no scopes")]
    props = shown or {}
    user = props.get("User") or "root"
    if default_cap and not cfg.lane_cpu_max_pct and (user != "root" or _service_memory_capped(props)):
        why = f"{ctx.unit} runs as {user}" if user != "root" else f"{ctx.unit} has a memory cap of its own"
        return [CheckResult("lane_limits", title, SKIP, f"the default lane memory cap is not used: {why}, so "
                            "lanes get no scopes and stay under the service's cap")]
    root_fix = (f"run {ctx.unit} as root, or remove the caps from {config_path(ctx.home)} and rely on the "
                "service's own MemoryMax (the hardened unit caps every browser with agentosd)")
    data = {"asked": asked, "warm": len(browsers), "user": user}
    if not browsers:
        if user != "root":
            return [CheckResult("lane_limits", title, WARN, f"{asked} set, but {ctx.unit} runs as {user}, and systemd "
                                "makes lane scopes only for root: the caps will not apply", root_fix, data=data)]
        return [CheckResult("lane_limits", title, PASS, f"{asked} set; no warm lane to check", data=data)]
    bare = [pid for pid in browsers if "/openajar-lane-" not in (ctx.host.cgroup(pid) or "")]
    data["without"] = len(bare)
    if bare:
        fix = root_fix if user != "root" else f"journalctl -u {ctx.unit} --no-pager | grep 'could not limit'"
        return [CheckResult("lane_limits", title, WARN, f"{asked} set, but {len(bare)} of {len(browsers)} warm lanes "
                            "run without them (systemd did not make their scopes)", fix, data=data)]
    return [CheckResult("lane_limits", title, PASS, f"{asked} in force on {len(browsers)} warm lanes", data=data)]


def _probe_proxy(ctx: DoctorContext, proxy: ProxyConfig) -> CheckResult:
    title = f"Proxy {proxy.label}"
    where = f"{proxy.host}:{proxy.port}"
    cid = f"proxy.{proxy.label}"
    try:
        took = ctx.host.tcp_connect(proxy.host, proxy.port, timeout=min(ctx.timeout, 10))
    except OSError as e:
        return CheckResult(cid, title, FAIL, f"cannot connect to {where} ({redact(str(e), proxy.password)})",
                           f"check the proxy is up and {where} is right: openajar proxy list")
    try:
        ip = ctx.host.proxy_exit_ip(proxy, timeout=ctx.timeout)
    except OSError as e:
        msg = redact(str(e), proxy.username, proxy.password)
        fix = (f"openajar proxy add {proxy.label} {where}:<user> (prompts for the password)"
               if "407" in msg else "check the proxy provider's status page")
        return CheckResult(cid, title, FAIL, f"{where} reachable, but no exit IP through it: {msg}", fix)
    expected = ctx.host.resolve(proxy.host) | {proxy.host}
    data = {"label": proxy.label, "host": proxy.host, "port": proxy.port, "exit_ip": ip,
            "connect_ms": int(took * 1000)}
    if ip not in expected:
        return CheckResult(cid, title, FAIL,
                           f"exit IP {ip} differs from the proxy host {proxy.host}; lanes on {proxy.label} would be "
                           "quarantined by their self-check", "ask the provider for a static exit, or point the "
                           f"proxy at its exit: openajar proxy add {proxy.label} <exit-host>:<port>:<user>", data=data)
    return CheckResult(cid, title, PASS, f"{where} answers in {int(took * 1000)} ms; exit IP {ip} matches", data=data)


def check_proxies(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.config is None:
        return [CheckResult("proxies", "Proxies", SKIP, "no node.toml")]
    try:
        proxies = load_proxies(ctx.config)
    except (OSError, ValueError) as e:
        return [CheckResult("proxies", "Proxies", FAIL, f"secrets/proxies.json cannot be read ({type(e).__name__})",
                            "re-add the proxies: openajar proxy add <label> host:port:user")]
    if not proxies:
        return [CheckResult("proxies", "Proxies", PASS, "no proxies configured (lanes exit from the node's IP)")]
    if not ctx.network:
        return [CheckResult("proxies", "Proxies", SKIP, f"{len(proxies)} configured; not probed (--offline)")]
    ordered = sorted(proxies.values(), key=lambda p: p.label)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ordered))) as pool:
        return list(pool.map(lambda p: _probe_proxy(ctx, p), ordered))


def check_migrations(ctx: DoctorContext) -> list[CheckResult]:
    from openajar.core import migrate

    try:
        statuses = migrate.status(ctx.home)
    except Exception as e:  # noqa: BLE001
        return [CheckResult("migrations", "Database schemas", FAIL, f"cannot read schema versions ({e})",
                            "openajar migrate status")]
    newer = [s for s in statuses if s.newer]
    pending = [s for s in statuses if s.pending and s.exists]
    if newer:
        names = ", ".join(f"{s.db} ({s.package} {s.current} > {s.latest})" for s in newer)
        return [CheckResult("migrations", "Database schemas", FAIL, f"written by a newer openajar: {names}",
                            "upgrade openajar (openajar upgrade), or restore the databases from a backup")]
    if pending:
        names = ", ".join(f"{s.db} ({s.package} {s.current} -> {s.latest})" for s in pending)
        return [CheckResult("migrations", "Database schemas", WARN, f"pending migrations: {names}",
                            "openajar migrate run (it backs up the databases first)")]
    return [CheckResult("migrations", "Database schemas", PASS,
                        f"{len([s for s in statuses if s.exists])} database(s) at their latest schema"),
            *_check_lane_formats(ctx)]


def _check_lane_formats(ctx: DoctorContext) -> list[CheckResult]:
    from openajar.node import lanefmt

    name = "Lane directory formats"
    try:
        lanes = lanefmt.lane_status(ctx.home)
    except Exception as e:  # noqa: BLE001
        return [CheckResult("lane_format", name, FAIL, f"cannot read lane formats ({e})", "openajar migrate status")]
    bad = [s for s in lanes if s.error or s.newer]
    if bad:
        names = ", ".join(f"{s.lane} ({s.error or f'format {s.current} > {s.latest}'})" for s in bad)
        return [CheckResult("lane_format", name, FAIL, f"lanes this openajar cannot open: {names}",
                            "upgrade openajar, or restore the lanes from a backup")]
    behind = [s for s in lanes if s.current < s.latest]
    if behind:
        names = ", ".join(f"{s.lane} ({s.current} -> {s.latest})" for s in behind)
        return [CheckResult("lane_format", name, WARN, f"lanes with format migrations pending: {names}",
                            "openajar migrate run (it backs up what it changes; warm lanes are left alone)")]
    unstamped = sum(1 for s in lanes if not s.stamped)
    note = f"; {unstamped} not stamped yet (openajar migrate run stamps them)" if unstamped else ""
    return [CheckResult("lane_format", name, PASS, f"{len(lanes)} lane(s) at format {lanefmt.LATEST}{note}")]


def check_backups(ctx: DoctorContext) -> list[CheckResult]:
    """The newest backup wherever it went: the default folder, and every place `openajar backup create
    --to` sent one (the node notes each backup it makes, openajar.ops.backup.record_made). A noted
    folder is listed again, so a deleted archive does not count; an s3:// one is taken as noted."""
    from openajar.ops.backup import list_archives, parse_name, read_made

    root = default_backup_dir(ctx.home)
    found: dict[str, tuple[float, str]] = {}  # name or URL -> (created, where)
    folders = {root}
    remote: list[tuple[float, str]] = []
    for row in read_made(ctx.home):
        location = row["location"]
        if location.startswith("s3://"):
            parsed = parse_name(location.rsplit("/", 1)[-1])
            if parsed:
                remote.append((parsed[1].timestamp(), location))
        else:
            folders.add(Path(location).parent)
    for folder in sorted(folders):
        for archive in list_archives(folder):
            found[str(archive.path)] = (archive.created.timestamp(), str(folder))
    for created, location in remote:
        found[location] = (created, location.rsplit("/", 1)[0])
    if not found:
        return [CheckResult("backups", "Backups", WARN, f"no backup in {root} or anywhere this node sent one; "
                            "a dead disk would lose every login",
                            "openajar backup create --to /mnt/backup (or --to s3://bucket/openajar)")]
    newest_at, newest = max((created, name) for name, (created, _) in found.items())
    places = sorted({where for _, where in found.values()})
    age_days = (ctx.host.now() - newest_at) / 86400
    detail = (f"{len(found)} in {', '.join(places)}, newest {newest.rsplit('/', 1)[-1]} "
              f"({age_days:.1f} days old)")
    if age_days > BACKUP_MAX_AGE_DAYS:
        return [CheckResult("backups", "Backups", WARN, detail, "openajar backup create")]
    off_disk = any(not Path(p).resolve().is_relative_to(ctx.home.resolve()) for p in places if "://" not in p)
    off_disk = off_disk or any("://" in p for p in places)
    return [CheckResult("backups", "Backups", PASS, detail + ("" if off_disk else "; keep a copy off this disk too"))]


def check_workspace_memory(ctx: DoctorContext) -> list[CheckResult]:
    """Every workspace memory file opens, passes `PRAGMA quick_check` and has a known schema
    (docs/design/workspace-memory.md 4.1). Read-only: nothing is created or migrated."""
    from openajar.memory.store import DB_SUFFIX, SCHEMA_VERSION

    root = ctx.home / "memory"
    files = sorted(root.glob(f"*{DB_SUFFIX}")) if root.is_dir() else []
    if not files:
        return [CheckResult("workspace_memory", "Workspace memory", SKIP, "no workspace has memory yet")]
    bad: list[str] = []
    for path in files:
        try:
            db = connect_ro(path)
            try:
                ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                row = db.execute("SELECT version FROM schema_version").fetchone()
            finally:
                db.close()
        except sqlite3.Error as e:
            bad.append(f"{path.name} ({e})")
            continue
        if not ok:
            bad.append(f"{path.name} (the integrity check failed)")
        elif row is None or row[0] > SCHEMA_VERSION:
            bad.append(f"{path.name} (schema {row[0] if row else 'missing'}; this build reads {SCHEMA_VERSION})")
    if bad:
        return [CheckResult("workspace_memory", "Workspace memory", FAIL, "cannot be read: " + ", ".join(bad),
                            "restore the file from a backup (openajar backup restore), or upgrade openajar")]
    return [CheckResult("workspace_memory", "Workspace memory", PASS,
                        f"{len(files)} workspace file(s) open and pass the integrity check; "
                        "openajar memory verify checks their hash chains")]


def client_connections(ctx: DoctorContext) -> list[Any]:
    """The connections `openajar connect` made on this computer (empty when the caller gave no reader)."""
    return ctx.read_connections(ctx.client_home) if ctx.read_connections is not None else []


EXPIRY_WARN_DAYS = 14


def check_connections(ctx: DoctorContext) -> list[CheckResult]:
    """On the owner's computer: which credential each connection's agents hold. The node token
    there is a warning: a copy of it is the whole node, and the owner cannot approve from this
    computer what agents using it ask (a token never decides its own family's requests)."""
    conns = client_connections(ctx)
    if not conns:
        return [CheckResult("connections", "Connections", SKIP, "no `openajar connect` connections on this computer")]
    out = []
    now = datetime.now(UTC)
    for c in conns:
        title = f"Connection {c.name}"
        if c.credential == "node_token":
            out.append(CheckResult(
                "connections", title, WARN,
                "its agents hold the node's own token: a copy of it opens the whole node, and you cannot approve "
                "from this computer what they ask",
                f"openajar connect --upgrade, then once every connection has its own grant: "
                f"ssh {c.ssh_host} openajar node rotate-token", data={"name": c.name, "credential": c.credential}))
            continue
        if c.credential == "controller":
            out.append(CheckResult("connections", title, PASS, "a controller: its agents hold its agent token",
                                   data={"name": c.name, "credential": c.credential}))
            continue
        data = {"name": c.name, "credential": c.credential, "grant": c.agent_grant_id,
                "expires_at": c.agent_expires_at}
        detail = f"agents use grant {c.agent_grant_id} ({c.agent_subject}, up to {c.agent_ceiling})"
        try:
            ends = datetime.fromisoformat(str(c.agent_expires_at)) if c.agent_expires_at else None
        except ValueError:
            ends = None
        if ends is not None and (ends - now).days < EXPIRY_WARN_DAYS:
            past = ends <= now
            out.append(CheckResult("connections", title, WARN,
                                   f"{detail}; it {'expired' if past else 'expires'} on {ends.date().isoformat()}",
                                   f"openajar connect {c.ssh_host} --name {c.name} --new-grant", data=data))
            continue
        out.append(CheckResult("connections", title, PASS, detail + (f", until {ends.date().isoformat()}" if ends
                                                                        else ""), data=data))
    return out


def check_keepalive(ctx: DoctorContext) -> list[CheckResult]:
    """Session keepalive (openajar.keepalive) opens real accounts unattended, so it is off until the owner
    turns it on; say which it is either way."""
    if ctx.config is None:
        return [CheckResult("keepalive", "Session keepalive", SKIP, "no node.toml")]
    if not ctx.config.keepalive:
        return [CheckResult("keepalive", "Session keepalive", PASS,
                            "off (the default): no lane is visited unattended, so a lane left idle for weeks may "
                            "come back to a checkpoint. `openajar lane keepalive --node on` turns it on",
                            data={"enabled": False})]
    return [CheckResult("keepalive", "Session keepalive", PASS,
                        "on: each idle lane gets a short read-only visit every few days, inside its waking hours "
                        "(`openajar lane keepalive <lane>` shows the plan, --off opts a lane out)",
                        data={"enabled": True})]


def _lane_specs(ctx: DoctorContext) -> list[Any]:
    """The lanes' specs, read only (an unreadable database or spec is skipped: the lane checks say so)."""
    from openajar.core.models import LaneSpec

    db = ctx.home / "openajar.db"
    if not db.exists():
        return []
    out = []
    conn = connect_ro(db)
    try:
        for (raw,) in conn.execute("SELECT spec FROM lanes ORDER BY id").fetchall():
            with contextlib.suppress(ValueError):
                out.append(LaneSpec.model_validate_json(raw))
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return out


def check_effect_rules(ctx: DoctorContext) -> list[CheckResult]:
    """Effect rule health (openajar.node.rulehealth, docs/design/egress-guard.md 8.12): a verified rule
    that went quiet while its site sends unknown writes, a burst of new unknown writes, a lane on a site
    with no rules. Read from the lanes' egress traces (names only); a warning, never a failure."""
    from openajar.node import rulehealth

    title = "Effect rules"
    if not lane_dirs(ctx.home):
        return [CheckResult("effect_rules", title, SKIP, "no lanes yet")]
    done = rulehealth.for_node(ctx.home, _lane_specs(ctx), now=ctx.host.now())
    flags = done["flags"]
    if flags:
        return [CheckResult("effect_rules", title, WARN, f["message"],
                            "openajar lane rules (and openajar lane gate-log <lane> to name a shape)"
                            if f["kind"] != "no_rules" else
                            "openajar lane rules; keep agents on that site to what a person approves by hand",
                            data={k: f.get(k) for k in ("kind", "site", "rule", "lanes", "shapes")})
                for f in flags]
    matched = sum(1 for r in done["rules"] if r.get("last"))
    return [CheckResult("effect_rules", title, PASS,
                        f"no drift seen; {matched} of {len(done['rules'])} commit and write rules matched in the "
                        f"egress traces" + (f" (since {done['since']})" if done.get("since") else ""),
                        data={"matched": matched, "rules": len(done["rules"])})]


def check_env_lanes(ctx: DoctorContext) -> list[CheckResult]:
    """Terminal lanes with no sandbox (they can read the vault and the node token) and sandboxed lanes
    this node cannot run (openajar.env.exposure; QA 2026-11-10, findings 29 and 31)."""
    from openajar.env import exposure
    status = {"warn": WARN, "fail": FAIL, "pass": PASS}
    return [CheckResult(r["id"], r["title"], status[r["status"]], r["detail"], r.get("fix"), data=r.get("data") or {})
            for r in exposure.doctor_rows(ctx.home)]


CHECKS: dict[str, Check] = {
    "config": check_config,
    "os": check_os,
    "python": check_python,
    "packages": check_packages,
    "camoufox": check_camoufox,
    "displays": check_displays,
    "service": check_service,
    "mcp": check_mcp,
    "permissions": check_permissions,
    "vault": check_vault,
    "disk": check_disk,
    "clock": check_clock,
    "memory": check_memory,
    "lane_limits": check_lane_limits,
    "env_lanes": check_env_lanes,
    "proxies": check_proxies,
    "migrations": check_migrations,
    "workspace_memory": check_workspace_memory,
    "backups": check_backups,
    "keepalive": check_keepalive,
    "effect_rules": check_effect_rules,
    "connections": check_connections,
    "claude_profiles": lambda ctx: ctx.claude_profiles(ctx) if ctx.claude_profiles is not None else [],
    "web": check_web,
}
CLIENT_CHECKS = ["connections", "claude_profiles"]  # what runs on a computer that is not a node but connects to nodes


@dataclass
class Report:
    node: str | None
    home: str
    results: list[CheckResult]
    generated_at: str

    @property
    def counts(self) -> dict[str, int]:
        counts = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for r in self.results:
            counts[r.status] += 1
        return counts

    @property
    def ok(self) -> bool:
        return not any(r.status == FAIL for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "home": self.home, "generated_at": self.generated_at, "ok": self.ok,
                "summary": self.counts, "checks": [r.to_dict() for r in self.results]}


def run(ctx: DoctorContext, only: list[str] | None = None) -> Report:
    unknown = [name for name in only or [] if name not in CHECKS]
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)} (known: {', '.join(CHECKS)})")
    results: list[CheckResult] = []
    for name, check in CHECKS.items():
        if only and name not in only:
            continue
        try:
            results.extend(check(ctx))
        except Exception as e:  # noqa: BLE001 - one broken probe must not hide the rest
            results.append(CheckResult(name, name, FAIL, f"the check itself failed: {type(e).__name__}",
                                       "report this as a bug (openajar doctor --json shows the rest)"))
    return Report(node=ctx.config.name if ctx.config else None, home=str(ctx.home), results=results,
                  generated_at=datetime.now(UTC).isoformat(timespec="seconds"))


def format_text(report: Report, *, color: bool = False) -> str:
    labels = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
    colors = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[2m"}
    lines = [f"openajar doctor: node {report.node or '(not initialized)'}, home {report.home}", ""]
    width = max((len(r.title) for r in report.results), default=10)
    for r in report.results:
        label = labels[r.status]
        if color:
            label = f"{colors[r.status]}{label}\033[0m"
        lines.append(f"{label}  {r.title.ljust(width)}  {r.detail}")
        if r.fix and r.status in (WARN, FAIL):
            lines.append(f"      {' ' * width}  fix: {r.fix}")
    c = report.counts
    lines += ["", f"{c[PASS]} passed, {c[WARN]} warnings, {c[FAIL]} failed, {c[SKIP]} skipped"]
    return "\n".join(lines)
