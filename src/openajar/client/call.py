"""`openajar call <tool>`: call any MCP tool from a shell, through a connection.

The call goes over the connection's MCP endpoint with its agents' grant, so it is exactly what an
agent on this computer could do: the same grant, the same policy, the same approvals. A result that
waits for a person (`approval_pending`) is shown as such; with `--wait` the command waits for the
decision (approval_wait) and, once approved, calls the tool again with the same arguments, as an
agent is told to. Nothing here approves anything.

`openajar flows pull|push|diff` (openajar.runner.flowsync) open their session the same way.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

import typer

from openajar.client.errors import ClientError
from openajar.client.mcp_http import MCP_PATH, McpError, McpSession, ToolError

TOKEN_ENV = "OPENAJAR_TOKEN"
PENDING = "approval_pending"
WAIT_STEP_S = 60.0  # one approval_wait long-poll (the node caps it at 120 seconds)
EXIT_PENDING = 2  # still waiting for a person when the command ends


class Session(Protocol):
    """What a command needs of a node: call a tool, get its result or a ToolError."""

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any: ...


class CallError(ClientError):
    """A tool refused (its error text starts with the code), or a person denied the call."""


class StillPending(ClientError):
    """The call still waits for a person."""

    def __init__(self, result: dict[str, Any]):
        super().__init__(f"waiting for a person to approve: {result.get('summary') or result.get('tool')}",
                         "approve it in the panel (Needs you), then run the same command again, or pass --wait")
        self.result = result


@contextmanager
def open_session(node: str | None = None, url: str | None = None, *, timeout: float = 60.0) -> Iterator[Session]:
    """An MCP session as this computer's agents have it: the connection `node` (the only one when
    None), or `url` (http://host:port, its /mcp) with the token in $OPENAJAR_TOKEN, for a dev node
    or a script."""
    if url:
        token = os.environ.get(TOKEN_ENV)
        if not token:
            raise ClientError(f"--url needs the token in ${TOKEN_ENV}",
                              "use the agent token `openajar dev up` prints, or a grant from `openajar grants issue`")
        parts = urlsplit(url if "://" in url else f"http://{url}")
        if parts.scheme != "http" or not parts.hostname or parts.port is None or parts.path not in ("", "/", MCP_PATH):
            raise ClientError(f"{url} is not an MCP address", "give it as http://127.0.0.1:7450 (its /mcp is used)")
        session = McpSession(parts.port, token, timeout=timeout, host=parts.hostname)
        try:
            session.initialize()
        except McpError as exc:
            raise ClientError(f"{url}: {exc.detail}", "is the node running, and is the token current?") from None
    else:
        from openajar.client.ops import Client

        client = Client(out=lambda _: None)
        session = client.mcp_session(client.pick(node), timeout=timeout)
    try:
        yield session
    finally:
        session.close()


def is_pending(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") == PENDING and bool(result.get("approval_id"))


def call_tool(session: Session, tool: str, arguments: dict[str, Any], *, wait_s: float = 0.0,
              say: Callable[[str], None] = lambda _: None, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Call `tool`. A tool error raises CallError with the node's own text. A call that waits for a
    person: with `wait_s`, wait for the decision up to that long; approved, call again with the same
    arguments and return that; denied or expired, CallError. Still waiting: StillPending."""
    try:
        result = session.call(tool, arguments)
    except ToolError as exc:
        raise CallError(str(exc).strip() or f"{tool} failed") from None
    except McpError as exc:
        raise ClientError(exc.detail) from None
    deadline = time.monotonic() + max(0.0, wait_s)
    while is_pending(result):
        approval = str(result["approval_id"])
        if time.monotonic() >= deadline:
            raise StillPending(result)
        say(f"Waiting for a person to approve: {result.get('summary') or tool} (approval {approval}).")
        status = _wait(session, approval, deadline, sleep)
        state = status.get("state")
        if state == "pending":
            raise StillPending(result)
        if state != "approved":
            note = f": {status['note']}" if status.get("note") else ""
            raise CallError(f"{tool}: the approval was {state}{note}")
        if status.get("edited"):  # the approver changed it: the run goes on with their version
            say("Approved with changes; the node carries on with the approved version.")
            return status
        say("Approved. Calling it again with the same arguments.")
        try:
            result = session.call(tool, arguments)
        except ToolError as exc:
            raise CallError(str(exc).strip() or f"{tool} failed") from None
    return result


def _wait(session: Session, approval: str, deadline: float, sleep: Callable[[float], None]) -> dict[str, Any]:
    while True:
        left = deadline - time.monotonic()
        try:
            status = session.call("approval_wait", {"approval_id": approval,
                                                    "wait_s": max(1.0, min(WAIT_STEP_S, left))})
        except ToolError as exc:
            raise CallError(str(exc).strip()) from None
        if not isinstance(status, dict):
            raise CallError(f"approval_wait answered {status!r}")
        if status.get("state") != "pending" or time.monotonic() >= deadline:
            return status
        sleep(0.5)


def parse_arguments(pairs: list[str] | None, raw: str | None, on: str | None) -> dict[str, Any]:
    """--json (an object, or @file), then each --arg key=value (a JSON value, else text), then --on."""
    args: dict[str, Any] = {}
    if raw:
        text = Path(raw[1:]).read_text(encoding="utf-8") if raw.startswith("@") else raw
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            raise ClientError(f"--json is not JSON: {exc}", "pass an object like '{\"on\": \"biz-a/x\"}'") from None
        if not isinstance(loaded, dict):
            raise ClientError("--json must be an object of argument: value")
        args.update(loaded)
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ClientError(f"--arg needs key=value, got {pair!r}")
        try:
            args[key] = json.loads(value)
        except ValueError:
            args[key] = value
    if on:
        args["on"] = on
    return args


def show(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


NodeOpt = Annotated[str | None, typer.Option("--node", help="The connection to use (default: the only one).")]
UrlOpt = Annotated[str | None, typer.Option("--url", help="An MCP address instead of a connection, like "
                                                          f"http://127.0.0.1:7450, with the token in ${TOKEN_ENV}.")]


def call(
    tool: Annotated[str, typer.Argument(help="The tool's name, as `openajar tools` lists it (for example "
                                              "environment or terminal_exec).")],
    arg: Annotated[list[str] | None, typer.Option("--arg", "-a", help="key=value; the value may be JSON "
                                                  "(--arg limit=5, --arg 'params={\"text\": \"hi\"}').")] = None,
    as_json: Annotated[str | None, typer.Option("--json", help="All the arguments as one JSON object, or @file.")]
    = None,
    on: Annotated[str | None, typer.Option("--on", help="The environment or lane the tool acts on (the `on` from "
                                                        "environment_open, or workspace/name).")]
    = None,
    wait: Annotated[float, typer.Option("--wait", help="Seconds to wait when a person must approve first; once "
                                                       "approved, the call runs again.")] = 0.0,
    node: NodeOpt = None,
    url: UrlOpt = None,
) -> None:
    """Call any openajar tool from a shell, as your agents would: the same grant, policy and approvals.

    For an agent whose session has not loaded the openajar tools, and for scripts.

    Claude Code loads MCP servers only when a session starts. `openajar tools` lists the tools.

    Examples:

      openajar call environment

      openajar call environment_open --arg title=checkout --arg keep=none

      openajar call terminal_exec --on e7k2 --arg cmd="pnpm test"

      openajar call file_read --json '{"on": "e7k2", "path": "README.md"}'

    Prints the result as JSON. Exit codes: 0 done, 1 refused or failed, 2 still waiting for a person.

    For 2, approve it in the panel and run the same command again, or pass --wait."""
    try:
        arguments = parse_arguments(arg, as_json, on)
        with open_session(node, url) as session:
            result = call_tool(session, tool, arguments, wait_s=wait, say=lambda line: typer.echo(line, err=True))
    except StillPending as exc:
        typer.echo(show(exc.result))
        typer.echo(f"{exc.message}. {exc.hint}", err=True)
        raise typer.Exit(EXIT_PENDING) from None
    except ClientError as exc:
        typer.secho(f"error: {exc.message}", err=True, fg=typer.colors.RED)
        if exc.hint:
            typer.echo(f"  {exc.hint}", err=True)
        raise typer.Exit(1) from None
    typer.echo(show(result))
