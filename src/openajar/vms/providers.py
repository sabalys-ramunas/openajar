"""Which provider vms.toml names, made from its settings and its secret: Hetzner (a token), Docker
(nothing), Morph Cloud (an API key and the gateway's pinned host keys), DigitalOcean (a token;
experimental), or the person's own machines
(openajar.vms.own: no token, an SSH key and a pinned host key for each machine).

A provider says what it can do with attributes, so few callers need its name: `capabilities`
(openajar.vms.provider), `firewall` and `closes_network` (openajar.vms.netwall). FREE names the ones
that rent nothing (the machines are the person's own computers), for costs and their words."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openajar.vms.models import TOKEN_FILE, TOKEN_FILES, VmError, VmSettings


def token_path(home: Path, settings: VmSettings) -> Path:
    """Where the provider's secret is kept (docker has none: the Hetzner path, never read)."""
    return Path(home) / TOKEN_FILES.get(settings.provider, TOKEN_FILE)


def configured(home: Path, settings: VmSettings) -> bool:
    if settings.provider == "own":
        from openajar.vms.own import every

        return bool(every(home))
    return settings.provider == "docker" or token_path(home, settings).exists()


def make(home: Path, settings: VmSettings) -> Any:
    """The provider for this home's settings. VmError when its secret is not set up."""
    s = settings
    if s.provider == "docker":
        from openajar.vms.docker import DockerProvider

        return DockerProvider(image=s.docker_image, docker=s.docker_bin, cpus=s.docker_cpus,
                              memory_gb=s.docker_memory_gb, pids=s.docker_pids)
    if s.provider == "own":
        from openajar.vms.own import OwnProvider

        return OwnProvider(home, s)
    path = token_path(home, s)
    if s.provider == "morph":
        if not path.exists():
            raise VmError("VMs are not set up on this node: run `openajar vm setup --provider morph` with a Morph "
                          "API key")
        from openajar.vms.morph import MorphProvider, gateway_known_hosts

        return MorphProvider(path.read_text(), api_url=s.morph_api_url, ssh_host=s.morph_ssh_host,
                             base_image=s.morph_base_image, usd_per_mcu=s.morph_usd_per_mcu,
                             storage_usd_per_gb_month=s.morph_storage_usd_per_gb_month,
                             eur_per_usd=s.morph_eur_per_usd, host_keys=gateway_known_hosts(home))
    if s.provider == "digitalocean":
        if not path.exists():
            raise VmError("VMs are not set up on this node: run `openajar vm setup --provider digitalocean` with a "
                          "DigitalOcean API token")
        from openajar.vms.digitalocean import DigitalOceanProvider

        return DigitalOceanProvider(path.read_text(), api_url=s.digitalocean_api_url,
                                    eur_per_usd=s.digitalocean_eur_per_usd, location=s.location)
    if not path.exists():
        raise VmError("VMs are not set up on this node: run `openajar vm setup` with a Hetzner "
                      "Cloud API token (read and write)")
    from openajar.vms.hetzner import HetznerProvider

    return HetznerProvider(path.read_text(), api_url=s.api_url)


# Nothing is rented or billed there: containers on this computer, or on a person's own machine. By name,
# since a record (a VM's, a save's) holds its provider's name long after the provider object is gone.
FREE = ("docker", "own")
