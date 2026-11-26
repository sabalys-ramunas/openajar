"""What openajar needs from a cloud: create, read, list by label and delete servers, plus the
firewall, a throwaway SSH key resource and snapshots. Hetzner (hetzner.py), Docker (docker.py),
Morph Cloud (morph.py), and a fake for tests and `openajar dev up`.

Providers differ in what they can do, so each declares `capabilities` (CAPABILITIES says what each
name means) and callers ask `capable(provider, name)` before they use one. A provider without the
attribute has DEFAULT_CAPABILITIES (what every provider had before the set existed)."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Server:
    id: str
    name: str
    status: str                 # initializing, starting, running, off, deleting, ...
    ipv4: str | None
    labels: dict[str, str]
    server_type: str = ""
    created: str = ""
    ssh_port: int = 22          # the port SSH answers on at ipv4 (the docker provider maps one per machine)
    ssh_user: str = "root"      # who the node signs in as (Morph: the instance's id, at its SSH gateway)
    # A provider that hands out the login itself (Morph: the instance's own private key, and the host key
    # of its SSH gateway, pinned when the provider was set up): openajar.vms.keys.adopt writes them 0600.
    ssh_key: str | None = field(default=None, repr=False)
    host_keys: list[str] = field(default_factory=list)
    # A machine that listens on another SSH host's loopback (openajar.vms.own: a container on a person's own
    # machine): how the node reaches that host, as openajar.env.models.RemoteVia's fields. `ipv4` and
    # `ssh_port` are then an address on that host.
    via: dict | None = None


@dataclass
class ServerSpec:
    name: str
    server_type: str
    image: str
    location: str
    user_data: str
    labels: dict[str, str]
    ssh_key_id: str | None = None
    firewall_id: str | None = None
    ttl_s: int | None = None    # a backstop: the provider deletes the server itself after this (Morph's TTL)


# What a provider can do. Callers check a name before they use what it stands for.
CAPABILITIES = {
    "firewall": "a provider firewall lets SSH in from the node only (Hetzner)",
    "snapshot": "a disk snapshot of a running server that new servers boot from",
    "pause": "pause and resume a server with its memory: processes keep running (pause_server, resume_server)",
    "branch": "copy a running server into new ones, memory included (not used yet)",
    "wake_on_http": "a paused server wakes on an HTTP request (not used yet)",
    "network_policy": "openajar can limit where a machine's outbound traffic goes",
    "cloud_init": "first-boot user data runs (the host key, the setup script); without it the node runs the setup",
}
DEFAULT_CAPABILITIES = frozenset({"firewall", "snapshot", "network_policy", "cloud_init"})


def capable(provider: Any, name: str) -> bool:
    """Does the provider declare this capability (CAPABILITIES)?"""
    return name in getattr(provider, "capabilities", DEFAULT_CAPABILITIES)


def capable_at(provider: Any, name: str, location: str | None) -> bool:
    """capable(), for one place of a provider whose places differ (openajar.vms.own: a machine handed over
    whole has no disk snapshot and no wall around it, a machine that holds containers has both)."""
    at = getattr(provider, "capable_at", None)
    return bool(at(name, location)) if callable(at) else capable(provider, name)


class Provider(Protocol):
    name: str
    firewall: bool              # False: the provider's machines are not on the internet (docker), no firewall
    capabilities: frozenset[str]

    async def check(self) -> None: ...
    async def price_hourly(self, server_type: str, location: str) -> float | None: ...
    async def server_types(self) -> list[dict]: ...
    async def image_info(self, image: str) -> dict | None: ...
    async def create_server(self, spec: ServerSpec) -> Server: ...
    async def get_server(self, server_id: str) -> Server | None: ...
    async def list_servers(self, labels: dict[str, str]) -> list[Server]: ...
    async def delete_server(self, server_id: str) -> None: ...
    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str: ...
    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str: ...
    async def delete_ssh_key(self, key_id: str) -> None: ...
    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]: ...
    async def resolve_image(self, image: str) -> str: ...
    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str: ...
    async def get_image(self, image_id: str) -> dict | None: ...
    async def delete_image(self, image_id: str) -> None: ...
    async def list_images(self, labels: dict[str, str]) -> list[dict]: ...


class Pausable(Protocol):
    """What a provider with the "pause" capability has too."""

    async def pause_server(self, server_id: str) -> None: ...
    async def resume_server(self, server_id: str) -> Server: ...
    async def set_ttl(self, server_id: str, seconds: int | None, action: str = "stop") -> None: ...
    async def paused_eur_month(self, server_type: str) -> float | None: ...


FAKE_PRICES = {"cx23": 0.006, "cax11": 0.0055, "cpx22": 0.0095, "cpx42": 0.04, "ccx23": 0.05}
# name: (cores, memory GB, disk GB, architecture, cpu type), like Hetzner's 2026 line
FAKE_SPECS = {"cx23": (2, 4.0, 40, "x86", "shared"), "cax11": (2, 4.0, 40, "arm", "shared"),
              "cpx22": (2, 4.0, 80, "x86", "shared"), "cpx42": (8, 16.0, 320, "x86", "shared"),
              "ccx23": (4, 16.0, 160, "x86", "dedicated")}


def _match(labels: dict[str, str], want: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in want.items())


@dataclass
class FakeProvider:
    """An in-memory cloud. Servers are running the moment they exist; `fail_create` and
    `fail_delete` make the next calls raise, for tests. `capabilities` turns features on: with
    "pause", servers pause and resume (`paused`); without "cloud_init", user data never runs, like
    on Morph, so the node runs the setup itself."""
    name: str = "fake"
    firewall: bool = True
    capabilities: frozenset[str] = DEFAULT_CAPABILITIES
    paused: set[str] = field(default_factory=set)        # servers paused now
    ttls: dict[str, tuple[int | None, str]] = field(default_factory=dict)  # server: (TTL seconds, action)
    fail_pause: int = 0                                  # the next pauses fail
    fail_resume: int = 0                                 # the next resumes fail
    pause_eur_month: float = 0.12                        # what a paused server costs a month
    ssh_user: str = "root"
    servers: dict[str, Server] = field(default_factory=dict)
    keys: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    firewalls: dict[str, list[str]] = field(default_factory=dict)
    snapshots: dict[str, dict[str, str]] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=lambda: dict(FAKE_PRICES))
    specs: dict[str, tuple[int, float, int, str, str]] = field(default_factory=lambda: dict(FAKE_SPECS))
    unavailable: set[str] = field(default_factory=set)   # types ("cx23") or "cx23@nbg1" the "cloud" is out of now
    listed: dict[str, list[str]] | None = None           # {type: locations} it lists as orderable (None: it does not say)
    zones: dict[str, str] = field(default_factory=lambda: {"nbg1": "eu-central", "fsn1": "eu-central",
                                                           "hel1": "eu-central", "ash": "us-east"})
    image_disk_gb: int = 10                              # a snapshot's disk (a server needs at least that much)
    made_in: list[tuple[str, str]] = field(default_factory=list)  # (type, location) of each server made
    fail_create: int = 0
    fail_delete: int = 0
    user_data: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    fail_snapshot: int = 0                               # the next snapshots fail, for tests
    # the next snapshots are made, but the call fails anyway (Hetzner's API unreachable while it made one)
    lost_snapshot: int = 0
    locked_snapshot: int = 0                             # the next snapshot calls are refused: resource is locked
    image_created: dict[str, str] = field(default_factory=dict)  # snapshot id: when it was made (ISO)
    images: dict[str, dict] = field(default_factory=dict)  # snapshot id: {server, gb}
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    async def check(self) -> None:
        return None

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        return self.prices.get(server_type)

    async def server_types(self) -> list[dict]:
        out = []
        for n, p in self.prices.items():
            cores, mem, disk, arch, cpu = self.specs.get(n, (2, 4.0, 40, "x86", "shared"))
            out.append({"name": n, "cores": cores, "memory_gb": mem, "disk_gb": disk, "architecture": arch,
                         "cpu_type": cpu, "price_hourly_eur": {"nbg1": p, "fsn1": p, "hel1": p},
                         "available": None if self.listed is None else list(self.listed.get(n, []))})
        return out

    async def locations(self) -> dict[str, str]:
        return dict(self.zones)

    async def image_info(self, image: str) -> dict | None:
        if image.startswith("snapshot:"):
            await self.resolve_image(image)
            return {"architecture": "x86", "disk_gb": self.image_disk_gb}
        return None

    async def create_server(self, spec: ServerSpec) -> Server:
        from openajar.vms.models import VmError
        if self.fail_create:
            self.fail_create -= 1
            raise VmError("fake: create refused")
        if spec.server_type not in self.prices:
            raise VmError(f"fake: no server type {spec.server_type}", provider_code="invalid_input")
        if spec.server_type in self.unavailable or f"{spec.server_type}@{spec.location}" in self.unavailable:
            raise VmError(f"fake: {spec.server_type} is sold out", provider_code="resource_unavailable")
        if spec.image.startswith("snap") and self.specs.get(spec.server_type, (0, 0, 40))[2] < self.image_disk_gb:
            raise VmError("Hetzner: service_error: image disk is bigger than server type disk",
                          provider_code="service_error")
        self.made_in.append((spec.server_type, spec.location))
        sid = str(next(self._ids))
        s = Server(id=sid, name=spec.name, status="running", ipv4=f"203.0.113.{int(sid) % 250 + 1}",
                   labels=dict(spec.labels), server_type=spec.server_type, ssh_user=self.ssh_user)
        self.servers[sid] = s
        if "cloud_init" in self.capabilities:
            self.user_data[sid] = spec.user_data
        if spec.ttl_s is not None:
            self.ttls[sid] = (spec.ttl_s, "stop")
        return s

    async def pause_server(self, server_id: str) -> None:
        from openajar.vms.models import VmError
        if self.fail_pause:
            self.fail_pause -= 1
            raise VmError("fake: the pause failed", provider_code="service_error")
        s = self.servers.get(server_id)
        if s is None:
            raise VmError(f"fake: no server {server_id}")
        s.status = "paused"
        self.paused.add(server_id)

    async def resume_server(self, server_id: str) -> Server:
        from openajar.vms.models import VmError
        if self.fail_resume:
            self.fail_resume -= 1
            raise VmError("fake: the resume failed", provider_code="service_error")
        s = self.servers.get(server_id)
        if s is None:
            raise VmError(f"fake: no server {server_id}")
        s.status = "running"
        self.paused.discard(server_id)
        return s

    async def set_ttl(self, server_id: str, seconds: int | None, action: str = "stop") -> None:
        self.ttls[server_id] = (seconds, action)

    async def paused_eur_month(self, server_type: str) -> float | None:
        return self.pause_eur_month

    async def get_server(self, server_id: str) -> Server | None:
        return self.servers.get(server_id)

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        return [s for s in self.servers.values() if _match(s.labels, labels)]

    async def update_labels(self, server_id: str, labels: dict[str, str]) -> None:
        """Optional for a provider (Hetzner has it, docker not): a server's labels, replaced."""
        if server_id in self.servers:
            self.servers[server_id].labels = dict(labels)

    async def delete_server(self, server_id: str) -> None:
        if self.fail_delete:
            self.fail_delete -= 1
            from openajar.vms.models import VmError
            raise VmError("fake: delete failed")
        if self.servers.pop(server_id, None) is not None:
            self.deleted.append(server_id)

    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        self.firewalls[name] = list(ssh_from)
        return f"fw-{name}"

    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        kid = f"key-{next(self._ids)}"
        self.keys[kid] = (public_key, dict(labels))
        return kid

    async def delete_ssh_key(self, key_id: str) -> None:
        self.keys.pop(key_id, None)

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        return [(k, v[0]) for k, v in self.keys.items() if _match(v[1], labels)]

    async def resolve_image(self, image: str) -> str:
        if image.startswith("snapshot:"):
            name = image.split(":", 1)[1]
            found = [i for i, lab in self.snapshots.items() if lab.get("agentos-image") == name]
            if not found:
                from openajar.vms.models import VmError
                raise VmError(f"no snapshot named {name}; build it with `openajar vm image build {name}`")
            return found[-1]
        return image

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        from openajar.vms.models import VmError
        if self.fail_snapshot:
            self.fail_snapshot -= 1
            raise VmError("fake: the snapshot failed")
        if self.locked_snapshot:
            self.locked_snapshot -= 1
            raise VmError("Hetzner: locked: cannot perform operation because resource is locked",
                          provider_code="locked")
        sid = f"snap-{next(self._ids)}"
        self.snapshots[sid] = dict(labels)
        self.images[sid] = {"server": server_id, "gb": 1.5}
        from datetime import UTC, datetime
        self.image_created.setdefault(sid, datetime.now(UTC).isoformat())
        if self.lost_snapshot:
            self.lost_snapshot -= 1
            raise VmError("Hetzner API unreachable (ConnectError)", provider_code="unreachable")
        return sid

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        return [{"id": i, "status": "available", "created": self.image_created.get(i, ""),
                 "gb": self.images.get(i, {}).get("gb", 1.5), "labels": dict(lab)}
                for i, lab in self.snapshots.items() if _match(lab, labels)]

    async def get_image(self, image_id: str) -> dict | None:
        if image_id not in self.snapshots:
            return None
        return {"id": image_id, "status": "available", "gb": self.images.get(image_id, {}).get("gb", 1.5),
                "labels": dict(self.snapshots[image_id])}

    async def delete_image(self, image_id: str) -> None:
        self.snapshots.pop(image_id, None)
        self.images.pop(image_id, None)


def boot_error_words(e: BaseException) -> str:
    """An exception in words for a log line or a record: its type, and its message when it has one
    (a timeout or a bare error has none, and a boot failure must always say why)."""
    msg = str(getattr(e, "message", "") or e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


async def server_state(prov: Any, provider_id: str | None) -> str:
    """What the provider says of a machine that did not boot, for its error (never an empty reason)."""
    if not provider_id:
        return "the provider made no server"
    try:
        s = await asyncio.wait_for(prov.get_server(provider_id), timeout=10)
    except Exception as e:  # noqa: BLE001 - the reason is the boot's; this only adds to it
        return f"the provider's state could not be read ({boot_error_words(e)})"
    if s is None:
        return "the provider has no such server any more"
    return f"the provider says the server is {s.status or 'in no state'}" + ("" if s.ipv4 else ", with no address yet")


def by_hand_words(rec: Any) -> str:
    """Where a person deletes a machine openajar could not (a VM record), for the overdue notification."""
    container = f"agentos-{rec.id.replace('_', '-')}"
    if rec.provider == "own":
        name, _, rest = (rec.provider_id or "").partition(":")
        if rest == container:  # a machine handed over whole: its server is the machine, named as the VM is
            return (f"Free your machine {name or rec.location} if openajar cannot: `openajar environments machine "
                    f"check {name or rec.location} --free`.")
        return (f"Remove its container on your machine {name or rec.location} if openajar cannot: `docker rm -f "
                f"{container}` there.")
    if rec.provider == "docker":
        return f"Remove its container if openajar cannot: `docker rm -f {container}`."
    return f"Delete it in the {rec.provider.capitalize()} console if openajar cannot."
