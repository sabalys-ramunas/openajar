"""`openajar proxy`: the proxies lanes and environments' browsers exit through."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from openajar.commands.base import fail, initialized_config, parse_proxy
from openajar.core.config import (
    NodeConfig,
    load_proxies,
    save_proxies,
)
from openajar.core.models import PROXY_LABEL_RE

proxy_app = typer.Typer(no_args_is_help=True, help="Proxies that lanes and environments' browsers exit through.")

LOCAL_NAME = "local"  # openajar.local.paths.NAME: the name `openajar local` gives its node (not imported: a circle)


def _is_local(cfg: NodeConfig) -> bool:
    """Is this home an openajar local one (the owner's own computer), where the environments' browsers are
    the only browsers? On a server node a proxy also serves account lanes, so it joins the pool only when asked."""
    return cfg.name == LOCAL_NAME


def _pool(home: Path) -> list[str]:
    """environments.toml browser_proxies; [] when the file does not load (the error shows where it is used)."""
    from openajar.environments.models import load_settings

    try:
        return list(load_settings(home).browser_proxies)
    except (OSError, ValueError):
        return []


def _browser_runs_on_node(cfg: NodeConfig) -> bool:
    """Do this node's environments run their browser on the node (browser = "node", which browser proxies do not
    cover)? Never with openajar local: its browser always runs on the machine, whatever the setting's default."""
    from openajar.environments.models import load_settings

    if _is_local(cfg):
        return False
    try:
        return load_settings(cfg.home).browser == "node"
    except (OSError, ValueError):
        return False


def _open_through(home: Path, label: str) -> list[str]:
    """The open environments whose browser exits through `label`, as "id (title)"."""
    from openajar.environments.store import EnvironmentStore

    if not (home / "environments.db").exists():
        return []
    try:
        envs = EnvironmentStore(home / "environments.db").open()
    except Exception:  # noqa: BLE001 - only a note; the command's own work is done
        return []
    return [f"{e.id} ({e.title})" for e in envs if e.browser_proxy == label]


def _saves_through(home: Path, label: str) -> list[str]:
    """The saves whose browser exited through `label` (a resume takes it again), as "title (save id)"."""
    from openajar.environments.saves import SaveStore

    try:
        saves = SaveStore(home).all()
    except Exception:  # noqa: BLE001 - only a note; the command's own work is done
        return []
    return [f"{m.title} ({m.id})" for m in saves if m.browser_proxy == label]


def _environments_through(home: Path, label: str) -> str:
    """The open environments and the saves that exit through `label`, as words for a note ("" when none)."""
    parts = []
    still = _open_through(home, label)
    if still:
        parts.append(f"open environments: {', '.join(still)}")
    saved = _saves_through(home, label)
    if saved:
        parts.append(f"saves: {', '.join(saved[:10])}{f' and {len(saved) - 10} more' if len(saved) > 10 else ''}")
    return "; ".join(parts)


def _set_pool(home: Path, labels: list[str]) -> None:
    from openajar.environments.models import update_settings

    try:
        update_settings(home, {"browser_proxies": labels})
    except ValueError as e:
        raise fail(f"environments.toml in {home} would not load with that change ({e}), so the environments' list "
                   "(browser_proxies) is as it was. secrets/proxies.json is changed. Fix environments.toml, then "
                   "run this again.") from None


@proxy_app.command("add")
def proxy_add(
    label: Annotated[str, typer.Argument(help="Short name lanes refer to, for example lt1.")],
    spec: Annotated[str, typer.Argument(help="host:port:user:pass (leave out :pass to be prompted).")],
    environments: Annotated[bool | None, typer.Option(
        "--environments/--no-environments",
        help="Let environments' browsers exit through it (environments.toml browser_proxies). Default: on with "
             "openajar local, off on a server node, where proxies also serve account lanes.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Point an existing label at another proxy, although the lanes, "
                                                    "devices and environments using it then reach their sites from a "
                                                    "new IP.")]
    = False,
) -> None:
    """Store a proxy in secrets/proxies.json (chmod 600). The password is never printed. Pointing
    an existing label at another host, port or user needs --yes: every lane and device using it
    gets a new IP.

    With --environments (the default with openajar local), environments opened from now on may take it for
    their browser: the browser on each environment's machine exits through one proxy of the list, and the
    credentials stay on this computer."""
    if not PROXY_LABEL_RE.fullmatch(label):
        raise typer.BadParameter("lowercase letters, digits, dot, dash or underscore", param_hint="LABEL")
    cfg = initialized_config()
    proxy = parse_proxy(label, spec)
    proxies = load_proxies(cfg)
    existed = label in proxies
    old = proxies.get(label)
    if old is not None and (old.host, old.port, old.username) != (proxy.host, proxy.port, proxy.username) and not yes:
        from openajar.core.layout import proxy_users

        users = proxy_users(cfg.home, label)
        envs = _environments_through(cfg.home, label)
        who = f" ({'; '.join([*users, envs] if envs else users)})" if users or envs else ""
        raise fail(f"proxy {label} already points at {old.host}:{old.port}. Pointing it elsewhere is a new IP for "
                   f"everything that uses it{who}: logged-in accounts may be challenged. Nothing changed. If you "
                   f"still want it: openajar proxy add {label} ... --yes")
    proxies[label] = proxy
    save_proxies(cfg, proxies)
    auth = "with credentials" if proxy.username else "without credentials"
    typer.echo(f"{'Updated' if existed else 'Added'} proxy {label}: {proxy.host}:{proxy.port} {auth}")
    moved = old is not None and (old.host, old.port, old.username) != (proxy.host, proxy.port, proxy.username)
    envs = _environments_through(cfg.home, label) if moved else ""
    if envs:
        typer.echo(f"Environments that exit through {label} get its new address: an open one when this node "
                   f"restarts, a save when it resumes (its `missing` says so). {envs}.")
    pool = _pool(cfg.home)
    want = _is_local(cfg) if environments is None else environments
    if want and label not in pool:
        _set_pool(cfg.home, [*pool, label])
    elif environments is False and label in pool:
        _set_pool(cfg.home, [p for p in pool if p != label])
    if label in _pool(cfg.home):
        where = (" Environments here run their browser on the node, which browser proxies do not cover: set "
                 'browser = "vm" in environments.toml for them to use it.' if _browser_runs_on_node(cfg) else "")
        typer.echo(f"Environments' browsers will exit through it: environments opened from now on.{where} "
                   f"Check it with `openajar proxy test {label}`.")
    else:
        typer.echo("Environments' browsers will not use it (add --environments for that).")


@proxy_app.command("list")
def proxy_list() -> None:
    """Proxies by label (credentials are never shown). "environments" marks the ones environments' browsers
    exit through."""
    cfg = initialized_config()
    proxies = load_proxies(cfg)
    if not proxies:
        typer.echo("No proxies. Add one with `openajar proxy add <label> host:port:user:pass`.")
        return
    pool = _pool(cfg.home)
    for p in sorted(proxies.values(), key=lambda p: p.label):
        auth = "with credentials" if p.username else "without credentials"
        typer.echo(f"{p.label}  {p.host}:{p.port}  {auth}" + ("  environments" if p.label in pool else ""))
    gone = [label for label in pool if label not in proxies]
    if gone:
        typer.echo(f"environments.toml names proxies that are not here: {', '.join(gone)} (environments skip them).")


@proxy_app.command("remove")
def proxy_remove(label: Annotated[str, typer.Argument(help="The proxy's label, as `openajar proxy list` shows it.")]
                 ) -> None:
    """Delete a proxy from secrets/proxies.json and from the environments' list. Refused while a lane or
    device uses it. An environment open now keeps exiting through it until it closes or this node restarts.
    After a restart its browser does not start (it never goes out without its proxy): close it and open it
    again."""
    from openajar.core.layout import proxy_users

    cfg = initialized_config()
    proxies = load_proxies(cfg)
    pool = _pool(cfg.home)
    if label not in proxies and label not in pool:
        raise fail(f"no proxy {label!r}; `openajar proxy list` shows the ones there are")
    users = proxy_users(cfg.home, label)
    if users:
        raise fail(f"proxy {label} is used by {'; '.join(users)}. Move them to another proxy first. Nothing changed.")
    if label in proxies:
        del proxies[label]
        save_proxies(cfg, proxies)
    if label in pool:
        _set_pool(cfg.home, [p for p in pool if p != label])
    typer.echo(f"Removed proxy {label}.")
    still = _open_through(cfg.home, label)
    if still:
        typer.echo(f"Open environments still exit through it until they close or this node restarts: "
                   f"{', '.join(still)}. After a restart their browser does not start. Close and open them again "
                   "to give them another proxy.")
    saved = _saves_through(cfg.home, label)
    if saved:
        typer.echo(f"Saves that exited through it take another proxy when they resume, a new address for the sites "
                   f"they were signed in to: {', '.join(saved[:10])}{f' and {len(saved) - 10} more' if len(saved) > 10 else ''}"
                   f". Add {label} again (`openajar proxy add {label} host:port:user:pass`) to keep their address.")


def _browser_language(home: Path) -> str:
    """environments.toml browser_language; "exit" when the file does not load."""
    from openajar.environments.models import load_settings

    try:
        return load_settings(home).browser_language
    except (OSError, ValueError):
        return "exit"


@proxy_app.command("test")
def proxy_test(label: Annotated[str | None, typer.Argument(help="One proxy's label (default: every proxy).")] = None,
               timeout: Annotated[float, typer.Option(help="Seconds to wait for each proxy.")] = 10.0) -> None:
    """Ask ipinfo.io through each proxy: its exit IP, country and timezone, or why it failed, and what an
    environment's browser through it claims (its timezone and languages: environments.toml
    browser_language), asked the way a launch asks. Exits 1 when any proxy fails."""
    from openajar.core.system import redact
    from openajar.environments.browserproxy import _probe, claims_words

    cfg = initialized_config()
    proxies = load_proxies(cfg)
    language = _browser_language(cfg.home)
    if label is not None and label not in proxies:
        raise fail(f"no proxy {label!r}; `openajar proxy list` shows the ones there are")
    chosen = [proxies[label]] if label is not None else sorted(proxies.values(), key=lambda p: p.label)
    if not chosen:
        typer.echo("No proxies. Add one with `openajar proxy add <label> host:port:user:pass`.")
        return
    failed = 0
    for p in chosen:
        try:
            info = _probe(p, timeout)  # the launch's own check, with its second source for a thin answer
        except OSError as e:
            failed += 1
            typer.echo(f"{p.label}  failed: {redact(str(e) or type(e).__name__, p.username, p.password)}")
            continue
        where = ", ".join(x for x in (info.get("city"), info.get("country")) if x) or "unknown place"
        typer.echo(f"{p.label}  exit {info['ip']}  {where}  {info.get('timezone') or 'no timezone'}")
        typer.echo(f"{' ' * len(p.label)}  {claims_words(info, language)}")
    if failed:
        raise typer.Exit(1)
