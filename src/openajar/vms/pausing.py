"""Paused machines, and the provider's own backstop. A mixin of VmManager.

On a provider with the "pause" capability (Morph) an environment's save keeps the machine itself,
paused with its memory, instead of a disk snapshot (openajar.environments.pausing):

- `pause`: the machine pauses (it bills only its storage) and its record turns "paused": not live,
  so it counts toward no limit and its hours stop. Its keys move aside to vms/paused/<vm id>/, so
  the closed environment's lane reaches nothing (its last kills fail at once, and a stray SSH never
  wakes it: wake on SSH is off). The reaper's orphan check leaves a paused server alone.
- `resume`: a new VM record for the same server (its hours start again), the keys back under the new
  id (the provider's fresh login on top, openajar.vms.keys.adopt), then the boot wait as for a new one.
- `forget_paused`: the server is deleted and the record closed (a save forgotten or pruned).

The backstop: a provider with `set_ttl` (Morph) deletes a server by itself once its TTL runs out, so
one this node forgot (a node that never came back) does not bill forever. A running machine's TTL is
its time limit plus the grace the environments' save gets plus BACKSTOP_MARGIN, moved along with each
extend; a paused one's is the save's life (saved_days) plus a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from datetime import timedelta

from openajar.core.errors import NotFound
from openajar.vms import keys as vmkeys
from openajar.vms.models import VmError, VmFull, VmLimit, VmRecord
from openajar.vms.provider import capable

log = logging.getLogger("openajar.vms")

BACKSTOP_MARGIN = timedelta(hours=1)
PAUSED_DIR = "paused"


class Pausing:
    """For VmManager (its store, provider, settings, clock, boots and envs)."""

    # ================================================================ the backstop
    def backstop_s(self, rec: VmRecord) -> int:
        """Seconds the provider keeps this machine by itself: past its limit, the save's grace and a margin."""
        from openajar.vms.manager import ENV_SAVE_GRACE

        end = rec.expires_at + ENV_SAVE_GRACE + BACKSTOP_MARGIN
        return max(3600, int((end - self._clock()).total_seconds()))

    async def push_backstop(self, rec: VmRecord) -> None:
        """Move the provider's TTL of a live machine to its new limit (after an extend)."""
        setter = getattr(self.provider(), "set_ttl", None)
        if not callable(setter) or not rec.provider_id:
            return
        try:
            await setter(rec.provider_id, self.backstop_s(rec), "stop")
        except Exception as e:  # noqa: BLE001 - the node's own reaper still deletes it at its limit
            log.warning("VM %s: the provider's backstop could not be moved: %s", rec.id, getattr(e, "message", e))

    # ================================================================ a machine that stopped
    async def start_machine(self, vm_id: str) -> bool:
        """A ready machine its provider says stopped (a reboot, a Docker Desktop restart, an agent's poweroff),
        started again with its disk as it was (the 0.7.0 audit, S1-04: it was called gone and deleted). Its address
        and SSH port are read again (docker publishes a new port) and its terminal lane follows. True once it runs;
        False when the provider cannot start one or the start failed. Nothing is deleted either way."""
        rec = self.store.get(vm_id)
        start = getattr(self.provider(), "start_server", None)
        if rec.state != "ready" or not rec.provider_id or not callable(start):
            return False
        try:
            server = await start(rec.provider_id)
        except Exception as e:  # noqa: BLE001 - the caller tells the owner; the next look tries again
            log.warning("VM %s: its stopped machine did not start: %s", vm_id, getattr(e, "message", e))
            return False
        if server is None:
            return False
        rec = self.store.get(vm_id)
        if (server.ipv4 or rec.host, server.ssh_port) != (rec.host, rec.port):
            rec.host, rec.port = server.ipv4 or rec.host, server.ssh_port
            self.store.put(rec)
            envs = self.runtime.get("envs") if self.runtime is not None else None
            if envs is not None and callable(getattr(envs, "rehost", None)) and envs.has(rec.lane_id):
                await envs.rehost(rec.lane_id, self._remote(rec))
        log.warning("VM %s for lane %s had stopped and was started again (%s port %s)", vm_id, rec.lane_id, rec.host,
                    rec.port)
        self._publish(rec, "started")
        return True

    # ================================================================ pause
    def can_pause(self) -> bool:
        return self.configured and capable(self.provider(), "pause")

    async def pause(self, vm_id: str, *, why: str, keep_days: int) -> dict:
        """Pause a ready machine with its memory. Returns what the save records of it: vm_id,
        provider_id, provider, server_type, location, price_hourly_eur, eur_month. VmError when the
        provider cannot or did not; the machine is then as it was."""
        rec = self.store.get(vm_id)
        prov = self.provider()
        if not capable(prov, "pause"):
            raise VmError(f"the {getattr(prov, 'name', 'cloud')} provider cannot pause a machine")
        if rec.state != "ready" or not rec.provider_id:
            raise VmError(f"VM {vm_id} is {rec.state}; only a ready machine pauses")
        await prov.pause_server(rec.provider_id)
        with contextlib.suppress(Exception):  # only the backstop: the save keeps track of it either way
            await prov.set_ttl(rec.provider_id, int(timedelta(days=keep_days + 1).total_seconds()), "stop")
        eur_month = None
        with contextlib.suppress(Exception):
            eur_month = await prov.paused_eur_month(rec.server_type)
        vmkeys.move(self.dir, rec.id, f"{PAUSED_DIR}/{rec.id}")
        rec.state, rec.deleted_at, rec.delete_reason = "paused", self._clock(), why[:300]
        self.store.put(rec)
        self._next_delete.pop(rec.id, None)
        self._publish(rec, "paused")
        log.info("VM %s for lane %s paused (%s)", rec.id, rec.lane_id, why)
        return {"vm_id": rec.id, "provider_id": rec.provider_id, "provider": rec.provider,
                "server_type": rec.server_type, "location": rec.location, "price_hourly_eur": rec.price_hourly_eur,
                "eur_month": eur_month}

    async def unpause(self, vm_id: str) -> VmRecord:
        """Undo a pause the save could not use (it failed after it): the same record runs on, with its
        keys back where its lane looks for them."""
        rec = self.store.get(vm_id)
        if rec.state != "paused" or not rec.provider_id:
            return rec
        server = await self.provider().resume_server(rec.provider_id)
        vmkeys.move(self.dir, f"{PAUSED_DIR}/{rec.id}", rec.id)
        vmkeys.adopt(self.dir, rec.id, server)
        rec.state, rec.deleted_at, rec.delete_reason = "ready", None, None
        self.store.put(rec)
        await self.push_backstop(rec)
        self._publish(rec, "resumed")
        return rec

    def paused(self) -> list[VmRecord]:
        return [r for r in self.store.recent(500) if r.state == "paused"]

    async def paused_there(self, vm_id: str) -> bool:
        """Is this paused machine still at the provider, paused (not deleted there, not woken)?"""
        try:
            rec = self.store.get(vm_id)
        except NotFound:
            return False
        if rec.state != "paused" or not rec.provider_id or not self.configured:
            return False
        server = await self.provider().get_server(rec.provider_id)
        return server is not None and server.status == "paused"

    # ================================================================ resume
    async def resume(self, vm_id: str, lane_id: str, *, ttl_minutes: int, purpose: str = "", created_by: str = "owner",
                     environment: str | None = None, kept: bool = False, wait_s: float = 0.0) -> VmRecord:
        """Resume a paused machine as a new VM record for `lane_id`, under the node's limits (machines at
        once, the month's budget), then wait for SSH as a new machine is waited for."""
        prov = self.provider()
        async with self._lock:
            old = self.store.get(vm_id)
            if old.state != "paused" or not old.provider_id:
                raise VmError(f"VM {vm_id} is {old.state}, not paused")
            self._check_room(old, ttl_minutes)
            now = self._clock()
            rec = VmRecord(id=f"vm_{secrets.token_hex(5)}", lane_id=lane_id, node=self.node, provider=old.provider,
                           provider_id=old.provider_id, profile=old.profile, server_type=old.server_type,
                           location=old.location, image=old.image, created_at=now,
                           expires_at=now + timedelta(minutes=ttl_minutes), created_by=created_by,
                           purpose=f"{purpose} (resumed {old.id})"[:300], price_hourly_eur=old.price_hourly_eur,
                           environment=environment, kept=kept, ssh_user=old.ssh_user, state="booting")
            self.store.put(rec)
        try:
            server = await prov.resume_server(old.provider_id)
        except Exception as e:
            rec.state, rec.error, rec.deleted_at = "failed", f"resume failed: {getattr(e, 'message', e)}", self._clock()
            self.store.put(rec)
            raise
        vmkeys.move(self.dir, f"{PAUSED_DIR}/{old.id}", rec.id)
        vmkeys.adopt(self.dir, rec.id, server)
        rec.host, rec.port, rec.ssh_user = server.ipv4, server.ssh_port, server.ssh_user or old.ssh_user
        self.store.put(rec)
        old.state, old.delete_reason = "deleted", f"resumed as {rec.id}"
        self.store.put(old)
        await self.push_backstop(rec)
        self._publish(rec, "resumed")
        task = asyncio.create_task(self._boot(rec.id), name=f"vm-boot-{rec.id}")
        self._boots[rec.id] = task
        task.add_done_callback(lambda _t, i=rec.id: self._boots.pop(i, None))
        if wait_s:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
        return self.store.get(rec.id)

    def _check_room(self, old: VmRecord, ttl_minutes: int) -> None:
        live = self.store.live()
        if len(live) >= self.max_live:
            raise VmFull(f"this node already has {len(live)} VMs running, and its limit is {self.max_live} at "
                         "once (vms.toml max_live); wait for one to be deleted")
        budget = self.settings.monthly_budget_eur
        if budget and old.price_hourly_eur is not None:
            worst = old.price_hourly_eur * -(-ttl_minutes // 60)
            booked = self.booked_eur()
            if booked + worst > budget:
                raise VmLimit(f"this VM could cost up to EUR {worst:.2f}; with EUR {booked:.2f} spent and booked by "
                              f"live VMs this month, that passes the monthly budget of EUR {budget:.2f} (vms.toml "
                              "monthly_budget_eur)")

    # ================================================================ forget
    async def forget_paused(self, vm_id: str, *, why: str = "its save was forgotten") -> bool:
        """Delete a paused machine at the provider and close its record. False when the provider did not
        (it keeps billing its storage; the caller keeps what points to it and tries again later)."""
        try:
            rec = self.store.get(vm_id)
        except NotFound:
            return True
        if rec.state != "paused":
            return True
        try:
            if rec.provider_id:
                await self.provider().delete_server(rec.provider_id)
        except Exception as e:  # noqa: BLE001
            log.warning("could not delete the paused machine %s: %s", vm_id, getattr(e, "message", e))
            return False
        rec.state, rec.delete_reason = "deleted", why[:300]
        self.store.put(rec)
        vmkeys.forget(self.dir / PAUSED_DIR, rec.id)
        self._publish(rec, "deleted")
        log.info("paused VM %s deleted (%s)", rec.id, why)
        return True
