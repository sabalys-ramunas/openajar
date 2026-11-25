"""Every probe ops makes of the machine, behind one class so each check runs against a fake.

`Host` is the real implementation (Linux first; harmless answers on a Mac). Unit tests pass
a subclass with canned answers. Rules: no probe prints, logs or returns a credential; errors
that could echo one are reduced to their type and a fixed message.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.core import proc
from openajar.core.layout import BROWSER_BIN
from openajar.core.models import ProxyConfig

EXIT_IP_URLS = ("https://ipinfo.io/ip", "https://api.ipify.org")
# The same service's JSON answer: the exit IP plus where ipinfo places it. The lane check asks for
# it through a proxy at most once a day per proxy (docs/API.md, "Lane check"); no cookie or
# account data is ever sent with it.
EXIT_INFO_URL = "https://ipinfo.io/json"
EXIT_INFO_KEYS = ("ip", "country", "region", "city", "timezone", "loc")  # loc: "latitude,longitude"
# A second answer on where the exit is, asked through the proxy only when ipinfo's leaves something out
# (openajar.environments.browserproxy): its keys are mapped to ipinfo's.
EXIT_GEO_URL = "https://get.geojs.io/v1/ip/geo.json"
EXIT_GEO_KEYS = {"ip": "ip", "country_code": "country", "region": "region", "city": "city", "timezone": "timezone"}
X_SOCKET_DIR = Path("/tmp/.X11-unix")
MEMINFO = Path("/proc/meminfo")
MiB = 1024 * 1024

# Read only: never ensure_fpgen_model() (it writes) and never `import fpgen` (it fetches a model).
_FPGEN_PROBE = r"""
import json
out = {}
try:
    from camoufox.fpgen_model import fpgen_data_dir, is_pinned
    data = fpgen_data_dir()
    out["dir"] = str(data)
    out["ok"] = bool(is_pinned(data))
    if not out["ok"]:
        out["error"] = "the pinned model is not there (or not complete)"
except PermissionError as e:
    out["error"] = f"cannot read {e.filename}"
except Exception as e:
    out["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
"""

_CAMOUFOX_PROBE = r"""
import json
out = {}
try:
    # Read-only on purpose: camoufox_path() runs remove_incompatible_data() and may rewrite
    # config.json, so resolve the build the library would launch (its pin, else the active
    # entry, else the first install) and read that build's version.json.
    from camoufox.multiversion import INSTALL_DIR, list_installed, load_config
    config = load_config()
    path = None
    try:
        from camoufox.browser_pin import effective_pin, matches
        pin = effective_pin(config)
    except Exception:
        pin = None
    installed = list_installed()
    if pin:
        for inst in installed:
            if matches(pin, inst.repo_name, inst.version.version or "", inst.version.build):
                path = inst.path
                break
    if path is None and config.get("active_version"):
        cand = INSTALL_DIR / config["active_version"]
        if (cand / "version.json").exists():
            path = cand
    if path is None and installed and not pin:
        path = installed[0].path
    if path is None:
        out["error"] = "no Camoufox browser installed under " + str(INSTALL_DIR)
    else:
        meta = json.loads((path / "version.json").read_text())
        out["path"] = str(path)
        out["verstr"] = meta["version"] + ("-" + meta["build"] if meta.get("build") else "")
except Exception as e:
    text = str(e).splitlines()[0][:200] if str(e) else ""
    out["error"] = type(e).__name__ + (": " + text if text else "")
print(json.dumps(out))
"""


def ipv4_first(address: tuple[str, int], timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
               source_address: Any = None, **_: Any) -> socket.socket:
    """socket.create_connection that tries the host's IPv4 addresses first, then the system's own order: on a
    dual stack connection a request then leaves from the IPv4 address, the one most sites see."""
    host, port = address[0], address[1]
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        try:
            return socket.create_connection((str(info[4][0]), port), timeout, source_address)
        except OSError:
            continue
    return socket.create_connection((host, port), timeout, source_address)


def redact(text: str, *secrets: str | None) -> str:
    """Replace every occurrence of each secret with ***. The longest secret goes first, so a short one
    (a username) inside a longer one (a password that starts with it) cannot leave a piece behind."""
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def read_meminfo(path: Path = MEMINFO) -> dict[str, int] | None:
    """/proc/meminfo in bytes (MemTotal, MemAvailable, SwapTotal, SwapFree, ...), or None where
    there is no /proc (a Mac running `openajar dev up`) or it cannot be read. The one reader of it
    in openajar: the lane manager's memory floor, the per-lane ceiling, the health service, doctor,
    the node's status for the panel and the controller, and the stealth lab all go through here
    (each in the units and with the fallback it needs)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    out: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def meminfo_mb(key: str, path: Path = MEMINFO) -> int | None:
    """One /proc/meminfo field in MB (MemAvailable, MemTotal, ...), or None without it."""
    info = read_meminfo(path)
    return info[key] // MiB if info and key in info else None


class Host:
    """The real machine."""

    # ---- identity -------------------------------------------------------------

    def platform(self) -> dict[str, str]:
        info = {"system": platform.system(), "machine": platform.machine(), "release": platform.release()}
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                key, _, value = line.partition("=")
                if key in ("ID", "VERSION_ID", "PRETTY_NAME"):
                    info[key.lower()] = value.strip().strip('"')
        except OSError:
            pass
        return info

    def python(self) -> dict[str, Any]:
        return {
            "version": tuple(sys.version_info[:3]),
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "executable": sys.executable,
        }

    def euid(self) -> int:
        return os.geteuid()

    def user_name(self, uid: int) -> str:
        try:
            import pwd

            return pwd.getpwuid(uid).pw_name
        except (KeyError, ImportError):
            return str(uid)

    def hostname(self) -> str:
        return socket.gethostname()

    def now(self) -> float:
        return time.time()

    # ---- files ----------------------------------------------------------------

    def lstat(self, path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except OSError:
            return None

    def exists(self, path: Path) -> bool:
        return Path(path).exists()

    def disk_usage(self, path: Path) -> tuple[int, int, int] | None:
        """(total, used, free) bytes of the filesystem holding path (its nearest existing parent)."""
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            u = shutil.disk_usage(p)
        except OSError:
            return None
        return u.total, u.used, u.free

    def fstype(self, path: Path) -> str | None:
        """The filesystem type of the mount holding path (tmpfs, ext4, ...), from /proc/mounts, or None."""
        try:
            target = os.path.realpath(path)
            best, kind = "", None
            for line in Path("/proc/mounts").read_text().splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount = parts[1].replace("\\040", " ")
                inside = target == mount or target.startswith(mount.rstrip("/") + "/")
                if inside and len(mount) >= len(best):
                    best, kind = mount, parts[2]
            return kind
        except OSError:
            return None

    def same_filesystem(self, a: Path, b: Path) -> bool:
        try:
            return os.stat(a).st_dev == os.stat(b).st_dev
        except OSError:
            return False

    # ---- packages and commands ---------------------------------------------------

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str], timeout: float = 10.0, env: dict[str, str] | None = None) -> tuple[int, str]:
        """(exit code, stdout). 127 when the program is missing, 124 on timeout."""
        try:
            proc = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout, env=env,
                                  stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return 127, ""
        except subprocess.TimeoutExpired:
            return 124, ""
        except OSError:
            return 126, ""
        return proc.returncode, proc.stdout

    def dpkg_installed(self, packages: list[str]) -> dict[str, bool] | None:
        """Which Debian packages are installed, or None without dpkg."""
        if not self.which("dpkg-query"):
            return None
        rc, out = self.run(["dpkg-query", "-W", "-f", "${Package} ${Status}\n", *packages])
        if rc not in (0, 1):
            return None
        status = {pkg: False for pkg in packages}
        for line in out.splitlines():
            name, _, rest = line.partition(" ")
            if name in status and rest.strip().endswith("install ok installed"):
                status[name] = True
        return status

    def camoufox(self, cache_home: Path | None = None) -> dict[str, str]:
        """The Camoufox browser this node would launch: {"path", "verstr"} or {"error"}.

        Runs in a subprocess so XDG_CACHE_HOME can point at the service user's cache.
        """
        env = dict(os.environ)
        if cache_home is not None:
            env["XDG_CACHE_HOME"] = str(cache_home)
        rc, out = self.run([sys.executable, "-I", "-c", _CAMOUFOX_PROBE], timeout=60, env=env)
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"the Camoufox probe failed (exit {rc})"}

    def fpgen_model(self, user: str | None = None) -> dict[str, Any]:
        """Is Camoufox's pinned fingerprint model in place and readable: {"ok", "dir"} or {"error",
        "dir"?}. With `user` (root only), checked as that user, the one agentosd runs as."""
        argv = [sys.executable, "-I", "-c", _FPGEN_PROBE]
        if user:
            argv = ["runuser", "-u", user, "--", *argv]
        rc, out = self.run(argv, timeout=60)
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"the fingerprint model check could not run (exit {rc})"}

    # ---- memory ---------------------------------------------------------------

    def meminfo(self) -> dict[str, int] | None:
        """/proc/meminfo in bytes (read_meminfo)."""
        return read_meminfo()

    # ---- processes, displays and ports ------------------------------------------

    def processes(self) -> list[tuple[int, list[str]]]:
        proc = Path("/proc")
        out: list[tuple[int, list[str]]] = []
        if not proc.is_dir():
            return out
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if raw:
                out.append((int(entry.name), [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")]))
        return out

    def pid_alive(self, pid: int) -> bool:
        return proc.pid_alive(pid)

    def environ(self, pid: int) -> bytes | None:
        """A process's environment block (NUL separated), or None when it cannot be read."""
        try:
            return Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return None

    def cgroup(self, pid: int) -> str | None:
        """A process's cgroup v2 path ("/system.slice/agentosd.service"), or None."""
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        except OSError:
            return None
        return next((line[3:] for line in lines if line.startswith("0::")), None)

    def browser_pids(self, profile: Path) -> list[int]:
        """camoufox-bin processes (any kind) that have this profile on their command line."""
        forms = {str(profile), str(Path(profile).resolve())}
        return [pid for pid, argv in self.processes()
                if argv and os.path.basename(argv[0]) == BROWSER_BIN and forms.intersection(argv)]

    def listening_ports(self) -> set[int] | None:
        ports: set[int] = set()
        found = False
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(name).read_text().splitlines()[1:]
            except OSError:
                continue
            found = True
            for line in lines:
                parts = line.split()
                if len(parts) > 3 and parts[3] == "0A":
                    ports.add(int(parts[1].rsplit(":", 1)[1], 16))
        return ports if found else None

    def port_in_use(self, port: int) -> bool:
        ports = self.listening_ports()
        if ports is not None:
            return port in ports
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def display_in_use(self, num: int) -> bool:
        """An X server holds display :num (live lock file, socket file, or abstract socket)."""
        lock = Path(f"/tmp/.X{num}-lock")
        try:
            pid = int(lock.read_text().strip())
            if pid and self.pid_alive(pid):
                return True
        except (OSError, ValueError):
            pass
        sock = X_SOCKET_DIR / f"X{num}"
        if sock.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                try:
                    s.connect(str(sock))
                    return True
                except OSError:
                    pass
        try:  # PrivateTmp hides the socket file, not the abstract socket
            return f"@/tmp/.X11-unix/X{num}" in Path("/proc/net/unix").read_text()
        except OSError:
            return False

    # ---- network --------------------------------------------------------------

    def resolve(self, host: str) -> set[str]:
        try:
            return {str(ipaddress.ip_address(host))}
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return set()
        return {str(ipaddress.ip_address(i[4][0])) for i in infos}

    def tcp_connect(self, host: str, port: int, timeout: float = 5.0) -> float:
        """Seconds a TCP connect took. Raises OSError."""
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return time.monotonic() - start

    def http_request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                     body: bytes | None = None, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(url)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=timeout)  # type: ignore[arg-type]  # None fails in http.client
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read(64 * 1024)
        finally:
            conn.close()

    def _through_proxy(self, proxy: ProxyConfig | None, url: str, *, timeout: float, accept: str) -> str:
        """GET an HTTPS URL through the proxy (HTTP CONNECT); the body's first 4 KiB as text. No proxy:
        straight from this computer, over IPv4 when it has a route (ipv4_first). Raises OSError with a
        message that never contains the proxy's credentials."""
        target = urlsplit(url)
        if proxy is None:
            conn = http.client.HTTPSConnection(target.hostname or "", target.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
            conn._create_connection = ipv4_first  # type: ignore[attr-defined]  # http.client connects with it
        else:
            headers = {}
            if proxy.username:
                token = base64.b64encode(f"{proxy.username}:{proxy.password or ''}".encode()).decode()
                headers["Proxy-Authorization"] = f"Basic {token}"
            conn = http.client.HTTPSConnection(proxy.host, proxy.port, timeout=timeout,
                                               context=ssl.create_default_context())
            conn.set_tunnel(target.hostname or "", target.port or 443, headers=headers)
        try:
            conn.request("GET", target.path or "/", headers={"User-Agent": "openajar-ops", "Accept": accept})
            resp = conn.getresponse()
            text = resp.read(4096).decode(errors="replace").strip()
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status} from {target.hostname}")
            return text
        except (OSError, ValueError, http.client.HTTPException) as e:
            message = redact(str(e) or type(e).__name__, *((proxy.username, proxy.password) if proxy else ()))
            if "407" in message:
                raise OSError("the proxy rejected its credentials (HTTP 407)") from None
            raise OSError(message) from None
        finally:
            conn.close()

    def proxy_exit_ip(self, proxy: ProxyConfig, *, timeout: float = 15.0) -> str:
        """The public IP a request through this proxy comes from (HTTP CONNECT, then HTTPS).

        Raises OSError with a message that never contains the proxy's credentials.
        """
        last: OSError | None = None
        for url in EXIT_IP_URLS:
            try:
                return str(ipaddress.ip_address(self._through_proxy(proxy, url, timeout=timeout,
                                                                    accept="text/plain")))
            except ValueError:
                last = OSError(f"no IP address in the answer from {urlsplit(url).hostname}")
            except OSError as e:
                if "407" in str(e):
                    raise
                last = e
        assert last is not None
        raise last

    def proxy_exit_info(self, proxy: ProxyConfig | None, *, timeout: float = 15.0) -> dict[str, str]:
        """The exit IP through this proxy and where ipinfo.io places it, in one request:
        {"ip", "country", "region", "city", "timezone", "loc"} (only the keys ipinfo gave). No proxy: this
        computer's own exit, over IPv4 when it has one. Raises OSError."""
        text = self._through_proxy(proxy, EXIT_INFO_URL, timeout=timeout, accept="application/json")
        try:
            data = json.loads(text)
            ip = str(ipaddress.ip_address(str(data.get("ip", "")).strip()))
        except (ValueError, AttributeError):
            raise OSError("no exit IP in the answer from ipinfo.io") from None
        out = {k: str(data[k]) for k in EXIT_INFO_KEYS if isinstance(data.get(k), str) and data[k]}
        out["ip"] = ip
        return out

    def proxy_exit_geo(self, proxy: ProxyConfig | None, *, timeout: float = 15.0) -> dict[str, str]:
        """The same as proxy_exit_info from a second service (geojs.io), in ipinfo's keys. Raises OSError."""
        text = self._through_proxy(proxy, EXIT_GEO_URL, timeout=timeout, accept="application/json")
        try:
            data = json.loads(text)
            ip = str(ipaddress.ip_address(str(data.get("ip", "")).strip()))
        except (ValueError, AttributeError):
            raise OSError("no exit IP in the answer from geojs.io") from None
        out = {ours: str(data[theirs]) for theirs, ours in EXIT_GEO_KEYS.items()
               if isinstance(data.get(theirs), str) and data[theirs]}
        try:
            out["loc"] = f"{float(data['latitude']):.4f},{float(data['longitude']):.4f}"
        except (KeyError, TypeError, ValueError):
            pass
        out["ip"] = ip
        return out

    def direct_exit_ip(self, *, timeout: float = 10.0) -> str:
        status, _, body = self.http_request("GET", EXIT_IP_URLS[0], timeout=timeout,
                                            headers={"User-Agent": "openajar-ops", "Accept": "text/plain"})
        if status != 200:
            raise OSError(f"HTTP {status}")
        return str(ipaddress.ip_address(body.decode().strip()))

    def unix_request(self, path: Path, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any] | None:
        """One JSON line to a local control socket; None when nothing answers."""
        from openajar.core.layout import trusted_socket

        try:
            trusted_socket(path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(str(path))
                s.sendall(json.dumps(payload).encode() + b"\n")
                chunks = []
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            return json.loads(b"".join(chunks) or b"{}")
        except (OSError, ValueError):
            return None
