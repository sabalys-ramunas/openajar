"""The task-first layout of `openajar`.

`openajar.cli` mounts every command where it always lived; `regroup` then shows about 14
top-level groups and hides the old names, which keep working exactly as before (same function,
same options, nothing printed extra) for at least one release. Nothing here changes a feature
package's own Typer: a group that gains commands is a shallow copy with longer lists, so
`openajar.workspaces.cli.app` and the rest stay what their own tests and `python -m` see.

    tests/unit/test_cli_groups.py   every path recorded before the regrouping still resolves
    tests/golden/cli/               the --help of every top-level group
"""

from __future__ import annotations

import copy
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import typer
import typer.main
from typer.models import CommandInfo, TyperInfo

START = "Start here"
BUILD = "Build and check"
WORK = "Lanes and work"
BOX = "This box"

ROOT_HELP = ("openajar gives every agent its own computer in the cloud: a terminal, a real browser and your "
             "repo. New here? `openajar setup`. Every day: `openajar help`.")

# Where each old top-level name lives now. The old name stays as a hidden alias. A top-level name
# missing here stays visible, so a new command is noticed rather than silently hidden.
HOME: dict[str, str] = {
    "disconnect": "connect", "nodes": "connect", "mcp-bridge": "connect",
    "health": "node", "doctor": "status",
    "dash": "open", "forward": "lane",
    "device": "lane", "proxy": "lane", "env": "lane", "identity": "lane",
    "memory": "workspace", "members": "workspace", "profile": "workspace",
    "mail": "vault", "git": "vault",
    "grants": "access", "approvals": "access", "confirmation": "access", "policy": "access",
    "audit": "access", "freeze": "access", "unfreeze": "access",
    "runner": "routines", "runs": "routines", "flows": "routines", "schedules": "routines",
    "workflows": "routines", "connector": "routines",
    "site-api": "routines", "brains": "routines", "pilot": "routines", "plugins": "routines",
    "config": "node", "backup": "node", "migrate": "node", "upgrade": "node", "logs": "node",
    "migrate-service-user": "node",
    "lanebench": "dev", "tools": "dev", "errors": "dev", "call": "dev",
    "flow": "routines", "agenda": "routines", "vm": "lane", "text-review": "dev",
    "version": "help",
    "envs": "environments",
}

# (name, panel, one-line help in the top-level list). Commands come before groups in Typer's
# listing, so this is also the order on screen.
VISIBLE: tuple[tuple[str, str, str], ...] = (
    ("setup", START, "Start here: VMs, GitHub, limits, and Claude Code and Codex."),
    ("up", START, "Set up and start this box, then print a sign-in link."),
    ("connect", START, "Let the agents on your computer use a node."),
    ("status", START, "Is it up, and which lanes it has (--check: what is wrong)."),
    ("open", START, "Watch a lane live and take control (--terminal: a dashboard)."),
    ("mcp", START, "openajar as a stdio MCP server, for any MCP client."),
    ("run", BUILD, "Run a command on the node against this working tree."),
    ("expose", BUILD, "Your laptop's dev server, opened in one browser lane."),
    ("local", START, "openajar on this computer: start, stop, status, logs."),
    ("web", START, "The web app in your browser: install, update, remove."),
    ("environments", BUILD, "Your agents' cloud computers: list, show, close, setup, repo."),
    ("studio", BUILD, "Check the pages you build: layout, contrast, brand, changes."),
    ("lane", WORK, "Browser lanes (one account each), devices and proxies."),
    ("workspace", WORK, "Workspaces: one per business or person, with memory, members."),
    ("vault", WORK, "Logins, codes and keys the lanes use, and mailboxes."),
    ("access", WORK, "What agents may do: grants, approvals, freezes, the log."),
    ("routines", WORK, "Work that runs by itself: flows, schedules, workflows, brains."),
    ("node", BOX, "Set up, run, back up and upgrade this node."),
    ("controller", BOX, "One address for Claude Code to reach several nodes."),
    ("panel", BOX, "The web panel: sign in, see lanes, approve from a browser."),
    ("dev", BOX, "For builders: a fake node, the tool and error references."),
    ("help", BOX, "The everyday commands, and the version."),
)

GUIDE = """\
openajar {version}: a computer in the cloud for each of your agents.

First time, on your computer:
  openajar setup                   VMs, GitHub, limits, and Claude Code and Codex, in a few questions
  openajar setup <step>            change one thing: vms, github, limits, agents, web, check, test-vm
Or run it on a server (always on, account browsers):
  openajar connect <user@host>     install a node on your server (if needed) and connect to it
  openajar connect --check         do Claude Code and Codex see it? what to try first
  openajar status                  your connections (openajar connect --remove NAME removes one)
On the node itself:
  openajar up                      set it up, start it, print a sign-in link (--here: this machine's agents too)
  openajar connect --by-hand       the steps to connect by hand, if you would rather
  openajar environments setup      what environments (one VM per chat) still need: token, image, git
  openajar environments repo add <repo>   connect a private repo: a deploy key to paste (or --token)
In a chat:
  "work on this in openajar"       the agent opens an environment, works there, pushes, closes it
Every day:
  openajar environments list       your agents' cloud computers: what runs, what it costs
  openajar run -- <command>        run your tests on the node, not your laptop
  openajar studio up               the page checks (ui_check, ui_diff) for your agent, on this computer
  openajar status                  is it up, which lanes it has
  openajar open <lane>             watch a lane live and take control
  openajar open --terminal         the terminal dashboard: Needs you, runs, activity
  openajar access approvals list   what waits for your yes
When something is wrong:
  openajar status --check          what is wrong and how to fix it
  openajar node logs               the node's log
  openajar access freeze node      stop every agent at once

Every group has --help, for example `openajar lane --help`.
Old command names (openajar dash, openajar grants, ...) still work.
"""


def command_name(info: CommandInfo) -> str:
    return info.name or typer.main.get_command_name(info.callback.__name__)


def _named(info: TyperInfo) -> str | None:
    name = info.name
    return name if isinstance(name, str) and name else None


def with_flag(original: Callable[..., Any], flag: str, annotation: Any,
              on_flag: Callable[[dict[str, Any]], Any], *, optional: tuple[str, ...] = (),
              missing: str = "") -> Callable[..., Any]:
    """`original` with one more boolean option. Its own parameters are read from its signature,
    so an option added to it later is forwarded without a change here. Parameters in `optional`
    may be left out when the flag is given; without the flag they are still required."""
    hints = typing.get_type_hints(original, include_extras=True)
    params = []
    for param in inspect.signature(original).parameters.values():
        param = param.replace(annotation=hints.get(param.name, param.annotation))
        if param.name in optional:
            param = param.replace(default=None)
        params.append(param)
    params.append(inspect.Parameter(flag, inspect.Parameter.KEYWORD_ONLY, default=False, annotation=annotation))

    def wrapper(**kwargs: Any) -> Any:
        if kwargs.pop(flag):
            return on_flag(kwargs)
        for name in optional:
            if kwargs.get(name) is None:
                raise typer.BadParameter(missing, param_hint=name.upper())
        return original(**kwargs)

    wrapper.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {p.name: p.annotation for p in params}
    wrapper.__name__ = original.__name__
    wrapper.__qualname__ = original.__qualname__
    wrapper.__module__ = original.__module__
    wrapper.__doc__ = original.__doc__
    wrapper.wraps_command = original  # type: ignore[attr-defined]  (the alias test follows it)
    return wrapper


def _clone(typer_app: typer.Typer, *, commands: list[CommandInfo] = (), groups: list[TyperInfo] = ()) -> typer.Typer:
    clone = copy.copy(typer_app)
    clone.registered_commands = [*typer_app.registered_commands, *commands]
    clone.registered_groups = [*typer_app.registered_groups, *groups]
    return clone


def _cmd(info: CommandInfo, name: str | None = None, **changes: Any) -> CommandInfo:
    out = copy.copy(info)
    out.name = name or command_name(info)
    for key, value in changes.items():
        setattr(out, key, value)
    return out


def _grp(info: TyperInfo, name: str | None = None, **changes: Any) -> TyperInfo:
    out = copy.copy(info)
    if name:
        out.name = name
    for key, value in changes.items():
        setattr(out, key, value)
    return out


# ---- the wrapped commands ----


def _status_check(kwargs: dict[str, Any]) -> None:
    try:
        from openajar.ops.cli import doctor_cmd
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("openajar.ops"):
            raise
        typer.secho("error: the checks are not part of this build", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    doctor_cmd(as_json=bool(kwargs.get("as_json")), offline=False, only=None, unit="agentosd")


def _open_terminal(kwargs: dict[str, Any]) -> None:
    try:
        from openajar.tui.cli import dash
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("openajar.tui"):
            raise
        typer.secho("error: the terminal dashboard is not part of this build", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    dash(url=None, token=None, shots_dir=None, refresh=30.0, demo=False, node=kwargs.get("node"))


def _connect_by_hand(kwargs: dict[str, Any]) -> None:
    for line in by_hand_lines(kwargs.get("ssh_host")):
        typer.echo(line)


def by_hand_lines(ssh_host: str | None) -> list[str]:
    """The tunnel, the grant and the `claude mcp add` line, for this box. After `openajar up`
    agents use the controller's URL; on a node alone, the node's."""
    from openajar.commands import base as cli
    from openajar.core.config import config_path, home_from_env

    home = home_from_env()
    try:
        from openajar.controller.cli import connect_lines as controller_lines
        from openajar.controller.config import config_file, controller_home, load_controller_config
    except ModuleNotFoundError:
        controller_lines = None
    node_cfg = cli.read_config(home) if config_path(home).exists() else None
    from openajar.node.tools import ssh_host as default_ssh_host

    host = ssh_host or default_ssh_host(node_cfg)  # OPENAJAR_SSH_HOST, then node.toml, then the hostname
    if controller_lines is not None and config_file(controller_home(home)).exists():
        ccfg = load_controller_config(controller_home(home))
        return ["By hand, on your computer. Keep a tunnel to this box open in one terminal:",
                *controller_lines(ccfg, host)[3:],
                ("That token is the agents' (controller.toml, mode 600): `openajar controller agent-token --new` "
                "replaces it.")]
    if node_cfg is None:
        raise cli.fail(f"no node.toml in {home}: run this on the node (or `openajar up` there first)")
    tunnel, add = cli.connect_lines(node_cfg, host)
    return ["By hand, on your computer. Keep the tunnel open in one terminal:",
            f"  {tunnel}",
            "Give your agents their own grant here on the node (it prints the token once):",
            f"  {cli.GRANT_FOR_AGENTS}",
            "Then register the server once with that token:",
            f"  {add}",
            "Keep the node token in node.toml to yourself: a token never approves what its own agents asked."]


CONNECT_EPILOG = ("Your connections: `openajar status`. Remove one: `openajar connect --remove NAME`. "
                  "The steps by hand: `openajar connect --by-hand`, on the node.")

ByHand = Annotated[bool, typer.Option("--by-hand", help="Print the tunnel, grant and `claude mcp add` steps "
                                                        "to do yourself (run it on the node), and change nothing.")]
Check = Annotated[bool, typer.Option("--check", help="Check this node (or this computer's connections) and "
                                                     "say how to fix what is wrong.")]
Terminal = Annotated[bool, typer.Option("--terminal", help="Open the terminal dashboard instead: Needs you, "
                                                           "runs, activity (--node picks the connection).")]


def _help_app(version_cmd: CommandInfo | None) -> typer.Typer:
    from openajar import __version__

    help_app = typer.Typer(invoke_without_command=True, help="The everyday commands, and the version.")

    @help_app.callback()
    def guide(ctx: typer.Context) -> None:
        """The everyday commands, and the version."""
        if ctx.invoked_subcommand is None:
            typer.echo(GUIDE.format(version=__version__), nl=False)

    if version_cmd is not None:
        help_app.registered_commands.append(_cmd(version_cmd, "version"))
    return help_app


@dataclass
class _Mounted:
    """Every command and group the root holds before the regrouping, by name. A package mounted
    without a name puts its commands at the top level, so they are listed with the root's own."""
    commands: dict[str, CommandInfo]
    groups: dict[str, TyperInfo]

    @classmethod
    def of(cls, app: typer.Typer) -> _Mounted:
        commands: dict[str, CommandInfo] = {command_name(info): info for info in app.registered_commands}
        groups: dict[str, TyperInfo] = {}
        for info in app.registered_groups:
            name = _named(info)
            if name:
                groups[name] = info
            else:
                for sub in info.typer_instance.registered_commands:
                    commands[command_name(sub)] = sub
        return cls(commands, groups)

    def group_typer(self, name: str) -> typer.Typer | None:
        return self.groups[name].typer_instance if name in self.groups else None

    def as_group(self, name: str, new_name: str | None = None) -> list[TyperInfo]:
        return [_grp(self.groups[name], new_name or name, hidden=False)] if name in self.groups else []

    def as_cmd(self, name: str, new_name: str | None = None,
               source: dict[str, CommandInfo] | None = None) -> list[CommandInfo]:
        source = self.commands if source is None else source
        return [_cmd(source[name], new_name or name, hidden=False)] if name in source else []


def _sub_commands(typer_app: typer.Typer | None) -> dict[str, CommandInfo]:
    return {command_name(c): c for c in typer_app.registered_commands} if typer_app else {}


def _sub_groups(typer_app: typer.Typer | None) -> dict[str, TyperInfo]:
    return {_named(g): g for g in typer_app.registered_groups if _named(g)} if typer_app else {}


def _visible_commands(m: _Mounted) -> list[CommandInfo]:
    """The commands shown at the top level, in their panels, with the flags regroup adds."""
    commands = m.commands
    panels = {name: (panel, short) for name, panel, short in VISIBLE}
    out: list[CommandInfo] = []
    if "setup" in commands:
        out.append(_cmd(commands["setup"], rich_help_panel=START, short_help=panels["setup"][1]))
    if "up" in commands:
        out.append(_cmd(commands["up"], rich_help_panel=START, short_help=panels["up"][1]))
    if "connect" in commands:
        original = commands["connect"].callback
        out.append(_cmd(commands["connect"], rich_help_panel=START, short_help=panels["connect"][1],
                        epilog=CONNECT_EPILOG,
                        callback=with_flag(original, "by_hand", ByHand, _connect_by_hand)))
    if "status" in commands:
        out.append(_cmd(commands["status"], rich_help_panel=START, short_help=panels["status"][1],
                        callback=with_flag(commands["status"].callback, "check", Check, _status_check)))
    if "open" in commands:
        out.append(_cmd(commands["open"], rich_help_panel=START, short_help=panels["open"][1],
                        callback=with_flag(commands["open"].callback, "terminal", Terminal, _open_terminal,
                                           optional=("lane",),
                                           missing="open needs a lane id, for example `openajar open "
                                                   "biz-a/instagram` (or --terminal for the dashboard)")))
    if "mcp" in commands:
        out.append(_cmd(commands["mcp"], rich_help_panel=START, short_help=panels["mcp"][1]))
    # the commands for your own code: run a command there, your dev server in a lane, a lane's port here
    for name in ("run", "expose"):
        if name in commands:
            out.append(_cmd(commands[name], rich_help_panel=BUILD, short_help=panels[name][1]))
    return out


def _built_groups(m: _Mounted) -> dict[str, typer.Typer]:
    """The visible groups, each a copy of the old one with the commands and groups it takes in."""
    freeze, unfreeze = _sub_commands(m.group_typer("freeze")), _sub_commands(m.group_typer("unfreeze"))
    built: dict[str, typer.Typer] = {}
    if "lane" in m.groups:
        built["lane"] = _clone(m.group_typer("lane"),
                               commands=[*m.as_cmd("lane", "freeze", freeze), *m.as_cmd("lane", "unfreeze", unfreeze),
                                         *m.as_cmd("forward")],
                               groups=[*m.as_group("device"), *m.as_group("proxy"), *m.as_group("env"),
                                       *m.as_group("identity"), *m.as_group("vm")])
    if "workspace" in m.groups:
        built["workspace"] = _clone(m.group_typer("workspace"), groups=[*m.as_group("memory"), *m.as_group("members"),
                                                                             *m.as_group("profile")])
    if "vault" in m.groups:
        built["vault"] = _clone(m.group_typer("vault"), groups=[*m.as_group("mail"), *m.as_group("git")])
    access = typer.Typer(no_args_is_help=True, help="What agents may do: grants, approvals, freezes and the "
                                                    "action log.")
    access.registered_groups = [*m.as_group("grants"), *m.as_group("approvals"), *m.as_group("confirmation"),
                                *m.as_group("freeze"), *m.as_group("unfreeze"), *m.as_group("audit"),
                                *m.as_group("policy")]
    built["access"] = access
    routines = typer.Typer(no_args_is_help=True, help="Work that runs by itself: runs, flows, schedules, "
                                                      "workflows and brains.")
    runner_groups = _sub_groups(m.group_typer("runner"))
    routines.registered_groups = [
        *m.as_group("runs"), *m.as_group("flows"), *m.as_group("schedules"),
        *([_grp(runner_groups["watches"], hidden=False)] if "watches" in runner_groups else []),
        *m.as_group("workflows"), *m.as_group("connector"),
        *m.as_group("site-api"), *m.as_group("brains"), *m.as_group("pilot"), *m.as_group("plugins"),
        *m.as_group("agenda")]
    built["routines"] = routines
    if "node" in m.groups:
        built["node"] = _clone(m.group_typer("node"),
                               commands=[*m.as_cmd("logs"), *m.as_cmd("migrate-service-user")],
                               groups=[*m.as_group("config"), *m.as_group("doctor"), *m.as_group("health"),
                                       *m.as_group("backup"), *m.as_group("migrate"), *m.as_group("upgrade")])
    for name in ("controller", "panel", "studio", "environments", "local", "web"):
        if name in m.groups:
            built[name] = m.group_typer(name)
    if "dev" in m.groups:
        built["dev"] = _clone(m.group_typer("dev"),
                              commands=[*m.as_cmd("tools"), *m.as_cmd("call"), *m.as_cmd("errors")],
                              groups=[*m.as_group("lanebench"), *m.as_group("text-review")])
    built["help"] = _help_app(m.commands.get("version"))
    return built


def regroup(app: typer.Typer) -> None:
    """Show the task-first groups at the top level and hide the old names. Call it once, after
    every command is mounted."""
    m = _Mounted.of(app)
    new_commands = _visible_commands(m)
    built = _built_groups(m)
    new_groups: list[TyperInfo] = []
    for name, panel, short in VISIBLE:
        if name not in built:
            continue
        base = m.groups.get(name) or TyperInfo(name=name)
        new_groups.append(_grp(base, name, typer_instance=built[name], hidden=False, rich_help_panel=panel,
                               short_help=short))

    # -- every other old name: hidden, and exactly as it was
    shown = {name for name, _, _ in VISIBLE}
    for name, info in m.commands.items():
        if name not in shown:
            new_commands.append(_cmd(info, name, hidden=name in HOME))
    for name, info in m.groups.items():
        if name not in shown:
            new_groups.append(_grp(info, hidden=name in HOME))

    app.registered_commands = new_commands
    app.registered_groups = new_groups
    app.info = copy.copy(app.info)
    app.info.help = ROOT_HELP


# Words a person tries for "tell me about this one", and the commands that answer it, best first.
DETAIL_WORDS = frozenset({"show", "info", "status", "details", "detail", "describe", "get", "inspect"})
DETAIL_COMMANDS = ("show", "status", "check", "get", "list")


def suggested_command(group: Any, ctx: Any, name: str) -> str | None:
    """The visible command of `group` a mistyped `name` most likely meant: a close spelling, else for a
    detail word (show, info, status) the group's command that shows one thing."""
    from openajar.core.text import close_match

    visible = [n for n in group.list_commands(ctx)
               if (cmd := group.get_command(ctx, n)) is not None and not getattr(cmd, "hidden", False)]
    found = close_match(name, visible, cutoff=0.7)
    if found is None and name.lower() in DETAIL_WORDS:
        found = next((c for c in DETAIL_COMMANDS if c in visible and c != name), None)
    return found


def suggest_unknown_commands() -> None:
    """Make every Typer group answer an unknown detail word with the command that shows one thing:
    `openajar lane show` says "No such command 'show'. Did you mean 'openajar lane check'?". Typer
    itself already names close spellings (`lane lsit`: 'list'); this adds what spelling cannot find."""
    from typer.core import TyperGroup

    if getattr(TyperGroup, "_openajar_suggests", False):
        return
    original = TyperGroup.resolve_command

    def resolve_command(self, ctx, args):  # type: ignore[no-untyped-def]
        try:
            return original(self, ctx, args)
        except Exception as exc:  # Typer's own (vendored) click UsageError: add to its message, raise it on
            message = getattr(exc, "message", None)
            name = str(args[0]) if args else ""
            if (isinstance(message, str) and message.startswith("No such command") and "Did you mean" not in message
                    and name and not name.startswith("-")):
                found = suggested_command(self, ctx, name)
                if found is not None:
                    exc.message = f"{message.rstrip('.')}. Did you mean '{ctx.command_path} {found}'?"  # type: ignore[attr-defined]
            raise

    TyperGroup.resolve_command = resolve_command  # type: ignore[method-assign]
    TyperGroup._openajar_suggests = True  # type: ignore[attr-defined]
