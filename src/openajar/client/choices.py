"""What `openajar connect` is asked and answers: the agent grant's options, which clients and how
to install, and the results and health it reports."""

from __future__ import annotations

import re
import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from openajar.client.errors import ClientError
from openajar.client.state import (
    Connection,
)
from openajar.client.tunnel import ServiceState
from openajar.core import toolsets

DEFAULT_CEILING = "commit"


DEFAULT_AGENT_TTL = "90d"


DEFAULT_CLIENT = "claude-code"


# memory_read: the agent reads a workspace's memory first (memory_context), as the node's instructions
# ask; writing memory stays a special the human adds with --special memory. environments: the agent
# may open its own cloud computer for the chat (environment_open, which never asks: the node's limits
# bound it) and run commands there, on lanes env/* only
DEFAULT_SPECIALS = ["environments", "memory_read"]


NEWER_SPECIALS = ("environments", "memory_read")  # names an older node refuses


CEILINGS = ("read", "draft", "commit", "pay", "irreversible")


CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


TTL_RE = re.compile(r"^[1-9][0-9]{0,4}[mhdw]$")


@dataclass
class Tools:
    ssh: str | None = None
    security: str | None = None
    launchctl: str | None = None
    systemctl: str | None = None
    journalctl: str | None = None
    claude: str | None = None
    rsync: str | None = None

    @classmethod
    def find(cls) -> Tools:
        return cls(**{name: shutil.which(name) for name in cls.__dataclass_fields__})


def computer_name() -> str:
    """This computer, for the agent grant's subject and the device label: `Robin-MacBook.local`
    becomes `robin-macbook`."""
    host = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9-]+", "-", host).strip("-")[:40] or "computer"


def parse_toolsets(value: str) -> list[str] | None:
    """`--toolsets files,flows` -> ["core", "files", "flows"] (core is always listed), "full" or
    "all" -> ["full"], "default" -> None (whatever the node lists by default)."""
    words = [w for w in re.split(r"[\s,]+", value.strip().lower()) if w]
    if not words:
        raise ClientError("--toolsets needs toolset names, full or default",
                          f"toolsets: {', '.join(toolsets.NAMES)}")
    if words == ["default"]:
        return None
    if set(words) & toolsets.FULL_ALIASES:
        return [toolsets.FULL]
    known = (*toolsets.NAMES, toolsets.OTHER)
    words = [toolsets.TOOLSET_ALIASES.get(w, w) for w in words]
    unknown = [w for w in words if w not in known]
    if unknown:
        raise ClientError(f"--toolsets: no toolset named {', '.join(unknown[:3])}",
                          f"toolsets: {', '.join(known)}, or full (every tool) or default (the node's)")
    return [name for name in known if name == toolsets.CORE or name in words]


@dataclass
class AgentOptions:
    """What the agent grant of a connection says. None: what the connection had, else the default."""
    ceiling: str | None = None
    ttl: str | None = None
    client: str | None = None
    specials: list[str] | None = None
    new_grant: bool = False
    toolsets: str | None = None  # `--toolsets`: names, "full", or "default"; None keeps what it had

    def chosen_toolsets(self, previous: list[str] | None) -> list[str] | None:
        """The toolsets the connection's URL asks for: None is the node's own default."""
        if self.toolsets is None:
            return previous
        return parse_toolsets(self.toolsets)

    def check(self) -> None:
        if self.toolsets is not None:
            parse_toolsets(self.toolsets)
        if self.ceiling is not None and self.ceiling not in CEILINGS:
            raise ClientError(f"--ceiling must be one of {', '.join(CEILINGS)}")
        if self.ttl is not None and not TTL_RE.match(self.ttl):
            raise ClientError("--agent-ttl looks like 30d, 12w or 8760h")
        if self.client is not None and not CLIENT_RE.match(self.client):
            raise ClientError("--client is a short lowercase name, for example claude-code or codex")


@dataclass
class ClientChoice:
    """Which of this computer's agent clients `connect` sets up (`--no-claude`, `--no-codex`,
    `--no-skills`). Each one is set up only when it is installed here."""
    claude: bool = True
    codex: bool = True
    skills: bool = True


@dataclass
class InstallChoice:
    """What connect does on a host with no node: ask once (`confirm`, None when nobody can answer),
    install without asking (`--install`), or never."""
    mode: Literal["ask", "yes", "no"] = "ask"
    confirm: Callable[[str], bool] | None = None


@dataclass
class ClientLine:
    """One line of connect's summary: a client and what happened to it."""
    client: str   # "Claude Code", "Codex", "Skills"
    state: str    # registered, already registered, not installed, skipped, failed, installed, updated
    detail: str = ""


@dataclass
class ConnectResult:
    conn: Connection
    tunnel_reused: bool
    token_changed: bool
    claude_lines: list[str] = field(default_factory=list)  # set when it must be registered by hand
    claude_error: str | None = None
    claude_profiles: list[str] = field(default_factory=list)  # the Claude Code profiles that hold the entry
    grant_issued: bool = False
    notes: list[str] = field(default_factory=list)  # warnings to show after the summary
    clients: list[ClientLine] = field(default_factory=list)
    installed: bool = False                # connect installed the node on the host first
    sign_in: str | None = None             # then: a one-time panel link through the tunnel


@dataclass
class CheckItem:
    what: str
    state: Literal["ok", "fail", "skip"]
    detail: str
    fix: str | None = None


@dataclass
class Health:
    conn: Connection
    service: ServiceState
    mcp: str
    ok: bool = False
    server: dict[str, Any] = field(default_factory=dict)
