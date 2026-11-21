"""`openajar mcp`: openajar as a stdio MCP server, for any client that starts a command (Codex, Cursor,
Claude Desktop, the MCP marketplaces, `uvx openajar mcp`, `npx -y openajar`).

Three cases, so a client never hangs on it and never gets a crash instead of an answer:
- the local node (`openajar local`, ~/.openajar) is set up and answers: bridge to it, exactly as
  `openajar mcp-bridge local` does (openajar.client.bridge);
- it is set up but stopped: start its service (as `openajar local start` does, without registering
  the clients again), then bridge;
- it was never set up on this computer, or it does not start: serve a small MCP server of its own
  (`HelpServer`) that answers initialize, says in its instructions and in its one tool what the person
  should run, and says the same on stderr.

stdout carries only MCP messages; everything else goes to stderr.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from openajar import __version__

INSTALL_LINE = "curl -fsSL https://openajar.com/install.sh | sh"
SETUP_LINE = "openajar setup"
DEFAULT_PROTOCOL = "2025-06-18"
HELP_TOOL = "openajar_setup_help"
START_TIMEOUT = 90.0  # how long the installed openajar may take to start the service


def log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(f"openajar mcp: {message}\n")
        sys.stderr.flush()


def not_installed_text() -> str:
    """For the npm launcher's twin (integrations/npm/bin/openajar.js): openajar is not on this computer."""
    return (f"openajar is not installed on this computer. To use it, install it in a terminal with "
            f"`{INSTALL_LINE}`, then run `{SETUP_LINE}`. " + _needs())


def _needs() -> str:
    return ("openajar gives the agent its own computer (a VM with a terminal and a real browser), so setup "
            "asks for a Hetzner Cloud token, or uses Docker running on this computer: one of the two is needed. "
            "Then start the agent again. Docs: https://github.com/sabalys-ramunas/openajar")


def not_set_up_text(home: Path) -> str:
    return (f"openajar is installed but not set up on this computer (nothing in {home}). Run `{SETUP_LINE}` "
            f"in a terminal (or install it again with `{INSTALL_LINE}`, which starts setup). " + _needs())


def no_start_text(why: str) -> str:
    return (f"openajar is set up on this computer, but its local service did not start: {why} "
            "Run `openajar local start` in a terminal and read what it says, then `openajar local logs`. "
            "Then start the agent again.")


def not_registered_text(home: Path) -> str:
    return (f"openajar's local node is in {home}, but the agents on this computer have no access to it yet. "
            "Run `openajar local start` in a terminal (it gives them access), then start the agent again.")


# ---- the server that only explains ----


class HelpServer:
    """A minimal stdio MCP server: initialize, ping, one tool that says what to run, empty prompts and
    resources. It never touches the network or the disk."""

    def __init__(self, problem: str, *, out: IO[bytes] | None = None):
        self.problem = problem
        self.out = out or sys.stdout.buffer

    def emit(self, message: dict) -> None:
        self.out.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
        self.out.flush()

    def answer(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "one JSON-RPC object per line"}}
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:
            return None  # a notification (initialized, cancelled) or a response: nothing to say
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            result: dict = {
                "protocolVersion": asked if isinstance(asked, str) and asked else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "openajar", "title": "openajar (not set up)", "version": __version__},
                "instructions": f"{self.problem} Tell the human this, in these words, before anything else; "
                                f"the {HELP_TOOL} tool says it again.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{
                "name": HELP_TOOL,
                "title": "How to set up openajar",
                "description": f"openajar is not ready on this computer. {self.problem}",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            }]}
        elif method == "tools/call":
            text = self.problem if params.get("name") == HELP_TOOL else (
                f"openajar has no tool {params.get('name')!r} until it is set up. {self.problem}")
            result = {"content": [{"type": "text", "text": text}], "isError": params.get("name") != HELP_TOOL}
        elif method in ("prompts/list", "resources/list", "resources/templates/list"):
            key = {"prompts/list": "prompts", "resources/list": "resources",
                   "resources/templates/list": "resourceTemplates"}[method]
            result = {key: []}
        else:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"openajar is not set up, so it has no {method}. {self.problem}"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve(self, stdin: IO[bytes] | None = None) -> int:
        stdin = stdin or sys.stdin.buffer
        with contextlib.suppress(KeyboardInterrupt, BrokenPipeError):
            for raw in iter(stdin.readline, b""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}})
                    continue
                reply = self.answer(message)
                if reply is not None:
                    self.emit(reply)
        return 0


# ---- deciding which one runs ----


@dataclass
class Deps:
    """What `openajar mcp` looks at and acts through. The defaults are this computer's; tests pass fakes."""
    home: Path | None = None
    client: Any = None
    answers: Any = None  # port -> something answers there
    start: Any = None  # (home, port) -> None when it runs now, else the reason it does not
    log: Any = log


def _answers(port: int) -> bool:
    from openajar.local.service import answers

    return answers(port)


def installed_cli() -> str | None:
    """The `openajar` the person installed (PATH, then ~/.local/bin), when it is another program than this
    one (`uvx openajar mcp` runs a copy in uv's cache): the service it starts must outlive that copy."""
    found = shutil.which("openajar") or (str(p) if (p := Path.home() / ".local/bin/openajar").exists() else None)
    if not found:
        return None
    try:
        same = Path(found).resolve().parent == Path(sys.executable).absolute().parent.resolve()
    except OSError:
        same = False
    return None if same else found


def _start(home: Path, port: int) -> str | None:
    """Start the local service on its port. The installed openajar does it when there is one (so the
    service runs that program, not a passing copy); else this one, as `openajar local start` does."""
    exe = installed_cli()
    if exe:
        try:
            proc = subprocess.run([exe, "mcp", "--start-only", "--home", str(home)], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=START_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{exe} could not start it ({exc})."
        if proc.returncode == 0:
            return None
        if proc.returncode != 2:  # 2: an older openajar without --start-only, so start it from here
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            return lines[-1].removeprefix("openajar mcp: ") if lines else f"{exe} exited {proc.returncode}."
    return start_here(home, port)


def start_here(home: Path, port: int | None) -> str | None:
    from openajar.client.errors import ClientError
    from openajar.local import service

    try:
        state = service.start(home, port)
    except ClientError as exc:
        return exc.message + (f" ({exc.hint})" if exc.hint else ".")
    return None if state.running else state.note


def plan(deps: Deps) -> tuple[Any, str | None]:
    """(the local connection to bridge to, None) or (None, the problem the help server explains)."""
    from openajar.client.errors import ClientError
    from openajar.local import home as local_home
    from openajar.local.paths import NAME, default_home

    home = (deps.home or default_home()).expanduser()
    try:
        cfg = local_home.read(home)
    except ClientError as exc:
        return None, no_start_text(exc.message + (f" ({exc.hint})." if exc.hint else "."))
    if cfg is None:
        return None, not_set_up_text(home)
    client = deps.client
    if client is None:
        from openajar.client.ops import Client

        client = deps.client = Client(out=deps.log)
    try:
        conn = client.get(NAME)
    except ClientError:
        return None, not_registered_text(home)
    try:
        token = client.tokens(conn.token_store).get(conn.name)
    except ClientError:
        token = None
    if not token:
        return None, not_registered_text(home)
    answers = deps.answers or _answers
    if answers(conn.local_port):
        return conn, None
    deps.log(f"the local openajar in {home} is not running; starting it")
    why = (deps.start or _start)(home, conn.local_port)
    if why is not None:
        return None, no_start_text(why)
    deps.log(f"started on port {conn.local_port}")
    return conn, None


def run(home: Path | None = None, *, deps: Deps | None = None, stdin: IO[bytes] | None = None,
        out: IO[bytes] | None = None) -> int:
    """`openajar mcp`. Returns only from the help server (the bridge ends the process itself)."""
    from openajar.client import bridge

    deps = deps or Deps(home=home)
    if deps.home is None:
        deps.home = home
    if stdin is None:  # the real command, not a test: the client stops us with a signal
        bridge.quit_on_signals()
    try:
        conn, problem = plan(deps)
    except Exception as exc:  # noqa: BLE001  (an answer that says what broke beats a client that hangs)
        conn, problem = None, no_start_text(f"{type(exc).__name__}: {exc}.")
    if conn is not None:
        bridge.serve_connection(deps.client, conn, stdin)
        return 0
    deps.log(problem)
    return HelpServer(problem, out=out).serve(stdin)


def start_only(home: Path | None) -> int:
    """`openajar mcp --start-only`: start the local service from this (installed) program, quietly; the
    reason on stderr and exit 1 when it does not run."""
    from openajar.local import home as local_home
    from openajar.local.paths import default_home

    root = (home or default_home()).expanduser()
    try:
        cfg = local_home.read(root)
    except Exception as exc:  # noqa: BLE001
        log(str(exc))
        return 1
    if cfg is None:
        log(not_set_up_text(root))
        return 1
    why = start_here(root, cfg.mcp_port)
    if why is not None:
        log(why)
        return 1
    return 0
