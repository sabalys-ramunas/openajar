"""Ops commands: doctor, backup, migrate, upgrade, health, and lane refresh-fingerprint.

`app` holds them all (mounted as `openajar ops ...` by the feature package contract). The root
CLI should also call `mount(root_app, lane_group=lane_app)`, which adds the documented names:

  openajar doctor [--json] [--offline] [--only a,b]
  openajar backup create|verify|restore|list|prune
  openajar migrate status|run
  openajar upgrade --source DIR
  openajar health check|status
  openajar lane refresh-fingerprint <lane> --keep-device
  openajar lane check <lane> | --all [--json] [--offline]
  openajar lane health <lane> | --all [--days N] [--fresh] [--json]

Passphrases are read from --passphrase-file (or OPENAJAR_BACKUP_PASSPHRASE_FILE), or asked on
the terminal; they are never accepted on the command line, where `ps` would show them.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from openajar.core import migrate
from openajar.core.config import config_path, home_from_env, load_config
from openajar.core.layout import default_backup_dir, ops_socket
from openajar.core.system import Host
from openajar.keepalive.cli import keepalive_cmd
from openajar.node import lanefmt
from openajar.ops import backup, doctor, logs
from openajar.ops.cli_common import JsonOpt, _emit, _errors, _fail, _home, _mib, _node_name, read_passphrase
from openajar.ops.lane_cli import (
    lane_audit_cmd,
    lane_check_cmd,
    lane_health_cmd,
    redteam_cmd,
    refresh_fingerprint_cmd,
    stealth_check_cmd,
)

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="Operations: doctor, backups, migrations, upgrades and health.")
backup_app = typer.Typer(no_args_is_help=True, help="Encrypted backups of OPENAJAR_HOME (profiles, vault, databases).")
migrate_app = typer.Typer(no_args_is_help=True, help="Versioned migrations of the databases and the lane directories.")
health_app = typer.Typer(no_args_is_help=True, help="Node health: exit IPs, proxies, disk, RAM, browser drift.")


# ---- passphrases ----------------------------------------------------------------------


PassFile = Annotated[Path | None, typer.Option("--passphrase-file", help="File holding the passphrase (chmod 600).")]


# ---- doctor -----------------------------------------------------------------------------

def _connections_reader() -> Any:
    """How the doctor reads this computer's `openajar connect` connections (None without the client)."""
    try:
        from openajar.client.state import connections_at
    except ModuleNotFoundError:
        return None
    return connections_at


def _claude_profiles_check() -> Any:
    """The doctor's check of the Claude Code profiles on this computer (None without the client): in each,
    an openajar or agentos entry whose address on this computer does not answer, or a connection's entry
    that is missing."""
    try:
        from openajar.client.claude import profile_problems, profiles
        from openajar.client.ops import connect_again
        from openajar.client.state import connections_at
    except ModuleNotFoundError:
        return None

    def check(ctx: doctor.DoctorContext) -> list[doctor.CheckResult]:
        home = ctx.client_home or Path.home()
        title = "Claude Code profiles"
        found = [p for p in profiles(home) if p.servers() is not None]
        if not found:
            return [doctor.CheckResult("claude_profiles", title, doctor.SKIP, "no Claude Code profile on this computer")]
        problems = profile_problems(home, connections_at(ctx.client_home), connect_again)
        if not problems:
            return [doctor.CheckResult("claude_profiles", title, doctor.PASS,
                                       f"every openajar entry answers, in {', '.join(p.label for p in found)}")]
        hint = (". Until it is fixed, any shell reaches the tools: openajar call <tool> (openajar tools lists "
                "them)")
        return [doctor.CheckResult("claude_profiles", f"Claude Code {p.profile.label}", doctor.FAIL, p.detail,
                                   p.fix + hint, data={"profile": str(p.profile.config_file), "entry": p.name})
                for p in problems]

    return check


def doctor_cmd(
    as_json: JsonOpt = False,
    offline: Annotated[bool, typer.Option("--offline", help="Skip checks that go through the network.")] = False,
    only: Annotated[str | None, typer.Option(help="Comma-separated check names, for example disk,proxies.")] = None,
    unit: Annotated[str, typer.Option(help="systemd unit of the daemon.")] = "agentosd",
) -> None:
    """Check this node and print what is wrong and how to fix it (read-only, never prints secrets).

    On your computer (no node.toml, but `openajar connect` connections) it checks the connections:
    a connection whose agents still hold the node token is a warning."""
    ctx = doctor.DoctorContext(home=_home(), unit=unit, network=not offline, read_connections=_connections_reader(),
                               claude_profiles=_claude_profiles_check())
    names = [n.strip() for n in only.split(",") if n.strip()] if only else None
    if names is None and not config_path(ctx.home).exists() and doctor.client_connections(ctx):
        names = doctor.CLIENT_CHECKS
        if not as_json:
            typer.echo("This computer is not a node; checking its connections (openajar connect).")
    try:
        report = doctor.run(ctx, names)
    except ValueError as e:
        _fail(str(e))
    if as_json:
        _emit(report.to_dict())
    else:
        typer.echo(doctor.format_text(report, color=sys.stdout.isatty()))
    raise typer.Exit(0 if report.ok else 1)


# ---- backup -----------------------------------------------------------------------------

Target = Annotated[str | None, typer.Option("--to", help="Directory or s3://bucket/prefix (default: "
                                                          "OPENAJAR_HOME/backups).")]


def _is_s3(target: str | None) -> bool:
    return bool(target) and str(target).startswith("s3://")


def _s3(target: str, home: Path):
    from openajar.core.s3 import S3Target

    return S3Target(target, home=home)


@backup_app.command("create")
def backup_create(
    to: Target = None,
    passphrase_file: PassFile = None,
    keep: Annotated[int, typer.Option(help="Keep this many backups of this node (0 keeps all).")] = 7,
    allow_live: Annotated[bool, typer.Option("--allow-live", help="Copy a warm lane while it runs when no daemon "
                                                                   "can pause it.")] = False,
    include_caches: Annotated[bool, typer.Option("--include-caches", help="Keep browser caches too.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Write one encrypted snapshot of OPENAJAR_HOME: profiles, fingerprints, vault, secrets, databases."""
    home = _home()
    node = _node_name(home)
    passphrase = read_passphrase(passphrase_file, confirm=True)
    coordinator = backup.DaemonCoordinator(home)
    with _errors():
        if _is_s3(to):
            remote = _s3(str(to), home)
            outgoing = default_backup_dir(home) / "outgoing"
            result = backup.create_backup(home, outgoing, passphrase, node=node, keep=None, coordinator=coordinator,
                                          allow_live=allow_live, include_caches=include_caches, record=False)
            try:
                remote.put(result.path.name, result.path)
                if remote.head(result.path.name) != result.size:
                    raise backup.BackupError("the uploaded size does not match; the upload is incomplete")
                result.location = remote.location(result.path.name)
                parsed = backup.parse_name(result.path.name)
                backup.record_made(home, result.location, parsed[1] if parsed else datetime.now(UTC), result.size)
                if keep:
                    result.removed = _prune_s3(remote, node, keep)
            finally:
                result.path.unlink(missing_ok=True)
        else:
            dest = Path(to) if to else default_backup_dir(home)
            result = backup.create_backup(home, dest, passphrase, node=node, keep=keep or None,
                                          coordinator=coordinator, allow_live=allow_live,
                                          include_caches=include_caches)
    if as_json:
        _emit(result.to_dict())
        return
    lanes = ", ".join(f"{lane['id']} ({lane['copied']})" for lane in result.lanes) or "none"
    typer.echo(f"Backup written: {result.location or result.path}")
    typer.echo(f"  {result.files} files, {_mib(result.bytes)} before compression, {_mib(result.size)} encrypted")
    typer.echo(f"  lanes: {lanes}")
    for w in result.warnings:
        typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)
    if result.skipped:
        typer.echo(f"  skipped {len(result.skipped)} symlink(s) or special file(s); see --json")
    if result.removed:
        typer.echo(f"  removed {len(result.removed)} older backup(s) (keeping {keep})")
    typer.echo("Keep the passphrase safe: without it this backup cannot be opened.")
    if not _is_s3(to) and Path(to or default_backup_dir(home)).resolve().is_relative_to(home.resolve()):
        typer.echo("This backup is on the node's own disk. Copy it elsewhere too, or use --to s3://... .")


def _prune_s3(remote, node: str, keep: int) -> list[str]:
    ours = []
    for obj in remote.list():
        parsed = backup.parse_name(obj.key)
        if parsed and parsed[0] == backup.safe_node(node):
            ours.append((parsed[1], obj.key))
    removed = []
    for _, key in sorted(ours)[:-keep]:
        remote.delete(key)
        removed.append(remote.location(key))
    return removed


@contextmanager
def _local_archive(archive: str, home: Path) -> Iterator[Path]:
    if not _is_s3(archive):
        yield Path(archive)
        return
    remote_dir, _, name = archive.rpartition("/")
    remote = _s3(remote_dir, home)
    with tempfile.TemporaryDirectory(prefix="openajar-restore-") as tmp:
        os.chmod(tmp, 0o700)
        local = Path(tmp) / name
        remote.get(name, local)
        yield local


@backup_app.command("verify")
def backup_verify(
    archive: Annotated[str, typer.Argument(help="A backup file, or s3://bucket/prefix/<file>.")],
    passphrase_file: PassFile = None,
    as_json: JsonOpt = False,
) -> None:
    """Decrypt and read a whole backup: every checksum, the manifest, and each database's integrity."""
    passphrase = read_passphrase(passphrase_file, confirm=False)
    with _errors(), _local_archive(archive, _home()) as path:
        contents = backup.verify_backup(path, passphrase)
    summary = contents.summary()
    if as_json:
        _emit({"ok": True, **summary})
        return
    typer.echo(f"OK: {archive}")
    typer.echo(f"  node {summary['node']}, made {summary['created_at']} by openajar {summary['agentos_version']}")
    typer.echo(f"  {summary['files']} files ({_mib(summary['bytes'])}), checksums match the manifest")
    typer.echo(f"  lanes: {', '.join(lane['id'] for lane in summary['lanes']) or 'none'}")
    typer.echo(f"  databases intact: {', '.join(summary['sqlite_checked']) or 'none'}")


@backup_app.command("restore")
def backup_restore(
    archive: Annotated[str, typer.Argument(help="A backup file, or s3://bucket/prefix/<file>.")],
    to: Annotated[Path | None, typer.Option("--to", help="Home to restore into (default OPENAJAR_HOME).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Move a non-empty home aside and restore anyway.")] = False,
    owner: Annotated[str | None, typer.Option(help="Give the restored files to this user (root only), for "
                                                   "example openajar.")] = None,
    passphrase_file: PassFile = None,
    as_json: JsonOpt = False,
) -> None:
    """Restore a backup onto an empty home (the node comes up with the same lanes, still logged in)."""
    home = to or _home()
    passphrase = read_passphrase(passphrase_file, confirm=False)
    with _errors(), _local_archive(archive, home) as path:
        result = backup.restore_backup(path, passphrase, home, force=force, owner=owner)
    if as_json:
        _emit(result.to_dict())
        return
    typer.echo(f"Restored {archive} into {result.home}")
    typer.echo(f"  node {result.node}, backup made {result.created_at}; {result.files} files ({_mib(result.bytes)})")
    typer.echo(f"  lanes: {', '.join(result.lanes) or 'none'}")
    if result.moved_aside:
        typer.echo(f"  the previous home is kept at {result.moved_aside}")
    if result.permission_problems:
        for p in result.permission_problems:
            typer.secho(f"  permissions: {p}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  permissions: every directory 700, every file 600")
    typer.echo("Next: openajar doctor, then start agentosd (sudo systemctl start agentosd).")


@backup_app.command("list")
def backup_list(to: Target = None, as_json: JsonOpt = False) -> None:
    """Backups in a directory or an S3 prefix, oldest first."""
    home = _home()
    with _errors():
        if _is_s3(to):
            remote = _s3(str(to), home)
            rows = [{"path": remote.location(o.key), "size": o.size, "created_at": parsed[1].isoformat(),
                     "node": parsed[0]} for o in remote.list() if (parsed := backup.parse_name(o.key))]
            rows.sort(key=lambda r: r["created_at"])
        else:
            rows = [a.to_dict() for a in backup.list_archives(Path(to) if to else default_backup_dir(home))]
    if as_json:
        _emit(rows)
        return
    if not rows:
        typer.echo("No backups. Make one with `openajar backup create`.")
        return
    for r in rows:
        typer.echo(f"{r['created_at']}  {_mib(r['size']).rjust(10)}  {r['path']}")


@backup_app.command("prune")
def backup_prune(
    keep: Annotated[int, typer.Option(help="How many of this node's backups to keep.")],
    to: Target = None,
) -> None:
    """Delete all but the newest KEEP backups of this node (and stale partial files)."""
    home = _home()
    node = _node_name(home)
    with _errors():
        if _is_s3(to):
            removed = _prune_s3(_s3(str(to), home), node, keep)
        else:
            removed = [str(p) for p in backup.prune(Path(to) if to else default_backup_dir(home), keep, node)]
    typer.echo(f"Removed {len(removed)} file(s)." if removed else "Nothing to remove.")
    for r in removed:
        typer.echo(f"  {r}")


# ---- migrate --------------------------------------------------------------------------

@migrate_app.command("status")
def migrate_status(as_json: JsonOpt = False) -> None:
    """Schema version of each package's database and the format of each lane, and what is pending."""
    with _errors():
        statuses = migrate.status(_home())
        lanes = lanefmt.lane_status(_home(), browser_pids=Host().browser_pids)
    if as_json:
        _emit({"databases": [s.to_dict() for s in statuses], "lanes": [s.to_dict() for s in lanes],
               "lane_format": lanefmt.LATEST})
        return
    for s in statuses:
        state = ("missing" if not s.exists else "NEWER than this openajar" if s.newer
                 else f"{len(s.pending)} pending" if s.pending else "current")
        typer.echo(f"{s.package:<10} {s.db:<16} v{s.current} of v{s.latest}  {state}")
    for ln in lanes:
        state = (f"broken: {ln.error}" if ln.error else "NEWER than this openajar" if ln.newer
                 else "not stamped yet" if not ln.stamped
                 else f"{len(ln.pending)} pending" if ln.pending else "current")
        busy = f" (in use: {ln.in_use})" if ln.in_use else ""
        typer.echo(f"lane       {ln.lane:<27} v{ln.current} of v{ln.latest}  {state}{busy}")


@migrate_app.command("run")
def migrate_run(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would run.")] = False,
    no_backup: Annotated[bool, typer.Option("--no-backup", help="Skip the databases' pre-migration backup.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Apply pending migrations: the databases (backed up first), then every cold lane's format
    (what a step changes is backed up first; a warm lane is left for the next run)."""
    result: migrate.MigrateResult | None = None
    try:
        result = migrate.migrate(_home(), backup=not no_backup, dry_run=dry_run)
        lanes = lanefmt.migrate_lanes(_home(), dry_run=dry_run, browser_pids=Host().browser_pids)
    except (migrate.MigrationError, lanefmt.LaneFormatError) as e:
        if as_json:
            # backup_dir is always the databases' (the upgrade's rollback restores it)
            db_backup = result.backup_dir if result is not None else getattr(e, "backup_dir", None)
            lane_backup = e.backup_dir if isinstance(e, lanefmt.LaneFormatError) else None
            _emit({"ok": False, "error": str(e), "backup_dir": str(db_backup) if db_backup else None,
                   "lanes_backup_dir": str(lane_backup) if lane_backup else None})
            raise typer.Exit(1) from None
        _fail(str(e))
    if as_json:
        _emit({"ok": True, "dry_run": dry_run, **result.to_dict(), "lanes": lanes.to_dict(),
               "lanes_backup_dir": str(lanes.backup_dir) if lanes.backup_dir else None})
        return
    if dry_run:
        pending = [f"{s.package} {s.db}: {', '.join(s.pending)}" for s in result.statuses if s.pending]
        pending += [f"lane {s.lane}: {', '.join(s.pending)}" + (f" (in use now: {s.in_use})" if s.in_use else "")
                    for s in lanes.statuses if s.pending]
        typer.echo("Would apply:\n  " + "\n  ".join(pending) if pending else "Nothing to migrate.")
        return
    if not result.applied and not lanes.applied and not lanes.skipped:
        typer.echo("Nothing to migrate: every database and lane is current.")
        return
    for package, version, name in result.applied:
        typer.echo(f"applied {package} {version}: {name}")
    if result.backup_dir:
        typer.echo(f"The databases before this run are in {result.backup_dir}")
    for lane, version, name in lanes.applied:
        typer.echo(f"applied lane {lane} format {version}: {name}")
    if lanes.backup_dir:
        typer.echo(f"What the lane steps changed is backed up in {lanes.backup_dir}")
    for lane, why in lanes.skipped:
        typer.echo(f"skipped lane {lane}: {why}. Close it and run `openajar migrate run` again.")


# ---- upgrade --------------------------------------------------------------------------

def upgrade_cmd(
    source: Annotated[Path, typer.Option(help="The new openajar checkout (after git pull).", exists=True,
                                         file_okay=False)] = Path("."),
    prefix: Annotated[Path | None, typer.Option(help="Install prefix (default: this openajar's own).")] = None,
    no_service: Annotated[bool, typer.Option("--no-service", help="A dev install: no systemd.")] = False,
    user: Annotated[str | None, typer.Option(help="Run agentosd as this system user (installer --user).")] = None,
    hardened: Annotated[bool, typer.Option("--hardened", help="Install the hardened unit.")] = False,
    unit: Annotated[str, typer.Option(help="systemd unit.")] = "agentosd",
    health_timeout: Annotated[float, typer.Option(help="Seconds to wait for the new version.")] = 90.0,
    skip_apt: Annotated[bool, typer.Option("--skip-apt", help="Pass --skip-apt to the installer.")] = False,
    skip_browser: Annotated[bool, typer.Option("--skip-browser", help="Pass --skip-browser to the installer.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Drain lanes, install the new code, migrate, restart, and roll back if it is not healthy."""
    from openajar.ops import upgrade

    home = _home()
    _node_name(home)
    if prefix is None:
        venv = Path(sys.prefix)
        prefix = venv.parent if venv.name == ".venv" else venv
    plan = upgrade.UpgradePlan(source=source.resolve(), prefix=prefix.resolve(), home=home, unit=unit,
                               service=not no_service, user=user, hardened=hardened, health_timeout=health_timeout,
                               installer_flags=[f for f, on in (("--skip-apt", skip_apt), ("--skip-browser", skip_browser))
                                                if on])
    if not yes:
        from openajar.ops.restart import confirm_line, open_lanes, warn_held

        lanes = open_lanes(home)
        warn_held(lanes)
        what = f"stop {unit}, " if plan.service else ""
        typer.confirm(f"Upgrade {plan.prefix} from {plan.source}? This will {what}drain lanes, install, migrate "
                      f"and restart.{confirm_line(lanes)}", abort=True)
    log = (lambda m: typer.echo(m, err=True)) if as_json else typer.echo
    result = upgrade.run_upgrade(plan, upgrade.SystemSteps(log=log), log=log)
    if as_json:
        _emit(result.to_dict())
    elif result.ok:
        typer.echo(f"Upgraded. The previous version is kept at {plan.snapshot_dir} for a manual rollback.")
    elif result.state == "rolled_back":
        typer.secho(f"Rolled back to the previous version: {result.error}", fg=typer.colors.YELLOW)
    elif result.state == "unchanged":
        typer.secho(f"Nothing changed: {result.error}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"The node is not healthy after the upgrade and the rollback: {result.error}. "
                    f"The previous code is in {plan.snapshot_dir}; see journalctl -u {unit}.", fg=typer.colors.RED)
    raise typer.Exit(0 if result.ok else 1)


# ---- service user ---------------------------------------------------------------------

def migrate_service_user_cmd(
    user: Annotated[str, typer.Option(help="The system user agentosd runs as after the switch.")] = "agentos",
    rollback: Annotated[bool, typer.Option("--rollback", help="Undo an earlier switch: back to root.")] = False,
    prefix: Annotated[Path | None, typer.Option(help="Install prefix (default: this openajar's own).")] = None,
    unit: Annotated[str, typer.Option(help="systemd unit.")] = "agentosd",
    health_timeout: Annotated[float, typer.Option(help="Seconds to wait for agentosd after the start.")] = 90.0,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Run agentosd as its own user with the hardened unit instead of root (or --rollback)."""
    from openajar.ops import service_user

    home = _home()
    _node_name(home)
    if prefix is None:
        venv = Path(sys.prefix)
        prefix = venv.parent if venv.name == ".venv" else venv
    plan = service_user.SwitchPlan(prefix=prefix.resolve(), home=home, user=user, unit=unit,
                                   health_timeout=health_timeout)
    if not yes:
        from openajar.ops.restart import confirm_line, open_lanes, warn_held

        lanes = open_lanes(home)
        warn_held(lanes)
        what = (f"Put {unit} back on root and give {home} back to its old owner?" if rollback else
                f"Switch {unit} from root to the user {user} with the hardened unit? Warm lanes close, "
                f"{home} is given to {user}, and {unit} restarts.")
        typer.confirm(what + confirm_line(lanes), abort=True)
    log = (lambda m: typer.echo(m, err=True)) if as_json else typer.echo
    steps = service_user.SystemSteps(log=log)
    result = (service_user.run_rollback if rollback else service_user.run_switch)(plan, steps, log=log)
    if as_json:
        _emit(result.to_dict())
    elif result.ok and rollback:
        typer.echo(f"{unit} runs as root again.")
    elif result.ok:
        typer.echo(f"{unit} runs as {user} with the hardened unit. To undo: sudo openajar migrate-service-user "
                   f"--rollback (the saved state is {plan.state_path}).")
    elif result.state == "rolled_back":
        typer.secho(f"Rolled back to root: {result.error}", fg=typer.colors.YELLOW)
    elif result.state == "unchanged":
        typer.secho(f"Nothing changed: {result.error}", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"{unit} is not healthy: {result.error}. The saved state is {plan.state_path}; "
                    f"see journalctl -u {unit}.", fg=typer.colors.RED)
    raise typer.Exit(0 if result.ok else 1)


# ---- health ---------------------------------------------------------------------------

def _print_health(report: dict[str, Any]) -> None:
    typer.echo(f"health {report.get('level')} at {report.get('at')}")
    for f in report.get("findings", []):
        typer.echo(f"  {f['level'].upper():<8} {f['title']}: {f['detail']}")


@health_app.command("check")
def health_check(
    offline: Annotated[bool, typer.Option("--offline", help="Skip proxy checks.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Run the health checks now (through the daemon when it runs, so warm lanes are checked too)."""
    home = _home()
    _node_name(home)
    resp = None if offline else Host().unix_request(ops_socket(home), {"op": "check"}, timeout=120)
    if resp and resp.get("ok"):
        report = resp["status"]
    else:
        import asyncio

        from openajar.ops.health import HealthMonitor

        report = asyncio.run(HealthMonitor(load_config(home)).run_once(proxies=not offline, lanes=False))
        report["note"] = "run without the daemon: warm lanes were not checked"
    if as_json:
        _emit(report)
        return
    _print_health(report)
    if report.get("note"):
        typer.echo(f"  ({report['note']})")


@health_app.command("status")
def health_status(as_json: JsonOpt = False) -> None:
    """The daemon's last health report."""
    resp = Host().unix_request(ops_socket(_home()), {"op": "status"}, timeout=10)
    if not resp or not resp.get("ok"):
        _fail("the daemon's ops socket does not answer (is agentosd running with the health service?)")
    if as_json:
        _emit(resp["status"])
        return
    _print_health(resp["status"])


# ---- lane refresh-fingerprint ------------------------------------------------------------


# ---- lane check ---------------------------------------------------------------------------


# ---- lane health --------------------------------------------------------------------------


# ---- wiring -----------------------------------------------------------------------------
# Typer apps for the root CLI's mount table (module, attr, command):
#   doctor_app -> "doctor", backup_app -> "backup", migrate_app -> "migrate",
#   upgrade_app -> "upgrade", health_app -> "health"; and lane_app is added to the root's
#   `lane` group without a name (`lane_app.add_typer(ops_cli.lane_app)`), so its command
#   becomes `openajar lane refresh-fingerprint`. `mount()` does all of that in one call.

app.command("doctor")(doctor_cmd)
app.add_typer(backup_app, name="backup")
app.add_typer(migrate_app, name="migrate")
app.command("upgrade")(upgrade_cmd)
app.command("migrate-service-user")(migrate_service_user_cmd)
app.add_typer(health_app, name="health")
app.command("refresh-fingerprint")(refresh_fingerprint_cmd)
app.command("lane-check")(lane_check_cmd)
app.command("lane-health")(lane_health_cmd)

def logs_cmd(
    lane: Annotated[str | None, typer.Option(help="Only this lane (a glob works: 'biz-a/*').")] = None,
    grant: Annotated[str | None, typer.Option(help="Only lines of calls made with this grant id.")] = None,
    run: Annotated[str | None, typer.Option(help="Only lines of this run.")] = None,
    workspace: Annotated[str | None, typer.Option(help="Only this workspace.")] = None,
    request: Annotated[str | None, typer.Option(help="Only this request id (rq_...).")] = None,
    since: Annotated[str | None, typer.Option(help="From this long ago (10m, 2h, 1d) or an ISO time.")] = None,
    level: Annotated[str, typer.Option(help="The lowest level shown: debug, info, warn or error.")] = "info",
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing new lines.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON object per line, as stored.")] = False,
    source: Annotated[str, typer.Option(help="auto (journald when agentosd runs under systemd), journal or "
                                             "file.")] = "auto",
    unit: Annotated[str, typer.Option(help="The systemd unit to read from journald.")] = logs.UNIT,
) -> None:
    """agentosd's log, filtered on the ids every line carries (lane, grant, run, workspace, request)."""
    from openajar.core.logs import human_line

    try:
        query = logs.Query(lane=lane, grant=grant, run=run, workspace=workspace, request=request,
                           since=logs.parse_since(since) if since else None, level=logs.parse_level(level))
    except ValueError as e:
        _fail(str(e), 2)
    if source not in ("auto", "journal", "file"):
        _fail("--source is auto, journal or file", 2)
    use_journal = source == "journal" or (source == "auto" and logs.journal_available(unit))
    if use_journal:
        if shutil.which("journalctl") is None:
            _fail("journalctl is not installed here; use --source file")
        lines = logs.journal_lines(unit, query.since, follow)
    else:
        home = home_from_env()
        if not follow and not logs.log_files(home):
            _fail(f"no log in {logs.log_dir(home)} and no journal for {unit}; is agentosd running here?")
        lines = logs.file_lines(home, follow)
    try:
        for line in logs.select(lines, query):
            typer.echo(json.dumps(line, ensure_ascii=False) if as_json else human_line(line))
    except (KeyboardInterrupt, BrokenPipeError):
        return


doctor_app = typer.Typer(invoke_without_command=True, help="Check this node and say how to fix what is wrong.")
doctor_app.callback()(doctor_cmd)
upgrade_app = typer.Typer(invoke_without_command=True, help="Drain, install, migrate, restart; roll back on failure.")
upgrade_app.callback()(upgrade_cmd)
# ---- stealth -------------------------------------------------------------------------------


lane_app = typer.Typer()
lane_app.command("redteam")(redteam_cmd)
lane_app.command("refresh-fingerprint")(refresh_fingerprint_cmd)
lane_app.command("check")(lane_check_cmd)
lane_app.command("health")(lane_health_cmd)
lane_app.command("audit")(lane_audit_cmd)
lane_app.command("stealth-check")(stealth_check_cmd)
lane_app.command("keepalive")(keepalive_cmd)


def mount(root: typer.Typer, lane_group: typer.Typer | None = None) -> None:
    """Add the ops commands to the root `openajar` CLI under their documented names (`lane_group`
    is the root's `lane` Typer)."""
    root.add_typer(doctor_app, name="doctor")
    root.add_typer(backup_app, name="backup")
    root.add_typer(migrate_app, name="migrate")
    root.add_typer(upgrade_app, name="upgrade")
    root.command("migrate-service-user")(migrate_service_user_cmd)
    root.add_typer(health_app, name="health")
    root.command("logs")(logs_cmd)
    if lane_group is not None:
        lane_group.add_typer(lane_app)
