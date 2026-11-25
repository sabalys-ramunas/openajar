"""`openajar vm ...`: disposable cloud VMs, from the node's shell.

Agents make VMs with the vm_create tool (agentosd keeps them and deletes them). These commands set
the provider up, show what exists and what it costs, delete a VM by hand, and build the snapshot
VMs boot from. They work while agentosd runs: they read the same records and, for a delete, mark
the VM so agentosd's reaper finishes the job (closing its lane) within a minute.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from openajar.core import catalog
from openajar.core.config import home_from_env, write_private
from openajar.core.errors import OpenajarError

app = typer.Typer(no_args_is_help=True, help="Disposable VMs that agents use as lanes (Hetzner, Morph, Docker or your "
                                             "own machine).")
image_app = typer.Typer(no_args_is_help=True, help="Snapshots VMs boot from.")
app.add_typer(image_app, name="image")
repo_app = typer.Typer(no_args_is_help=True, help="Read-only git tokens for private repos on VMs (vm_clone).")
app.add_typer(repo_app, name="repo")

_state: dict = {"home": None}


@app.callback()
def main(home: Annotated[Path | None, typer.Option(help="OPENAJAR_HOME (default: $OPENAJAR_HOME)")] = None) -> None:
    """Disposable VMs that agents use as lanes (Hetzner, Morph, Docker or your own machine)."""
    _state["home"] = home


def _home() -> Path:
    return Path(_state["home"] or home_from_env())


def _manager(runtime=None):
    from openajar.vms.manager import VmManager
    return VmManager(_home(), runtime=runtime)


def _tokens():
    """The repo tokens, in this node's vault (opened the way the daemon opens it)."""
    from openajar.commands.base import cli_runtime
    from openajar.vms.repos import RepoTokens
    return RepoTokens(cli_runtime().get("vault"))


def _run(coro):
    try:
        return asyncio.run(coro)
    except OpenajarError as e:
        typer.echo(catalog.cli_error(e.code, e.message), err=True)
        raise typer.Exit(1) from None


@app.command()
def setup(token_stdin: Annotated[bool, typer.Option("--token-stdin", help="Read the token from stdin.")] = False,
          provider: Annotated[str, typer.Option("--provider", help="hetzner (default), morph, or digitalocean "
                                                "(experimental).")] = "hetzner",
          ) -> None:
    """Store a cloud API token for this node and check it: a Hetzner Cloud token (read and write), with
    --provider morph a Morph Cloud API key, or with --provider digitalocean a DigitalOcean API token
    (experimental).

    Hetzner: make the token in the Hetzner Cloud console (your project, Security, API tokens). Use a
    project that holds nothing but these VMs: the token can delete any server in its project.
    Morph: make an API key at cloud.morph.so (API keys). openajar rents instances in that account.
    DigitalOcean (experimental): make a token with write access at cloud.digitalocean.com (API, Tokens).
    openajar deletes only what it tagged, but the token reaches every Droplet of its team."""
    from openajar.vms.hetzner import HetznerProvider
    from openajar.vms.models import TOKEN_FILE, load_settings

    if provider not in ("hetzner", "morph", "digitalocean"):
        raise typer.BadParameter("the provider is hetzner, morph or digitalocean (docker needs no token: set provider = "
                                 '"docker" in vms.toml; your own machine needs none either: `openajar environments '
                                 "machine add NAME user@host`)")
    name = {"morph": "Morph API key", "digitalocean": "DigitalOcean API token"}.get(provider, "Hetzner Cloud API token")
    if token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = typer.prompt(name, hide_input=True).strip()
    if not token:
        raise typer.BadParameter("no token given")
    settings = load_settings(_home())
    if provider == "morph":
        _setup_morph(token, settings)
        return
    if provider == "digitalocean":
        _setup_digitalocean(token, settings)
        return
    _run(HetznerProvider(token, api_url=settings.api_url).check())
    path = _home() / TOKEN_FILE
    write_private(path, token + "\n")
    typer.echo(f"Saved to {path} (mode 600). The token works.")
    typer.echo("Next: `openajar vm types` to check the profiles in vms.toml, then restart agentosd so its "
               "reaper picks the provider up.")


def _setup_morph(token: str, settings) -> None:
    """Check the key, keep it, pin the SSH gateway's host key, and make morph vms.toml's provider."""
    from openajar.vms.models import MORPH_TOKEN_FILE
    from openajar.vms.morph import MorphProvider, pin_gateway

    _run(MorphProvider(token, api_url=settings.morph_api_url).check())
    path = _home() / MORPH_TOKEN_FILE
    write_private(path, token + "\n")
    typer.echo(f"Saved to {path} (mode 600). The key works.")
    fingerprints = _run(asyncio.to_thread(pin_gateway, _home(), settings.morph_ssh_host))
    typer.echo(f"Pinned the host key of {settings.morph_ssh_host}, Morph's SSH gateway (compare it with Morph's "
               "if you can):")
    for fp in fingerprints:
        typer.echo(f"  {fp}")
    use_provider(_home(), "morph")
    typer.echo('vms.toml now says provider = "morph". Prices are estimates from Morph\'s MCU rates '
               "(morph_usd_per_mcu, morph_eur_per_usd in vms.toml).")
    typer.echo("Next: `openajar vm image build dev` (a snapshot environments start from in seconds), then restart "
               "agentosd so its reaper picks the provider up.")


def _setup_digitalocean(token: str, settings) -> None:
    """Check the token, keep it, and make digitalocean vms.toml's provider. It says it is experimental."""
    from openajar.vms.digitalocean import EXPERIMENTAL, DigitalOceanProvider
    from openajar.vms.models import DIGITALOCEAN_TOKEN_FILE, load_settings

    typer.echo(EXPERIMENTAL)
    _run(DigitalOceanProvider(token, api_url=settings.digitalocean_api_url).check())
    path = _home() / DIGITALOCEAN_TOKEN_FILE
    write_private(path, token + "\n")
    typer.echo(f"Saved to {path} (mode 600). The token works.")
    use_provider(_home(), "digitalocean")
    now = load_settings(_home())
    typer.echo(f'vms.toml now says provider = "digitalocean". Machines are Droplets in {now.location} (`location` in '
               "vms.toml names another region), and prices are estimates in EUR from DigitalOcean's USD prices "
               "(digitalocean_eur_per_usd).")
    typer.echo("A snapshot costs about USD 0.06 per GB a month there: set snapshot_eur_per_gb_month = 0.052 in "
               "environments.toml so saves show what they cost.")
    typer.echo("Next: `openajar vm types` to see what the profiles rent, then restart agentosd so its reaper picks "
               "the provider up.")


def use_provider(home: Path, provider: str) -> None:
    """Set vms.toml's provider, keeping the rest of the file (checked as the service would load it)."""
    import tomllib

    import tomli_w

    from openajar.vms.models import SETTINGS_FILE, VmSettings

    path = home / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data["provider"] = provider
    VmSettings(**data)
    write_private(path, tomli_w.dumps(data))


@app.command()
def types(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Server types you can order in the node's location now, cheapest first (EUR an hour, gross),
    and the type each profile in vms.toml would rent today."""
    mgr = _manager()
    loc = mgr.settings.location

    async def go():
        offers = await mgr.offers(loc)
        picks = {}
        for name in mgr.settings.profiles:
            try:
                picks[name] = await mgr.plan("vm/types", profile=name)
            except OpenajarError as e:
                picks[name] = {"error": e.message}
        return offers, picks

    offers, picks = _run(go())
    if json_out:
        typer.echo(json.dumps({"location": loc, "types": offers, "profiles": picks}, indent=2, default=str))
        return
    typer.echo(f"{'TYPE':<10} {'CPU':>4} {'RAM':>5} {'DISK':>5}  {'ARCH':<5} {'CPU TYPE':<9} EUR/h in {loc}")
    for t in offers:
        typer.echo(f"{t['name']:<10} {t.get('cores') or 0:>4} {t.get('memory_gb') or 0:>4g}G {t.get('disk_gb') or 0:>4}G  "
                   f"{t.get('architecture') or '':<5} {t.get('cpu_type') or '':<9} {t['price']:.4f}")
    typer.echo("")
    for name, p in mgr.settings.profiles.items():
        pick = picks[name]
        got = pick.get("error") or f"rents {pick['server_type']} at EUR {pick['price_hourly_eur'] or 0:.4f}/h"
        typer.echo(f"profile {name}: {p.words()}, image {p.image}: {got}")


@app.command("list")
def list_cmd(all_: Annotated[bool, typer.Option("--all", help="Include deleted VMs.")] = False,
             json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """VMs of this node: lane, state, time left, cost so far."""
    mgr = _manager()
    vms = mgr.list(include_past=all_, limit=100)
    if json_out:
        typer.echo(json.dumps({"vms": vms, **mgr.report()}, indent=2, default=str))
        return
    if not vms:
        typer.echo("No VMs." if not all_ else "No VMs yet.")
    for v in vms:
        left = f"{v['minutes_left']} min left" if v["minutes_left"] is not None else (v["delete_reason"] or "")
        cost = f"EUR {v['cost_eur_so_far']:.3f}" if v["cost_eur_so_far"] is not None else "EUR ?"
        typer.echo(f"{v['id']}  {v['lane']:<24} {v['state']:<9} {v['server_type']:<7} {v['host'] or '-':<15} "
                   f"{cost:<11} {left}")
    r = mgr.report()
    budget = f" of EUR {r['monthly_budget_eur']:.2f}" if r["monthly_budget_eur"] else ""
    typer.echo(f"\n{r['live']} live (at most {r['max_live']}); this month about EUR {r['month_spend_eur']:.2f}{budget}")


@app.command()
def destroy(ref: Annotated[str, typer.Argument(help="A VM id (vm_...) or its lane (workspace/name).")]) -> None:
    """Delete a VM now. agentosd's reaper then closes its lane (within a minute)."""
    mgr = _manager()

    async def go():
        rec = mgr.find(ref)
        if not rec.live:
            return rec
        rec.state, rec.delete_reason = "deleting", "deleted by the owner (openajar vm destroy)"
        mgr.store.put(rec)
        if rec.provider_id:
            await mgr.provider().delete_server(rec.provider_id)
        return rec

    rec = _run(go())
    typer.echo(f"Deleted the server of {rec.id} (lane {rec.lane_id}). agentosd closes the lane on its next pass.")


@app.command()
def status() -> None:
    """Is the provider set up, what runs, what it cost this month, the reaper's last pass."""
    mgr = _manager()
    typer.echo(json.dumps(mgr.report(), indent=2, default=str))


@image_app.command("build")
def image_build(name: Annotated[str, typer.Argument(help="Image name, e.g. dev.")] = "dev",
                profile: Annotated[str | None, typer.Option(help="Profile whose server type builds it.")] = None,
                script: Annotated[Path | None, typer.Option(help="A bash script to run instead of the default "
                                                                  "(git, build tools, Python with uv, Node 22, Docker; "
                                                                  "for dev also the environments' setup and browser).",
                                                            exists=True, dir_okay=False)] = None) -> None:
    """Build a snapshot: a VM from Ubuntu, the setup script, a snapshot, the VM deleted. Then set
    `image = "snapshot:<name>"` on a profile in vms.toml, and VMs boot ready in under a minute."""
    mgr = _manager()
    text = script.read_text() if script else None
    if text is None and name == "dev":  # environments boot from it: their setup and browser come with it
        from openajar.environments.machine import dev_image_script

        text = dev_image_script()
    out = _run(mgr.build_image(name, profile=profile, script=text, on_step=lambda m: typer.echo(f"- {m}")))
    typer.echo(json.dumps(out, indent=2))


@repo_app.command("add")
def repo_add(pattern: Annotated[str, typer.Argument(help="github.com/acme/api, or github.com/acme/* for all of acme")],
             user: Annotated[str | None, typer.Option(help="git user name for the token (default: x-access-token "
                                                         "on GitHub, oauth2 on GitLab)")] = None,
             token_stdin: Annotated[bool, typer.Option("--token-stdin", help="Read the token from stdin.")] = False
             ) -> None:
    """Store a read-only git token for private repos that agents may put on VMs (vm_clone).

    On GitHub: a fine-grained token with access to only these repos and only "Contents: read".
    agentosd clones on the node with it and copies the code to the VM; the token never reaches a
    VM. Kept in the vault."""
    if token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = typer.prompt("Read-only git token", hide_input=True).strip()
    out = _run(_tokens().add(pattern, token, user=user))
    typer.echo(f"Stored a token for {out['pattern']} (git user {out['user']}) in the vault.")


@repo_app.command("list")
def repo_list() -> None:
    """Repo patterns with a stored token (never the tokens)."""
    rows = _run(_tokens().list())
    if not rows:
        typer.echo("No repo tokens. Add one with `openajar vm repo add github.com/acme/*`.")
    for r in rows:
        used = f"used {r['use_count']}x, last {r['last_used_at']}" if r["use_count"] else "never used"
        typer.echo(f"{r['pattern']:<40} user {r['user']:<16} {used}")


@repo_app.command("remove")
def repo_remove(pattern: Annotated[str, typer.Argument(help="The pattern as `openajar vm repo list` shows it.")]
                ) -> None:
    """Delete a stored repo token."""
    if not _run(_tokens().remove(pattern)):
        typer.echo(f"No token for {pattern}.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted the token for {pattern}.")
