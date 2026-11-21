"""`openajar connect | disconnect | nodes | status | open`: connect this computer to openajar nodes.

Agents on this computer hold their connection's own grant; the owner's CLI tools (dash,
approvals, open) hold the owner's device credential. The node token never stays here.

Mount it at the top level of the main CLI with `app.add_typer(client_app)`. Until then,
`uv run python -m openajar.client connect mynode` runs the same commands.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import Annotated, Any

import typer

from openajar.client.call import call as call_command
from openajar.client.choices import AgentOptions, ClientChoice, ConnectResult, Health, InstallChoice
from openajar.client.errors import ClientError
from openajar.client.ops import Client, browsers_of
from openajar.client.state import DEFAULT_REMOTE_HOME, Connection, shown

# Tracebacks never print local variables: they would include the token.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="Connect this computer's agents to openajar nodes.")


class TokenStoreChoice(StrEnum):
    auto = "auto"
    keychain = "keychain"
    file = "file"


def client() -> Client:
    return Client(out=typer.echo)


def fail(exc: ClientError) -> typer.Exit:
    typer.secho(f"error: {exc.message}", err=True, fg=typer.colors.RED)
    for line in exc.details:
        typer.echo(line, err=True)
    if exc.hint:
        typer.echo(f"  {exc.hint}", err=True)
    return typer.Exit(1)


def table(rows: list[tuple[str, ...]], indent: str = "") -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo(indent + "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


@app.command()
def connect(
    ssh_host: Annotated[str | None, typer.Argument(
        help="The node's SSH host, as in ~/.ssh/config (for example mynode).", show_default=False)] = None,
    name: Annotated[str | None, typer.Option(help="Connection name (default: from the host). Claude Code sees "
                                                  "the server as openajar-NAME.")] = None,
    local_port: Annotated[int | None, typer.Option(min=1, max=65535,
                                                   help="Local tunnel port (default 7400, else the next free one).")]
    = None,
    remote_home: Annotated[str, typer.Option(help="OPENAJAR_HOME on the node.")] = DEFAULT_REMOTE_HOME,
    token_store: Annotated[TokenStoreChoice, typer.Option(
        help="Where the credentials are kept: keychain (the macOS default) or file (chmod 600, the Linux default).")]
    = TokenStoreChoice.auto,
    ceiling: Annotated[str | None, typer.Option(
        help="The most the agents' grant may do: read, draft, commit (publish or send), pay or irreversible "
             "(default commit, or what the connection had).", show_default=False)] = None,
    agent_ttl: Annotated[str | None, typer.Option(
        "--agent-ttl", help="How long the agents' grant lasts, for example 90d or 26w (default 90d).",
        show_default=False)] = None,
    client_name: Annotated[str | None, typer.Option(
        "--client", help="Who uses the grant: its subject is CLIENT@this-computer (default claude-code).",
        show_default=False)] = None,
    special: Annotated[list[str] | None, typer.Option(
        "--special", help="A special capability for the agents' grant, for example exec or memory (repeatable; default "
                            "memory_read, which reads workspace memory and never writes it).",
        show_default=False)] = None,
    new_grant: Annotated[bool, typer.Option("--new-grant", help="Issue a new agent grant and revoke the old one.")]
    = False,
    toolsets: Annotated[str | None, typer.Option(
        "--toolsets", help="Which tools the agents list: toolset names (for example files,flows; core is always "
                           "in), full for every tool, or default for the node's own setting (default: what the "
                           "connection had). Agents reach the rest with tools_more and tools_call.",
        show_default=False)] = None,
    upgrade: Annotated[bool, typer.Option(
        "--upgrade", help="Move every connection that still holds the node token to its own grant.")] = False,
    remove: Annotated[str | None, typer.Option(
        "--remove", metavar="NAME", help="Remove the connection NAME (see `openajar status`): revoke its grant, "
                                         "stop its tunnel, delete its credentials and its Claude Code and Codex "
                                         "entries.",
        show_default=False)] = None,
    install: Annotated[bool, typer.Option(
        "--install", help="If the host has no openajar node, install one without asking (Ubuntu or Debian, root or "
                          "passwordless sudo). A host with a node is never touched.")] = False,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Print every command connect would run on the host, run only a read-only check, and "
                          "change nothing.")] = False,
    check: Annotated[bool, typer.Option(
        "--check", help="Check each connection (or the one named): the tunnel, the grant, and that Claude Code "
                        "and Codex see the server.")] = False,
    no_claude: Annotated[bool, typer.Option("--no-claude", help="Leave Claude Code (and its skills) alone.")] = False,
    no_codex: Annotated[bool, typer.Option("--no-codex", help="Leave Codex's config.toml alone.")] = False,
    no_skills: Annotated[bool, typer.Option("--no-skills", help="Do not copy the openajar skills into Claude "
                                                                "Code.")] = False,
) -> None:
    """Tunnel to a node, give its agents their own grant, and register it with Claude Code. Reads the node's
    MCP port and its own token over SSH (kept in memory, never printed or stored), keeps an SSH tunnel up
    with launchd (systemd on Linux), and asks the node through the tunnel for two credentials: a grant for
    the agents (CLIENT@this-computer, every lane, up to the highest level the node allows, revocable per
    connection) that Claude Code sends, and the owner's device credential that `openajar open` and
    `openajar access approvals` use. Both go to the macOS Keychain, in separate items. Safe to run again: a working grant and
    tunnel are kept.

    Claude Code and Codex are registered when installed here, and the openajar skills go into Claude Code.

    Codex runs `openajar mcp-bridge NAME`, which reads the same stored token: no token in config.toml.

    A host with no node gets one first, after one question (`--install` skips it, `--dry-run` shows it)."""
    if check:
        raise typer.Exit(check_command(ssh_host))
    if dry_run:
        if not ssh_host:
            raise fail(ClientError("--dry-run needs the host", "for example `openajar connect root@my-server --dry-run`"))
        try:
            for line in client().install_plan(ssh_host, remote_home):
                typer.echo(line)
        except ClientError as exc:
            raise fail(exc) from None
        return
    if remove is not None:
        if ssh_host or upgrade:
            raise fail(ClientError("--remove takes only the connection's name", f"`openajar connect --remove {remove}`"))
        disconnect(remove)
        return
    c = client()
    agent = AgentOptions(ceiling=ceiling, ttl=agent_ttl, client=client_name, specials=special,
                         new_grant=new_grant, toolsets=toolsets)
    choice = ClientChoice(claude=not no_claude, codex=not no_codex, skills=not (no_skills or no_claude))
    if upgrade:
        if ssh_host or name or local_port:
            raise fail(ClientError("--upgrade moves every old connection; leave out the host, --name and --local-port"))
        if not c.legacy():
            typer.echo("Every connection already has its own grant. Nothing to upgrade.")
            return
        failed = False
        for conn, outcome in c.upgrade(agent=agent, clients=choice):
            if isinstance(outcome, ClientError):
                failed = True
                typer.secho(f"error: {conn.name}: {outcome.message}", err=True, fg=typer.colors.RED)
                if outcome.hint:
                    typer.echo(f"  {outcome.hint}", err=True)
                continue
            summary(c, outcome, brief=True)
        if failed:
            raise typer.Exit(1)
        return
    if not ssh_host:
        raise fail(ClientError("connect needs the node's SSH host", "for example `openajar connect mynode`, "
                                                                  "or `openajar connect --upgrade`"))
    asker = None
    if sys.stdin.isatty():
        def asker(question: str) -> bool:
            return typer.confirm(question, default=True)
    try:
        result = c.connect(ssh_host, name=name, local_port=local_port, remote_home=remote_home,
                           token_store=token_store.value, agent=agent, clients=choice,
                           install=InstallChoice(mode="yes" if install else "ask", confirm=asker))
    except ClientError as exc:
        raise fail(exc) from None
    summary(c, result)
    if result.claude_error:
        raise typer.Exit(1)  # claude is there but refused the entry: the setup is not complete


def toolsets_label(conn: Connection) -> str:
    if not conn.toolsets:
        return "the node's default (change it with --toolsets)"
    if conn.toolsets == ["full"]:
        return "every tool (--toolsets full)"
    return f"{', '.join(conn.toolsets)}; agents load the rest with tools_more"


CLIENT_LABEL = {"Claude Code": "Claude Code", "Codex": "Codex", "Skills": "Claude Code skills"}


def summary(c: Client, result: ConnectResult, *, brief: bool = False) -> None:
    conn = result.conn
    helper = c.paths.headers_helper
    for line in result.clients:
        label = CLIENT_LABEL.get(line.client, line.client)
        if line.client == "Claude Code" and line.state in ("registered", "already registered"):
            typer.echo(f"Claude Code: {line.state} {conn.claude_name} (user scope) at {conn.url}")
            typer.echo(f"  Tools listed: {toolsets_label(conn)}")
            typer.echo(f"  Claude Code runs {shown(helper)} {conn.name} for the token each time it connects,")
            typer.echo("  so the token is not in ~/.claude.json.")
        elif line.client == "Claude Code" and line.state in ("not installed", "failed"):
            why = result.claude_error or "`claude` is not on PATH"
            typer.echo(f"Claude Code: not registered ({why}). Register it by hand:")
            for manual in result.claude_lines:
                typer.echo(f"  {manual}")
        elif line.client == "Codex" and line.state in ("registered", "already registered"):
            typer.echo(f"Codex: {line.state} {line.detail}")
            typer.echo(f"  Codex runs `openajar mcp-bridge {conn.name}`, which reads the same stored token, so "
                       "there is no token in config.toml.")
        else:
            typer.echo(f"{label}: {line.state}" + (f" ({line.detail})" if line.detail else ""))
    for note in result.notes:
        typer.secho(f"warning: {note}", err=True, fg=typer.colors.YELLOW)
    typer.echo("")
    typer.echo(f"Connected {conn.name}. Undo all of it with: openajar connect --remove {conn.name}")
    if result.installed:
        if result.sign_in:
            typer.echo(f"Panel sign-in (works once, for 10 minutes): {result.sign_in}")
        else:
            typer.echo(f"Panel sign-in: `ssh {conn.ssh_host} sudo -u agentos OPENAJAR_HOME={conn.remote_home} "
                       f"/opt/agentos/.venv/bin/openajar panel login --base-url http://localhost:{conn.local_port}`")
    if brief:
        return
    seen = [line.client for line in result.clients if line.state in ("registered", "already registered")]
    if seen:
        typer.echo("T3 Code: its Claude and Codex threads see this server too. Start a new thread.")
    typer.echo(try_line(seen))


def try_line(clients: list[str]) -> str:
    if "Claude Code" in clients:
        return 'Try it: start a new Claude Code session and ask "list my openajar lanes".'
    if "Codex" in clients:
        return 'Try it: start a new Codex session and ask "list my openajar lanes".'
    return "Try it: register a client (see above), then ask your agent to \"list my openajar lanes\"."


def check_command(name: str | None) -> int:
    """`openajar connect --check`: for each connection, the tunnel, the grant, Claude Code and Codex."""
    c = client()
    try:
        conns = [c.get(name)] if name else c.connections()
    except ClientError as exc:
        raise fail(exc) from None
    if not conns:
        typer.echo("No connections. Add one with `openajar connect <ssh-host>`.")
        return 1
    failed = False
    seen: list[str] = []
    for i, conn in enumerate(conns):
        if i:
            typer.echo("")
        typer.echo(f"{conn.name} ({conn.ssh_host}):")
        for item in c.check(conn):
            mark = {"ok": "ok  ", "fail": "FAIL", "skip": "--  "}[item.state]
            colour = {"ok": typer.colors.GREEN, "fail": typer.colors.RED, "skip": None}[item.state]
            typer.secho(f"  {mark} {item.what}: {item.detail}", fg=colour)
            if item.fix and item.state == "fail":
                typer.echo(f"         fix: {item.fix}")
            if item.state == "fail":
                failed = True
            elif item.state == "ok" and item.what in ("Claude Code", "Codex"):
                seen.append(item.what)
    typer.echo("")
    typer.echo(try_line(seen))
    return 1 if failed else 0


@app.command()
def disconnect(name: Annotated[str, typer.Argument(help="Connection name, as shown by `openajar status`.")]) -> None:
    """Remove a connection: revoke its agent grant and end the owner credential on the node, then
    stop and delete the tunnel, the stored credentials and the Claude Code entry."""
    c = client()
    try:
        removed = c.disconnect(name)
    except ClientError as exc:
        raise fail(exc) from None
    typer.echo(f"Disconnected {name}. Removed:")
    for item in removed:
        if item.startswith("(could not"):
            typer.secho(f"  {item}", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"  {item}")


def mcp_cell(health: Health) -> str:
    if health.ok:
        return f"ok ({health.conn.node_name or health.server.get('name', 'openajar')})"
    return health.mcp


@app.command()
def nodes() -> None:
    """Connections on this computer: the health of each tunnel, and which grant its agents use."""
    c = client()
    conns = c.connections()
    if not conns:
        typer.echo("No connections. Add one with `openajar connect <ssh-host>`.")
        return
    rows = [("NAME", "SSH HOST", "URL", "NODE PORT", "TUNNEL", "MCP", "AGENTS USE")]
    try:
        for conn in conns:
            health = c.health(conn)
            rows.append((conn.name, conn.ssh_host, conn.url, str(conn.remote_port), health.service.label,
                         mcp_cell(health), c.credential_label(conn)))
    except ClientError as exc:
        raise fail(exc) from None
    table(rows)
    if c.legacy():
        typer.secho("warning: a connection still gives its agents the node token, so you cannot approve from "
                    "here what they ask. Run `openajar connect --upgrade`.", err=True, fg=typer.colors.YELLOW)


def lane_counts(whoami: dict[str, Any]) -> str:
    lanes = whoami.get("browsers") or whoami.get("lanes") or {}  # "lanes" from a node before 0.4
    total = lanes.get("total", 0)
    parts = [f"{n} {state}" for state, n in lanes.items() if state != "total" and n]
    return f"{total} total" + (f", {', '.join(parts)}" if parts else "")


def show_status(conn: Connection, info: dict[str, Any]) -> None:
    show_node(f"{conn.name}: ", f"via {conn.url}", info)


def show_node(prefix: str, where: str, info: dict[str, Any]) -> None:
    who = info["whoami"]
    typer.echo(f"{prefix}node {who.get('node', '?')} (openajar {who.get('version', '?')}) {where}")
    typer.echo(f"  lanes: {lane_counts(who)}")
    lanes = info["lanes"]
    if not lanes:
        typer.echo("  No lanes yet. Create one on the node with `openajar lane create <workspace/name>`.")
        return
    rows = [("LANE", "STATE", "ENGINE", "PROXY", "LAST USED")]
    for lane in lanes:
        last = (lane.get("last_used") or "never")[:16].replace("T", " ")
        rows.append((lane.get("id", "?"), lane.get("state", "?"), lane.get("engine", "?"),
                     lane.get("proxy") or "-", last))
    table(rows, indent="  ")
    exposed = [(lane.get("id", "?"), e) for lane in lanes for e in lane.get("exposed") or [] if isinstance(e, dict)]
    for lane_id, e in exposed:  # openajar expose: the owner's dev server, open in that lane only
        until = str(e.get("until") or "?")[:16].replace("T", " ")
        typer.echo(f"  exposed in {lane_id}: {e.get('url', '?')} (until {until} UTC)")


def _on_a_node() -> bool:
    """This box is a node (a node.toml in OPENAJAR_HOME)."""
    from openajar.core.config import config_path, home_from_env

    return config_path(home_from_env()).exists()


def node_status() -> dict[str, Any] | None:
    """This box's own node, when the CLI runs on one (a node.toml in OPENAJAR_HOME): `{ node, home,
    whoami?, lanes?, error? }`, asked over its loopback MCP port with its own token. None off a node."""
    from openajar.client.mcp_http import McpError, McpSession
    from openajar.core.config import config_path, home_from_env, load_config

    home = home_from_env()
    if not config_path(home).exists():
        return None
    try:
        cfg = load_config(home)
    except Exception as exc:  # noqa: BLE001  (a node.toml that does not parse: say so, as doctor does)
        return {"node": None, "home": str(home), "error": f"node.toml could not be read: {exc}",
                "fix": "openajar status --check"}
    out: dict[str, Any] = {"node": cfg.name, "home": str(home)}
    try:
        with McpSession(cfg.mcp_port, cfg.mcp_token, timeout=10, host="127.0.0.1") as session:
            out["whoami"] = session.call("whoami")
            out["lanes"] = browsers_of(session.call("browsers_list"))
    except McpError as exc:
        out["error"] = (f"agentosd does not answer on port {cfg.mcp_port}" if exc.failure.value == "unreachable"
                        else f"agentosd on port {cfg.mcp_port}: {exc.detail}")
        out["fix"] = "openajar up starts it; openajar status --check says what is wrong"
    return out


def show_node_status(info: dict[str, Any]) -> None:
    if info.get("error"):
        typer.echo(f"This box is node {info.get('node') or '?'} ({info['home']}), and it is not running.")
        typer.secho(f"  {info['error']}", fg=typer.colors.YELLOW)
        typer.echo(f"  {info['fix']}")
        return
    show_node("This box: ", f"in {info['home']}", info)
    typer.echo("On your computer, `openajar connect <ssh-host>` lets Claude Code use it.")


@app.command()
def status(name: Annotated[str | None, typer.Argument(help="Connection name (default: every connection).")]
           = None,
           as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False) -> None:
    """Ask a node through its tunnel who it is and which lanes it has. On a node with no connections,
    this node itself."""
    import json

    c = client()
    try:
        conns = [c.get(name)] if name else c.connections()
    except ClientError as exc:
        raise fail(exc) from None
    if not conns:
        local = node_status()
        if local is not None:
            if as_json:
                typer.echo(json.dumps({"node": local, "connections": []}, indent=2, default=str))
            else:
                show_node_status(local)
            if local.get("error"):
                raise typer.Exit(1)
            return
        if as_json:
            typer.echo(json.dumps({"node": None, "connections": []}, indent=2))
        else:
            typer.echo("No connections. Add one with `openajar connect <ssh-host>`.")
        raise typer.Exit(1)
    failed = False
    rows: list[dict[str, Any]] = []
    for i, conn in enumerate(conns):
        if i and not as_json:
            typer.echo("")
        try:
            info = c.status(conn)
        except ClientError as exc:
            failed = True
            rows.append({"name": conn.name, "url": conn.url, "error": exc.message, "fix": exc.hint})
            if not as_json:
                typer.secho(f"error: {exc.message}", err=True, fg=typer.colors.RED)
                if exc.hint:
                    typer.echo(f"  {exc.hint}", err=True)
            continue
        rows.append({"name": conn.name, "url": conn.url, **info})
        if not as_json:
            show_status(conn, info)
    if as_json:
        typer.echo(json.dumps({"node": None, "connections": rows}, indent=2, default=str))
    if failed:
        raise typer.Exit(1)


@app.command("open")
def open_lane(
    lane: Annotated[str, typer.Argument(help="Lane id, for example biz-a/instagram.")],
    node: Annotated[str | None, typer.Option("--node", help="Connection name (default: the node that has the lane).")]
    = None,
    wake: Annotated[bool, typer.Option("--wake", help="Start the lane's browser first if it is asleep.")] = False,
    print_only: Annotated[bool, typer.Option("--print", help="Print the link instead of opening the browser.")]
    = False,
) -> None:
    """Open a lane's live view in your browser, through the node's tunnel.

    In the viewer you can take control (the agent waits) and hand it back. The link expires. On the node
    itself (no connections) it prints the node's own control link and the SSH tunnel, as `openajar lane view
    --control` does."""
    import webbrowser

    c = client()
    if node is None and not c.connections() and _on_a_node():
        # on the node itself there is nothing to connect to: the node's own link, as `openajar lane view`
        from openajar.commands.lane_control import lane_view_link

        lane_view_link(lane, control=True, ttl=None)
        return
    try:
        conn = c.get(node) if node else c.find_lane(lane)
        link = c.view_link(conn, lane, wake=wake)
    except ClientError as exc:
        raise fail(exc) from None
    until = f", valid until {link['expires_at']}" if link.get("expires_at") else ""
    if print_only or not webbrowser.open(link["url"]):
        typer.echo(f"Live view of {lane} on {conn.name}{until}:")
        typer.echo(f"  {link['url']}")
        return
    typer.echo(f"Opened the live view of {lane} on {conn.name}{until}.")



@app.command("expose")
def expose_command(
    port: Annotated[int, typer.Argument(help="The port your dev server listens on here, for example 5173.")],
    lane: Annotated[str, typer.Option("--lane", help="The browser lane that may open it, for example "
                                                     "personal/linkedin.")],
    node: Annotated[str | None, typer.Option("--node", help="Connection name (default: the node that has the lane).")]
    = None,
    name: Annotated[str | None, typer.Option("--name", help="The address's name: https://NAME.expose.openajar.invalid "
                                                            "(default: dev).")] = None,
    for_: Annotated[str | None, typer.Option("--for", help="How long at most: 30m, 2h, 1d (default 2h, at most "
                                                           "12h).")] = None,
) -> None:
    """Open your dev server (localhost:PORT here) in one browser lane, so you can try your app with the
    lane's real, logged-in accounts: "Sign in with LinkedIn" and the redirect back, embeds, cookies.

    The lane opens it at https://NAME.expose.openajar.invalid, and agents driving the lane see it in browsers_list.

    Only that lane, only while this command runs: Ctrl-C, --for or a dropped SSH session ends it.

    No port opens anywhere and the lane's proxy never carries it. WebSockets (hot reload) do not pass: reload.
    """
    from openajar.client.expose import parse_for, runner

    c = client()
    try:
        conn = c.get(node) if node else c.find_lane(lane)
        run = runner(c, conn, out=typer.echo)
        outcome = run.expose(lane=lane, name=name or "dev", port=port, ttl_s=parse_for(for_))
    except ClientError as exc:
        raise fail(exc) from None
    except KeyboardInterrupt:
        typer.echo("Ended.")
        raise typer.Exit(130) from None
    typer.echo(outcome.message)
    raise typer.Exit(outcome.code)


@app.command("forward")
def forward_command(
    target: Annotated[str, typer.Argument(help="A terminal or service lane and its port: workspace/name:PORT, for "
                                               "example dev/web:3000.")],
    local: Annotated[int | None, typer.Option("--local", help="The port here (default: the same port, or the next "
                                                              "free one).")] = None,
    node: Annotated[str | None, typer.Option("--node", help="Connection name (default: the only connection).")]
    = None,
) -> None:
    """Reach a port of a terminal or service lane at http://localhost here, while this command runs.

    For example the dev server a service lane runs. Only this computer's localhost gets it, through your SSH session.

    Nothing opens on the node. Ctrl-C ends it.
    """
    from openajar.client.expose import runner

    lane, sep, port_s = target.rpartition(":")
    if not sep or not lane or not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
        raise fail(ClientError(f"{target!r} is not a lane and a port", "write workspace/name:PORT, for example dev/web:3000"))
    c = client()
    try:
        conn = c.get(node) if node else _only_connection(c)
        run = runner(c, conn, out=typer.echo)
        outcome = run.forward(lane=lane, port=int(port_s), local=local)
    except ClientError as exc:
        raise fail(exc) from None
    except KeyboardInterrupt:
        typer.echo("Ended.")
        raise typer.Exit(130) from None
    typer.echo(outcome.message)
    raise typer.Exit(outcome.code)


def _only_connection(c: Client) -> Connection:
    conns = c.connections()
    if len(conns) == 1:
        return conns[0]
    if not conns:
        raise ClientError("no connections", "add one with `openajar connect <ssh-host>`")
    raise ClientError("there are several connections", "choose one with --node (see `openajar nodes`)")


def _mount_run() -> None:
    from openajar.client.run_cli import EPILOG, run_command

    # everything after the first word of the command is the command's, flags included (`uv run pytest -q`)
    app.command("run", epilog=EPILOG, context_settings={"allow_interspersed_args": False,
                                                        "ignore_unknown_options": True})(run_command)


_mount_run()

app.command("call")(call_command)


@app.command("mcp-bridge", hidden=True)
def mcp_bridge(name: Annotated[str, typer.Argument(help="Connection name, as shown by `openajar status`.")]) -> None:
    """Serve the connection NAME as a stdio MCP server, for Codex and other clients that start a command.
    Reads the token from this computer's store (as Claude Code's headersHelper does) and forwards to the
    connection's HTTP MCP URL. Only MCP messages go to stdout; it exits when stdin closes."""
    from openajar.client.bridge import run

    raise typer.Exit(run(name))
