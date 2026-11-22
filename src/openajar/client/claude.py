"""Register a connection with Claude Code, and the equivalent lines for other MCP clients.

The entry is a user-scope HTTP server with a `headersHelper` instead of a static header:

    claude mcp add-json --scope user openajar-<name> '{"type": "http", "url": ...,
        "headersHelper": "~/.config/openajar/bin/openajar-mcp-headers <name>"}'

`claude mcp add --header "Authorization: Bearer <token>"` would put the token on Claude
Code's command line (visible in `ps`) and in ~/.claude.json. The helper reads it from the
Keychain each time Claude Code connects, and again after a 401, so a rotated token only
needs `openajar connect` to run again.

Claude Code can run with several profiles on one computer: the default one keeps its user-scope
servers in ~/.claude.json, and one started with `CLAUDE_CONFIG_DIR=DIR` keeps them in DIR/.claude.json
(people keep a work and a personal one, and T3 Code makes its own). `profiles` finds them all, and
`ClaudeCode` drives the `claude` CLI with that profile's CLAUDE_CONFIG_DIR, so every profile gets the
entry and a session started with any of them sees the tools.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.client.errors import ClientError
from openajar.client.state import Connection, shown

CLAUDE_TIMEOUT = 60
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
ANSWER_TIMEOUT = 1.0  # how long a check waits for a loopback endpoint to accept a connection

# Claude Code loads MCP servers when a session starts: what connect tells the person.
RESTART_NOTE = ("Claude Code sessions that are open now load MCP servers only when they start: restart them "
                "to see the tools. Until then any shell reaches them: `openajar call <tool>` "
                "(`openajar tools` lists them).")


@dataclass(frozen=True)
class Profile:
    """One Claude Code profile: the default one (`config_dir` None, ~/.claude.json), or the folder a
    session started with CLAUDE_CONFIG_DIR uses (its user-scope servers in DIR/.claude.json)."""

    home: Path
    config_dir: Path | None = None

    @property
    def config_file(self) -> Path:
        return (self.config_dir or self.home) / ".claude.json"

    @property
    def skills_dir(self) -> Path:
        return (self.config_dir or self.home / ".claude") / "skills"

    @property
    def label(self) -> str:
        """The profile's file, as messages show it: ~/.claude.json, ~/.claude_work/.claude.json."""
        return shown(self.config_file)

    def command(self, line: str) -> str:
        """A `claude ...` line that acts on this profile when a person runs it."""
        if self.config_dir is None:
            return line
        return f"{CONFIG_DIR_ENV}={shlex.quote(str(self.config_dir))} {line}"

    def servers(self) -> dict[str, dict[str, Any]] | None:
        """The profile's user-scope MCP servers, or None when its file is missing or does not parse."""
        try:
            data = json.loads(self.config_file.read_text())
        except (OSError, ValueError):
            return None
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return {}
        return {name: entry for name, entry in servers.items() if isinstance(entry, dict)}


def profiles(home: Path, env: Mapping[str, str] | None = None) -> list[Profile]:
    """Every Claude Code profile here: the default one, the folder $CLAUDE_CONFIG_DIR names, and each
    folder ~/.claude* that holds a .claude.json (a profile a session was started with), once each."""
    env = os.environ if env is None else env
    found = [Profile(home)]
    seen = {_key(found[0].config_file)}
    named = env.get(CONFIG_DIR_ENV)
    dirs = [Path(named).expanduser()] if named else []
    try:
        dirs += sorted(p for p in home.glob(".claude*") if p.is_dir() and (p / ".claude.json").is_file())
    except OSError:
        pass
    for folder in dirs:
        profile = Profile(home, folder)
        key = _key(profile.config_file)
        if key not in seen:
            seen.add(key)
            found.append(profile)
    return found


def _key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def entry_json(conn: Connection, helper: str) -> str:
    return json.dumps({"type": "http", "url": conn.url, "headersHelper": helper})


def manual_lines(conn: Connection, helper: str, where: list[Profile] | None = None) -> list[str]:
    """The lines that register the connection by hand, in each profile of `where` (default: the default one)."""
    lines = [f"claude mcp remove --scope user {conn.claude_name}",
             f"claude mcp add-json --scope user {conn.claude_name} {shlex.quote(entry_json(conn, helper))}"]
    if not where:
        return lines
    return [profile.command(line) for profile in where for line in lines]


LEGACY_HELPER = "agentos-mcp-headers"  # legacy-name: the headers helper an entry before the rename runs
LEGACY_PREFIX = "agentos-"  # legacy-name: the entries' names before the rename


class ClaudeCode:
    def __init__(self, exe: str, home: Path, profile: Profile | None = None):
        self.exe = exe
        self.home = home  # run from $HOME so no project's .mcp.json or local scope gets involved
        self.profile = profile or Profile(home)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != CONFIG_DIR_ENV}
        if self.profile.config_dir is not None:
            env[CONFIG_DIR_ENV] = str(self.profile.config_dir)
        try:
            return subprocess.run([self.exe, "mcp", *args], check=False, capture_output=True, text=True,
                                  timeout=CLAUDE_TIMEOUT, cwd=self.home, stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            raise ClientError(f"`claude mcp {args[0]}` did not finish in {CLAUDE_TIMEOUT} seconds") from None
        except OSError as exc:
            raise ClientError(f"could not run claude: {exc.strerror}") from None

    def register(self, conn: Connection, helper: str) -> bool:
        """Replace any user-scope entry with the same name, then add ours. False when the profile
        already held exactly this entry (nothing is run)."""
        wanted = json.loads(entry_json(conn, helper))
        if (self.profile.servers() or {}).get(conn.claude_name) == wanted:
            return False
        self._run("remove", "--scope", "user", conn.claude_name)
        proc = self._run("add-json", "--scope", "user", conn.claude_name, entry_json(conn, helper))
        if proc.returncode != 0:
            text = (proc.stderr or proc.stdout).strip()
            raise ClientError(f"claude mcp add-json failed: {text.splitlines()[-1] if text else proc.returncode}",
                              "add it by hand with the lines below")
        return True

    def get(self, name: str) -> str | None:
        """What `claude mcp get` says about the entry, or None when Claude Code has none."""
        proc = self._run("get", name)
        return proc.stdout if proc.returncode == 0 else None

    def unregister(self, name: str) -> bool:
        servers = self.profile.servers()
        if servers is not None and name not in servers:
            return False  # the profile's file says there is no such user-scope entry: nothing to run
        return self._run("remove", "--scope", "user", name).returncode == 0

    def user_entry(self, name: str) -> dict[str, Any]:
        """The user-scope entry as the profile's .claude.json holds it, or {}: `claude mcp get` does not
        print an entry's headersHelper, so that is where the helper an entry runs is read."""
        return (self.profile.servers() or {}).get(name) or {}

    def unregister_legacy(self, name: str, url: str | None = None) -> bool:
        """Remove an entry an older version wrote under its old name: only one that runs through our
        headers helper or points at our node's `url` (an entry the owner made under that name for
        something else stays). Whether or not the new entry was there already."""
        servers = self.profile.servers()
        if servers is not None:
            if name not in servers:
                return False
            return self.unregister(name) if legacy_ours(servers[name], url) else False
        found = self.get(name)  # no readable .claude.json: what `claude mcp get` prints tells
        if not found:
            return False
        entry = self.user_entry(name)
        urls = [entry.get("url"), *(line.strip().removeprefix("URL:").strip() for line in found.splitlines()
                                    if line.strip().startswith("URL:"))]
        ours = LEGACY_HELPER in found or LEGACY_HELPER in str(entry.get("headersHelper") or "") or (
            url is not None and any(same_node(url, other) for other in urls))
        return self.unregister(name) if ours else False

    def unregister_stale(self, url: str | None = None, answers: Callable[[str], bool] | None = None) -> list[str]:
        """Every entry from before the rename (named agentos-*) that is ours: it runs our old headers helper,
        points at our node's `url`, or is an MCP address on this computer where nothing answers any more
        (a connection that was removed or renamed). Returns the names removed."""
        removed = []
        for name, entry in sorted((self.profile.servers() or {}).items()):
            if name.startswith(LEGACY_PREFIX) and legacy_ours(entry, url, answers=answers) and self.unregister(name):
                removed.append(name)
        return removed


def legacy_ours(entry: dict[str, Any], url: str | None = None, *,
                answers: Callable[[str], bool] | None = None) -> bool:
    """Whether an entry under a name from before the rename is one openajar wrote: it runs our old headers
    helper, it points at our node, or it is an MCP address on this computer that does not answer.
    An entry elsewhere, or one that answers, stays: the owner may have made it for something else."""
    if LEGACY_HELPER in str(entry.get("headersHelper") or ""):
        return True
    target = entry.get("url")
    if url is not None and same_node(url, target):
        return True
    return dead_loopback(target, answers)


def dead_loopback(url: Any, answers: Callable[[str], bool] | None = None) -> bool:
    """A loopback MCP address (http://localhost:PORT/mcp) where nothing listens."""
    if not isinstance(url, str) or not loopback_mcp(url):
        return False
    return not (answers or endpoint_answers)(url)


def loopback_mcp(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "http" and parts.hostname in _LOOPBACK and parts.port is not None
                and parts.path.rstrip("/") == "/mcp")
    except ValueError:
        return False


def endpoint_answers(url: str, timeout: float = ANSWER_TIMEOUT) -> bool:
    """Whether something accepts a connection at the URL's host and port."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if not host or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def same_node(url: str, other: Any) -> bool:
    """Whether two MCP URLs reach the same node: equal, or both on this computer at one port and path
    (an older version wrote 127.0.0.1 where this one writes localhost)."""
    if not isinstance(other, str) or not other:
        return False
    if url == other:
        return True
    a, b = urlsplit(url), urlsplit(other)
    try:
        return (a.hostname in _LOOPBACK and b.hostname in _LOOPBACK and a.port == b.port
                and a.path.rstrip("/") == b.path.rstrip("/"))
    except ValueError:  # a port that is not a number
        return False


@dataclass
class Registered:
    """What registering a connection in every Claude Code profile did."""

    written: list[Profile]                      # the entry was added or replaced
    kept: list[Profile]                         # it was there already, exactly
    failed: list[tuple[Profile, str]]           # claude refused it there
    removed: list[tuple[Profile, str]]          # entries from before the rename that went

    @property
    def done(self) -> list[Profile]:
        return [*self.written, *self.kept]


def register_everywhere(exe: str, home: Path, conn: Connection, helper: str,
                        found: list[Profile] | None = None) -> Registered:
    """The connection's entry in every profile, and the entries from before the rename removed there."""
    out = Registered([], [], [], [])
    for profile in found if found is not None else profiles(home):
        claude = ClaudeCode(exe, home, profile)
        try:
            (out.written if claude.register(conn, helper) else out.kept).append(profile)
        except ClientError as exc:
            out.failed.append((profile, exc.message))
            continue
        gone = [conn.legacy_claude_name] if claude.unregister_legacy(conn.legacy_claude_name, conn.url) else []
        gone += [name for name in claude.unregister_stale(conn.url) if name not in gone]
        out.removed += [(profile, name) for name in gone]
    return out


@dataclass
class ProfileProblem:
    """An entry in one profile that does not work: what is wrong and the command that fixes it."""

    profile: Profile
    name: str
    detail: str
    fix: str


def profile_problems(home: Path, conns: list[Connection], again: Callable[[Connection], str], *,
                     env: Mapping[str, str] | None = None,
                     answers: Callable[[str], bool] | None = None) -> list[ProfileProblem]:
    """What `openajar status --check` reports about Claude Code on this computer: in each profile, an
    openajar or agentos entry on this computer whose address does not answer, and a connection's entry
    that is missing (a session started with that profile has no openajar tools). `again` says how to
    make a connection again (openajar.client.ops.connect_again), which writes every profile."""
    answers = answers or endpoint_answers
    by_name = {c.claude_name: c for c in conns}
    problems: list[ProfileProblem] = []
    for profile in profiles(home, env):
        servers = profile.servers()
        if servers is None:
            continue  # no such file (a profile never used), or one that does not parse: Claude Code says so
        for name, entry in sorted(servers.items()):
            url = entry.get("url")
            if not name.startswith(("openajar-", LEGACY_PREFIX)) or not dead_loopback(url, answers):
                continue
            conn = by_name.get(name)
            if conn is not None and same_node(conn.url, url):
                detail, fix = f"{name} points at {url}, and nothing answers there", again(conn)
            elif conn is not None:
                detail = f"{name} points at {url}, an old address where nothing answers"
                fix = f"{again(conn)} (it writes the current address in every profile)"
            else:
                detail = f"{name} points at {url}, where nothing answers, and no openajar connection here uses it"
                fix = f"remove it: `{profile.command(f'claude mcp remove --scope user {name}')}`"
            problems.append(ProfileProblem(profile, name, detail, fix))
        for conn in conns:
            if conn.claude_registered and conn.claude_name not in servers:
                problems.append(ProfileProblem(
                    profile, conn.claude_name,
                    f"no {conn.claude_name}, so Claude Code sessions started with this profile do not see the tools",
                    again(conn)))
    return problems
