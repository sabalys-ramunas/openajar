"""`openajar environments env push ID [FILES...]`: the app's local env files on an environment's machine,
plain settings copied and secrets bound as placeholders (openajar.environments.envfiles).

    openajar environments env push e7k2 apps/api/.env apps/web/.env.local --dry-run
    openajar environments env push e7k2 apps/api/.env --plain DATABASE_URL --host ZOHO_API_KEY=www.zohoapis.eu

The person runs it in their own terminal (or approves an agent's proposal of it once): it reads their
local files and binds secrets in the node's vault, which no tool does. It shows a table first (key,
class, what happens) and asks; `--yes` skips the question, `--dry-run` shows the table only. A
secret's value goes to the vault only (as `openajar environments secret add` puts it there), never to
the machine, the screen or the environment's call log. The machine's file is written over the
environment's SSH at the same path under its workdir, replacing what was there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core.errors import InvalidRequest, NotFound
from openajar.environments import envfiles as ef
from openajar.environments.cli import _managers, app

env_app = typer.Typer(no_args_is_help=True, help=(
    "The app's local env files on an environment's machine: plain settings copied, secrets bound as "
    "placeholders, and the rest never sent."))
app.add_typer(env_app, name="env")  # openajar environments env ...: logins_cli imports this module

WRITE_TIMEOUT_S = 60.0


def _fail(message: str) -> typer.Exit:
    from openajar.environments.secrets_cli import _fail as fail

    return fail(InvalidRequest(message))


def _git(cwd: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _root(path: Path) -> Path:
    """The local repository the file is in (its git top level), else the current folder."""
    top = _git(path.parent, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else Path.cwd().resolve()


def _default_files() -> list[Path]:
    """`env_files` of the local .openajar/environment.toml, else ./.env."""
    from openajar.environments import repoconfig

    root = _root(Path.cwd() / "x")
    for name in repoconfig.CONFIG_PATHS:
        cfg = root / name
        if cfg.is_file():
            try:
                found = repoconfig.parse(cfg.read_text()).env_files
            except (repoconfig.ConfigError, OSError, UnicodeDecodeError) as e:
                raise _fail(str(e)) from None
            if found:
                return [root / p for p in found]
            break
    if (Path.cwd() / ".env").is_file():
        return [Path.cwd() / ".env"]
    raise _fail("name the env files to push, like apps/api/.env (or list them as env_files in "
                ".openajar/environment.toml)")


def _read(path: Path) -> tuple[str, ef.Parsed]:
    """(its path in the repository, what it holds)."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise _fail(f"{path} is not a file")
    if path.stat().st_size > ef.FILE_MAX_BYTES:
        raise _fail(f"{path} is larger than {ef.FILE_MAX_BYTES // 1024} KB; is it an env file?")
    root = _root(path)
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        raise _fail(f"{path} is not in the repository at {root}; run this from the repository") from None
    try:
        return rel, ef.parse(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        raise _fail(f"{rel} is not a text file") from None
    except ValueError as e:
        raise _fail(f"{rel} {e}") from None


def _environment(mgr: Any, ref: str) -> Any:
    try:
        return mgr.get(ref)
    except (NotFound, ValueError):
        pass
    found = [e for e in mgr.store.open() if e.title == ref]
    if len(found) == 1:
        return found[0]
    raise _fail(f"no open environment {ref}; `openajar environments list` shows their ids")


def _check_repo(env: Any, roots: set[Path]) -> None:
    """Refuse files from another repository than the environment's (when both are known)."""
    if not env.repo:
        return
    from openajar.core.errors import OpenajarError
    from openajar.git.wire import parse_remote

    for root in roots:
        url = _git(root, "remote", "get-url", "origin")
        if not url:
            continue
        try:
            slug = parse_remote(url).slug
        except OpenajarError:
            continue
        if slug != env.repo:
            raise _fail(f"these files are from {slug}, and environment {env.id} is {env.repo}")


def _overrides(plain: list[str], secret: list[str], skip: list[str], host: list[str]) -> ef.Overrides:
    from openajar.environments.network import check_host

    hosts: dict[str, list[str]] = {}
    for h in host:
        key, sep, name = h.partition("=")
        try:
            checked = check_host(name.strip()) if sep and key.strip() and name.strip() else ""
        except ValueError as e:
            raise _fail(str(e)) from None
        if not checked or checked.startswith((".", "*")) or ":" in checked:
            raise _fail(f"--host is KEY=HOST with one host name, like --host ZOHO_API_KEY=www.zohoapis.eu (not {h!r})")
        hosts.setdefault(key.strip(), []).append(checked)
    return ef.Overrides(plain=plain, secret=secret, skip=skip, hosts=hosts)


def _vault() -> Any:
    """The node's vault, or None when it has none yet (then nothing is bound)."""
    from openajar.commands.base import cli_runtime

    return cli_runtime().get("vault")


async def _bound(vault: Any, repo: str | None) -> dict[str, tuple[str, ...]]:
    from openajar.environments import appsecrets

    return {b.var: tuple(b.hosts) for b in await appsecrets.bound_for(vault, repo)}


def _no_vault(plans: list[list[ef.Row]]) -> None:
    for rows in plans:
        for i, r in enumerate(rows):
            if r.action == ef.BIND:
                rows[i] = ef.Row(r.entry, ef.SECRET, ef.HOLD, "this node has no vault to bind it in",
                                 fix="openajar vault init, then run this again", use=r.use)


async def _bind(vault: Any, rows: list[ef.Row], repo: str | None) -> list[str]:
    from openajar.environments import appsecrets

    unsealed = getattr(vault, "_unsealed", None)
    if callable(unsealed):
        await unsealed()
    done = []
    for r in rows:
        if r.action == ef.BIND:
            await appsecrets.add(vault, r.entry.key, r.entry.value, list(r.hosts), repo=repo)
            done.append(f"{r.entry.key} to {', '.join(r.hosts)}")
    return done


def write_script(workdir: str, files: list[tuple[str, str]]) -> str:
    """The shell script that writes each (path in the repository, content) under `workdir` on the
    machine: a temp file, mode 0600, owned as the workdir is, then renamed over the old one."""
    import secrets

    lines = ["set -e"]
    for rel, content in files:
        dest = f"{workdir.rstrip('/')}/{rel}"
        end = "OPENAJAR_ENV_" + secrets.token_hex(8)
        q, tmp = shlex.quote(dest), shlex.quote(dest + ".openajar-tmp")
        lines += [f"mkdir -p \"$(dirname {q})\"", f"cat > {tmp} <<'{end}'", content.rstrip("\n"), end,
                  f"chmod 600 {tmp}", f"chown --reference={shlex.quote(workdir)} {tmp} 2>/dev/null || true",
                  f"mv -f {tmp} {q}"]
    return "\n".join(lines) + "\n"


def write_files(vms: Any, env: Any, files: list[tuple[str, str]]) -> None:
    """Write the files on the environment's machine over its SSH (as `show` reads its git)."""
    from openajar.vms import ssh

    try:
        rec = vms.store.get(env.vm_id or "")
    except NotFound:
        rec = None
    if rec is None or rec.state != "ready" or not rec.host:
        raise _fail(f"environment {env.id}'s machine is not ready; nothing was sent")
    code, out = asyncio.run(ssh.run(vms.remote(rec), write_script(env.workdir, files), timeout_s=WRITE_TIMEOUT_S))
    if code != 0:
        raise _fail(f"writing the files on the machine failed (exit {code}): {out.strip()[-300:]}")


def _confirm(yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise _fail("nothing was sent: read the table, then run again with --yes")
    if not typer.confirm("Send this to the environment?", default=False):
        typer.echo("Nothing was sent.")
        raise typer.Exit(1)


def _as_json(env: Any, files: list[tuple[str, list[ef.Row]]], sent: bool) -> str:
    out = []
    for rel, rows in files:
        keys = [{"key": r.entry.key, "class": r.cls, "action": r.action, "note": r.note, "hosts": list(r.hosts),
                 **({"value": r.entry.value} if r.action == ef.COPY and r.note != ef.PLAIN_SAID else {}),
                 **({"fix": r.fix} if r.fix else {})} for r in rows]
        out.append({"path": rel, "to": f"{env.workdir.rstrip('/')}/{rel}", "keys": keys, "counts": ef.counts(rows)})
    return json.dumps({"environment": env.id, "sent": sent, "files": out}, indent=2)


@env_app.command("push")
def push(env_ref: Annotated[str, typer.Argument(metavar="ID", help="An environment id, like e7k2 (or its title).")],
         files: Annotated[list[Path] | None, typer.Argument(metavar="FILES", help="Local env files, like apps/api/.env "
                                                             "(default: env_files in .openajar/environment.toml, "
                                                             "else ./.env).")] = None,
         plain: Annotated[list[str] | None, typer.Option("--plain", metavar="KEY", help="Copy this key as it is "
                                                         "(a pattern like NEXT_PUBLIC_* works).")] = None,
         secret: Annotated[list[str] | None, typer.Option("--secret", metavar="KEY", help="Treat this key as a "
                                                          "secret.")] = None,
         host: Annotated[list[str] | None, typer.Option("--host", metavar="KEY=HOST", help="Bind this secret to "
                                                        "HOST, like ZOHO_API_KEY=www.zohoapis.eu.")] = None,
         skip: Annotated[list[str] | None, typer.Option("--skip", metavar="KEY", help="Leave this key out.")]
         = None,
         dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the table only; send nothing.")] = False,
         yes: Annotated[bool, typer.Option("--yes", help="Send without asking (after reading the table).")]
         = False,
         json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Put local env files on an environment's machine: plain settings copied, secrets bound as placeholders.

    Each key is sorted into plain or secret (when in doubt, a secret). The machine's file, at the same
    path under the repository's folder, gets plain values as written, `KEY=${KEY}` for a secret bound to
    its host (the node puts the real value in for that host only), and a comment for a secret a
    placeholder cannot carry. A secret's value is never shown or sent to the machine."""
    from openajar.environments.secrets_cli import _run
    from openajar.vault.models import ENV_VAR_RE

    mgr, vms = _managers()
    env = _environment(mgr, env_ref)
    if not env.open:
        raise _fail(f"environment {env.id} is {env.state}; open it again, then push")
    overrides = _overrides(plain or [], secret or [], skip or [], host or [])
    paths = list(files or []) or _default_files()
    read = [_read(p) for p in paths]
    _check_repo(env, {_root(p.expanduser().resolve()) for p in paths})
    vault = _vault()
    bound = _run(_bound(vault, env.repo)) if vault is not None else {}
    plans = [ef.plan_file(parsed.entries, overrides=overrides, bound=bound, live=set(env.secrets), bindable=ENV_VAR_RE)
             for _rel, parsed in read]
    ef.across_files(plans)
    if vault is None:
        _no_vault(plans)
    named = list(zip([rel for rel, _ in read], plans, strict=True))
    if json_out and (dry_run or not yes):
        typer.echo(_as_json(env, named, sent=False))
        if not dry_run:
            raise _fail("nothing was sent: --json needs --yes or --dry-run")
        return
    if not json_out:
        _show(env, read, named)
    if dry_run:
        typer.echo("Nothing was sent (--dry-run).")
        return
    if env.state != "ready":  # before anything is bound: a push binds and writes together
        raise _fail(f"environment {env.id} is {env.state}; push when it is ready. Nothing was sent")
    _confirm(yes)
    all_rows = [r for _rel, rows in named for r in rows]
    bound_now = _run(_bind(vault, all_rows, env.repo)) if vault is not None else []
    write_files(vms, env, [(rel, ef.render(rows)) for rel, rows in named])
    for rel, rows in named:  # the files are written; the call log is a convenience
        with contextlib.suppress(Exception):
            mgr.record_call(env.terminal_lane, "env_push", ok=True, summary=ef.summary(rel, rows))
    if json_out:
        typer.echo(_as_json(env, named, sent=True))
        return
    _done(env, named, bound_now)


def _show(env: Any, read: list[tuple[str, ef.Parsed]], named: list[tuple[str, list[ef.Row]]]) -> None:
    for (rel, parsed), (_rel, rows) in zip(read, named, strict=True):
        typer.echo(f"{rel} -> {env.workdir.rstrip('/')}/{rel} on {env.id} ({env.title})")
        for line in ef.table(rows):
            typer.echo(f"  {line}")
        if parsed.bad_lines:
            typer.echo(f"  Left out: line {', '.join(map(str, parsed.bad_lines[:10]))}, not KEY=value.")
        fixes = ef.fixes(rows)
        if fixes:
            typer.echo("  Not sent, and what you can do:")
            for f in fixes:
                typer.echo(f"    {f}")
        typer.echo("")


def _done(env: Any, named: list[tuple[str, list[ef.Row]]], bound_now: list[str]) -> None:
    for rel, rows in named:
        c = ef.counts(rows)
        parts = [f"{c[a]} {w}" for a, w in ((ef.COPY, "copied"), (ef.PLACEHOLDER, "placeholders"),
                                            (ef.BIND, "bound"), (ef.HOLD, "not sent"), (ef.SKIP, "left out"))
                 if c.get(a)]
        typer.echo(f"Wrote {env.workdir.rstrip('/')}/{rel}: {', '.join(parts)}.")
    if bound_now:
        scope = f"environments of {env.repo}" if env.repo else "every environment"
        typer.echo(f"Bound {'; '.join(bound_now)} for {scope}. The machine gets their placeholders when the "
                   "environment opens again (close it and open the same title: a save resumes); until then "
                   "they read as empty.")
    typer.echo("Restart the app on the machine so it reads the new values.")
