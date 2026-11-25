"""`openajar` command: the root, which registers every command group (openajar.commands, the
feature packages' CLIs) and then regroups the top level (openajar.cli_groups).

Lane commands talk to the store through a LaneManager directly, without the
daemon, so they work before `openajar node serve` runs. The lane control commands
(take, handback, stop, deny, view) are the exception: control lives in the running
daemon, so they call its HTTP API with the node token. Heavy modules (the lane
manager, uvicorn, the MCP server, httpx) are imported only by the commands that need them.
The helpers they share are in openajar.commands.base.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from openajar import __version__, lanebench_cli
from openajar.commands.base import cli_runtime, fail, plugins_cli_runtime
from openajar.commands.devices import device_app
from openajar.commands.features import mount_features, wire_feature_clis
from openajar.commands.members import members_app
from openajar.commands.node import config_app, node_app
from openajar.commands.proxy import proxy_app
from openajar.commands.solver import solver_app
from openajar.core import catalog
from openajar.core.config import NodeConfig
from openajar.dev import cli as dev_cli
from openajar.local import cli as local_cli
from openajar.setup import web_cli
from openajar.setup.cli import setup as setup_command
from openajar.studio import cli as studio_cli

# isort: off
# `openajar lane` lists its commands in the order they are made: lanes, then lane_sites, then browser_control
from openajar.commands.lanes import lane_app
from openajar.commands import lane_sites, lane_control  # noqa: F401  (their commands join lane_app)
# isort: on

# feature CLIs (a plugin's among them) and tests read these here
__all__ = ["app", "cli_runtime", "plugins_cli_runtime"]

# Tracebacks never print local variables: they would include the MCP token and proxy passwords.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="openajar: a self-hosted computer for your agents.")
app.add_typer(node_app, name="node")
app.add_typer(proxy_app, name="proxy")
app.add_typer(solver_app, name="solver")
app.add_typer(lane_app, name="lane")
app.add_typer(device_app, name="device")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(version: Annotated[bool, typer.Option("--version", help="Print the openajar version and exit.",
                                               callback=_print_version, is_eager=True)] = False) -> None:
    """openajar: a self-hosted computer for your agents."""


@app.command()
def version() -> None:
    """Print the openajar version."""
    typer.echo(__version__)


@app.command()
def tools(
    markdown: Annotated[bool, typer.Option("--markdown", help="The full reference as Markdown.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable, one object per tool.")] = False,
) -> None:
    """The MCP tools this build serves, listed from the same definitions the server uses."""
    import json
    import tempfile

    from openajar.core.runtime import Runtime
    from openajar.mcp.providers import feature_providers, tool_providers
    from openajar.mcp.reference import reference_markdown, reference_rows
    from openajar.plugins.entries import PLUGINS

    runtime = Runtime(config=NodeConfig(home=Path(tempfile.mkdtemp(prefix="openajar-tools-"))))
    from openajar.core.tools import ToolSpec

    # a plugin's tools are served only while it is on, and this build has them whatever a node chose
    providers = [*tool_providers(), *feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))]
    # providers may also return MCP prompts (PromptSpec); the reference lists the tools
    specs = [spec for provider in providers for spec in provider(runtime) if isinstance(spec, ToolSpec)]
    if markdown:
        typer.echo(reference_markdown(specs), nl=False)
    elif as_json:
        typer.echo(json.dumps(reference_rows(specs), indent=2))
    else:
        for row in reference_rows(specs):
            summary = " ".join(row["description"].split()).split(". ")[0].rstrip(".")
            typer.echo(f"{row['name']:<24} {row['effect']:<15} {summary[:90]}")


@app.command()
def errors(
    code: Annotated[str | None, typer.Argument(help="Show one code (for example busy).")] = None,
    markdown: Annotated[bool, typer.Option("--markdown", help="The full catalog as Markdown text.")]
    = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable, one object per code.")] = False,
) -> None:
    """Every error code: what it means, what an agent should do, and how to fix it."""
    import dataclasses
    import json

    if code is not None:
        found = catalog.entry(code)
        if found is None:
            raise fail(f"no error code {code!r}; `openajar errors` lists them")
        if as_json:
            typer.echo(json.dumps(dataclasses.asdict(found), indent=2))
            return
        typer.echo(f"{found.code}: {found.meaning}")
        typer.echo(f"  agent: {found.agent}")
        typer.echo(f"  fix:   {found.fix}")
        typer.echo(f"  retry: {found.retry_text()}")
        return
    if markdown:
        typer.echo(catalog.markdown(catalog.scan()), nl=False)
    elif as_json:
        typer.echo(json.dumps([dataclasses.asdict(e) for _, e in sorted(catalog.entries().items())], indent=2))
    else:
        for item in sorted(catalog.entries().values(), key=lambda e: e.code):
            typer.echo(f"{item.code:<26} {item.meaning[:90]}")


# `openajar setup`: the guided setup of local mode (openajar.setup.cli)
app.command("setup")(setup_command)
# `openajar mcp`: a stdio MCP server for any MCP client (openajar.local.mcp_command)
app.command("mcp")(local_cli.mcp_command)


def legacy_app() -> None:
    """The old command (legacy-name: `agentos`): the same program, and one line on stderr saying so."""
    print("agentos is now called openajar: this ran openajar, so use `openajar` from now on.",  # legacy-name
          file=sys.stderr)
    app(prog_name="openajar")


app.add_typer(config_app, name="config")
app.add_typer(members_app, name="members")

# Each feature package ships a typer `app`, mounted when installed: a package missing from this
# build simply has no commands.
mount_features(app)
wire_feature_clis()
app.add_typer(dev_cli.app, name="dev")  # openajar dev up: a fake node on this machine (openajar.dev)
app.add_typer(local_cli.app, name="local")  # openajar local: the node on this computer, environments only
app.add_typer(web_cli.app, name="web")  # openajar web: the prebuilt web app, optional (openajar.setup.webapp)
app.add_typer(studio_cli.app, name="studio")  # openajar studio up: the studio tools with a real browser, on a laptop
app.add_typer(lanebench_cli.app, name="lanebench")  # the local lane benchmark (tests/lanebench)

try:  # openajar up: a controller and this node, joined, in one process
    from openajar.controller.cli import up_command as _up_command
except ModuleNotFoundError as _exc:
    if not (_exc.name or "").startswith("openajar.controller"):
        raise
else:
    app.command("up")(_up_command)

try:  # doctor, backup, migrate, upgrade, health, and `lane refresh-fingerprint`
    from openajar.ops import cli as _ops_cli
except ModuleNotFoundError as _exc:
    if not (_exc.name or "").startswith("openajar.ops"):
        raise
else:
    _ops_cli.mount(app, lane_group=lane_app)

# The task-first top level (about 14 groups); every old name stays a hidden alias (openajar.cli_groups).
from openajar.cli_groups import regroup as _regroup
from openajar.cli_groups import suggest_unknown_commands as _suggest_unknown_commands

_regroup(app)
_suggest_unknown_commands()

if __name__ == "__main__":
    app()
