"""Data for disposable cloud VMs (openajar.vms): settings, profiles and the VM record."""

from __future__ import annotations

import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openajar.core.errors import OpenajarError

SETTINGS_FILE = "vms.toml"
TOKEN_FILE = "secrets/hetzner.token"
MORPH_TOKEN_FILE = "secrets/morph.token"      # the Morph Cloud API key (openajar.vms.morph), 0600

# paused: the machine is kept with its memory and bills only its storage (a provider with the "pause"
# capability; an environment's save, openajar.environments.pausing). Not live: it counts toward no limit.
VmState = Literal["creating", "booting", "ready", "deleting", "deleted", "failed", "paused"]
LIVE_STATES = ("creating", "booting", "ready", "deleting")


class VmError(OpenajarError):
    """The cloud provider refused or failed (a bad token, a server type that does not exist, the
    project's server limit, the API down)."""
    code = "vm_error"

    def __init__(self, message: str, *, provider_code: str | None = None):
        super().__init__(message)
        self.provider_code = provider_code   # the provider's own error code, e.g. resource_unavailable


class VmLimit(OpenajarError):
    """A VM limit the owner set on this node (how many at once, how long, the monthly budget)."""
    code = "vm_limit"


class VmFull(VmLimit):
    """The node already has as many VMs as max_live allows (an environment's open says it as
    environment_full)."""


Arch = Literal["x86", "arm", "any"]
CpuType = Literal["shared", "dedicated", "any"]


class Size(BaseModel):
    """What a VM needs. The node rents the cheapest server type that has at least this much,
    unless `server_type` names one."""
    model_config = ConfigDict(extra="forbid")
    server_type: str | None = None  # pinned: exactly this type, no picking
    min_cpu: int = Field(default=0, ge=0, le=512)
    min_memory_gb: float = Field(default=0, ge=0, le=4096)
    min_disk_gb: int = Field(default=0, ge=0, le=100_000)
    arch: Arch = "x86"              # x86 runs everything; arm (Ampere) is sometimes cheaper
    cpu_type: CpuType = "any"

    @field_validator("server_type")
    @classmethod
    def _type(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", v):
            raise ValueError(f"not a server type name: {v!r}")
        return v

    def words(self) -> str:
        if self.server_type:
            return f"server type {self.server_type}"
        bits = [f"{self.min_cpu}+ vCPU" if self.min_cpu else "",
                f"{self.min_memory_gb:g}+ GB RAM" if self.min_memory_gb else "",
                f"{self.min_disk_gb}+ GB disk" if self.min_disk_gb else "",
                f"{self.cpu_type} CPU" if self.cpu_type != "any" else ""]
        arch = "" if self.arch == "any" else f" {self.arch}"
        fit = ", ".join(b for b in bits if b)
        return f"the cheapest{arch} server" + (f" with {fit}" if fit else "")


class Profile(Size):
    """A size an agent asks for by name. Without `server_type`, the node picks the cheapest server
    type that meets the minimums, at the price of the day."""
    image: str = "ubuntu-24.04"     # an image name, a snapshot id, or "snapshot:<name>" (openajar vm image build)
    location: str | None = None     # None: the node-wide location
    description: str = ""


DEFAULT_PROFILES = {
    "small": Profile(description="the cheapest x86 VM: scripts, tests, small builds"),
    "build": Profile(min_cpu=8, min_memory_gb=16, description="8+ vCPU, 16+ GB: big builds and test suites"),
}


class VmSettings(BaseModel):
    """`$OPENAJAR_HOME/vms.toml`. Every limit here is enforced by the node, whatever a grant says."""
    model_config = ConfigDict(extra="forbid")
    # docker: local containers with sshd (openajar.vms.docker); morph: Morph Cloud (openajar.vms.morph)
    provider: Literal["hetzner", "docker", "morph"] = "hetzner"
    location: str = "nbg1"
    default_profile: str = "small"
    profiles: dict[str, Profile] = Field(default_factory=lambda: dict(DEFAULT_PROFILES))
    max_live: int = Field(default=3, ge=0, le=50)               # VMs alive at once on this node
    default_ttl_minutes: int = Field(default=60, ge=5)
    max_ttl_minutes: int = Field(default=24 * 60, ge=5)         # a VM never lives longer than this
    idle_minutes: int = Field(default=15, ge=1)                 # no command for this long counts as idle
    hour_margin_minutes: int = Field(default=5, ge=1, le=30)    # an idle VM goes in the last minutes of its paid hour
    monthly_budget_eur: float = Field(default=50.0, ge=0)       # 0: no budget; refuse VMs past it
    ssh_from: list[str] = Field(default_factory=list)           # CIDRs SSH is open to; empty: this node's IP
    firewall: bool = True                                       # a provider firewall: SSH from ssh_from only
    boot_timeout_s: int = Field(default=300, ge=30)
    clone_max_mb: int = Field(default=2048, ge=1)               # vm_clone refuses a bigger checkout
    clone_timeout_s: int = Field(default=600, ge=30)
    reap_interval_s: int = Field(default=60, ge=10)
    api_url: str = "https://api.hetzner.cloud/v1"
    docker_image: str = "agentos-env:latest"                    # the docker provider's base image (deploy/environments)
    # the prebuilt docker_image to download, {version} being this openajar's; "" builds it here instead
    docker_pull: str = "ghcr.io/sabalys-ramunas/agentos-env:{version}"
    docker_bin: str = "docker"
    docker_cpus: float = Field(default=2.0, gt=0, le=512)       # a docker machine's CPUs (a size asks for its own)
    docker_memory_gb: float = Field(default=4.0, ge=0.5, le=4096)  # and memory (no swap on top)
    docker_pids: int = Field(default=512, ge=64, le=1_000_000)  # and processes and threads at once
    # Morph Cloud (openajar.vms.morph). Its prices are in MCUs and USD (cloud.morph.so/web/subscribe, 2026):
    # an hour of a machine is max(vCPU, memory GB / 4, disk GB / 16) MCUs, an MCU is USD 0.05, and stored
    # snapshots and paused machines are 5 TB-hours an MCU (USD 0.0073 per GB a month). openajar turns them
    # into EUR with morph_eur_per_usd and shows them as estimates; set them to your plan and the day's rate.
    morph_api_url: str = "https://cloud.morph.so/api"
    morph_ssh_host: str = "ssh.cloud.morph.so"                  # Morph's SSH gateway
    morph_base_image: str = "morphvm-minimal"                   # what a machine starts from with no snapshot
    morph_usd_per_mcu: float = Field(default=0.05, ge=0)
    morph_storage_usd_per_gb_month: float = Field(default=0.0073, ge=0)
    morph_eur_per_usd: float = Field(default=0.86, gt=0)

    @field_validator("profiles")
    @classmethod
    def _names(cls, v: dict[str, Profile]) -> dict[str, Profile]:
        for name in v:
            if not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", name):
                raise ValueError(f"profile names are lowercase letters, digits and dashes: {name!r}")
        return v

    @model_validator(mode="after")
    def _check(self) -> VmSettings:
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile {self.default_profile!r} is not one of the profiles")
        if self.default_ttl_minutes > self.max_ttl_minutes:
            raise ValueError("default_ttl_minutes is longer than max_ttl_minutes")
        if self.provider == "docker" and "location" not in self.model_fields_set:
            self.location = "local"  # a container on this computer, not a Hetzner site
        if self.provider == "morph" and "location" not in self.model_fields_set:
            self.location = "morph"  # Morph picks the site; it has one region
        return self


def load_settings(home: Path) -> VmSettings:
    path = Path(home) / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    return VmSettings(**data)


class VmRecord(BaseModel):
    id: str                                   # vm_<hex>
    lane_id: str                              # the VM lane; for an image build, "image/<name>"
    kind: Literal["lane", "image"] = "lane"   # image: a short-lived build VM, never a lane
    node: str                                 # the node that made it (the provider label)
    provider: str = "hetzner"
    provider_id: str | None = None            # the provider's server id
    profile: str
    server_type: str
    location: str
    image: str
    state: VmState = "creating"
    host: str | None = None                   # public IPv4
    created_at: datetime
    ready_at: datetime | None = None
    expires_at: datetime
    deleted_at: datetime | None = None
    delete_reason: str | None = None
    created_by: str = "owner"                 # grant id, or "owner"
    purpose: str = ""
    price_hourly_eur: float | None = None
    fallbacks: list[tuple[str, float | None]] = Field(default_factory=list)  # (type, EUR/h) to try if server_type is sold out
    asked: tuple[str, float | None] | None = None  # (type, EUR/h) planned, when it had no room and a fallback was made
    no_room: list[tuple[str, str]] = Field(default_factory=list)  # (type, location) that had no room when it was made
    error: str | None = None
    delete_attempts: int = 0
    port: int = 22                            # SSH port at `host` (the docker provider maps one per machine)
    environment: str | None = None            # the agent environment this VM is the machine of (openajar.environments)
    kept: bool = False                        # an environment kept live (keep "live"): max_ttl_minutes does not bound it
    network: str = "full"                     # full, or limited / none: its network closed outside it (openajar.vms.netwall)
    ssh_user: str = "root"                    # who the node signs in as (Morph: the instance id, at its gateway)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def billed_hours(self, now: datetime) -> int:
        """Hours started since creation: what the provider bills, at most."""
        end = self.deleted_at or now
        secs = max(0.0, (end - self.created_at).total_seconds())
        return max(1, int(-(-secs // 3600)))

    def cost_eur(self, now: datetime) -> float | None:
        if self.price_hourly_eur is None:
            return None
        return round(self.billed_hours(now) * self.price_hourly_eur, 4)
