"""`openajar setup`'s DigitalOcean step: the API token (checked, kept 0600) and provider = "digitalocean"
in vms.toml (openajar.vms.digitalocean). It says the provider is experimental before it asks for anything."""

from __future__ import annotations

import asyncio
from typing import Any

from openajar.core.errors import OpenajarError


def digitalocean_check(token: str, api_url: str) -> str | None:
    """None when DigitalOcean takes the token, else what is wrong, in words."""
    from openajar.vms.digitalocean import DigitalOceanProvider

    try:
        asyncio.run(DigitalOceanProvider(token, api_url=api_url).check())
    except OpenajarError as e:
        return e.message
    return None


def use_digitalocean(ctx: Any, settings: Any) -> None:
    """Say it is experimental, ask for the token (three tries), keep it, and make it the provider."""
    from openajar.core.config import write_private
    from openajar.vms.cli import use_provider
    from openajar.vms.digitalocean import EXPERIMENTAL, HOURLY
    from openajar.vms.models import DIGITALOCEAN_TOKEN_FILE

    ui, home = ctx.ui, ctx.home
    path = home / DIGITALOCEAN_TOKEN_FILE
    ui.say(EXPERIMENTAL)
    if path.exists() and digitalocean_check(path.read_text().strip(), settings.digitalocean_api_url) is None:
        ui.say("The saved DigitalOcean token works.")
    else:
        ui.say("Make a token with write access at cloud.digitalocean.com (API, Tokens). openajar deletes only what "
               "it tagged, but the token reaches every Droplet of its team.")
        token = None
        for _ in range(3):
            token = ui.secret("Paste the DigitalOcean token (hidden; Enter to skip)")
            if not token:
                break
            problem = digitalocean_check(token, settings.digitalocean_api_url)
            if problem is None:
                break
            ui.say(f"That token did not work: {problem}")
            token = None
        if not token:
            ui.say("No DigitalOcean token saved. Run `openajar setup vms` when you have one.")
            ctx.todo.append("VMs")
            return
        write_private(path, token + "\n")
        ui.say(f"The token works. It is saved in {path}, and only you can read it.")
    use_provider(home, "digitalocean")
    ui.say("Machines are Droplets in Frankfurt (fra1) unless `location` in vms.toml names another region. A save's "
           "snapshot costs about USD 0.06 per GB a month there: set snapshot_eur_per_gb_month = 0.052 in "
           "environments.toml so saves show what they cost.")
    ctx.vms = f"DigitalOcean, experimental ({HOURLY})"
