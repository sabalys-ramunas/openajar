"""Your own machine as the computer of environments (vms.toml: provider = "own").

A person who already has a machine (a box at home, a server rented elsewhere, a VM at any cloud) registers
it once with `openajar environments machine add NAME user@host`, and from then on each environment is a
container on that machine, made the way the docker provider makes one on this computer (openajar.vms.docker:
the same image, the same walls, the same limits). Nothing is rented and nothing is billed.

What openajar needs from the machine: an SSH login that may run Docker (root, a member of the docker group,
or a login with passwordless sudo), Linux on x86-64 or arm64, Docker Engine, and an sshd that allows port
forwarding (the default).

What openajar puts on it, and nothing else:

- one line in that login's `~/.ssh/authorized_keys`, for a key openajar made for this machine (`restrict,
  port-forwarding`: commands and forwards, no terminal, no agent). Not when the person names a key of their
  own (`--key`): then nothing is written there;
- the environments image (`docker_image` in vms.toml), downloaded or built there;
- for each environment one container and one docker network, and for each save one image, all labelled as
  this openajar home's (agentos-home), which is how `machine remove` finds exactly them.

No daemon, no firewall rule, no port opened to the network: a container's sshd is published on the machine's
own loopback (`-p 127.0.0.1::22`), and the node reaches it through its SSH connection to the machine
(`Server.via`, a ProxyCommand in openajar.vms.ssh). openajar never stops, restarts or wipes the machine, and
never touches a container, image or network it did not make.

How it is reached: every docker call is `ssh <machine> docker ...` with the machine's key and its pinned
host key (no config file, no agent, no password: openajar.vms.ssh.ssh_base). The host key is read once at
`machine add` and confirmed by the person (or matched against the key their own ssh already trusts).

Several machines can be registered. New environments go to the one vms.toml's `location` names (`machine
use`); environments and saves on another machine keep working there: a server's id is `<machine>:<container
id>` and an image's `<machine>//<docker image>`, so every later call finds its machine, and a save resumes on
the machine that holds it. A machine that does not answer raises (it is never read as "its containers are
gone"), so an unreachable machine's environments wait instead of being ended.

What differs from a rented VM, said plainly: an environment shares the machine's kernel (a container, weaker
than a VM for code you do not trust); its saves' disk part lives in that machine's Docker; and when the
machine restarts, its open environments are gone and come back from their last save.

A machine can also be handed over as a whole (`machine add --whole`, Machine.mode "whole"): no Docker, the
agent is root on the machine itself, one environment at a time. That is openajar.vms.whole; this module
keeps the registry, the checks and the provider that both ways share.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from openajar.core.config import write_private
from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import utcnow
from openajar.env.models import RemoteHost
from openajar.vms.docker import CALL_TIMEOUT_S, DockerProvider
from openajar.vms.models import VmError, VmSettings, load_settings
from openajar.vms.provider import Server, ServerSpec
from openajar.vms.ssh import ssh_base

log = logging.getLogger("openajar.vms")

DIR = "machines"                 # under OPENAJAR_HOME/vms: one folder per machine
RECORD = "machine.json"
KEY = "id_ed25519"
KNOWN_HOSTS = "known_hosts"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
IMAGE_SEP = "//"                 # "<machine>//<docker image>": never part of a docker image's own name
SSH_FAILED = 255                 # ssh's own exit code: the machine was not reached (a command's is its own)
KEY_OPTIONS = "restrict,port-forwarding"  # what openajar's key may do on the machine: commands and forwards
MIN_FREE_GB = 10.0               # the image is about 4 GB, and each environment writes on top of it
ARCHES = {"x86_64": "x86", "amd64": "x86", "aarch64": "arm", "arm64": "arm"}
DOCKER_INSTALL = "curl -fsSL https://get.docker.com | sh"
DOCKER_INSTALL_S = 900.0
PULL_TIMEOUT_S = 3600.0
BUILD_TIMEOUT_S = 3600.0


class Machine(BaseModel):
    """One registered machine (OPENAJAR_HOME/vms/machines/<name>/machine.json). No secret is in it: the key
    is a file beside it (0600), or the person's own file, named by its path."""
    model_config = ConfigDict(extra="ignore")
    name: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    user: str
    key: str = ""                              # "": the key openajar made, beside this record; else a path
    docker: list[str] = Field(default_factory=lambda: ["docker"])  # how docker runs there (or: sudo -n docker)
    installed_key: bool = False                # openajar wrote its key's line there (remove takes it out)
    installed_image: bool = False              # openajar put the environments image there (remove takes it out)
    installed_docker: bool = False             # openajar installed Docker there, when its owner said yes (it stays)
    # "containers": each environment is a container in its Docker. "whole": the machine itself is the one
    # environment's computer, the agent root on it (openajar.vms.whole); `sudo`: its login is not root
    mode: Literal["containers", "whole"] = "containers"
    sudo: bool = False
    added_at: datetime = Field(default_factory=utcnow)
    # what the last check found (shown by `machine list`; the limits are read again from Docker itself)
    system: str = ""
    arch: str = ""
    cores: int = 0
    memory_gb: float = 0.0
    disk_gb: float = 0.0
    docker_version: str = ""


# ---------------------------------------------------------------- the registry (files on the node)

def machines_dir(home: Path) -> Path:
    return Path(home) / "vms" / DIR


def folder(home: Path, name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise InvalidRequest(f"a machine's name is lowercase letters, digits and dashes, like homebox (not {name!r})")
    return machines_dir(home) / name


def alias(name: str) -> str:
    """The HostKeyAlias of a machine: its pinned host key does not depend on its address."""
    return f"openajar-machine-{name}"


def key_path(home: Path, m: Machine) -> Path:
    return Path(m.key).expanduser() if m.key else folder(home, m.name) / KEY


def known_hosts_path(home: Path, name: str) -> Path:
    return folder(home, name) / KNOWN_HOSTS


def load(home: Path, name: str) -> Machine:
    path = folder(home, name) / RECORD
    try:
        return Machine.model_validate_json(path.read_text())
    except OSError:
        raise NotFound(f"no machine named {name}; `openajar environments machine list` shows them") from None
    except ValueError as e:
        raise VmError(f"the record of machine {name} cannot be read ({path}): {str(e)[:120]}") from None


def every(home: Path) -> list[Machine]:
    d = machines_dir(home)
    if not d.is_dir():
        return []
    out = []
    for sub in sorted(d.iterdir()):
        if (sub / RECORD).is_file() and NAME_RE.match(sub.name):
            with contextlib.suppress(NotFound, VmError):
                out.append(load(home, sub.name))
    return out


def save(home: Path, m: Machine) -> None:
    d = folder(home, m.name)
    d.mkdir(parents=True, exist_ok=True)
    for p in (machines_dir(home).parent, machines_dir(home), d):
        p.chmod(0o700)
    write_private(d / RECORD, m.model_dump_json(indent=2) + "\n")


def forget(home: Path, name: str) -> None:
    shutil.rmtree(folder(home, name), ignore_errors=True)


def default_name(home: Path, settings: VmSettings | None = None) -> str | None:
    """The machine new environments go to: vms.toml's `location` when it names one, else the only one."""
    names = [m.name for m in every(home)]
    try:
        where = (settings or load_settings(home)).location
    except (ValueError, OSError):
        where = ""
    if where in names:
        return where
    return names[0] if len(names) == 1 else None


def is_whole(home: Path, name: str | None) -> bool:
    """Is this machine handed over as a whole (openajar.vms.whole)? False for one that is not set up."""
    with contextlib.suppress(NotFound, VmError, InvalidRequest):
        return load(home, name or "").mode == "whole"
    return False


def remote(home: Path, m: Machine) -> RemoteHost:
    """The machine itself as an SSH target of the node (the same strict ssh as a VM's)."""
    return RemoteHost(vm_id=f"machine-{m.name}", host=m.host, port=m.port, user=m.user,
                      key_path=str(key_path(home, m)), known_hosts=str(known_hosts_path(home, m.name)),
                      host_alias=alias(m.name), provider="own", label=f"machine {m.name}")


def via(home: Path, m: Machine) -> dict:
    """How a container on the machine is reached: through the machine (openajar.env.models.RemoteVia)."""
    return {"host": m.host, "port": m.port, "user": m.user, "key_path": str(key_path(home, m)),
            "known_hosts": str(known_hosts_path(home, m.name)), "host_alias": alias(m.name)}


def split_image(image: str) -> tuple[str | None, str]:
    """("homebox", "agentos-env:save-ab12") of "homebox//agentos-env:save-ab12"; (None, image) without one."""
    name, sep, rest = image.partition(IMAGE_SEP)
    if sep and NAME_RE.match(name) and rest:
        return name, rest
    return None, image


def split_server(server_id: str) -> tuple[str | None, str]:
    name, sep, rest = (server_id or "").partition(":")
    if sep and NAME_RE.match(name) and rest:
        return name, rest
    return None, server_id


# ---------------------------------------------------------------- commands on the machine

def _last_line(*texts: bytes | str) -> str:
    for text in texts:
        said = (text.decode(errors="replace") if isinstance(text, bytes) else text or "").strip().splitlines()
        if said:
            return said[-1][:300]
    return ""


async def run(home: Path, m: Machine, command: str, *, stdin: bytes | None = None,
              timeout_s: float = CALL_TIMEOUT_S) -> tuple[int, str, str]:
    """Run one command on the machine over the node's SSH login: (exit code, stdout, stderr). 255 is ssh's
    own failure (the machine was not reached, or refused the key)."""
    argv = [*ssh_base(remote(home, m)), command]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise VmError(f"cannot run ssh ({e.strerror or e}); install the OpenSSH client on this computer") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise VmError(f"machine {m.name}: `{command.split()[0]}` took longer than {int(timeout_s)} s",
                      provider_code="unreachable") from None
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def unreachable(m: Machine, said: str) -> VmError:
    return VmError(f"machine {m.name} ({m.user}@{m.host}) did not answer over ssh ({said or 'no answer'}). Is it on "
                   f"and reachable from this computer? `openajar environments machine check {m.name}` says what is "
                   "wrong", provider_code="unreachable")


# What `machine add` and `machine check` read from the machine, as key=value lines (one ssh, POSIX sh). Docker
# is looked for on the PATH an ssh command gets and where installers put it, and tried as the login itself,
# then with passwordless sudo.
CHECK_SCRIPT = r"""echo "system=$(uname -s)"
echo "arch=$(uname -m)"
echo "user=$(id -un)"
echo "uid=$(id -u)"
if [ "$(id -u)" = 0 ] || sudo -n true >/dev/null 2>&1; then echo "root=1"; fi
echo "os_id=$(. /etc/os-release 2>/dev/null; echo "${ID:-}")"
echo "os_version=$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-}")"
echo "nproc=$(getconf _NPROCESSORS_ONLN 2>/dev/null)"
echo "mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)"
echo "disk_kb=$(df -Pk / 2>/dev/null | awk 'NR==2 {print $2}')"
echo "root_free_kb=$(df -Pk / 2>/dev/null | awk 'NR==2 {print $4}')"
set -- $SSH_CONNECTION
echo "sshd_port=${4:-}"
bin=""
for c in docker /usr/bin/docker /usr/local/bin/docker /snap/bin/docker; do
  if command -v "$c" >/dev/null 2>&1; then bin=$c; break; fi
done
d=""
if [ -n "$bin" ]; then
  echo "docker_found=1"
  if "$bin" info >/dev/null 2>&1; then d="$bin"
  elif sudo -n "$bin" info >/dev/null 2>&1; then d="sudo -n $bin"
  else echo "docker_error=$("$bin" info 2>&1 | tail -n 1)"; fi
fi
echo "docker=$d"
if [ -n "$d" ]; then
  echo "info=$($d info --format '{{json .}}' 2>/dev/null | head -n 1)"
  root=$($d info --format '{{.DockerRootDir}}' 2>/dev/null)
  echo "free_kb=$(df -Pk "${root:-/var/lib/docker}" 2>/dev/null | awk 'NR==2 {print $4}')"
fi
"""


class Facts(BaseModel):
    """What a machine said about itself (CHECK_SCRIPT)."""
    system: str = ""
    arch: str = ""
    user: str = ""
    uid: int | None = None
    root: bool = False            # the login is root, or has passwordless sudo
    os_id: str = ""               # /etc/os-release: ubuntu, debian, ...
    os_version: str = ""
    host_cores: int = 0           # the machine's own (cores and memory_gb are what its Docker says)
    host_memory_gb: float = 0.0
    disk_gb: float = 0.0
    root_free_gb: float | None = None
    sshd_port: int | None = None
    docker_found: bool = False
    docker: list[str] = Field(default_factory=list)
    docker_error: str = ""
    docker_version: str = ""
    docker_os: str = ""
    rootless: bool = False
    podman: bool = False
    cores: int = 0
    memory_gb: float = 0.0
    free_gb: float | None = None


def parse_check(text: str) -> Facts:
    got: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.isidentifier():
            got[k] = v.strip()
    info: dict = {}
    with contextlib.suppress(ValueError):
        loaded = json.loads(got.get("info") or "{}")
        info = loaded if isinstance(loaded, dict) else {}
    security = [str(x) for x in info.get("SecurityOptions") or []]
    free = got.get("free_kb", "")
    port = got.get("sshd_port", "")

    def gb(key: str) -> float | None:
        return round(int(got[key]) / 2**20, 1) if got.get(key, "").isdigit() else None

    return Facts(system=got.get("system", ""), arch=got.get("arch", ""), user=got.get("user", ""),
                 uid=int(got["uid"]) if got.get("uid", "").isdigit() else None, root=got.get("root") == "1",
                 os_id=got.get("os_id", ""), os_version=got.get("os_version", ""),
                 host_cores=int(got["nproc"]) if got.get("nproc", "").isdigit() else 0,
                 host_memory_gb=gb("mem_kb") or 0.0, disk_gb=gb("disk_kb") or 0.0, root_free_gb=gb("root_free_kb"),
                 sshd_port=int(port) if port.isdigit() else None, docker_found=got.get("docker_found") == "1",
                 docker=got.get("docker", "").split(), docker_error=got.get("docker_error", ""),
                 docker_version=str(info.get("ServerVersion") or ""), docker_os=str(info.get("OSType") or ""),
                 rootless=any("rootless" in s for s in security),
                 podman=bool(got.get("docker")) and "ServerVersion" not in info and "host" in info,
                 cores=int(info.get("NCPU") or 0), memory_gb=round(float(info.get("MemTotal") or 0) / 2**30, 1),
                 free_gb=round(int(free) / 2**20, 1) if free.isdigit() else None)


def problems(f: Facts) -> list[str]:
    """Why this machine cannot hold environments, each with what to do. [] when it can."""
    out = []
    if f.system != "Linux":
        what = {"Darwin": "macOS"}.get(f.system, f.system or "an unknown system")
        return [f"it runs {what}, and this version of openajar brings in Linux machines only"]
    if f.arch not in ARCHES:
        out.append(f"its CPU is {f.arch or 'unknown'}; the environments image is made for x86-64 and arm64")
    who = f.user or "this login"
    if not f.docker_found:
        out.append(NO_DOCKER + f" On the machine: `{DOCKER_INSTALL}`, then `sudo usermod -aG docker {who}`, log in "
                   "again, and run this again")
    elif not f.docker:
        out.append(f"Docker is there, but {who} may not use it ({f.docker_error or 'docker info failed'}). On the "
                   f"machine: `sudo usermod -aG docker {who}`, log in again, and run this again")
    elif f.podman:
        out.append("its `docker` is Podman, which openajar has not been made for; install Docker Engine")
    elif f.rootless:
        out.append("its Docker runs rootless, which cannot set the wall that keeps an environment off the machine "
                   "and its network; use the system's Docker (the rootful one)")
    elif f.docker_os and f.docker_os != "linux":
        out.append(f"its Docker runs {f.docker_os} containers; the environments image is a Linux one")
    if f.docker and f.free_gb is not None and f.free_gb < MIN_FREE_GB:
        out.append(f"Docker's disk there has {f.free_gb:g} GB free; the image alone is about 4 GB, so free at least "
                   f"{MIN_FREE_GB:g} GB")
    return out


NO_DOCKER = "Docker is not installed there."

# Docker's own install script (get.docker.com), run as root there, and only after the machine's owner said
# yes to it (`machine add`, or --install-docker). $1 is the login: it joins the docker group, so openajar's
# next ssh runs docker as that login. The script is fetched to a file first, so a download cut short runs
# nothing.
INSTALL_DOCKER = """set -eu
export DEBIAN_FRONTEND=noninteractive
if ! command -v curl >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  apt-get update -q >/dev/null
  apt-get install -y -q --no-install-recommends ca-certificates curl >/dev/null
fi
t=$(mktemp)
curl -fsSL https://get.docker.com -o "$t"
sh "$t" >/dev/null
rm -f "$t"
if [ -d /run/systemd/system ]; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
if [ -n "${1:-}" ] && [ "$1" != root ]; then usermod -aG docker "$1"; fi
i=0
while ! docker info >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then echo "Docker is installed, but its service did not start" >&2; exit 1; fi
  sleep 2
done
docker --version
"""


def as_root(f: Facts | Machine) -> str:
    """The shell that runs a script as root there: the login's own when it is root, else through sudo."""
    is_root = f.uid == 0 if isinstance(f, Facts) else not f.sudo
    return "sh -s" if is_root else "sudo -n sh -s"


def cannot_install_docker(f: Facts) -> str | None:
    """Why openajar cannot install Docker there itself, or None when it can (root, or passwordless sudo)."""
    if f.system != "Linux" or f.docker_found:
        return "nothing to install"
    if not f.root:
        return (f"{f.user or 'the login'} is not root and has no passwordless sudo there, so openajar cannot "
                "install it")
    return None


async def install_docker(home: Path, m: Machine, f: Facts) -> str:
    """Run Docker's install script there as root. Returns what `docker --version` says; raises VmError."""
    command = f"{as_root(f)} -- {shlex.quote(f.user or m.user)}"
    code, out, err = await run(home, m, command, stdin=INSTALL_DOCKER.encode(), timeout_s=DOCKER_INSTALL_S)
    if code == SSH_FAILED:
        raise unreachable(m, _last_line(err, out))
    if code != 0:
        raise VmError(f"machine {m.name}: Docker's install script failed there ({_last_line(err, out) or f'exit {code}'})")
    return _last_line(out)


async def read_facts(home: Path, m: Machine) -> Facts:
    code, out, err = await run(home, m, "sh -s", stdin=CHECK_SCRIPT.encode(), timeout_s=60)
    if code == SSH_FAILED or (code != 0 and not out.strip()):
        raise unreachable(m, _last_line(err, out))
    return parse_check(out)


def forwarding_refused(home: Path, m: Machine, port: int, *, timeout_s: float = 15.0) -> str | None:
    """Does the machine's sshd carry a connection to its own loopback (`ssh -W`, what every environment's
    connection uses)? None when it does, else why not. Tried against the sshd's own port there: it answers
    with its banner."""
    argv = ssh_base(remote(home, m))
    # LogLevel INFO: ssh then says why the channel did not open ("administratively prohibited", or
    # "connect failed" when nothing listens there); at ERROR it only says "stdio forwarding failed"
    argv = [("LogLevel=INFO" if a == "LogLevel=ERROR" else a) for a in argv]
    argv = [*argv[:-2], "-W", f"127.0.0.1:{int(port)}", argv[-1]]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return f"cannot run ssh ({e.strerror or e})"
    timer = threading.Timer(timeout_s, proc.kill)
    timer.start()
    try:
        banner = proc.stdout.read(4) if proc.stdout is not None else b""
        proc.kill()
        err = (proc.stderr.read() if proc.stderr is not None else b"").decode(errors="replace")
    finally:
        timer.cancel()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                if pipe is not None:
                    pipe.close()
    if banner == b"SSH-":
        return None
    if "prohibited" in err:  # "channel 0: open failed: administratively prohibited"
        return ("its sshd refuses port forwarding, and openajar reaches each environment through its ssh connection "
                "to the machine. In /etc/ssh/sshd_config there set `AllowTcpForwarding yes` (or `local`), reload "
                "sshd, and run this again")
    return None  # the sshd may listen on another address than loopback: the first environment's boot will say


# ---------------------------------------------------------------- the host key and the login key

def parse_target(text: str) -> tuple[str | None, str, int | None]:
    """(user, host, port) of `user@host`, `host`, `user@host:2222` or `ssh://user@host:2222`."""
    t = (text or "").strip().removeprefix("ssh://")
    user, at, rest = t.rpartition("@")
    host, port = rest, None
    if rest.startswith("["):  # [2001:db8::1]:2222
        end = rest.find("]")
        host, tail = rest[1:end], rest[end + 1:]
        if tail.startswith(":") and tail[1:].isdigit():
            port = int(tail[1:])
    elif rest.count(":") == 1 and rest.rsplit(":", 1)[1].isdigit():
        host, port = rest.rsplit(":", 1)[0], int(rest.rsplit(":", 1)[1])
    if not host or any(c.isspace() for c in t) or host.startswith("-") or (at and not user) or user.startswith("-"):
        raise InvalidRequest(f"not an ssh address: {text!r}. Write it as user@host, like me@192.168.1.20 or "
                             "root@box.example.com:2222")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidRequest(f"not a port: {port}")
    return (user or None), host, port


def resolve_target(user: str | None, host: str, port: int | None) -> tuple[str, str, int, str]:
    """(user, host name, port, why not) as the person's own ssh would connect: their ~/.ssh/config may name
    the host, its user and its port. `why not` is set when their config reaches it through another host
    (ProxyJump, ProxyCommand), which openajar's own strict ssh does not follow."""
    args = ["ssh", "-G"] + (["-p", str(port)] if port else []) + ([f"{user}@{host}"] if user else [host])
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    got: dict[str, str] = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        got.setdefault(k.lower(), v.strip())
    hop = got.get("proxyjump") or got.get("proxycommand") or ""
    why = "" if hop in ("", "none") else (
        f"your ssh config reaches {host} through another host ({hop[:80]}), and openajar connects to a machine "
        "directly. Give an address this computer reaches without a jump host")
    import getpass

    name = user or got.get("user") or getpass.getuser()
    number = port or (int(got["port"]) if got.get("port", "").isdigit() else 22)
    return name, got.get("hostname") or host, number, why


def scan_host_keys(host: str, port: int, *, keyscan: str = "ssh-keyscan") -> list[str]:
    """The machine's SSH host keys as "<type> <base64>" (ssh-keyscan). Raises VmError when it gives none."""
    try:
        p = subprocess.run([keyscan, "-T", "10", "-p", str(port), "-t", "ed25519,ecdsa,rsa", host],
                           capture_output=True, text=True, timeout=40, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VmError(f"could not read the host key of {host} ({type(e).__name__}); is ssh-keyscan installed, and "
                      "is the machine reachable from this computer?") from None
    keys = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            keys.append(f"{parts[1]} {parts[2]}")
    if not keys:
        raise VmError(f"{host} port {port} gave no SSH host key. Is the machine on, is that its ssh port, and does "
                      "this computer reach it?")
    return sorted(set(keys))


def pin(home: Path, name: str, keys: list[str]) -> list[str]:
    """Keep the machine's host keys (one line each under its alias) and return their SHA256 fingerprints."""
    d = folder(home, name)
    d.mkdir(parents=True, exist_ok=True)
    for p in (machines_dir(home).parent, machines_dir(home), d):
        p.chmod(0o700)
    path = known_hosts_path(home, name)
    write_private(path, "".join(f"{alias(name)} {k}\n" for k in keys))
    try:
        fp = subprocess.run(["ssh-keygen", "-l", "-f", str(path)], capture_output=True, text=True, timeout=30,
                            check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        fp = ""
    out = []
    for line in fp.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append(f"{parts[1]} ({parts[-1].strip('()')})")
    return out


def trusted_already(host: str, port: int, keys: list[str], *, known_hosts: Path | None = None) -> bool:
    """Does the person's own ssh already trust one of these keys for this host (~/.ssh/known_hosts)?"""
    path = known_hosts or Path.home() / ".ssh" / "known_hosts"
    if not path.is_file():
        return False
    name = host if port == 22 else f"[{host}]:{port}"
    try:
        out = subprocess.run(["ssh-keygen", "-F", name, "-f", str(path)], capture_output=True, text=True, timeout=20,
                             check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    known = {" ".join(line.split()[1:3]) for line in out.splitlines() if line and not line.startswith("#")}
    return bool(known & set(keys))


def make_key(home: Path, name: str) -> str:
    """A new key for this machine (0600, beside its record). Returns the public line."""
    from openajar.vms.keys import _pair

    d = folder(home, name)
    d.mkdir(parents=True, exist_ok=True)
    priv, pub = _pair(f"openajar-machine-{name}")
    write_private(d / KEY, priv)
    write_private(d / f"{KEY}.pub", pub + "\n")
    return pub


def public_key(home: Path, name: str) -> str | None:
    with contextlib.suppress(OSError):
        return (folder(home, name) / f"{KEY}.pub").read_text().strip() or None
    return None


def authorized_line(public: str) -> str:
    return f"{KEY_OPTIONS} {public.strip()}"


# Run by the person's own ssh, once: openajar's key goes into the login's authorized_keys (no second copy).
# A file whose last line has no newline gets one first, so the new line never joins the person's own key.
INSTALL_KEY = ("umask 077; mkdir -p ~/.ssh && k=$(cat) && [ -n \"$k\" ] && f=~/.ssh/authorized_keys && touch \"$f\" "
               "&& { grep -qxF \"$k\" \"$f\" || { [ -z \"$(tail -c 1 \"$f\")\" ] || echo >> \"$f\"; "
               "echo \"$k\" >> \"$f\"; }; }")


def install_key_argv(home: Path, name: str, user: str, host: str, port: int) -> list[str]:
    """The person's own ssh (their config, their agent, their password prompt), checked against the host
    key just pinned, that adds openajar's key. openajar sees neither their key nor their password."""
    return ["ssh", "-p", str(port), "-o", f"UserKnownHostsFile={known_hosts_path(home, name)}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
            "-o", f"HostKeyAlias={alias(name)}", "-o", "UpdateHostKeys=no", "-o", "ClearAllForwardings=yes",
            "-T", f"{user}@{host}", f"sh -c {shlex.quote(INSTALL_KEY)}"]


def remove_key_script(public: str) -> str:
    """Take openajar's line out of authorized_keys again (by the key itself), and nothing else."""
    blob = shlex.quote(public.split()[1])
    # grep's status 1 is "no other line is left" (the file becomes empty); 2 is an error, and then the
    # file is left as it is
    return (f"f=~/.ssh/authorized_keys; [ -f \"$f\" ] || exit 0; umask 077; grep -vF {blob} \"$f\" > \"$f.openajar\"; "
            "rc=$?; if [ $rc -le 1 ]; then cat \"$f.openajar\" > \"$f\"; fi; rm -f \"$f.openajar\"; [ $rc -le 1 ]\n")


# ---------------------------------------------------------------- the image on the machine

def build_context(source: Path, scripts: Path) -> bytes:
    """The image's build folder as a tar (what `docker build -` reads): deploy/environments and the setup
    scripts, as `openajar environments setup --build` puts them in one folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        files = [f for f in sorted(source.iterdir()) if f.is_file()]
        files += [scripts / n for n in ("setup.sh", "browser.sh", "browser-requirements.txt", "desktop.sh")]
        for f in files:
            data = f.read_bytes()
            info = tarfile.TarInfo(f.name)
            info.size, info.mode = len(data), 0o755 if f.suffix == ".sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class MachineDocker(DockerProvider):
    """The docker provider with every docker call run on one machine over SSH."""

    name = "own"
    type_name = "own"

    def __init__(self, home: Path, machine: Machine, settings: VmSettings):
        super().__init__(image=settings.docker_image, docker=" ".join(machine.docker), cpus=settings.docker_cpus,
                         memory_gb=settings.docker_memory_gb, pids=settings.docker_pids)
        self.home, self.machine = Path(home), machine
        self.said = f"machine {machine.name}"
        self.image_hint = f"put it there with `openajar environments machine check {machine.name} --image`"

    def __repr__(self) -> str:
        return f"MachineDocker({self.machine.name}, {self.image})"

    async def _run(self, *args: str, stdin: bytes | None = None, ok_codes: tuple[int, ...] = (0,),
                   timeout_s: float = CALL_TIMEOUT_S) -> str:
        command = shlex.join([*self.machine.docker, *args])
        code, out, err = await run(self.home, self.machine, command, stdin=stdin, timeout_s=timeout_s + 15)
        if code == SSH_FAILED:
            raise unreachable(self.machine, _last_line(err, out))
        if code not in ok_codes:
            raise VmError(f"{self.said}: docker {args[0]}: {_last_line(err, out) or f'exit {code}'}")
        return out

    async def has_image(self) -> str | None:
        """None when the machine has this openajar's image, else what is wrong with it."""
        try:
            if not await self._image_exists(self.image):
                return f"no image {self.image} there"
            await self._check_version(self.image)
        except VmError as e:
            return e.message
        return None

    async def pull(self, source: str) -> str | None:
        """`docker pull` of the prebuilt image there, named as this provider's image. None when done, else
        why not (no image for this version or CPU, no network there, an older image)."""
        try:
            await self._run("pull", "-q", source, timeout_s=PULL_TIMEOUT_S)
            await self._check_version(source)
            if source != self.image:
                await self._run("tag", source, self.image)
        except VmError as e:
            return e.message
        return None

    async def build(self, context: bytes) -> None:
        """`docker build` there, the build folder sent as a tar on stdin."""
        await self._run("build", "-q", "-t", self.image, "-", stdin=context, timeout_s=BUILD_TIMEOUT_S)
        await self._check_version(self.image)

    async def mine(self, labels: dict[str, str]) -> dict[str, list[str]]:
        """What this openajar home made there: its containers, networks and images (by label)."""
        filt: list[str] = []
        for k, v in sorted(labels.items()):
            filt += ["--filter", f"label={k}={v}"]
        containers = (await self._run("ps", "-aq", *filt)).split()
        networks = (await self._run("network", "ls", "-q", *filt)).split()
        images = [i["id"] for i in await self.list_images(labels)]
        return {"containers": containers, "networks": networks, "images": images}

    async def remove_mine(self, labels: dict[str, str]) -> dict[str, list[str]]:
        """Remove exactly those (never a container, network or image without this home's label)."""
        found = await self.mine(labels)
        if found["containers"]:
            await self._run("rm", "-f", "-v", *found["containers"], ok_codes=(0, 1))
        for n in found["networks"]:
            await self._run("network", "rm", n, ok_codes=(0, 1))
        for i in found["images"]:
            await self._run("rmi", "-f", i, ok_codes=(0, 1))
        return found


class OwnProvider:
    """Every registered machine, behind the provider protocol (see the module's docstring)."""

    name = "own"
    firewall = False        # nothing is opened on the machine: a container's sshd is on its loopback
    closes_network = True   # limited and none: the entrypoint's closed wall, as on docker (openajar.vms.netwall)
    capabilities = frozenset({"snapshot", "network_policy"})
    type_name = "own"

    def __init__(self, home: Path, settings: VmSettings):
        self.home, self.settings = Path(home), settings
        self._keys: dict[str, tuple[str, dict[str, str]]] = {}
        self._made: dict[str, tuple[float, Any]] = {}

    def __repr__(self) -> str:
        return f"OwnProvider({', '.join(m.name for m in every(self.home)) or 'no machine'})"

    # ------------------------------------------------------------------ machines
    def on(self, name: str) -> Any:
        """One machine's side of the provider (made again when its record changed): its Docker
        (MachineDocker), or the machine itself when it was handed over whole (openajar.vms.whole)."""
        path = folder(self.home, name) / RECORD
        try:
            stamp = path.stat().st_mtime
        except OSError:
            self._made.pop(name, None)
            raise VmError(f"no machine named {name} is set up on this node; `openajar environments machine list` "
                          "shows them") from None
        made = self._made.get(name)
        if made is None or made[0] != stamp:
            m = load(self.home, name)
            if m.mode == "whole":
                from openajar.vms.whole import WholeMachine

                docker: Any = WholeMachine(self.home, m, self.settings)
            else:
                docker = MachineDocker(self.home, m, self.settings)
            docker._keys = self._keys  # one login key store: the node makes a key before it names the machine
            made = self._made[name] = (stamp, docker)
        return made[1]

    def _known(self, name: str | None) -> Any:
        if not name:
            return None
        with contextlib.suppress(VmError, InvalidRequest, NotFound):
            return self.on(name)
        return None

    def default(self) -> str:
        """The machine new environments go to: what vms.toml names now (so `machine use` holds without a
        restart), else what this node's settings named when it started, else the only machine."""
        name = default_name(self.home)
        if name is None:
            name = default_name(self.home, self.settings)
        if name is None:
            if every(self.home):
                raise VmError("several machines are set up and none is the default: pick one with `openajar "
                              "environments machine use NAME`")
            raise VmError("no machine is set up on this node: add yours with `openajar environments machine add "
                          "NAME user@host`")
        return name

    def place(self, image: str | None = None) -> str:
        """Where a new environment's container goes: the machine that holds its image (a save resumes where
        it was made), else the default machine. The node plans with it as the `location`."""
        name, _ = split_image(image or "")
        return name if name and self._known(name) is not None else self.default()

    def size_type(self, size: str) -> str:
        return f"{self.type_name}-{size}"

    def _out(self, name: str, s: Server) -> Server:
        m = self.on(name).machine
        if m.mode == "whole":  # the machine itself: reached directly, as the node reaches it
            return replace(s, id=f"{name}:{s.id}")
        return replace(s, id=f"{name}:{s.id}", via=via(self.home, m))

    def capable_at(self, what: str, location: str | None) -> bool:
        """openajar.vms.provider.capable_at: a whole machine has no disk snapshot and no wall around it."""
        m = self._known(location)
        return what in getattr(m, "capabilities", self.capabilities)

    async def busy(self, location: str | None) -> str | None:
        """The lane of the environment that holds this machine, when it is handed over whole and one is open
        on it ("" for a holder that names none). None for a free one, and for a machine of containers."""
        m = self._known(location)
        if m is None or m.machine.mode != "whole":
            return None
        held = await m.held()
        return None if held is None else str((held.get("labels") or {}).get("agentos-lane") or "")

    def whole(self, location: str | None) -> bool:
        """Is this machine handed over as a whole (for the words of an open, a close and a list)?"""
        m = self._known(location)
        return m is not None and m.machine.mode == "whole"

    # ------------------------------------------------------------------ provider
    async def check(self) -> None:
        docker = self.on(self.default())
        await docker.check()

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        return 0.0

    async def server_types(self) -> list[dict]:
        return await self.on(self.default()).server_types()

    async def locations(self) -> dict[str, str]:
        """Each machine is a place of its own: a container never moves to another machine for room."""
        return {m.name: m.name for m in every(self.home)}

    async def image_info(self, image: str) -> dict | None:
        return None

    async def resolve_image(self, image: str) -> str:
        name, inner = split_image(image)
        if name is not None and self._known(name) is None:
            raise VmError(f"the image {inner} is on machine {name}, which is no longer set up on this node")
        name = name or self.default()
        return f"{name}{IMAGE_SEP}{await self.on(name).resolve_image(inner)}"

    async def create_server(self, spec: ServerSpec) -> Server:
        name, image = split_image(spec.image)
        name = name or (spec.location if self._known(spec.location) is not None else self.default())
        server = await self.on(name).create_server(replace(spec, image=image, location=name))
        return self._out(name, server)

    async def get_server(self, server_id: str) -> Server | None:
        name, cid = split_server(server_id)
        docker = self._known(name)
        if docker is None:
            return None  # its machine was removed from this node: nothing of it is left to reach
        server = await docker.get_server(cid)
        return self._out(name or "", server) if server is not None else None

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        """This home's containers on every machine. A machine that does not answer raises: the reaper must
        never read "not listed" as "deleted" for a machine that is only switched off."""
        out: list[Server] = []
        for m in every(self.home):
            out += [self._out(m.name, s) for s in await self.on(m.name).list_servers(labels)]
        return out

    async def delete_server(self, server_id: str) -> None:
        name, cid = split_server(server_id)
        docker = self._known(name)
        if docker is not None:
            await docker.delete_server(cid)

    async def sweep(self, labels: dict[str, str]) -> list[str]:
        gone: list[str] = []
        for m in every(self.home):
            with contextlib.suppress(VmError):
                gone += await self.on(m.name).sweep(labels)
        return gone

    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        return ""  # openajar changes no firewall on a person's machine

    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        kid = f"key-{name}"
        self._keys[kid] = (public_key.strip(), dict(labels))
        return kid

    async def delete_ssh_key(self, key_id: str) -> None:
        self._keys.pop(key_id, None)

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        return [(k, v[0]) for k, v in self._keys.items() if all(v[1].get(a) == b for a, b in labels.items())]

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        """`docker commit` on the container's own machine: the image stays in that machine's Docker."""
        name, cid = split_server(server_id)
        docker = self._known(name)
        if docker is None:
            raise VmError(f"the machine of {server_id} is no longer set up on this node, so it cannot be saved")
        return f"{name}{IMAGE_SEP}{await docker.create_snapshot(cid, description, labels)}"

    async def get_image(self, image_id: str) -> dict | None:
        name, inner = split_image(image_id)
        docker = self._known(name)
        if docker is None:
            return None
        info = await docker.get_image(inner)
        return {**info, "id": image_id} if info is not None else None

    async def delete_image(self, image_id: str) -> None:
        name, inner = split_image(image_id)
        docker = self._known(name)
        if docker is not None:
            await docker.delete_image(inner)

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        """The images this home made, on the machines that answer (one that does not is skipped and logged:
        fewer images listed only ever means fewer deleted)."""
        out: list[dict] = []
        for m in every(self.home):
            try:
                found = await self.on(m.name).list_images(labels)
            except VmError as e:
                log.info("machine %s did not list its images: %s", m.name, e.message)
                continue
            out += [{**i, "id": f"{m.name}{IMAGE_SEP}{i['id']}"} for i in found]
        return out


def image_source(settings: VmSettings) -> str:
    """vms.toml's docker_pull with this version in it: the prebuilt image a machine downloads ("": never)."""
    from openajar import __version__

    return settings.docker_pull.replace("{version}", __version__).strip()

