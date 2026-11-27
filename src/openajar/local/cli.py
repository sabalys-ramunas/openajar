"""`openajar local`: openajar on this computer, for the agents on it. Imported by openajar.cli for every
command, so the node itself (openajar.local.node) is imported only when `serve` runs."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Annotated

import typer

from openajar.client.errors import ClientError
from openajar.local.paths import DEFAULT_PORT, default_home, log_path

app = typer.Typer(no_args_is_help=True, help=(
    "openajar on this computer: it serves your agents (Claude Code, Codex) and rents a cloud VM only "
    "while an agent has an environment open."))

HomeOption = Annotated[Path | None, typer.Option("--home", help="The local node's folder (default: ~/.openajar).")]
PortOption = Annotated[int | None, typer.Option(
    "--port", min=1, max=65535,
    help=f"Port on 127.0.0.1 (default: the one it used before, else {DEFAULT_PORT} or the next free one).")]


def _home(home: Path | None) -> Path:
    return (home or default_home()).expanduser()


def _fail(exc: ClientError) -> typer.Exit:
    typer.secho(f"error: {exc.message}", err=True, fg=typer.colors.RED)
    if exc.hint:
        typer.echo(f"  {exc.hint}", err=True)
    return typer.Exit(1)


def _show(state, *, as_json: bool = False) -> None:
    if as_json:
        data = dataclasses.asdict(state)
        data["log_path"] = str(state.log_path)
        typer.echo(json.dumps(data, indent=2))
        return
    typer.echo(state.note)
    if state.url:
        typer.echo(f"  MCP   {state.url}")
    if state.pid:
        typer.echo(f"  pid   {state.pid}")
    typer.echo(f"  log   {state.log_path}")


@app.command("serve")
def serve(home: HomeOption = None, port: PortOption = None,
          verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
          skills: Annotated[bool | None, typer.Option(
              "--skills/--no-skills",
              help="Bring the Claude Code skills openajar installed up to this openajar (default: only when "
                   "`openajar local start` registered the clients for this home).")] = None) -> None:
    """Run the local node in the foreground (the service runs this). Ctrl-C stops it."""
    from openajar.core import logs
    from openajar.local import home as local_home
    from openajar.local.node import run
    from openajar.node.daemon import install_log_scrubbers

    root = _home(home)
    try:
        # the remembered port as it is: Claude Code and Codex were given it. Only `start` moves it (and
        # registers them again); a port someone else took fails here, in the log, until then.
        known = local_home.read(root)
        chosen = port or (known.mcp_port if known is not None else local_home.choose_port(root, None))
        cfg = local_home.ensure(root, chosen)
    except ClientError as exc:
        raise _fail(exc) from None
    logs.setup(root, verbose=verbose, journal=False)
    install_log_scrubbers(cfg)
    if skills if skills is not None else local_home.registered(root):
        _refresh_skills()
    else:  # a second home (a test's, a builder's) or one told not to register: the person's folders stay as they are
        logging.getLogger("openajar.local").info("the Claude Code skills are left as they are: this home did not "
                                                "register the clients (`openajar local start` does)")
    try:
        # only the home `start` registered renews the agent grant the clients read (never a test's or a builder's)
        asyncio.run(run(cfg, renew=local_home.registered(root)))
    except KeyboardInterrupt:
        pass
    except ClientError as exc:  # a setting the person can fix, in words (environments.toml browser = "node")
        logging.getLogger("openajar.local").error("the local node did not start: %s %s", exc.message, exc.hint or "")
        raise _fail(exc) from None
    except Exception:
        # through the log (scrubbed), never a raw traceback: it can quote anything
        logging.getLogger("openajar.local").exception("the local node stopped on an unexpected error")
        raise typer.Exit(1) from None


def _refresh_skills() -> None:
    """The Claude Code skills openajar installed, brought up to this openajar (an update restarts the service:
    copied only by `openajar setup agents` and `openajar connect`, they were a day old on 2026-11-16). Only
    openajar's own skill folders; never a reason the node does not start."""
    from openajar.client import skills

    log = logging.getLogger("openajar.local")
    try:
        done = skills.refresh(Path.home())
    except OSError as exc:
        log.warning("could not refresh the Claude Code skills in %s: %s", skills.skills_dir(Path.home()), exc.strerror)
        return
    if done:
        log.info("Claude Code skills in %s: %s", skills.skills_dir(Path.home()), done)


@app.command("start")
def start(
    home: HomeOption = None,
    port: PortOption = None,
    register: Annotated[bool, typer.Option("--register/--no-register",
                                           help="Register Claude Code and Codex here as openajar-local.")] = True,
    claude: Annotated[bool, typer.Option("--claude/--no-claude", help="Register Claude Code.")] = True,
    codex: Annotated[bool, typer.Option("--codex/--no-codex", help="Register Codex.")] = True,
    skills: Annotated[bool, typer.Option("--skills/--no-skills", help="Install the Claude Code skills.")] = True,
) -> None:
    """Start the local node as a service (launchd on macOS, systemd on Linux), now and at every login."""
    from openajar.local import home as local_home
    from openajar.local import service
    from openajar.local.register import register_clients

    root = _home(home)
    try:
        local_home.mark_registered(root, register and skills)  # what its serve may refresh, before it starts
        state = service.start(root, port)
        _show(state)
        if register and state.port:
            typer.echo("")
            for line in register_clients(root, state.port, claude=claude, codex=codex, skills=skills):
                typer.echo(line)
    except ClientError as exc:
        raise _fail(exc) from None
    if not state.running:
        raise typer.Exit(1)


@app.command("stop")
def stop(home: HomeOption = None) -> None:
    """Stop the local node. It stays stopped, also after a restart, until `openajar local start`."""
    from openajar.local import service

    try:
        _show(service.stop(_home(home)))
    except ClientError as exc:
        raise _fail(exc) from None


@app.command("restart")
def restart(home: HomeOption = None,
            if_running: Annotated[bool, typer.Option(
                "--if-running", help="Do nothing, and say nothing, when the service is not running (the installer "
                                     "passes this after an update).")] = False) -> None:
    """Start the running local node again, so it runs the openajar installed now: same home, same port."""
    from openajar.local import service

    try:
        state = service.restart(_home(home))
    except ClientError as exc:
        raise _fail(exc) from None
    if state is None:
        if if_running:
            return
        typer.echo("The local service is not running, so there is nothing to restart. Start it with "
                   "`openajar local start`.")
        raise typer.Exit(1)
    typer.echo(state.note)
    if not state.running:
        raise typer.Exit(1)


@app.command("status")
def status(home: HomeOption = None,
           as_json: Annotated[bool, typer.Option("--json", help="Machine-readable.")] = False) -> None:
    """Is it running, on which port, which environments are open, and what it closed after a sleep."""
    from openajar.local import service

    try:
        state = service.status(_home(home))
    except ClientError as exc:
        raise _fail(exc) from None
    _show(state, as_json=as_json)
    if not state.running:
        raise typer.Exit(1)


@app.command("logs")
def logs(home: HomeOption = None,
         lines: Annotated[int, typer.Option("--lines", "-n", min=1, help="How many lines.")] = 50,
         follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing new lines.")] = False) -> None:
    """The end of the local node's log."""
    from openajar.local import service

    root = _home(home)
    path = log_path(root)
    if not path.exists():
        typer.echo(f"No log yet at {path}. Start it with `openajar local start`.")
        return
    for line in service.log_tail(root, lines):
        typer.echo(line)
    if not follow:
        return
    with path.open(errors="replace") as f:
        f.seek(0, 2)
        try:
            while True:
                line = f.readline()
                if line:
                    typer.echo(line.rstrip("\n"))
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


# `openajar mcp`, mounted at the top level by openajar.cli (it sits here, not in openajar.client, because it
# starts the local service: openajar.local may import openajar.client, never the other way round).
def mcp_command(
    home: Annotated[Path | None, typer.Option("--home", hidden=True,
                                              help="The local node's folder (default: ~/.openajar).")] = None,
    start_only: Annotated[bool, typer.Option("--start-only", hidden=True,
                                             help="Only start the local service, then exit.")] = False,
) -> None:
    """openajar as a stdio MCP server, for any MCP client that starts a command (Cursor, Claude Desktop,
    Codex, `npx -y openajar`, `uvx openajar mcp`). It talks to openajar on this computer and starts it
    when it is stopped. Not set up yet? It still answers, and tells the agent and you what to run.
    Only MCP messages go to stdout; it exits when stdin closes."""
    from openajar.local import mcp_command as command

    if start_only:
        raise typer.Exit(command.start_only(home))
    raise typer.Exit(command.run(home))
