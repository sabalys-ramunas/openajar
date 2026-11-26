"""`openajar environments adopt [--yes]`: give the computers of this computer's earlier agent grants to its
current one (openajar.environments.adopt says what moves and why it is a command).

Without --yes it lists what would move and changes nothing; with it, it moves them and writes the action
log. Its command joins `openajar environments` (openajar.environments.cli's app; logins_cli imports this
module, and openajar.commands.features mounts logins_cli).
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

import typer

from openajar.core.config import home_from_env
from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.environments.cli import _fail, _home, app


def _vault() -> Any:
    """The vault of this node, for the logins bound by title (None: they are not read)."""
    if _home().expanduser().resolve() != home_from_env().expanduser().resolve():
        return None  # the CLI opens the vault of OPENAJAR_HOME only
    from openajar.commands.base import cli_runtime

    return cli_runtime().get("vault")


def _as(row: dict) -> str:
    return f" as \"{row['as']}\" (this grant has a computer of that title)" if row.get("as") else ""


def _lines(out: dict, moved: bool) -> list[str]:
    to = out["to"]
    if not (out["saves"] or out["environments"] or out["logins"]):
        lines = [(f"Nothing to move: the saves and open environments of {to['agent']} are its current grant's "
                  f"({to['grant']}).")]
    else:
        grants = ", ".join(f"{', '.join(f['grants'])} ({f['state']})" for f in out["from"])
        lines = [(f"{'Moved' if moved else 'Would move'} to grant {to['grant']} ({to['agent']}), from its earlier "
                  f"grants {grants}:")]
        lines += [f"  save {m['id']}  \"{m['title']}\"{_as(m)}" + (", with its disk snapshot" if m["snapshot"] else "")
                  for m in out["saves"]]
        lines += [f"  open environment {e['id']}  \"{e['title']}\"{_as(e)}, let go so its title takes it back"
                  for e in out["environments"]]
        lines += [f"  login {x['name']} ({x['site']}) bound to \"{x['title']}\"{_as(x)}" for x in out["logins"]]
    if out.get("logins_unread"):
        lines.append(f"Logins were not read: {out['logins_unread']}. Run this again where they can be.")
    for f in out["left"]:
        lines.append(f"Left alone: {', '.join(f['grants'])}, {f['why']}.")
    if out["saves"] or out["environments"] or out["logins"]:
        lines.append("Its agent resumes each with environment_open and the title. Nothing was deleted." if moved else
                     "Nothing moved yet: `openajar environments adopt --yes` moves them. Nothing is deleted.")
    return lines


@app.command()
def adopt(yes: Annotated[bool, typer.Option("--yes", help="Move them. Without it, only list what would move.")] = False,
          json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Give the computers of this computer's earlier agent grants to its current one.

    openajar 0.7.0 and before gave each new local agent grant (after its 90 days, a freeze, or new default
    terms) computers of its own, so the agent no longer saw its saves and open environments. This lists those
    of this computer's agent under its earlier grants (revoked or expired), and with --yes gives them to its
    current grant, once: the saves, the open environments and the logins bound to them by title. A title both
    have keeps the current one's; the other is renamed "<title> (earlier)". Nothing is deleted, and the move
    is written to the action log."""
    from openajar.environments.adopt import Adoption, node_policy
    from openajar.environments.saves import SaveStore
    from openajar.environments.store import EnvironmentStore

    home = _home()
    with node_policy(home) as policy:
        if policy is None:
            raise _fail(InvalidRequest(f"{home} has no grants, so no agent grant to give computers to"))
        db = home / "environments.db"
        adoption = Adoption(policy=policy, store=EnvironmentStore(db) if db.exists() else None,
                            saves=SaveStore(home), vault=_vault())
        try:
            out = asyncio.run(adoption.apply() if yes else adoption.plan())
        except OpenajarError as e:
            raise _fail(e) from None
    if json_out:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    for line in _lines(out, yes):
        typer.echo(line)
