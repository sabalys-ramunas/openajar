"""The agent's own shell on its environment's machine, in local mode: `ssh openajar-<env id>`, and with it
scp, rsync and git, the way it works on the laptop.

In local mode (`openajar local`) the agent runs on the same computer as the node, and the node already
reaches the VM over SSH. When an environment is ready the node gives the agent the same reach:

- a key of the environment's own (OPENAJAR_HOME/ssh/<env id>/id_ed25519), not the node's VM key, put
  in the machine's authorized_keys with `restrict,pty` and a forced command (AGENT_SSH below). So the
  agent's sessions get a terminal and run what was asked, but forward nothing: no ssh agent (the
  person's own keys never reach the VM), no X11, no port in either direction (a `-R` would let code on
  the VM reach this computer's loopback, where the node listens; `ssh.port_forwards` and a comment in
  the entry point at environment_port instead, FORWARDS_COMMENT). The forced command notes each
  session in /run/agentos-ssh, which is how the idle rule tells the agent's sessions from the node's
  own (ACTIVITY), since both come from this computer's address;
- a host entry in OPENAJAR_HOME/ssh/config: `Host openajar-<env id>`, the VM's address, user root, that
  key with IdentitiesOnly, and the VM's host key pinned (the node made it and handed it to the VM, so
  StrictHostKeyChecking is yes against a known_hosts file of one line under the entry's own alias).
  `openajar setup ssh` puts `Include ~/.openajar/ssh/config` at the top of ~/.ssh/config, so plain
  `ssh openajar-<id>` works; without it the command is `ssh -F ~/.openajar/ssh/config openajar-<id>`.

The entry and the key go when the environment closes, fails, or is saved and its VM deleted, and come
back when it is resumed (a new machine, a new key). A server node writes none: there the agent is on
another computer, and it works through openajar's tools only.

What runs over this ssh is not in openajar's call log: the node sees that a session ran, not what it ran.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from openajar.core.config import write_private
from openajar.core.errors import OpenajarError
from openajar.environments.models import Environment

log = logging.getLogger("openajar.environments")

DIR = "ssh"
CONFIG = "config"
KEY_COMMENT = "agentos-agent-ssh"
WRAPPER = "/usr/local/lib/agentos-env/agent-ssh"
SESSIONS = "/run/agentos-ssh"
BEGIN, END = "# >>> agentos environment ", "# <<< agentos environment "
_ID_OK = re.compile(r"^[a-z0-9]{4,40}$")

# Port forwards stay off on both ends: the machine's key is `restrict` (a -R would let code on the VM
# reach this computer's loopback, where the node listens), and the entry has ClearAllForwardings, so
# a forward in the person's own `Host *` is not even tried. ssh then ignores -L silently (exit 0, a
# local port that refuses: round 3, T3), so the entry and the `ssh` block say it, and what to use.
# Why not a refusal from ssh itself: ClearAllForwardings clears the command line's forwards and the
# config's alike, and no option clears only the config's; without it a `Host *` LocalForward or
# DynamicForward of the person's would bind ports on every session (and ExitOnForwardFailure end it),
# and the server's refusal ("administratively prohibited") shows only once a connection is made.
# A LocalCommand that prints a warning would run for every scp and rsync too.
FORWARDS_COMMENT = ("# Port forwards (ssh -L, -R, -D) are off for this host and are ignored. For a dev server, "
                    "environment_port(port) gives a local link.")
FORWARDS_WORDS = ("off: ssh -L, -R and -D do nothing on this host. For a dev server on the machine, "
                  'tools_call(name="environment_port", arguments={"on": "<your on>", "port": 3000}) gives you a '
                  "local http://127.0.0.1:<port>/ link")

# The forced command of the agent's key: note the session (its own pid, which the asked command keeps
# through exec), then run what was asked as sshd would: a login shell, the sftp server (scp uses SFTP
# now), or the command through the user's shell (rsync, git, a plain command).
AGENT_SSH = f"""#!/bin/sh
# openajar: the agent's own ssh into this machine (openajar local). Each session is noted in {SESSIONS},
# so the node counts it as work when it decides the machine is idle; then what was asked runs.
mkdir -p {SESSIONS} 2>/dev/null && touch {SESSIONS}/last {SESSIONS}/s.$$ 2>/dev/null
c=${{SSH_ORIGINAL_COMMAND:-}}
case "$c" in
  "") exec "${{SHELL:-/bin/sh}}" -l ;;
  internal-sftp)
    for s in /usr/lib/openssh/sftp-server /usr/libexec/openssh/sftp-server /usr/lib/ssh/sftp-server; do
      [ -x "$s" ] && exec "$s"
    done
    echo "openajar: no sftp-server on this machine" >&2; exit 1 ;;
  *) exec "${{SHELL:-/bin/sh}}" -c "$c" ;;
esac
"""

# What the node runs over its own connection to see the agent's ssh: the machine's clock, the last
# session start, and each session still running. A session running a command (rsync, a test) is in use
# now; an interactive one counts from the last time its terminal was used. Ends that left a note behind
# are swept. One line: `agentos-ssh now=<epoch> last=<epoch> live=<n>`.
ACTIVITY = f"""d={SESSIONS}; now=$(date +%s); last=0; live=0
[ -f "$d/last" ] && last=$(stat -c %Y "$d/last" 2>/dev/null || echo 0)
for f in "$d"/s.*; do
  [ -e "$f" ] || continue
  p=${{f##*/s.}}
  if cat "/proc/$p/environ" 2>/dev/null | tr '\\0' '\\n' | grep -q '^SSH_CONNECTION='; then
    live=$((live + 1))
    t=$(readlink "/proc/$p/fd/0" 2>/dev/null || true)
    case "$t" in
      /dev/pts/*) for x in $(stat -c '%X %Y' "$t" 2>/dev/null); do [ "$x" -gt "$last" ] && last=$x; done ;;
      *) last=$now ;;
    esac
  else
    rm -f "$f"
  fi
done
echo "agentos-ssh now=$now last=$last live=$live"
"""
_ACTIVITY_RE = re.compile(r"agentos-ssh now=(\d+) last=(\d+) live=(\d+)")


def authorize_script(public_key: str) -> str:
    """Run on the machine by the node: the forced command, and the agent's key in authorized_keys in
    place of any earlier one (a resumed save still has the last machine's)."""
    line = f'restrict,pty,command="{WRAPPER}" {public_key.strip()} {KEY_COMMENT}'
    return (f"set -eu\numask 077\nmkdir -p {WRAPPER.rsplit('/', 1)[0]} ~/.ssh\n"
            f"cat > {WRAPPER}.new <<'OPENAJAR_AGENT_SSH'\n{AGENT_SSH.rstrip()}\nOPENAJAR_AGENT_SSH\n"
            f"chmod 755 {WRAPPER}.new && mv {WRAPPER}.new {WRAPPER}\n"
            "f=~/.ssh/authorized_keys\ntouch \"$f\"\n"
            f"grep -v ' {KEY_COMMENT}$' \"$f\" > \"$f.new\" || true\n"
            f"printf '%s\\n' {shlex.quote(line)} >> \"$f.new\"\nchmod 600 \"$f.new\" && mv \"$f.new\" \"$f\"\n")


def parse_activity(text: str) -> tuple[int, int] | None:
    """(seconds since the agent's ssh was last used, sessions running), by the machine's own clock."""
    m = _ACTIVITY_RE.search(text or "")
    if m is None:
        return None
    now, last, live = (int(g) for g in m.groups())
    return (max(0, now - last) if last else -1), live


# ------------------------------------------------------------------ files on this computer


def ssh_dir(home: Path) -> Path:
    return Path(home) / DIR


def config_path(home: Path) -> Path:
    return ssh_dir(home) / CONFIG


def alias(env_id: str) -> str:
    return f"openajar-{env_id}"


def shown_path(path: Path) -> str:
    """A path as a person types it: ~/... under the home folder."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def user_config() -> Path:
    return Path.home() / ".ssh" / "config"


def _q(value: str) -> str:
    return f'"{value}"' if any(c.isspace() for c in value) else value


def entry(env_id: str, remote: Any, key: Path, known_hosts: Path) -> str:
    name = alias(env_id)
    via = []
    if getattr(remote, "via", None) is not None:  # a container on the person's own machine: through that machine
        from openajar.vms.ssh import via_command

        via = [f"  ProxyCommand {via_command(remote.via)}"]
    lines = [f"{BEGIN}{env_id} (openajar writes this and removes it when the environment closes)",
             f"Host {name}",
             f"  HostName {remote.host}",
             f"  Port {remote.port}",
             f"  User {remote.user}",
             *via,
             f"  IdentityFile {_q(str(key))}",
             "  IdentitiesOnly yes",
             "  IdentityAgent none",
             "  ForwardAgent no",
             "  ForwardX11 no",
             f"  {FORWARDS_COMMENT}",
             "  ClearAllForwardings yes",
             f"  HostKeyAlias {name}",
             f"  UserKnownHostsFile {_q(str(known_hosts))}",
             "  StrictHostKeyChecking yes",
             "  UpdateHostKeys no",
             "  ServerAliveInterval 15",
             "  ServerAliveCountMax 4",
             f"{END}{env_id}"]
    return "\n".join(lines) + "\n"


def _blocks(text: str) -> list[tuple[str | None, str]]:
    """The config as (env id or None, text) pieces, so one entry is replaced or removed alone."""
    out: list[tuple[str | None, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        if cur is None and line.startswith(BEGIN):
            if buf:
                out.append((None, "".join(buf)))
            cur, buf = line[len(BEGIN):].split(" ", 1)[0].strip(), [line]
        elif cur is not None and line.startswith(END):
            buf.append(line)
            out.append((cur, "".join(buf)))
            cur, buf = None, []
        else:
            buf.append(line)
    if buf:
        out.append((cur, "".join(buf)))
    return out


def _write_config(home: Path, pieces: list[tuple[str | None, str]]) -> None:
    d = ssh_dir(home)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    write_private(config_path(home), "".join(text for _, text in pieces))


def present(home: Path, env_id: str) -> bool:
    path = config_path(home)
    with contextlib.suppress(OSError):
        return any(eid == env_id for eid, _ in _blocks(path.read_text()))
    return False


def make_key(home: Path, env_id: str) -> tuple[Path, str]:
    """A new key for the environment's agent: (the private key's path, the public line)."""
    from openajar.vms.keys import _pair

    if not _ID_OK.match(env_id):
        raise ValueError(f"not an environment id: {env_id!r}")
    d = ssh_dir(home) / env_id
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ssh_dir(home), 0o700)
    priv, pub = _pair(f"openajar-{env_id}")
    key = d / "id_ed25519"
    write_private(key, priv)
    write_private(d / "id_ed25519.pub", pub + "\n")
    return key, " ".join(pub.split()[:2])


def write_entry(home: Path, env_id: str, remote: Any, key: Path) -> None:
    """The host entry, and its known_hosts line: the VM's host key as the node pinned it."""
    pinned = Path(remote.known_hosts).read_text().split()
    if len(pinned) < 3:
        raise ValueError(f"no pinned host key for {remote.vm_id}")
    kh = ssh_dir(home) / env_id / "known_hosts"
    write_private(kh, f"{alias(env_id)} {pinned[1]} {pinned[2]}\n")
    path = config_path(home)
    text = path.read_text() if path.exists() else ""
    pieces = [(eid, t) for eid, t in _blocks(text) if eid != env_id]
    if pieces and not pieces[-1][1].endswith("\n"):
        pieces[-1] = (pieces[-1][0], pieces[-1][1] + "\n")
    pieces.append((env_id, entry(env_id, remote, key, kh)))
    _write_config(home, pieces)


def remove_entry(home: Path, env_id: str) -> bool:
    """The entry, its key and its known_hosts go. True when there was an entry."""
    path = config_path(home)
    had = False
    with contextlib.suppress(OSError):
        pieces = _blocks(path.read_text())
        had = any(eid == env_id for eid, _ in pieces)
        if had:
            _write_config(home, [(eid, t) for eid, t in pieces if eid != env_id])
    if _ID_OK.match(env_id):
        shutil.rmtree(ssh_dir(home) / env_id, ignore_errors=True)
    return had


# ------------------------------------------------------------------ ~/.ssh/config


def include_target(home: Path) -> str:
    return shown_path(config_path(home))


def _include_paths(line: str) -> list[str]:
    parts = line.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "include":
        return []
    return shlex.split(parts[1], comments=True)


def included(home: Path) -> bool:
    """~/.ssh/config includes the openajar config (anywhere in it), so plain `ssh openajar-<id>` works."""
    want = config_path(home).resolve()
    try:
        text = user_config().read_text()
    except OSError:
        return False
    for line in text.splitlines():
        for p in _include_paths(line):
            q = Path(os.path.expanduser(p))
            if not q.is_absolute():
                q = user_config().parent / q
            with contextlib.suppress(OSError):
                if q.resolve() == want:
                    return True
    return False


def add_include(home: Path) -> bool:
    """Put `Include <openajar config>` at the top of ~/.ssh/config (made 0600 if it is not there), and
    keep the rest as it is. At the top, so its entries' settings come before any `Host *` of the
    person's (ssh takes the first value it reads). False when it was there already."""
    if included(home):
        return False
    path = user_config()
    if path.is_symlink():  # a dotfiles setup links it: change the file it points at, keep the link
        path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    head = (f"# openajar: your agents' environments (ssh openajar-<id>); `openajar setup ssh` added this\n"
            f"Include {_q(include_target(home))}\n\n")
    if path.exists():
        mode = path.stat().st_mode & 0o777
        rest = path.read_text()
        tmp = path.with_name(path.name + ".openajar-new")
        tmp.write_text(head + rest)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    else:
        write_private(path, head)
    config_path(home).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not config_path(home).exists():
        write_private(config_path(home), "")
    return True


def ssh_field(home: Path, env: Environment) -> dict:
    """What an environment's result says about the agent's own ssh."""
    host = alias(env.id)
    w = env.workdir or "/work"
    if included(home):
        ssh, scp, rsync = "ssh", "scp", "rsync -az"
    else:
        cfg = shown_path(config_path(home))
        ssh, scp, rsync = f"ssh -F {cfg}", f"scp -F {cfg}", f"rsync -az -e 'ssh -F {cfg}'"
    return {"host": host, "command": f"{ssh} {host}",
            "examples": [f"{ssh} {host} 'cd {w} && git status'",
                         f"{rsync} --exclude node_modules --exclude .git ./ {host}:{w}/",
                         f"{scp} {host}:{w}/dist.tgz ."],
            "port_forwards": FORWARDS_WORDS}


# ------------------------------------------------------------------ the manager's side


class SshAccess:
    """For EnvironmentManager (its home, store, vms, envs and on_laptop)."""

    _ssh_failed: set[str]

    def ssh_wanted(self) -> bool:
        """Local mode only: the agent is on this computer. A server node's agent is not."""
        from openajar.environments.ports import on_laptop

        return on_laptop(self)

    def ssh_of(self, env: Environment) -> dict | None:
        """The `ssh` field for the environment's holder; None on a server node or while it has no entry."""
        if not self.ssh_wanted() or env.state != "ready" or not present(self.home, env.id):
            return None
        return ssh_field(self.home, env)

    async def ssh_up(self, env: Environment) -> bool:
        """The agent's key on the machine, then its host entry. Never fails the environment: without it
        the agent still has every openajar tool."""
        if not self.ssh_wanted() or not env.vm_id:
            return False
        try:
            vms = self.vms()
            remote = vms.remote(vms.store.get(env.vm_id))
            key, pub = make_key(self.home, env.id)
            res = await self.envs().exec(env.terminal_lane, "sh -s", stdin=authorize_script(pub), timeout_s=30)
            if res.exit_code != 0:
                raise RuntimeError((res.stderr or res.stdout or "").strip()[-300:] or f"exit {res.exit_code}")
            write_entry(self.home, env.id, remote, key)
        except (OpenajarError, OSError, ValueError, RuntimeError) as e:
            self.ssh_failed().add(env.id)
            log.warning("environment %s: no ssh for the agent: %s", env.id, getattr(e, "message", e))
            with contextlib.suppress(OSError):
                remove_entry(self.home, env.id)
            return False
        self.ssh_failed().discard(env.id)
        log.info("environment %s: ssh %s works from this computer", env.id, alias(env.id))
        return True

    def ssh_failed(self) -> set[str]:
        if not hasattr(self, "_ssh_failed"):
            self._ssh_failed = set()
        return self._ssh_failed

    def ssh_down(self, env: Environment) -> None:
        with contextlib.suppress(OSError):
            if remove_entry(self.home, env.id):
                log.info("environment %s: its ssh entry %s is removed", env.id, alias(env.id))

    async def ssh_keep(self, env: Environment, now: datetime | None = None) -> None:
        """For the reaper: the entry back after a restart (once; a machine that refused it is left), and
        the agent's ssh use counted as use of the machine (`ssh_seen`)."""
        if not self.ssh_wanted() or env.state != "ready":
            return
        if not present(self.home, env.id):
            if env.id in self.ssh_failed():
                return
            await self.ssh_up(env)
        await self.ssh_seen(env, now)

    async def ssh_seen(self, env: Environment, now: datetime | None = None) -> datetime | None:
        """Ask the machine when the agent's ssh was last used, and note it as the terminal lane's last use,
        so the idle rule and the autosave see it like a command run through openajar. The check is the
        node's own command: its own use of the lane is not noted (else the check alone would keep every
        machine busy). Returns the time noted, or None."""
        envs = self._get("envs")
        if envs is None:
            return None
        lane = env.terminal_lane
        before = seen = None
        with contextlib.suppress(Exception):
            before = envs.store.last_used(lane)
        try:
            from openajar.environments.activity import own

            res = await envs.exec(lane, ACTIVITY, timeout_s=15, **own(envs))
            got = parse_activity(res.stdout)
            if got is not None and got[0] >= 0:
                seen = (now or self._clock()) - timedelta(seconds=got[0])
        except OpenajarError:
            pass
        finally:
            keep = [t for t in (before, seen) if t is not None]
            with contextlib.suppress(Exception):
                envs.store.touch(lane, at=max(_aware(t) for t in keep) if keep else env.created_at)
        return seen


def _aware(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)
