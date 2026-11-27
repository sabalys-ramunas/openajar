"""When the VM reaper may delete a machine: positive evidence, never silence (the 0.7.0 audit, S1-01, S1-11,
S2-01, S5b-05). Functions of VmManager, kept here so its module stays within the size limit.

An environment's machine past its time limit is the environments' reaper's to save first (openajar.environments
.saving): `env_hold` says why the VM reaper still keeps one. It keeps it while the environments service is not
running (environments.toml did not read: nothing could save it), while a save or close of that environment
runs or the environments' reaper could not check it, until the environments' reaper has finished a whole pass
that began after this process started or the computer woke, and for ENV_SAVE_GRACE of awake time (time.monotonic,
which stops while the computer sleeps) from when the VM reaper first saw it expired. Never by the wall clock: a
laptop that slept through the limit and the grace has had none of it. The awake time it has had is kept on the
record (VmRecord.held_s), and so is the overdue alert, so a node that restarts every few minutes still lets it go
after the grace and tells the owner once (R1 #2).

`reconcile` compares the provider's servers with the records. A ready record whose server one listing left out
is read back by its id: only the provider's clear "no such server" marks it deleted outside openajar, and then it
is only marked (nothing is deleted on the strength of a list). A record of another provider than the one vms.toml
names now (a switch from Hetzner to Docker) is `lost`: openajar cannot reach it any
more, so it never sends its id to this provider, and the owner is told where to look.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.core.errors import NotFound
from openajar.vms.models import VmError, VmRecord, provider_name
from openajar.vms.provider import by_hand_words

log = logging.getLogger("openajar.vms")

ENV_SAVE_GRACE = timedelta(minutes=15)    # an environment's VM past its limit: the environments save it first
ORPHAN_GRACE = timedelta(minutes=10)      # an unknown server younger than this may still be ours, being made


def env_mark(vms: Any) -> int:
    """The environments' reaper's passes begun so far (0 without the service): the VM reaper keeps an
    environment's machine until a pass that began after this mark has finished."""
    envs = vms.runtime.get("environments") if vms.runtime is not None else None
    mark = getattr(envs, "reap_mark", None)
    return int(mark()) if callable(mark) else 0


def held_awake(vms: Any, rec: VmRecord) -> float:
    """The awake seconds the VM reaper has kept this machine past its limit, across restarts: what this process
    saw since its last look is added to the record (the first look in a process adds nothing)."""
    now, last = vms._awake(), vms._expired_seen.get(rec.id)
    vms._expired_seen[rec.id] = now
    if last is None or now <= last:
        return rec.held_s
    with contextlib.suppress(NotFound):
        fresh = vms.store.get(rec.id)
        if fresh.live:  # read and written with no await between: no other write of this process is lost
            fresh.held_s += now - last
            vms.store.put(fresh)
            rec.held_s = fresh.held_s
    return rec.held_s


def not_expired(vms: Any, rec: VmRecord) -> None:
    """Extended, or not past its limit yet: its grace and its overdue alert start afresh."""
    vms._expired_seen.pop(rec.id, None)
    if rec.held_s or rec.overdue_told:
        with contextlib.suppress(NotFound):
            fresh = vms.store.get(rec.id)
            if fresh.live:
                fresh.held_s, fresh.overdue_told = 0.0, False
                vms.store.put(fresh)


def env_hold(vms: Any, rec: VmRecord) -> str | None:
    """Why the VM reaper keeps this machine past its time limit, or None when it may delete it. Counts the awake
    time it has been kept (held_awake), so call it once a pass."""
    if rec.environment is None:
        return None
    held_s = held_awake(vms, rec)
    envs = vms.runtime.get("environments") if vms.runtime is not None else None
    if envs is None:
        return "the environments service is not running on this node (does environments.toml read?), so nothing could save it"
    holds = getattr(envs, "holds_machine", None)
    held = holds(rec.environment) if callable(holds) else None
    if held:
        return held
    since = getattr(envs, "reaped_since", None)
    if callable(since) and not since(vms._env_mark):
        return "the environments' reaper has not finished a pass since the node started or woke"
    if held_s < ENV_SAVE_GRACE.total_seconds():
        return "the environments' reaper saves it first"
    return None


def overdue(vms: Any, rec: VmRecord, now: datetime, alert: timedelta) -> bool:
    """Past its limit by more than `alert` (an environment's machine: by more than the grace too, in awake time
    since it was first seen expired, so a wake does not alert at once)."""
    if rec.environment is None:
        return now - rec.expires_at > alert
    if rec.state == "ready":  # held for its save: the awake time it has been kept, across restarts
        return now >= rec.expires_at and rec.held_s > (alert + ENV_SAVE_GRACE).total_seconds()
    return now - rec.expires_at > alert + ENV_SAVE_GRACE  # a delete that does not go through


async def alert_overdue(vms: Any, rec: VmRecord, now: datetime, out: dict, alert: timedelta) -> None:
    """A live machine past its limit by more than `alert` (overdue): in the pass's `overdue`, and the owner told
    once (the told flag is on the record, so a restart does not tell again)."""
    if not (rec.live and overdue(vms, rec, now, alert)):
        return
    out["overdue"].append(rec.id)
    if rec.id in vms._alerted or rec.overdue_told:
        return
    vms._alerted.add(rec.id)
    rec.overdue_told = True
    vms.store.put(rec)
    held = env_hold(vms, rec) if rec.state == "ready" else None
    await vms._notify(f"VM {rec.id} is still running past its time limit",
                      f"Lane {rec.lane_id} ({rec.server_type}, {rec.host or 'no IP'}) should have been deleted at "
                      f"{rec.expires_at:%H:%M} UTC: "
                      f"{rec.error or (f'openajar keeps it because {held}' if held else 'no error')}. "
                      + by_hand_words(rec), urgent=True)


def _parse_time(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def lost(vms: Any, rec: VmRecord, current: str) -> None:
    """A record of another provider than vms.toml's: ended as lost (never deleted through this one). Its state is
    "deleted" with `lost` set, never a state of its own: 0.8.0 and 0.7.0 cannot read a state they do not know (R1 #3)."""
    rec.state, rec.lost, rec.deleted_at = "deleted", True, vms._clock()
    was = f"your own machine {rec.location}" if rec.provider == "own" else provider_name(rec.provider)
    now_on = "your own machines" if current == "own" else provider_name(current)
    rec.delete_reason = f"lost: it was made with {was}, and this node now uses {now_on}, so openajar cannot reach it"
    vms.store.put(rec)
    vms._expired_seen.pop(rec.id, None)
    vms._publish(rec, "lost")
    log.warning("VM %s (%s server %s): %s", rec.id, was, rec.provider_id, rec.delete_reason)
    where = f"on {was}" if rec.provider == "own" else f"on a {was} server"
    await vms._notify(f"VM {rec.id} is out of openajar's reach",
                      f"Lane {rec.lane_id} ran {where} ({rec.provider_id or 'no id'}, {rec.server_type}), and "
                      f"this node now uses {now_on} (vms.toml provider). openajar did not delete it and cannot: "
                      f"{_where_words(rec, was)}", urgent=True)


async def not_ours(vms: Any, rec: VmRecord, refused: VmError) -> None:
    """A record whose server the provider reads back as not this home's (a damaged or restored record): ended as
    lost, and that server is never deleted. A machine of this home the record lost track of still carries this
    home's labels, so the orphan sweep finds it."""
    rec.state, rec.lost, rec.deleted_at = "deleted", True, vms._clock()
    rec.delete_reason = f"lost: {refused.message}"
    vms.store.put(rec)
    vms._expired_seen.pop(rec.id, None)
    vms._next_delete.pop(rec.id, None)
    vms._publish(rec, "lost")
    log.warning("VM %s (server %s): %s", rec.id, rec.provider_id, rec.delete_reason)
    await vms._notify(f"VM {rec.id} named a server that is not this openajar's",
                      f"Lane {rec.lane_id}'s record names server {rec.provider_id}, and {provider_name(rec.provider)} "
                      f"reads it back as not this openajar's ({refused.message}). openajar did not delete it, and "
                      "no longer tracks it.", urgent=True)


def _where_words(rec: VmRecord, was: str) -> str:
    """Where the owner finds a lost machine, by the provider that made it."""
    if rec.provider == "docker":
        return "look for it with `docker ps -a` and remove it there if you do not need it."
    if rec.provider == "own":
        return (f"look for it on your machine {rec.location} (`docker ps -a` there, or `openajar environments "
                f"machine check {rec.location}` for a machine handed over whole) and remove it there if you do not need it.")
    return f"look for it in {was}'s console and delete it there if you do not need it, since it may still bill."


async def _gone(vms: Any, rec: VmRecord, why: str) -> None:
    """A ready record whose server the provider says is gone: marked deleted, nothing is deleted."""
    rec.state, rec.deleted_at, rec.delete_reason = "deleted", vms._clock(), why
    vms.store.put(rec)
    vms._expired_seen.pop(rec.id, None)
    envs = vms.runtime.get("envs") if vms.runtime is not None else None
    if envs is not None and envs.has(rec.lane_id):
        lane = envs.get(rec.lane_id)
        if lane.remote is not None and lane.remote.vm_id == rec.id:
            with contextlib.suppress(Exception):
                await envs.remove(rec.lane_id, vm_ok=True)
    vms._publish(rec, "deleted")
    log.warning("VM %s for lane %s: %s", rec.id, rec.lane_id, why)


async def reconcile(vms: Any, now: datetime, out: dict) -> None:
    """Compare the provider's servers of this node with the records, both ways."""
    prov = vms.provider()
    servers = await prov.list_servers(vms._labels())
    by_id = {s.id: s for s in servers}
    live = {r.provider_id: r for r in vms.store.live() if r.provider_id}
    creating = any(r.state == "creating" for r in vms.store.live())
    for s in servers:
        if s.id in live:
            continue
        vm_id = s.labels.get("agentos-vm", "")
        with contextlib.suppress(NotFound):
            rec = vms.store.get(vm_id) if vm_id else None
            if rec is not None and (rec.live or rec.state == "paused" or rec.lost):  # a save's, or one known as lost
                continue
        created = _parse_time(s.created)
        if creating and (created is None or now - created < ORPHAN_GRACE):
            continue  # maybe the server a create is making right now
        log.warning("VM reaper: deleting server %s (%s): no live openajar record", s.id, s.name)
        try:
            await prov.delete_server(s.id)
            out["orphans"].append(s.id)
        except Exception as e:  # noqa: BLE001
            log.warning("VM reaper: could not delete orphan %s: %s", s.id, getattr(e, "message", e))
    for pid, rec in live.items():
        if rec.state != "ready" or pid in by_id:
            continue
        try:  # one listing that left it out is no evidence: the provider is asked for this server by its id
            server = await prov.get_server(pid)
        except Exception as e:  # noqa: BLE001 - no answer decides nothing
            log.info("VM reaper: the list left out %s (%s), and asking for it failed: %s", rec.id, pid,
                     getattr(e, "message", e))
            continue
        if server is not None:
            log.info("VM reaper: the provider's list left out %s (%s), but it is there (%s)", rec.id, pid, server.status)
            continue
        await _gone(vms, rec, "deleted outside openajar")
        out["gone"].append(rec.id)
    if not creating:
        for kid, _name in await prov.list_ssh_keys(vms._labels()):
            with contextlib.suppress(Exception):
                await prov.delete_ssh_key(kid)
        sweep = getattr(prov, "sweep", None)  # the docker provider: networks no machine uses any more
        if callable(sweep):
            with contextlib.suppress(Exception):
                await sweep(vms._labels())
