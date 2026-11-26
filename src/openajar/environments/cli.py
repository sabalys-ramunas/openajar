"""`openajar environments ...` (alias `openajar envs`): your agents' cloud computers, from the node's shell.

Agents open environments with environment_open (agentosd keeps them, and closes them when they are
done, idle or out of time). Opening one never asks a person; the node's limits bound it. These
commands list them, show one, copy a file or a folder off one, write one's browser replay to a folder,
close one by hand, set those limits, and check what the node needs.
They work while agentosd runs: a close marks the environment, and agentosd's reaper deletes its
machine, browser and tunnels within a minute.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env
from openajar.core.errors import InvalidRequest, NotFound, OpenajarError
from openajar.environments.machines_cli import machine_app, own_image, whole_default
from openajar.environments.machines_cli import set_home as _machines_home
from openajar.environments.models import SIZES, WORKSPACE
from openajar.environments.secrets_cli import secret_app

app = typer.Typer(no_args_is_help=True, help="Your agents' cloud computers: list, show, download, close, allow, "
                                             "set up, connect repositories.")

_state: dict = {"home": None}


@app.callback()
def main(home: Annotated[Path | None, typer.Option(help="OPENAJAR_HOME (default: $OPENAJAR_HOME)")] = None) -> None:
    """Your agents' cloud computers: list, show, download, close, allow, set up, connect repositories."""
    _state["home"] = home
    _machines_home(home)


def _home() -> Path:
    return Path(_state["home"] or home_from_env())


def _fail(e: OpenajarError) -> typer.Exit:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    return typer.Exit(1)


def _managers() -> tuple[Any, Any]:
    from openajar.core.runtime import Runtime
    from openajar.environments.manager import EnvironmentManager
    from openajar.vms.manager import VmManager

    rt = Runtime()
    vms = VmManager(_home(), runtime=rt)
    rt.register("vms", vms)
    return EnvironmentManager(_home(), runtime=rt), vms


def _git_state(vms: Any, env: Any) -> list[dict] | None:
    """The environment's repositories (every one under /work), read over its VM's SSH (as agentosd
    reads them)."""
    from openajar.environments.probes import GIT_STATE, parse_git_state
    from openajar.vms import ssh

    if not env.vm_id:
        return None
    try:
        rec = vms.store.get(env.vm_id)
    except NotFound:
        return None
    if rec.state != "ready" or not rec.host:
        return None
    code, out = asyncio.run(ssh.run(vms.remote(rec), GIT_STATE, timeout_s=30))
    return parse_git_state(out) if code == 0 else None


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _unsaved_words(unsaved: dict) -> str:
    """Like "1 changed file and 2 commits not pushed", naming only what there is."""
    parts = []
    if unsaved.get("dirty_files"):
        parts.append(_count(len(unsaved["dirty_files"]), "changed file", "changed files"))
    if unsaved.get("unpushed"):
        parts.append(_count(len(unsaved["unpushed"]), "commit", "commits") + " not pushed")
    if unsaved.get("stashes"):
        parts.append(_count(len(unsaved["stashes"]), "stash", "stashes"))
    if unsaved.get("unreadable"):
        parts.append(_count(len(unsaved["unreadable"]), "repository git cannot read", "repositories git cannot read"))
    if unsaved.get("loose_files"):
        parts.append(_count(unsaved.get("loose_count") or len(unsaved["loose_files"]), "file in no git repository",
                            "files in no git repository"))
    return " and ".join(parts) or "nothing unsaved"


TITLE_SHOWN = 40
HEADER = ("ID", "STATE", "TITLE", "REPOSITORY", "MACHINE", "COST", "TIME LEFT")


def _cells(s: dict) -> tuple[str, ...]:
    """One environment as the list's cells. The repository is shown whole; a long title is cut at a
    word, with an ellipsis."""
    cost = f"EUR {s['cost_eur']:.3f}" if s.get("cost_eur") is not None else "EUR ?"
    left = f"{s['minutes_left']} min" if s.get("minutes_left") is not None else (s.get("closed_reason") or "-")
    title = s["title"]
    if len(title) > TITLE_SHOWN:
        title = title[:TITLE_SHOWN].rsplit(" ", 1)[0].rstrip(" ,.;:") + "..."
    return (s["id"], s["state"], title, s.get("repo") or "-", s["machine"]["server_type"] or "-", cost, left)


def _table(rows: list[dict]) -> list[str]:
    """The environments as lines of a table, with its header row first."""
    cells = [HEADER, *(_cells(s) for s in rows)]
    widths = [max(len(row[i]) for row in cells) for i in range(len(HEADER))]
    return ["  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in cells]


@app.command("list")
def list_cmd(all_: Annotated[bool, typer.Option("--all", help="Closed and failed ones too.")] = False,
             json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Environments of this node: state, title, repository, machine, cost so far, time left."""
    mgr, _vms = _managers()
    envs = mgr.store.recent(100) if all_ else mgr.store.open()
    rows = [mgr.summary(e) for e in envs]
    if json_out:
        typer.echo(json.dumps({"environments": rows}, indent=2, default=str))
        return
    if not rows:
        typer.echo("No open environments. `openajar environments list --all` shows closed and failed ones too."
                   if not all_ else "No environments yet.")
        return
    for line in _table(rows):
        typer.echo(line)
    typer.echo(f"\nThis month about EUR {mgr.month_spend():.2f} on environments.")


@app.command()
def show(env_id: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2.")],
         json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """One environment: its machine, lanes, the calls its agent made, and its unsaved work."""
    mgr, vms = _managers()
    try:
        env = mgr.get(env_id)
    except (NotFound, ValueError):
        raise _fail(NotFound(f"no environment {env_id}")) from None
    out = mgr.summary(env)
    repos = _git_state(vms, env) if env.open else None
    out["unsaved"] = mgr.unsaved_of(repos)
    out["git"] = repos
    out.update(mgr.push_lists(env))
    out["machine_state"] = env.vm_state
    out["calls"] = [{"at": c.at.isoformat(), "tool": c.tool, "summary": c.summary, "ok": c.ok,
                     "exit_code": c.exit_code, "error": c.error} for c in reversed(env.calls[-20:])]
    if json_out:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    for line in _table([out]):
        typer.echo(line)
    typer.echo(f"  agent {out['agent']['name']}, lanes {out['lanes']['terminal']} and {out['lanes']['browser']}, "
               f"workdir {out['workdir']}")
    if out["unsaved"]:
        typer.echo(f"  not saved: {_unsaved_words(out['unsaved'])}")
    if out["machine_state"]:
        typer.echo(f"  machine {out['machine_state']}")
    for p in out["pushed"]:
        typer.echo(f"  pushed {p['branch']} {p['commit']} at {p['at']}")
    for p in out["deleted"]:
        typer.echo(f"  deleted {p['branch']} at {p['at']}")
    for c in out["calls"][:10]:
        typer.echo(f"  {c['at'][:19]} {c['tool']:<18} {_outcome(c):<10} {c['summary'][:80]}")


@app.command()
def replay(env_id: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2.")],
           out: Annotated[Path | None, typer.Option("--out", metavar="DIR", help="The folder it goes into "
                                                    "(default: replay-<id> here).")] = None,
           json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Write the replay of an environment's browser to a folder, also after its VM is gone.

    The folder gets its frames, timeline.json and index.html: a page that steps through the frames
    with their captions and the calls between them. It reads the node's files, so agentosd may be
    stopped."""
    from openajar.environments.replay_cli import export

    try:
        done = export(_home(), env_id, (out or Path(f"replay-{env_id}")).expanduser().resolve())
    except NotFound as e:
        raise _fail(e) from None
    if json_out:
        typer.echo(json.dumps(done, indent=2))
        return
    typer.echo(f"Wrote {_count(done['frames'], 'frame', 'frames')} and {_count(done['calls'], 'call', 'calls')} "
               f"of {env_id} into {done['folder']}. Open {done['page']} to step through them.")


def _outcome(c: dict) -> str:
    """ok, failed, or a refusal's code; a command's exit code when it is not 0."""
    if not c["ok"]:
        return c.get("error") or "failed"
    return f"exit {c['exit_code']}" if c.get("exit_code") not in (None, 0) else "ok"


@app.command()
def close(env_id: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2.")],
          discard: Annotated[bool, typer.Option("--discard", help="Close it even with work that is not pushed.")]
          = False) -> None:
    """Close an environment; refused while its work is not pushed, unless --discard.

    agentosd deletes its machine, browser and tunnels within a minute."""
    mgr, vms = _managers()
    try:
        env = mgr.get(env_id)
    except (NotFound, ValueError):
        raise _fail(NotFound(f"no environment {env_id}")) from None
    if not env.open:
        typer.echo(f"Environment {env.id} is already {env.state}.")
        return
    if env.keep == "save" and not discard:
        pass  # agentosd saves it (disk snapshot and files) before it deletes the machine
    elif not discard and env.state == "ready":
        repos = _git_state(vms, env)
        unsaved = mgr.unsaved_of(repos) if repos is not None else {"dirty_files": [], "unpushed": []}
        if repos is None or unsaved:
            from openajar.environments.errors import UnsavedWork

            why = ("its machine does not answer, so its work cannot be checked" if repos is None else
                   f"it has {_unsaved_words(unsaved)}")
            raise _fail(UnsavedWork(f"environment {env.id}: {why}; run again with --discard to delete it anyway"))
    reason = "closed by the owner (openajar environments close)" + (" (discarded)" if discard else "")

    def closing(e: Any) -> bool:
        if not e.open:
            return False
        e.state, e.closed_reason = "closing", reason
        return True

    # one write transaction: agentosd records calls on the same row meanwhile, and neither loses the other's
    env = mgr.store.update(env.id, closing)
    if env.state != "closing":
        typer.echo(f"Environment {env.id} is already {env.state}.")
        return
    if env.keep == "save" and not discard:
        typer.echo(f"Closing {env.id} ({env.title}). agentosd saves it first, then deletes its machine, on its next "
                   "pass (within a minute). `openajar environments saves` lists the save.")
        return
    typer.echo(f"Closing {env.id} ({env.title}). agentosd deletes its machine on its next pass (within a minute).")


DOWNLOAD_TIMEOUT_S = 900.0


def _daemon_base(cfg: Any) -> str:
    host = cfg.mcp_host if cfg.mcp_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{cfg.mcp_port}"


def _http_client() -> Any:
    """The HTTP client the download command talks to agentosd with (tests give one of their own)."""
    import httpx

    return httpx.Client(timeout=DOWNLOAD_TIMEOUT_S)


def _daemon_error(r: Any) -> str:
    try:
        err = r.json().get("error") or {}
        return f"{err.get('code', 'error')}: {err.get('message', '')}".rstrip(": ")
    except (ValueError, AttributeError):
        return f"agentosd answered {r.status_code}"


def unpack(archive: Path, into: Path) -> tuple[list[str], list[str]]:
    """Extract a download's .tar.gz into `into` with tarfile's data filter: no absolute path, no "..",
    no link that leaves the folder, no device. A member the filter refuses is skipped and named.
    Returns (the regular files extracted, the members skipped)."""
    import tarfile

    files: list[str] = []
    skipped: list[str] = []
    into.mkdir(parents=True, exist_ok=True)

    def keep(m: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
        try:
            kept = tarfile.data_filter(m, dest)
        except tarfile.FilterError:
            skipped.append(m.name)
            return None
        if kept.isreg():
            files.append(kept.name.removeprefix("./"))
        return kept

    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(into, filter=keep)
    return files, skipped


@app.command()
def download(env_id: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2.")],
             path: Annotated[str, typer.Argument(metavar="PATH", help="A file or a folder under /work, like "
                                                 "/work/shop/dist (a relative one starts at the workdir).")],
             to: Annotated[Path, typer.Option("--to", metavar="DIR", help="The folder it goes into (default: "
                                              "this one).")] = Path("."),
             json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Copy a file or a folder off an environment's machine into DIR, without the agent.

    agentosd packs it on the machine (at most 512 MB packed) and gives a one-use link, which this
    fetches and unpacks. It needs agentosd running."""
    import os
    import tempfile

    from openajar.core.config import config_path, load_config

    home = _home()
    if not config_path(home).exists():
        raise _fail(InvalidRequest(f"no node.toml in {home}; run `openajar node init` first (or set OPENAJAR_HOME)"))
    cfg = load_config(home)
    if not cfg.mcp_token:
        raise _fail(InvalidRequest("this node has no token for its own API, so the CLI cannot reach agentosd"))
    import httpx

    base = _daemon_base(cfg)
    into = to.expanduser().resolve()
    try:
        with _http_client() as http:
            r = http.post(f"{base}/api/environments/{env_id}/download", json={"path": path},
                          headers={"Authorization": f"Bearer {cfg.mcp_token}"})
            if r.status_code != 200:
                raise _fail(InvalidRequest(_daemon_error(r)))
            link = r.json()
            parts = httpx.URL(link["url"])
            into.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".openajar-download-", suffix=".tgz", dir=into)
            archive = Path(tmp)
            try:
                with os.fdopen(fd, "wb") as f, http.stream("GET", f"{base}{parts.raw_path.decode()}") as got:
                    if got.status_code != 200:
                        got.read()
                        raise _fail(InvalidRequest(_daemon_error(got)))
                    for chunk in got.iter_bytes():
                        f.write(chunk)
                files, skipped = unpack(archive, into)
            finally:
                archive.unlink(missing_ok=True)
    except httpx.HTTPError as e:
        raise _fail(InvalidRequest(f"agentosd is not reachable at {base} ({type(e).__name__}); start agentosd "
                                   "and run this again")) from None
    out = {"environment": env_id, "path": link["path"], "kind": link["kind"], "into": str(into), "files": len(files),
           "bytes": link["bytes"], "sha256": link["sha256"], "skipped": skipped}
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(f"Copied the {link['kind']} {link['path']} into {into}: {_count(len(files), 'file', 'files')}.")
    if skipped:
        typer.echo(f"Skipped {_count(len(skipped), 'link', 'links')} that point outside it: {', '.join(skipped[:10])}")


def _gb(n: float | None) -> str:
    return "-" if not n else f"{n:.1f} GB"


@app.command()
def saves(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The saved environments: what each save holds and what its snapshot costs a month.

    An agent resumes one with environment_open and the same title. Saves older than saved_days
    (environments.toml) are removed, and you are told the day before."""
    from openajar.environments.adopt import left_behind, node_policy
    from openajar.environments.models import load_settings
    from openajar.environments.saves import SaveStore, public

    s = load_settings(_home())
    with node_policy(_home()) as policy:
        earlier = left_behind(policy)
    rows = [public(m, s.snapshot_eur_per_gb_month) | {"family": m.family, "commits": m.commits, "files": m.files,
                                                      "earlier": m.family in earlier}
            for m in SaveStore(_home()).all()]
    if json_out:
        typer.echo(json.dumps({"saved": rows}, indent=2))
        return
    if not rows:
        typer.echo("No saved environments. One is saved when it goes idle or closes (unless it is single use), "
                   "or with environment_save.")
        return
    for r in rows:
        snap = f"snapshot {_gb(r['snapshot_gb'])}, EUR {r['cost_eur_month']:.2f} a month" if r["snapshot_gb"] else \
            f"paused machine, about EUR {r['cost_eur_month']:.2f} a month" if r.get("paused") else "files only"
        typer.echo(f"{r['id']}  \"{r['title']}\"  {r['repo'] or 'no repository'}  {r['branch'] or '-'}  "
                   f"{r['saved_at']}  {r['kind']}  {snap}  {r['files']} files, {r['commits']} commits"
                   + ("  (earlier grant)" if r["earlier"] else ""))
    total = sum(r["cost_eur_month"] for r in rows)
    typer.echo(f"Snapshots and paused machines together: EUR {total:.2f} a month (snapshots at EUR "
               f"{s.snapshot_eur_per_gb_month:g} per GB, snapshot_eur_per_gb_month in environments.toml).")
    if any(r["earlier"] for r in rows):
        typer.echo("Saves marked (earlier grant) are this computer's agent's under an earlier agent grant, which it "
                   "no longer reaches: `openajar environments adopt` gives them to its current grant.")


def _forget_now(mgr: Any, title: str | None, save_id: str | None) -> dict:
    return asyncio.run(mgr.forget(None, title, save_id))


@app.command()
def forget(title: Annotated[str, typer.Argument(metavar="TITLE", help="The environment's title, as `saves` lists it.")],
           save: Annotated[str | None, typer.Option("--save", help="Only this save id.")] = None) -> None:
    """Forget the saves of a title (or one save), and delete their disk snapshots at the provider."""
    mgr, _vms = _managers()
    try:
        out = _forget_now(mgr, title, save)
    except OpenajarError as e:
        raise _fail(e) from None
    typer.echo(f"Forgot {len(out['forgot'])} save{'s' if len(out['forgot']) != 1 else ''} "
               f"({', '.join(out['forgot']) or 'none'}); deleted {out['snapshots_deleted']} snapshot"
               f"{'s' if out['snapshots_deleted'] != 1 else ''}.")
    for k in out.get("snapshots_kept") or []:
        typer.echo(f"Kept snapshot {k['id']}: save {', '.join(k['used_by'])} still resumes from it.")
    for k in out.get("snapshots_left") or []:
        typer.echo(k["words"])
    for k in out.get("kept") or []:
        typer.echo(f"Kept {k['id']}: {k['why']}.")


def _saves_key(*, create: bool) -> bytes | None:
    """The saves key from the node's vault (openajar.environments.saving.saves_key)."""
    from openajar.commands.base import cli_runtime
    from openajar.environments.models import load_settings
    from openajar.environments.saving import saves_key

    try:
        return asyncio.run(saves_key(cli_runtime().get("vault"), encrypt=load_settings(_home()).encrypt_saves,
                                     create=create))
    except OpenajarError as e:
        raise _fail(e) from None


@app.command("save-export")
def save_export(save_id: Annotated[str, typer.Argument(metavar="ID", help="A save id, like s7k2m9p3x4q.")],
                file: Annotated[Path, typer.Argument(metavar="FILE", help="The file to write.")]) -> None:
    """Write one save to a file, to move it to another node (`save-import` there).

    The file holds the work unsealed (mode 0600): keep it private. The disk snapshot stays at the
    provider; the other node boots from it only with the same provider account, else from the files."""
    from openajar.environments.saves import SaveStore, export_save

    try:
        m = export_save(SaveStore(_home()), save_id, file, _saves_key(create=False))
    except OpenajarError as e:
        raise _fail(e) from None
    typer.echo(f"Wrote save {m.id} (\"{m.title}\", {m.files} files, {m.commits} commits) to {file}. It is not "
               "sealed: keep it private.")


def _import_family(policy: Any, given: str | None) -> str | None:
    """The family an imported save goes to: the one of the grant --family names, else this computer's agent's
    now (its local agent grant), else (no such grant) the one in the file."""
    if given:
        if policy is None:
            return given
        try:
            return str(policy.grant_record(given)["env_family"])
        except NotFound:
            raise NotFound(f"there is no grant {given} on this node: `openajar grants list` shows them") from None
    current = policy.local_agent_grant() if policy is not None else None
    return current["family"] if current is not None else None


@app.command("save-import")
def save_import(file: Annotated[Path, typer.Argument(metavar="FILE", help="A file from save-export.")],
                family: Annotated[str | None, typer.Option("--family", help="A grant (its id, as `openajar grants "
                                                          "list` shows it) whose agent may resume it; default: this "
                                                          "computer's agent.")]
                = None) -> None:
    """Add a save from another node's `save-export`; an agent resumes it with its title.

    It goes to this computer's agent (its agent grant now), or with --family to the agent of that grant."""
    from openajar.environments.adopt import node_policy
    from openajar.environments.saves import SaveStore, import_save

    store = SaveStore(_home())
    with node_policy(_home()) as policy:
        try:
            m = import_save(store, file, _saves_key(create=True), family=_import_family(policy, family))
        except OpenajarError as e:
            raise _fail(e) from None
        live = policy.live_family(m.family) if policy is not None else {}  # no grants: every caller is the owner
    if live is not None:
        whose = f" for {live['subject']}: its" if live else "."
        typer.echo(f"Imported save {m.id} (\"{m.title}\"){whose} environment_open(title=\"{m.title}\") resumes it.")
    else:
        typer.echo(f"Imported save {m.id} (\"{m.title}\") under grant family {m.family}, which no live agent grant of "
                   "this node has, so no agent can resume it yet. Import it again with --family <grant id> "
                   "(`openajar grants list` shows them).")
    sharing = [o.id for o in store.all() if m.snapshot is not None and o.id != m.id and o.snapshot is not None
               and o.snapshot.id == m.snapshot.id]
    if sharing:
        typer.echo(f"It shares its disk snapshot with save {', '.join(sharing)}: forgetting one keeps the snapshot "
                   "while another save names it.")


repo_app = typer.Typer(no_args_is_help=True, help=(
    "Connect private repositories: a key or token in the vault, and the declaration for env."))
app.add_typer(repo_app, name="repo")
app.add_typer(secret_app, name="secret")
app.add_typer(machine_app, name="machine")


def _remote_or_fail(remote: str) -> Any:
    from openajar.git.wire import parse_remote

    try:
        r = parse_remote(remote)
    except OpenajarError as e:
        raise _fail(e) from None
    if r.repo.endswith("/*"):
        raise _fail(InvalidRequest("name one repository, like github.com/me/shop (for owner/*, use `openajar vault "
                                   "git key add` and `openajar vault git remote allow env`)"))
    return r


def _env_rules(home: Path) -> list[Any]:
    from openajar.git.remotes import Remotes

    return [r for r in Remotes(home / "git" / "remotes.json").list() if r.scope.split("/")[0] == WORKSPACE]


@repo_app.command("add")
def repo_add(remote: Annotated[str, typer.Argument(help="The repository, like github.com/me/shop.")],
             token: Annotated[bool, typer.Option("--token", help="Store a fine-grained token (read from a prompt) "
                                                 "instead of making a deploy key.")] = False,
             stdin: Annotated[bool, typer.Option("--stdin", help="Read the token from stdin.")] = False,
             replace: Annotated[bool, typer.Option("--replace", help="Make a new key or token even if the vault has "
                                                   "one for this repository.")] = False,
             read_only: Annotated[bool, typer.Option("--read-only", help="Clone only, no pushes.")] = False) -> None:
    """Connect a private repository: a deploy key (printed, to paste) or a token, and the declaration.

    The same as `openajar vault git key add --host H --repo O/N` and `openajar vault git remote allow
    env H/O/N`. Run it on the node, as the owner; an agent never does."""
    from openajar.git import cli as gitcli
    from openajar.git import keys as keymod
    from openajar.git.remotes import Remotes

    r = _remote_or_fail(remote)
    have = None if replace else keymod.pick(gitcli.vault_credentials(), r)
    if have is None:
        out = gitcli.make_credential(r.host, [r.repo], token=token, stdin=stdin, replace=replace)
        gitcli.echo_credential(out, pushers="agents")
        key = out["type"] == "ssh"
    else:
        key = have.type == "ssh"
        typer.echo(f"The vault has the {'deploy key' if key else 'token'} {have.name} for "
                   f"{r.host}/{', '.join(have.repos)} already (--replace makes a new one).")
        if have.public_key:
            typer.echo(f"If it is not added yet, add it as a deploy key, with write access: "
                       f"{gitcli.deploy_key_hint(r.host, [r.repo])}")
            typer.echo("")
            typer.echo(have.public_key)
            typer.echo("")
    try:
        rule = Remotes(_home() / "git" / "remotes.json").allow(WORKSPACE, r.slug, push=not read_only)
    except OpenajarError as e:
        raise _fail(e) from None
    typer.echo(f"Declared {r.slug} for environments ({'clone only' if not rule.push else 'clone and push'}).")
    typer.echo(("Once the key is added there, agents" if key else "Agents") + " can open it: "
               f"environment_open(title=..., repo=\"{r.slug}\").")
    from openajar.environments.store import EnvironmentStore

    if any(row["repo"] == r.slug for row in EnvironmentStore(_home() / "environments.db").requests()):
        typer.echo("An agent's card for it in Needs you closes on its own within a few minutes, once the node "
                   "can read it.")


@repo_app.command("list")
def repo_list(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The repositories declared for environments, and the key or token each one uses."""
    from openajar.git import keys as keymod
    from openajar.git.wire import Remote

    rules = _env_rules(_home())
    creds = _creds_or_none()
    rows = []
    for rule in rules:
        if creds is None:
            cred = None
        elif rule.repo.endswith("/*"):  # owner/*: a credential made for owner/* too
            cred = next((c for c in creds if c.host == rule.host and rule.repo in c.repos), None)
        else:
            cred = keymod.pick(creds, Remote(rule.host, rule.repo))
        rows.append({"repo": rule.slug, "scope": rule.scope, "push": rule.push,
                     "credential": cred.public() if cred else None})
    if json_out:
        typer.echo(json.dumps({"repos": rows}, indent=2))
        return
    if not rows:
        typer.echo("No repository is connected for environments. Connect one: "
                   "openajar environments repo add github.com/<owner>/<name>")
        return
    for row in rows:
        c = row["credential"]
        used = (f"{'deploy key' if c['type'] == 'ssh' else 'token'} {c['name']}" if c else
                "no key or token in the vault: public only" if creds is not None else "the vault is not set up")
        typer.echo(f"{row['repo']:<40} {'clone and push' if row['push'] else 'clone only':<15} {used}")


def _creds_or_none() -> list[Any] | None:
    from openajar.commands.base import cli_runtime
    from openajar.git import cli as gitcli

    if cli_runtime().get("vault") is None:
        return None
    return gitcli.vault_credentials()


@repo_app.command("remove")
def repo_remove(remote: Annotated[str, typer.Argument(help="The repository, like github.com/me/shop.")],
                keep_key: Annotated[bool, typer.Option("--keep-key", help="Keep its key or token in the vault.")]
                = False) -> None:
    """Disconnect a repository from environments: its declaration, and the key or token made for it alone."""
    from openajar.git import cli as gitcli
    from openajar.git.remotes import Remotes

    r = _remote_or_fail(remote)
    remotes = Remotes(_home() / "git" / "remotes.json")
    try:
        remotes.deny(WORKSPACE, r.slug)
    except NotFound:
        raise _fail(NotFound(f"{r.slug} is not connected for environments; `openajar environments repo list` "
                             "shows what is")) from None
    typer.echo(f"Removed {r.slug} from environments: new ones can no longer clone it through the node.")
    if keep_key:
        return
    still = [rule for rule in remotes.list() if rule.matches(r)]
    creds = _creds_or_none() or []
    for cred in (c for c in creds if c.host == r.host and c.repos == (r.repo,)):
        if still:
            typer.echo(f"Kept {cred.name}: {', '.join(rule.scope for rule in still)} still may use {r.slug}.")
            continue
        gitcli.remove_credential(cred)
        typer.echo(f"Removed {cred.name} from the vault. Remove it at {r.host} too "
                   f"({'the deploy key' if cred.type == 'ssh' else 'revoke the token'}).")


def _write_settings(updates: dict[str, Any], home: Path | None = None) -> Path:
    """Merge `updates` into environments.toml in `home` (default: this command's --home or OPENAJAR_HOME)."""
    from openajar.environments.models import update_settings

    return update_settings(home or _home(), updates)


@app.command()
def allow(max_live: Annotated[int | None, typer.Option("--max-live", help="Open at once, at most (default 3).")
                               ] = None,
          max_hours: Annotated[float | None, typer.Option("--max-hours", help="How long one lives, extend included, "
                                                          "at most (default 8).")] = None,
          monthly_eur: Annotated[float | None, typer.Option("--monthly-eur", help="What environments may book in a "
                                                            "month, in EUR (default 50; 0: no budget).")] = None,
          max_size: Annotated[str | None, typer.Option("--max-size", help="The biggest machine an agent may ask "
                                                       "for: small, medium, large or xlarge (default).")] = None,
          keep_live: Annotated[bool | None, typer.Option("--keep-live/--no-keep-live", help="Let agents keep a "
                                                         "machine running between chats (keep \"live\"; it bills all "
                                                         "month).")] = None,
          max_live_kept: Annotated[int | None, typer.Option("--max-live-kept", help="Machines kept live at once, at "
                                                            "most (default 1).")] = None,
          network: Annotated[str | None, typer.Option("--network", help="What machines reach: full (default), "
                                                      "limited (registries, code hosts, --host) or none.")] = None,
          host: Annotated[list[str] | None, typer.Option("--host", help="One more host limited reaches, like "
                                                         "api.example.com or .example.com (repeat for more).")] = None,
          warm: Annotated[int | None, typer.Option("--warm", help="Machines kept booted for the next open (default "
                                                   "0: none). Each one bills while it waits.")] = None,
          days: Annotated[int, typer.Option(help="No longer used: opening one never asks, so there is no yes "
                                                 "to last.", hidden=True)] = 30,
          workspace: Annotated[str, typer.Option(help="No longer used.", hidden=True)] = "env") -> None:
    """Set the limits that bound the environments agents open.

    Opening an environment never asks you. These limits bound it instead, and they hold without this command. Past one, the node refuses an open or an extend with environment_limit, whatever the agent read. The VM limits in vms.toml hold too."""
    from openajar.environments.models import load_settings

    del days, workspace  # kept so older scripts still run; nothing is granted any more
    if max_size is not None and max_size not in SIZES:
        raise _fail(InvalidRequest(f"--max-size is one of {', '.join(SIZES)}"))
    path = _write_settings({"max_live": max_live, "max_hours": max_hours, "monthly_eur": monthly_eur,
                            "max_size": max_size, "allow_keep_live": keep_live, "max_live_kept": max_live_kept,
                            **_network_updates(network, host), "warm": warm})
    typer.echo(f"Environments: {_limit_words(load_settings(_home()))}. Limits in {path}.")
    s = load_settings(_home())
    if s.allow_keep_live:
        typer.echo(f"Agents may keep up to {s.max_live_kept} machine{'s' if s.max_live_kept != 1 else ''} live between "
                   "chats (keep \"live\"). Each bills its full hourly price until it is closed.")
    typer.echo(_network_words(s))
    if s.warm:
        typer.echo(f"Warm pool: {s.warm} machine{'s' if s.warm != 1 else ''} kept booted for the next open, each deleted "
                   f"after {s.warm_minutes} min unclaimed (warm_minutes). Each one counts against the limits above and "
                   "bills while it waits.")
    typer.echo("Opening one never asks you; the node refuses one past these limits.")


def _network_updates(network: str | None, hosts: list[str] | None) -> dict[str, Any]:
    from openajar.environments.models import load_settings
    from openajar.environments.network import LEVELS, check_host

    out: dict[str, Any] = {}
    if network is not None:
        if network not in LEVELS:
            raise _fail(InvalidRequest(f"--network is one of {', '.join(LEVELS)}"))
        out["network"] = network
    if hosts:
        try:
            added = [check_host(h) for h in hosts]
        except ValueError as e:
            raise _fail(InvalidRequest(str(e))) from None
        out["allow_hosts"] = list(dict.fromkeys([*load_settings(_home()).allow_hosts, *added]))
    return out


def _network_words(s: Any) -> str:
    if s.network == "full":
        return "Network: full (machines reach the internet)." + (
            f" With limited they would also reach {', '.join(s.allow_hosts)}." if s.allow_hosts else "")
    more = f", and {', '.join(s.allow_hosts)}" if s.allow_hosts and s.network == "limited" else ""
    what = "package registries and code hosts" + more if s.network == "limited" else "the git gateway alone"
    return (f"Network: {s.network} (new machines reach {what}, and the hosts of bound secrets). Agents may narrow it, "
            "never widen it. Machines open now keep theirs.")


def _limit_words(s: Any) -> str:
    """The limits that bound environment_open, in words."""
    live = "the VM limit (vms.toml max_live)" if s.max_live is None else str(s.max_live)
    budget = f"EUR {s.monthly_eur:g}" if s.monthly_eur else "no budget"
    cpu, memory = SIZES[s.max_size]
    return (f"at most {live} open at once, {s.max_hours:g} hours each, {budget} a month, machines up to "
            f"{s.max_size} ({cpu} CPU, {memory} GB)")


@app.command()
def setup(build: Annotated[bool, typer.Option("--build", help="Build the docker provider's image here (10 to 15 "
                                                            "minutes).")] = False,
          pull: Annotated[bool, typer.Option("--pull", help="Download the docker provider's prebuilt image for this "
                                                          "version (builds it here when it cannot).")] = False,
          ) -> None:
    """Check what environments need on this node, and say what is missing.

    The provider (a Hetzner token, or Docker and its image), the image machines boot from, and the
    repositories declared for workspace env in the git gateway."""
    from openajar.vms.manager import VmManager

    home = _home()
    vms = VmManager(home)
    s = vms.settings
    lines: list[tuple[str, str, str]] = []

    async def check_provider() -> str | None:
        try:
            await vms.provider().check()
        except OpenajarError as e:
            return e.message
        return None

    if s.provider == "docker" and build:
        _build_image(s.docker_image, s.docker_bin)
    elif s.provider == "docker" and pull:
        _get_image(s.docker_image, s.docker_bin, s.docker_pull)
    elif s.provider == "own" and (build or pull) and vms.configured:
        own_image(home, build=build)
    problem = asyncio.run(check_provider()) if vms.configured else (
        "no machine of yours is set up: `openajar environments machine add NAME user@host`" if s.provider == "own"
        else "no DigitalOcean token: run `openajar vm setup --provider digitalocean`" if s.provider == "digitalocean"
        else "no Hetzner token: run `openajar vm setup`, or set provider = \"docker\" in vms.toml to use Docker "
             "here, or bring a machine of your own with `openajar environments machine add NAME user@host`")
    if problem is None:
        lines.append(("ok", f"provider {s.provider}", "answers"))
        if getattr(vms.provider(), "experimental", False):
            from openajar.vms.digitalocean import EXPERIMENTAL

            lines.append(("note", "experimental", EXPERIMENTAL))
    else:
        lines.append(("missing", f"provider {s.provider}", problem))
    if problem is None:
        try:
            asyncio.run(vms.provider().resolve_image("snapshot:dev"))
            lines.append(("ok", "image", "the dev snapshot: machines boot ready"))
        except OpenajarError:
            lines.append(("ok", "image", "Ubuntu 24.04, set up by cloud-init on each boot (a few minutes); "
                                        "`openajar vm image build dev` makes them boot ready"
                          if s.provider not in ("docker", "own") else
                          "none: the machine itself, set up by `machine add`" if whole_default(home, s) else s.docker_image))
    from openajar.git.remotes import Remotes

    declared = [r for r in Remotes(home / "git" / "remotes.json").list() if r.scope.split("/")[0] == "env"]
    from openajar.git import login

    gh = login.load(home)
    if gh is not None and gh.any_repo:
        lines.append(("ok", "git", f"any repository the GitHub login {gh.login or ''} reaches, clone and push "
                                   "(`openajar setup github` changes it)".replace("  ", " ")))
    elif declared:
        lines.append(("ok", "git", "connected for env: " + ", ".join(r.slug for r in declared)
                      + " (`openajar environments repo list` shows their keys)"))
    else:
        lines.append(("missing", "git", ("no repository is connected for environments, so only public repositories "
                                         "clone and nothing can be pushed. Connect a private one (a deploy key to "
                                         "paste, or --token): `openajar environments repo add "
                                         "github.com/<owner>/<name>`")))
    if s.provider == "docker":
        lines.append(("note", "isolation", docker_isolation_words()))
    if s.provider == "own":
        from openajar.environments.machines_cli import ISOLATION, WHOLE

        lines.append(("note", "isolation", WHOLE if whole_default(home, s) else ISOLATION))
    lines.append(("note", "grants", ("agents connected with `openajar connect` or `openajar dev up` may open one (the "
                                     "environments special: vm, exec and the owner's bound logins on env/* lanes "
                                     "only). Nothing in one asks you, opening and pushing included; the limits bound "
                                     "it")))
    from openajar.environments.models import load_settings

    env_settings = load_settings(home)
    refused, where = _local_browser(home, env_settings.browser)
    lines.append(refused or _browser_line(where, "whole" if whole_default(home, s) else s.provider, s.docker_image,
                                          s.docker_bin))
    lines.append(("note", "limits", _limit_words(env_settings) + " (`openajar environments allow` changes them)"))
    for status, what, words in lines:
        typer.echo(f"{status:<8} {what:<18} {words}")
    if any(st == "missing" for st, _w, _x in lines if _w.startswith("provider")):
        raise typer.Exit(1)


def _local_browser(home: Path, said: str) -> tuple[tuple[str, str, str] | None, str]:
    """(a refusal line, where the browser runs). On a laptop's home (openajar local) it runs on the VM,
    also when environments.toml does not say so, and "node" is refused (openajar.local.node.
    environments_browser). Any other home: as its environments.toml says."""
    try:
        from openajar.local import home as local_home
        from openajar.local.paths import NAME

        cfg = local_home.read(home)
    except Exception:  # noqa: BLE001 - not a local home, or one this check cannot read: the node's way
        return None, said
    if cfg is None or cfg.name != NAME:
        return None, said
    from openajar.client.errors import ClientError
    from openajar.local.node import environments_browser

    try:
        return None, environments_browser(home)
    except ClientError as e:
        return ("missing", "browser", f"{e.message} {e.hint or ''}".strip()), "vm"


def _browser_line(where: str, provider: str, image: str, docker: str) -> tuple[str, str, str]:
    """Where the environments' browser runs (environments.toml browser), and for "vm" on docker whether
    the image has it (its label org.agentos.browser)."""
    if where != "vm":
        return ("ok", "browser", "on this node, with its traffic through the machine (browser = \"node\")")
    words = "on each machine (browser = \"vm\"), driven over its SSH connection"
    if provider == "whole":
        return ("ok", "browser", words + "; your machine's setup installed it")
    if provider == "own":
        return ("ok", "browser", words + "; the environments image on your machine has it")
    if provider != "docker":
        return ("ok", "browser", words + "; the dev snapshot has it, any other image installs it on the first open")
    import subprocess

    try:
        out = subprocess.run([docker, "image", "inspect", "-f", '{{index .Config.Labels "org.agentos.browser"}}',
                              image], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    if out == "1":
        return ("ok", "browser", words + f"; {image} has it" + _desktop_words(image, docker))
    return ("missing", "browser", words + f", but {image} has no browser, so each machine installs it when it "
                                          "opens (minutes): `openajar environments setup --pull`")


def _desktop_words(image: str, docker: str) -> str:
    """Whether the image has the desktop at this openajar's version (its label org.agentos.desktop). An image
    without it still works: each machine installs it on its first screen (a minute more)."""
    import subprocess

    from openajar.environments.vmbrowser import DESKTOP_VERSION

    try:
        out = subprocess.run([docker, "image", "inspect", "-f", '{{index .Config.Labels "org.agentos.desktop"}}',
                              image], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    if out == str(DESKTOP_VERSION):
        return " and its desktop"
    return (", but not this version's desktop, so each machine installs it on its first screen (a minute): "
            "`openajar environments setup --build`")


def docker_isolation_words(platform: str | None = None) -> str:
    """What the docker provider keeps a machine from, and what it does not, said plainly."""
    import sys

    mac = (platform or sys.platform) == "darwin"
    words = ("each machine has its own network, CPU, memory and process limits (vms.toml docker_cpus, "
             "docker_memory_gb, docker_pids) and a firewall that keeps it off this computer and your network: it "
             "reaches the internet only. It still shares a kernel ")
    words += ("with Docker Desktop's Linux VM, which can read the folders you share with Docker (your home folder "
              "by default)" if mac else "with this computer")
    words += (", so a machine that breaks out of its container is on this computer. Use the docker provider for "
              "your own code; the cloud provider (hetzner) is the isolated one.")
    if mac:
        words += (" macOS cannot tell which account opened a local connection, so another account on this Mac "
                  "could use an environment's browser tunnel port.")
    return words


def _image_source() -> Path | None:
    """The folder with the image's Dockerfile and entrypoint: inside the package (any install: the wheel
    carries deploy/environments as openajar/environments/image), or a checkout's deploy/environments."""
    here = Path(__file__).resolve()
    for folder in (here.parent / "image", here.parents[3] / "deploy" / "environments"):
        if (folder / "Dockerfile").exists():
            return folder
    return None


def _build_image(image: str, docker: str) -> None:
    """docker build of deploy/environments/Dockerfile with the package's setup script, in a temp folder."""
    import shutil
    import subprocess
    import tempfile

    here = Path(__file__).resolve()
    source = _image_source()
    if source is None:
        raise _fail(NotFound("the image's Dockerfile is not in this installation of openajar: install it again "
                             "(scripts/install.sh), or build the image from a checkout of openajar"))
    with tempfile.TemporaryDirectory(prefix="agentos-env-") as tmp:
        for f in source.iterdir():
            if f.is_file():
                shutil.copy(f, tmp)
        for name in ("setup.sh", "browser.sh", "browser-requirements.txt", "desktop.sh"):
            shutil.copy(here.parent / name, tmp)
        typer.echo(f"Building {image} here (10 to 15 minutes the first time)...")
        done = subprocess.run([docker, "build", "-q", "-t", image, tmp], check=False)
        if done.returncode != 0:
            raise typer.Exit(done.returncode)
    typer.echo(f"Built {image}.")


def _pull_source(pull: str) -> str:
    """vms.toml's docker_pull with this version in it: the prebuilt image to download ("" never downloads)."""
    from openajar import __version__

    return pull.replace("{version}", __version__).strip()


def _pull_image(image: str, docker: str, pull: str) -> bool:
    """docker pull of the prebuilt image for this version of openajar, tagged as `image`.

    False, after saying why, when there is none to pull (no network, no image for this version or for
    this computer's CPU) or it is older than this openajar: the caller builds it here instead."""
    import subprocess

    from openajar.vms.docker import IMAGE_LABEL, IMAGE_VERSION

    source = _pull_source(pull)
    if not source:
        return False
    typer.echo(f"Downloading {source} (a minute or two)...")
    try:
        done = subprocess.run([docker, "pull", "-q", source], capture_output=True, text=True, timeout=1800,
                              check=False)
        if done.returncode != 0:
            said = (done.stderr or done.stdout or "").strip().splitlines()
            typer.echo(f"It did not download ({said[-1][:200] if said else f'exit {done.returncode}'}).")
            return False
        label = subprocess.run([docker, "image", "inspect", "-f", f'{{{{index .Config.Labels "{IMAGE_LABEL}"}}}}',
                                source], capture_output=True, text=True, timeout=60, check=False).stdout.strip()
        if not label.isdigit() or int(label) < IMAGE_VERSION:
            typer.echo(f"{source} is older than this openajar.")
            return False
        if source != image and subprocess.run([docker, "tag", source, image], capture_output=True, timeout=60,
                                              check=False).returncode != 0:
            typer.echo(f"It downloaded, but docker could not name it {image}.")
            return False
    except (OSError, subprocess.TimeoutExpired) as e:
        typer.echo(f"It did not download ({type(e).__name__}).")
        return False
    typer.echo(f"Downloaded {image}.")
    return True


def _get_image(image: str, docker: str, pull: str) -> None:
    """The docker provider's image: the prebuilt one for this version when it downloads, else built here."""
    if _pull_image(image, docker, pull):
        return
    if _pull_source(pull):
        typer.echo("Building it here instead.")
    _build_image(image, docker)
