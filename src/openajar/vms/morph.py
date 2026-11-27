"""Morph Cloud (https://cloud.morph.so/docs): machines that start from a snapshot in about a second,
and pause with their memory.

Bring your own account, like Hetzner: the owner pastes a Morph API key (`openajar vm setup --provider
morph`), kept in `$OPENAJAR_HOME/secrets/morph.token` (0600), and openajar rents instances in that
account. Only the calls openajar makes, over HTTPS with httpx (the morphcloud package is not used).
This module never logs, prints or stores the key; every error is a VmError with Morph's own words,
never the request headers.

What differs from Hetzner, said plainly:

- Resources come with the snapshot an instance starts from. A start (`POST /instance`) restores the
  snapshot's memory and processes, with the snapshot's vCPU, memory and disk; a boot (`POST
  /snapshot/{id}/boot`, `POST /image/{id}/boot`) is a cold start into the resources asked for. So the
  server types are openajar's sizes (TYPES): an instance starts warm when its snapshot has that size
  (the `dev` snapshot is built at the default size), else it boots cold into it.
- SSH goes through Morph's gateway: `<instance id>@ssh.cloud.morph.so`, with the instance's own key
  (`GET /instance/{id}/ssh/key`), which openajar.vms.keys.adopt writes 0600 under vms/<vm id>/. The
  API key is never on a command line (Morph's own SDK puts `<instance id>:<api key>` in the user
  name, which `ps` on the node would show). The gateway's host key is read once at setup
  (`pin_gateway`, into vms/morph_known_hosts) and pinned for every instance, since there is no
  cloud-init to plant a host key of openajar's own.
- No cloud-init: the node runs the environment's setup over SSH (openajar.environments.bringup), and
  the `dev` snapshot has it done already.
- No provider firewall and no outbound rules: the gateway is the only way in, and Morph's API has no
  egress control (`network_policy` is not a capability).
- Labels are Morph metadata, and lists filter by them (`metadata[key]=value`).
- Prices: Morph bills MCUs in USD (VmSettings morph_*). These are estimates, shown as such; the
  node's EUR limits hold with them.
- Pause keeps memory: a paused instance bills only its storage, and resumes in seconds with its
  processes running (openajar.environments.pausing). Wake on SSH and HTTP are turned off first, so
  nothing wakes it but openajar.
- A backstop: every instance has a Morph TTL (`ttl_seconds`, action stop), so one the node forgets
  (a node that never came back) does not bill forever. The node moves it along as it extends the
  machine (openajar.vms.pausing).
"""

from __future__ import annotations

import asyncio
import logging
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from openajar.vms.models import VmError
from openajar.vms.provider import Server, ServerSpec, check_ours

log = logging.getLogger("openajar.vms")

API_URL = "https://cloud.morph.so/api"
SSH_HOST = "ssh.cloud.morph.so"
SSH_PORT = 22
BASE_IMAGE = "morphvm-minimal"
LOCATION = "morph"
KNOWN_HOSTS_FILE = "morph_known_hosts"   # in $OPENAJAR_HOME/vms: the gateway's host keys, pinned at setup
TIMEOUT_S = 60.0                         # a pause or a snapshot is a synchronous save on Morph's side
RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
SAFE_METHODS = frozenset({"GET", "DELETE"})
UNREACHABLE = "unreachable"
KEY_TRIES, KEY_WAIT_S = 5, 1.0           # the instance's key, read right after the start
# openajar's sizes as Morph machines: (vCPU, memory GB, disk GB). Disk is 16 GB a vCPU, so it never sets
# the price alone (an MCU is 1 vCPU, 4 GB of memory and 16 GB of disk, whichever is most).
TYPES = {"morph-small": (2, 4, 32), "morph-medium": (4, 8, 64), "morph-large": (8, 16, 128),
         "morph-xlarge": (16, 32, 256)}
STATUS = {"ready": "running", "pending": "starting", "paused": "paused", "saving": "saving", "error": "error"}


def mcu_per_hour(vcpus: float, memory_gb: float, disk_gb: float) -> int:
    """Morph's MCUs an hour for a machine of this size: max(vCPU, memory / 4 GB, disk / 16 GB)."""
    return max(math.ceil(vcpus), math.ceil(memory_gb / 4), math.ceil(disk_gb / 16))


class _AnyLocation(dict):
    """Prices by location for a provider with one region: the same price whatever vms.toml says."""

    def __init__(self, price: float):
        super().__init__()
        self.price = price

    def get(self, key: Any, default: Any = None) -> Any:
        return self.price

    def __bool__(self) -> bool:
        return True


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _gb(mb: Any) -> float:
    try:
        return round(float(mb) / 1024, 3)
    except (TypeError, ValueError):
        return 0.0


def _type_of(spec: dict) -> str:
    """The openajar size an instance's resources are, or "" when none is."""
    for name, (cpu, mem, disk) in TYPES.items():
        if (spec.get("vcpus"), spec.get("memory"), spec.get("disk_size")) == (cpu, mem * 1024, disk * 1024):
            return name
    return ""


def gateway_known_hosts(home: Path) -> list[str]:
    """The pinned host keys of Morph's gateway ("ssh-ed25519 AAAA..."), [] when none is pinned yet."""
    try:
        text = (Path(home) / "vms" / KNOWN_HOSTS_FILE).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            out.append(f"{parts[1]} {parts[2]}")
    return out


def pin_gateway(home: Path, host: str = SSH_HOST, *, keyscan: str = "ssh-keyscan") -> list[str]:
    """Read the gateway's host keys once (ssh-keyscan) and keep them in vms/morph_known_hosts, so every
    later connection checks them (StrictHostKeyChecking). Returns their SHA256 fingerprints, for the
    owner to compare with Morph's. Trust on first use: run it again after Morph changes its keys."""
    from openajar.core.config import write_private

    try:
        p = subprocess.run([keyscan, "-T", "10", "-t", "ed25519,ecdsa,rsa", host], capture_output=True, text=True,
                           timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VmError(f"could not read the host key of {host} ({type(e).__name__}); is ssh-keyscan installed, "
                      "and does this computer reach the internet?") from None
    lines = [ln for ln in p.stdout.splitlines() if ln and not ln.startswith("#") and len(ln.split()) >= 3]
    if not lines:
        raise VmError(f"{host} gave no SSH host key; try again in a minute")
    d = Path(home) / "vms"
    d.mkdir(parents=True, exist_ok=True)
    write_private(d / KNOWN_HOSTS_FILE, "\n".join(lines) + "\n")
    fp = subprocess.run(["ssh-keygen", "-l", "-f", str(d / KNOWN_HOSTS_FILE)], capture_output=True, text=True,
                        timeout=30, check=False)
    return [ln.strip() for ln in fp.stdout.splitlines() if ln.strip()]


class MorphProvider:
    name = "morph"
    firewall = False
    capabilities = frozenset({"snapshot", "pause", "branch", "wake_on_http"})
    # what an image build keeps: Morph's gateway signs in with the instance's own key and host key
    script_prefix = "export OPENAJAR_KEEP_SSH=1\n"
    retry_delays: tuple[float, ...] = RETRY_DELAYS_S
    # this openajar home's id (vms/home-id; VmManager.provider sets it): an instance or a snapshot is deleted by
    # its id only once Morph reads back this home's label in its metadata (openajar.vms.provider.check_ours)
    home_id: str | None = None

    def __init__(self, token: str, *, api_url: str = API_URL, ssh_host: str = SSH_HOST,
                 base_image: str = BASE_IMAGE, usd_per_mcu: float = 0.05, storage_usd_per_gb_month: float = 0.0073,
                 eur_per_usd: float = 0.86, host_keys: list[str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not token or not token.strip():
            raise VmError("no Morph API key: run `openajar vm setup --provider morph` and paste one")
        self._token = token.strip()
        self._api = api_url.rstrip("/")
        self.ssh_host = ssh_host
        self.base_image = base_image
        self.usd_per_mcu = usd_per_mcu
        self.storage_usd = storage_usd_per_gb_month
        self.eur_per_usd = eur_per_usd
        self.host_keys = list(host_keys or [])
        self._transport = transport

    def __repr__(self) -> str:  # never the key
        return f"MorphProvider({self._api})"

    # ------------------------------------------------------------------ http
    async def _call(self, method: str, path: str, *, params: Any = None, body: Any = None,
                    ok_404: bool = False) -> Any:
        """One API call, tried again with backoff while Morph is busy or out of reach."""
        delays = list(self.retry_delays)
        while True:
            try:
                return await self._call_once(method, path, params=params, body=body, ok_404=ok_404)
            except VmError as e:
                if not delays or not self._retryable(method, e):
                    raise
                wait = delays.pop(0)
                log.info("Morph %s %s: %s; trying again in %g s", method, path, e.message, wait)
                await asyncio.sleep(wait)

    @staticmethod
    def _retryable(method: str, e: VmError) -> bool:
        why = getattr(e, "retry", None)
        if why == "not_sent" or e.provider_code == "rate_limit_exceeded":
            return True
        return method in SAFE_METHODS and why in ("no_answer", "server_error")

    async def _call_once(self, method: str, path: str, *, params: Any = None, body: Any = None,
                         ok_404: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": "openajar-vms"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as c:
                r = await c.request(method, self._api + path, params=params, json=body, headers=headers)
        except httpx.HTTPError as e:
            err = VmError(f"Morph API unreachable ({type(e).__name__})", provider_code=UNREACHABLE)
            err.retry = "not_sent" if isinstance(e, NOT_SENT) else "no_answer"  # type: ignore[attr-defined]
            raise err from None
        if r.status_code == 404 and ok_404:
            return None
        if r.status_code >= 400:
            raise self._error(r)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            raise VmError(f"Morph answered {method} {path} with something that is not JSON") from None

    @staticmethod
    def _error(r: httpx.Response) -> VmError:
        try:
            data = r.json()
        except ValueError:
            data = {}
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, list):  # a validation error: [{loc, msg}]
            detail = "; ".join(str(d.get("msg", d)) for d in detail[:3] if isinstance(d, dict))
        code = (data.get("error_code") if isinstance(data, dict) else None) or f"http_{r.status_code}"
        msg = str(detail or r.reason_phrase or "error")[:300]
        if r.status_code == 401:
            msg = "the API key was refused (revoked, or not a Morph API key); run `openajar vm setup --provider morph`"
        elif r.status_code == 402:
            msg += " (the account has no credit left: add MCUs at cloud.morph.so)"
        err = VmError(f"Morph: {code}: {msg}", provider_code=code)
        if r.status_code == 429:
            err.provider_code = "rate_limit_exceeded"
        elif r.status_code == 409:
            err.provider_code = "conflict"
        elif r.status_code >= 500:
            err.retry = "server_error"  # type: ignore[attr-defined]
        return err

    # ------------------------------------------------------------------ servers
    def _server(self, d: dict) -> Server:
        status = STATUS.get(str(d.get("status") or ""), str(d.get("status") or ""))
        meta = d.get("metadata") or {}
        return Server(id=str(d.get("id", "")), name=str(meta.get("openajar-name") or d.get("id", "")), status=status,
                      ipv4=self.ssh_host if status == "running" else None, labels=dict(meta),
                      server_type=_type_of(d.get("spec") or {}), created=_iso(d.get("created")), ssh_port=SSH_PORT,
                      ssh_user=str(d.get("id", "")), host_keys=list(self.host_keys))

    async def check(self) -> None:
        await self._call("GET", "/instance", params={"metadata[agentos-check]": "none"})

    def _price(self, server_type: str) -> float:
        cpu, mem, disk = TYPES[server_type]
        return round(mcu_per_hour(cpu, mem, disk) * self.usd_per_mcu * self.eur_per_usd, 4)

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        if server_type not in TYPES:
            raise VmError(f"Morph has no server type {server_type!r} in openajar; it has {', '.join(TYPES)}")
        return self._price(server_type)

    async def paused_eur_month(self, server_type: str) -> float | None:
        """What a paused machine of this size costs a month: its memory and disk, stored (an estimate)."""
        if server_type not in TYPES:
            return None
        _cpu, mem, disk = TYPES[server_type]
        return round((mem + disk) * self.storage_usd * self.eur_per_usd, 4)

    async def server_types(self) -> list[dict]:
        return [{"name": n, "cores": cpu, "memory_gb": float(mem), "disk_gb": disk, "cpu_type": "shared",
                 "architecture": "x86", "price_hourly_eur": _AnyLocation(self._price(n)), "available": None,
                 "estimate": True} for n, (cpu, mem, disk) in TYPES.items()]

    async def locations(self) -> dict[str, str]:
        return {LOCATION: LOCATION}

    async def _snapshot(self, snapshot_id: str) -> dict | None:
        return await self._call("GET", f"/snapshot/{snapshot_id}", ok_404=True)

    async def image_info(self, image: str) -> dict | None:
        """A snapshot's disk (a machine started from it needs at least that much); None for a base image."""
        if not image.startswith(("snapshot:", "snapshot_")):
            return None
        snap = await self._snapshot(await self.resolve_image(image))
        if snap is None:
            raise VmError(f"Morph has no snapshot {image}")
        return {"architecture": "x86", "disk_gb": _gb((snap.get("spec") or {}).get("disk_size"))}

    async def resolve_image(self, image: str) -> str:
        """`snapshot:<name>`: the newest ready snapshot labelled agentos-image=<name>. A Morph snapshot or
        image id stays as it is; any other name (ubuntu-24.04, the profiles' default) is the base image."""
        if image.startswith("snapshot:"):
            name = image.split(":", 1)[1]
            found = [s for s in await self._snapshots({"agentos-image": name}) if s.get("status") == "ready"]
            if not found:
                raise VmError(f"no snapshot named {name}; build it with `openajar vm image build {name}`")
            return str(max(found, key=lambda s: int(s.get("created") or 0))["id"])
        if image.startswith(("snapshot_", "img_", "morphvm")):
            return image
        return self.base_image

    async def create_server(self, spec: ServerSpec) -> Server:
        if spec.server_type not in TYPES:
            raise VmError(f"Morph has no server type {spec.server_type!r} in openajar", provider_code="invalid_input")
        if not self.host_keys:
            raise VmError("the host key of Morph's SSH gateway is not pinned on this node; run `openajar vm setup "
                          "--provider morph` again")
        cpu, mem, disk = TYPES[spec.server_type]
        meta = {**spec.labels, "openajar-name": spec.name}
        ttl = {"ttl_seconds": spec.ttl_s, "ttl_action": "stop"} if spec.ttl_s else {}
        size = {"vcpus": cpu, "memory": mem * 1024, "disk_size": disk * 1024}
        if spec.image.startswith("snapshot_"):
            snap = await self._snapshot(spec.image)
            if snap is None:
                raise VmError(f"Morph has no snapshot {spec.image}")
            if _type_of(snap.get("spec") or {}) == spec.server_type:  # warm: its memory and processes come back
                data = await self._call("POST", "/instance", body={"snapshot_id": spec.image, "metadata": meta, **ttl})
            else:  # cold, into this size
                data = await self._call("POST", f"/snapshot/{spec.image}/boot", body={**size, "metadata": meta, **ttl})
        else:
            data = await self._call("POST", f"/image/{spec.image}/boot", body={**size, "metadata": meta, **ttl})
        server = self._server(data or {})
        if not server.id:
            raise VmError("Morph started an instance but did not say its id")
        try:
            server.ssh_key = await self._ssh_key(server.id)
        except BaseException:
            await self._quiet_delete(server.id)  # an instance openajar cannot reach must not bill
            raise
        return server

    async def _ssh_key(self, instance_id: str) -> str:
        """The instance's own private key (the gateway's login for this instance alone)."""
        last: VmError | None = None
        for _ in range(KEY_TRIES):
            try:
                data = await self._call("GET", f"/instance/{instance_id}/ssh/key") or {}
            except VmError as e:
                last = e
            else:
                key = str(data.get("private_key") or "")
                if key.strip():
                    return key if key.endswith("\n") else key + "\n"
            await asyncio.sleep(KEY_WAIT_S)
        raise VmError(f"Morph gave no SSH key for instance {instance_id}" + (f": {last.message}" if last else ""))

    async def _quiet_delete(self, instance_id: str) -> None:
        try:
            await self.delete_server(instance_id)
        except Exception as e:  # noqa: BLE001 - the reaper deletes it as an orphan later
            log.warning("Morph: could not delete instance %s after a failed start: %s", instance_id, e)

    async def get_server(self, server_id: str) -> Server | None:
        if not server_id:
            return None
        data = await self._call("GET", f"/instance/{server_id}", ok_404=True)
        return None if data is None else self._server(data)

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        data = await self._call("GET", "/instance", params={f"metadata[{k}]": v for k, v in sorted(labels.items())})
        return [self._server(d) for d in (data or {}).get("data") or []]

    async def delete_server(self, server_id: str) -> None:
        """Delete an instance of this home: read back first, a 404 is done, and one without this home's label in
        its metadata is refused (the account may run other instances; never a DELETE by a bare id)."""
        data = await self._call("GET", f"/instance/{server_id}", ok_404=True) if server_id else None
        if data is None:
            return
        check_ours(data.get("metadata"), self.home_id, f"Morph: instance {server_id}")
        await self._call("DELETE", f"/instance/{server_id}", ok_404=True)

    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        return ""  # no provider firewall: the gateway is the only way in

    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        return ""  # each instance has its own key (create_server reads it)

    async def delete_ssh_key(self, key_id: str) -> None:
        return None

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        return []

    # ------------------------------------------------------------------ pause
    async def pause_server(self, server_id: str) -> None:
        """Pause with memory. Wake on SSH and HTTP go off first, so only openajar resumes it."""
        await self._call("POST", f"/instance/{server_id}/wake-on", body={"wake_on_ssh": False, "wake_on_http": False})
        await self._call("POST", f"/instance/{server_id}/pause")

    async def resume_server(self, server_id: str) -> Server:
        data = await self._call("POST", f"/instance/{server_id}/resume")
        server = self._server(data or {}) if data else await self.get_server(server_id)
        if server is None:
            raise VmError(f"Morph has no instance {server_id} to resume")
        server.ssh_key = await self._ssh_key(server_id)
        return server

    async def set_ttl(self, server_id: str, seconds: int | None, action: str = "stop") -> None:
        await self._call("POST", f"/instance/{server_id}/ttl", body={"ttl_seconds": seconds, "ttl_action": action})

    # ------------------------------------------------------------------ snapshots
    async def _snapshots(self, labels: dict[str, str]) -> list[dict]:
        data = await self._call("GET", "/snapshot", params={f"metadata[{k}]": v for k, v in sorted(labels.items())})
        return list((data or {}).get("data") or [])

    @staticmethod
    def _image(s: dict) -> dict:
        spec = s.get("spec") or {}
        status = str(s.get("status") or "")
        return {"id": str(s.get("id")), "status": {"ready": "available", "pending": "creating"}.get(status, status),
                "created": _iso(s.get("created")),
                "gb": round(_gb(spec.get("disk_size")) + _gb(spec.get("memory")), 3) or None,  # disk and memory
                "labels": dict(s.get("metadata") or {})}

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        meta = {**labels, "openajar-description": description[:200]}
        data = await self._call("POST", f"/instance/{server_id}/snapshot", body={"metadata": meta}) or {}
        if not data.get("id"):
            raise VmError("Morph made no snapshot (it said no id)")
        return str(data["id"])

    async def get_image(self, image_id: str) -> dict | None:
        snap = await self._snapshot(image_id)
        return None if snap is None else self._image(snap)

    async def delete_image(self, image_id: str) -> None:
        """Delete a snapshot of this home: read back first, a 404 is done, one without this home's label refused."""
        snap = await self._snapshot(image_id) if image_id else None
        if snap is None:
            return
        check_ours(snap.get("metadata"), self.home_id, f"Morph: snapshot {image_id}")
        await self._call("DELETE", f"/snapshot/{image_id}", ok_404=True)

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        return [self._image(s) for s in await self._snapshots(labels)]
