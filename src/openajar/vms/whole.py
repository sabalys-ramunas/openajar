"""A machine of your own handed over as a whole (`openajar environments machine add NAME root@host --whole`).

The other way to bring a machine in (openajar.vms.own keeps the registry and the provider both share): no
Docker and no container. The machine itself is the computer of one environment at a time, and the agent is
root on it, as on a rented VM. It is for a server you give to the agent entirely: a fresh Ubuntu 24.04 with
nothing of yours on it (`machine add` checks that and says what it found).

What openajar does there, as root:

- at `machine add`: the same setup a rented VM gets (openajar/environments/setup.sh, the browser and the
  desktop: packages from Ubuntu's archive, Node, uv, Camoufox under /opt), and the folder STATE_DIR;
- when an environment opens: one file, SERVER (who holds the machine: the same labels a rented VM carries),
  and one line in root's authorized_keys for that environment's own key. The machine's sshd and its host
  key are left as they are: the environment pins the host key `machine add` pinned;
- when the environment closes: RELEASE. That key's line goes, what the environment left running is ended
  (its jobs and commands by their process groups, what works in /work, the browser and its screen), /work
  is emptied, openajar's own files of the session go, and SERVER is removed. Nothing else is touched:
  packages the agent installed, services and containers it started and files it wrote outside /work stay,
  since openajar cannot tell them from the system's own. A truly clean machine is its system installed again.

openajar never restarts, powers off or reinstalls the machine.

What a whole machine cannot do, which a container on your machine or a rented VM can:

- no disk snapshot: a save holds the files (what `snapshots = false` saves on any provider: commits, changed
  files, keep_paths, services, the note), and resumes onto the emptied machine;
- no closed network: the machine is on your network as itself, and nothing outside it could hold a wall
  the agent, as root, could not take down. `network` limited and none are refused there;
- one environment at a time: a second open is refused until the first is closed.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shlex
from pathlib import Path
from typing import Any

from openajar.core.models import utcnow
from openajar.vms import own
from openajar.vms.docker import SIZE_TYPES
from openajar.vms.models import VmError, VmSettings, home_id_of
from openajar.vms.own import (
    ARCHES,
    MIN_FREE_GB,
    SSH_FAILED,
    Facts,
    Machine,
    _last_line,
    as_root,
    known_hosts_path,
    unreachable,
)
from openajar.vms.provider import Server, ServerSpec, check_ours
from openajar.vms.ssh import PGID_DIR

STATE_DIR = "/var/lib/openajar-machine"
SERVER = f"{STATE_DIR}/server.json"
WORK = "/work"                    # openajar.environments.machine.WORK (the setup makes it)
SYSTEM = ("ubuntu", "24.04")      # what the setup is made for and tried on (openajar.environments.machine.BASE_IMAGE)
STATE_TIMEOUT_S = 40.0
RELEASE_TIMEOUT_S = 120.0
IMAGE = "machine"                 # a whole machine boots no image: it is what it is

# What `machine add --whole` reads beside own.CHECK_SCRIPT, as root: may root sign in with a key, is the
# machine held, did the setup run, and the signs of a machine that is in use (not_fresh puts them in words).
FRESH_SCRIPT = rf"""echo "rootlogin=$( (sshd -T 2>/dev/null || /usr/sbin/sshd -T 2>/dev/null) | awk '$1 == "permitrootlogin" {{print $2}}')"
echo "held=$(cat {SERVER} 2>/dev/null | tr -d '\n')"
[ -f /var/lib/agentos-env/ready ] && echo "setup=1"
echo "users=$(awk -F: '$3 >= 1000 && $3 < 60000 {{print $1}}' /etc/passwd | tr '\n' ' ')"
echo "ports=$( (ss -Htln 2>/dev/null || true) | awk '{{print $4}}' | grep -v -e '^127\.' -e '^\[::1\]' -e '%lo' | sed 's/.*://' | sort -un | tr '\n' ' ')"
echo "containers=$( (docker ps -aq 2>/dev/null || true) | wc -l | tr -d ' ')"
echo "work=$(ls -A {WORK} 2>/dev/null | head -n 3 | tr '\n' ' ')"
d=""
for p in /var/www /srv /var/lib/postgresql /var/lib/mysql /var/lib/mongodb /var/lib/redis; do
  [ -d "$p" ] && [ -n "$(ls -A "$p" 2>/dev/null)" ] && d="$d $p"
done
for p in /home/*/[!.]* /root/[!.]*; do
  case "$p" in */snap) continue;; esac
  [ -e "$p" ] && d="$d $p"
done
echo "data=$(echo $d | cut -c1-200)"
"""


async def read_fresh(home: Path, m: Machine, f: Facts) -> dict[str, str]:
    """FRESH_SCRIPT's lines, read as root. {} for a login that is not root and has no sudo (problems says so)."""
    if not f.root:
        return {}
    code, out, err = await own.run(home, m, as_root(f), stdin=FRESH_SCRIPT.encode(), timeout_s=60)
    if code == SSH_FAILED:
        raise unreachable(m, _last_line(err, out))
    return parse_fresh(out)


def parse_fresh(text: str) -> dict[str, str]:
    got: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.isidentifier():
            got[k] = v.strip()
    return got


def problems(f: Facts, got: dict[str, str], home_id: str = "") -> list[str]:
    """Why this machine cannot be handed over whole, each with what to do. [] when it can."""
    containers = "Bring it in with containers instead (leave out --whole): that works on any Linux with Docker"
    if f.system != "Linux":
        what = {"Darwin": "macOS"}.get(f.system, f.system or "an unknown system")
        return [f"it runs {what}, and this version of openajar brings in Linux machines only"]
    out = []
    if f.arch not in ARCHES:
        out.append(f"its CPU is {f.arch or 'unknown'}; openajar's setup is made for x86-64 and arm64")
    if (f.os_id, f.os_version) != SYSTEM:
        has = f"{f.os_id or 'an unknown system'} {f.os_version}".strip()
        out.append(f"it runs {has}, and a whole machine must be a fresh Ubuntu 24.04 (what openajar's setup is made "
                   f"for). {containers}")
    if not f.root:
        out.append(f"{f.user or 'the login'} is not root and has no passwordless sudo there, and openajar sets a "
                   "whole machine up and runs it as root. Add it as root@host")
    rootlogin = got.get("rootlogin", "")
    if rootlogin in ("no", "forced-commands-only"):
        out.append(f"its sshd does not let root sign in (PermitRootLogin {rootlogin}), and an environment works "
                   "there as root. In /etc/ssh/sshd_config there set `PermitRootLogin prohibit-password`, reload "
                   "sshd, and run this again")
    held = holder(got.get("held", ""))
    if held is not None and held.get("labels", {}).get("agentos-home") != home_id:
        out.append("another openajar holds it as a whole machine now (an environment is open on it). Close that "
                   f"environment there, or remove {SERVER} on the machine if that openajar is gone")
    if f.root_free_gb is not None and f.root_free_gb < MIN_FREE_GB:
        out.append(f"its disk has {f.root_free_gb:g} GB free; the setup alone needs about 6 GB, so free at least "
                   f"{MIN_FREE_GB:g} GB")
    return out


def not_fresh(f: Facts, got: dict[str, str]) -> list[str]:
    """The signs that this machine is in use, in words. [] for one that looks fresh. A machine openajar set
    up before (`setup`) may have its own /work."""
    out = []
    if (got.get("containers") or "0") not in ("0", ""):
        out.append(f"its Docker has {got['containers']} container{'s' if got['containers'] != '1' else ''}")
    own_ports = {str(f.sshd_port or 22), "22"}
    ports = [p for p in got.get("ports", "").split() if p not in own_ports]
    if ports:
        out.append(f"something there listens on port{'s' if len(ports) != 1 else ''} {', '.join(ports[:6])}")
    users = got.get("users", "").split()
    if len(users) > 1:
        out.append(f"it has {len(users)} user accounts ({', '.join(users[:4])})")
    if got.get("work") and got.get("setup") != "1":
        out.append(f"its {WORK} folder is not empty ({', '.join(got['work'].split()[:3])}), and openajar empties "
                   "that folder each time an environment closes")
    if got.get("data"):
        out.append(f"it holds files: {', '.join(got['data'].split()[:5])}")
    return out


def holder(text: str) -> dict | None:
    """SERVER's content as the server it names. None for a free machine; {"id": ""} for a file that does
    not read (the machine is then held by nobody openajar can name, and stays so until a person looks)."""
    text = (text or "").strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        got = json.loads(text)
        if isinstance(got, dict) and isinstance(got.get("labels"), dict):
            return got
    return {"id": "", "name": "", "labels": {}}


def take_script(state: dict, key_line: str) -> str:
    """Hold the machine for one server, or say who holds it. The file is made with noclobber, so of two
    opens at once one gets it. Then the environment's own key may sign in as root."""
    blob = base64.b64encode(json.dumps(state).encode()).decode()
    return f"""set -eu
umask 077
mkdir -p {STATE_DIR} /root/.ssh
if [ -f {SERVER} ] && [ ! -s {SERVER} ]; then rm -f {SERVER}; fi
if ! ( set -C; echo {blob} | base64 -d > {SERVER} ) 2>/dev/null; then
  echo "state=held"
  echo "held=$(cat {SERVER} 2>/dev/null | tr -d '\\n')"
  exit 0
fi
f=/root/.ssh/authorized_keys
touch "$f"
[ -z "$(tail -c 1 "$f")" ] || echo >> "$f"
echo {shlex.quote(key_line.strip())} >> "$f"
echo "state=made"
"""


def release_script(server_id: str, key_line: str = "") -> str:
    """End one server's hold of the machine and clean up after its environment (see the module's docstring
    for what goes and what stays). Does nothing when another server holds the machine now."""
    marker = shlex.quote(f'"id": {json.dumps(server_id)}')
    blob = shlex.quote(key_line.split()[1]) if len(key_line.split()) > 1 else ""
    drop_key = (f'f=/root/.ssh/authorized_keys; if [ -f "$f" ]; then grep -vF {blob} "$f" > "$f.openajar"; rc=$?; '
                'if [ $rc -le 1 ]; then cat "$f.openajar" > "$f"; fi; rm -f "$f.openajar"; fi') if blob else ":"
    return f"""set -u
if [ -f {SERVER} ] && ! grep -qF {marker} {SERVER}; then echo "state=other"; exit 0; fi
{drop_key}
left() {{
  for d in /proc/[0-9]*; do
    pid=${{d#/proc/}}
    [ "$pid" = "$$" ] && continue
    cwd=$(readlink "$d/cwd" 2>/dev/null) || continue
    exe=$(readlink "$d/exe" 2>/dev/null) || exe=""
    case "$cwd" in {WORK}|{WORK}/*) echo "$pid"; continue;; esac
    case "$exe" in {WORK}/*|/opt/agentos-browser/*) echo "$pid";; esac
  done
}}
for sig in TERM KILL; do
  for p in {PGID_DIR}/*.pgid; do
    [ -f "$p" ] || continue
    g=$(cat "$p" 2>/dev/null)
    [ -n "$g" ] && kill -s $sig -- "-$g" 2>/dev/null
  done
  for pid in $(left); do kill -s $sig "$pid" 2>/dev/null; done
  if command -v pkill >/dev/null 2>&1; then
    pkill -$sig -f vmbrowser_ctl 2>/dev/null
    for n in Xvfb Xvnc x11vnc openbox; do pkill -$sig -x "$n" 2>/dev/null; done
  fi
  [ $sig = TERM ] && sleep 2
done
rm -rf {PGID_DIR} /tmp/agentos-save* /tmp/agentos-restore* /run/agentos-browser /var/lib/agentos-env/recording
rm -rf /root/.config/agentos /root/.git-credentials
if [ -d {WORK} ]; then find {WORK} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>/dev/null; fi
rm -f {SERVER}
echo "state=released"
"""


class WholeMachine:
    """One machine handed over whole, as one place of openajar.vms.own.OwnProvider (the methods it calls
    on a machine's Docker, answered by the machine itself)."""

    name = "own"
    type_name = "own"
    capabilities: frozenset[str] = frozenset()  # no snapshot, no wall, no cloud-init (the node runs the setup)

    def __init__(self, home: Path, machine: Machine, settings: VmSettings):
        self.home, self.machine, self.settings = Path(home), machine, settings
        self.home_id = home_id_of(self.home)  # a release reads this home's label back first (delete_server)
        self.said = f"machine {machine.name}"
        self._keys: dict[str, tuple[str, dict[str, str]]] = {}

    def __repr__(self) -> str:
        return f"WholeMachine({self.machine.name})"

    async def _root(self, script: str, *, timeout_s: float = STATE_TIMEOUT_S) -> dict[str, str]:
        """Run a script there as root and read its key=value lines. A machine that does not answer raises
        (never "it is free"); so does a script that failed."""
        code, out, err = await own.run(self.home, self.machine, as_root(self.machine), stdin=script.encode(), timeout_s=timeout_s)
        if code == SSH_FAILED:
            raise unreachable(self.machine, _last_line(err, out))
        if code != 0:
            raise VmError(f"{self.said}: {_last_line(err, out) or f'exit {code}'}")
        return parse_fresh(out)

    async def held(self) -> dict | None:
        """The server that holds the machine now (SERVER), or None when it is free."""
        got = await self._root(f'echo "held=$(cat {SERVER} 2>/dev/null | tr -d \'\\n\')"; echo "read=1"')
        if got.get("read") != "1":
            raise VmError(f"{self.said}: its state could not be read")
        return holder(got.get("held", ""))

    def _server(self, state: dict, *, login: bool = False) -> Server:
        m = self.machine
        keys: list[str] = []
        if login:  # the environment pins the host key `machine add` pinned (openajar.vms.keys.adopt)
            with contextlib.suppress(OSError):
                lines = known_hosts_path(self.home, m.name).read_text().splitlines()
                keys = [" ".join(ln.split()[1:3]) for ln in lines if len(ln.split()) >= 3]
        return Server(id=str(state.get("id") or ""), name=str(state.get("name") or ""), status="running",
                      ipv4=m.host, labels={str(k): str(v) for k, v in (state.get("labels") or {}).items()},
                      server_type=str(state.get("type") or self.type_name), created=str(state.get("created") or ""),
                      ssh_port=m.port, ssh_user="root", host_keys=keys)

    # ------------------------------------------------------------------ provider (one machine's side)
    async def check(self) -> None:
        await self.held()

    async def server_types(self) -> list[dict]:
        """The machine itself, under each name an environment's size may ask for: whatever is asked, the
        environment gets the whole machine."""
        m = self.machine
        arch = ARCHES.get(m.arch, "x86")
        names = (self.type_name, *(f"{self.type_name}-{n.split('-', 1)[1]}" for n in SIZE_TYPES))
        return [{"name": n, "cores": m.cores or 1, "memory_gb": m.memory_gb or 1.0, "disk_gb": int(m.disk_gb),
                 "cpu_type": "dedicated", "architecture": None, "host_architecture": arch,
                 "price_hourly_eur": _Free(), "available": None, "pinned": n != self.type_name} for n in names]

    async def resolve_image(self, image: str) -> str:
        """A whole machine boots no image: it is the Ubuntu its owner installed, set up by `machine add`. A
        snapshot by name is not there (so an environment plans the plain system, openajar.environments.sizing)."""
        if image.startswith("snapshot:"):
            raise VmError(f"{self.said} is handed over whole and boots no image, so it has no snapshot named "
                          f"{image.split(':', 1)[1]}; set image = \"auto\" in environments.toml")
        return IMAGE

    async def create_server(self, spec: ServerSpec) -> Server:
        key = self._keys.get(spec.ssh_key_id or "")
        if key is None:
            raise VmError(f"{self.said}: the machine has no login key")
        state = {"id": spec.name, "name": spec.name, "labels": dict(spec.labels), "type": spec.server_type,
                 "created": utcnow().isoformat(), "key": key[0]}
        got = await self._root(take_script(state, key[0]))
        if got.get("state") == "made":
            return self._server(state, login=True)
        other = holder(got.get("held", "")) or {}
        lane = (other.get("labels") or {}).get("agentos-lane") or "another openajar"
        raise VmError(f"your machine {self.machine.name} is handed over whole, so it holds one environment at a "
                      f"time, and one is open on it now ({lane}). Close that one first (environment_close), or tell "
                      "the user: a second machine (`openajar environments machine add`), or this one brought in "
                      "with containers, holds several", provider_code="machine_in_use")

    async def get_server(self, server_id: str) -> Server | None:
        state = await self.held()
        if not server_id or state is None or state.get("id") != server_id:
            return None
        return self._server(state)

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        state = await self.held()
        if state is None or any(state.get("labels", {}).get(k) != v for k, v in labels.items()):
            return []
        return [self._server(state)]

    async def delete_server(self, server_id: str) -> None:
        """Release the machine (empty /work, end what ran there) when this home's environment holds it: the
        machine itself says who holds it, by id and by this home's label. Held by none, or by another id:
        nothing to release."""
        state = await self.held()
        if not server_id or state is None or state.get("id") != server_id:
            return
        check_ours(state.get("labels"), self.home_id, f"{self.said}: the environment that holds it ({server_id})")
        await self._root(release_script(server_id, str(state.get("key") or "")), timeout_s=RELEASE_TIMEOUT_S)

    async def sweep(self, labels: dict[str, str]) -> list[str]:
        return []

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        raise VmError(f"{self.said} is handed over whole, and a whole machine has no disk snapshot: its saves hold "
                      "the files")

    async def get_image(self, image_id: str) -> dict | None:
        return None

    async def delete_image(self, image_id: str) -> None:
        return None

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        return []

    # ------------------------------------------------------------------ for `machine check` and `remove`
    async def mine(self, labels: dict[str, str]) -> dict[str, list[str]]:
        """What this openajar home holds there: the machine, when one of its environments is open on it."""
        return {"containers": [s.id for s in await self.list_servers(labels)], "networks": [], "images": []}

    async def remove_mine(self, labels: dict[str, str]) -> dict[str, list[str]]:
        found = await self.mine(labels)
        for sid in found["containers"]:
            await self.delete_server(sid)
        return found

    async def forget(self) -> None:
        """`machine remove`: openajar's own folder there goes (the setup's packages and tools stay)."""
        await self._root(f"rm -rf {STATE_DIR}; echo done=1")


class _Free(dict):
    """Prices by location for a machine that is yours: free, whatever it is called."""

    def get(self, key: Any, default: Any = None) -> Any:
        return 0.0

    def __bool__(self) -> bool:
        return True
