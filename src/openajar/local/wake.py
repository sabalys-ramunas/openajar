"""The catch-up after a sleep or a restart: on a laptop, the reapers must not wait.

While a laptop sleeps, its VMs keep running and billing, and nothing on the laptop runs. Time
limits are wall-clock times, so the first reaper pass after the wake closes every environment past
its limit. But the reaper's own wait counts awake time only (asyncio's clock stops while the
computer sleeps on macOS and Linux), so that pass could come up to a minute late. CatchUp
runs it at once:

- when the service starts (a restart after a crash, a reboot, `openajar local start`);
- when the wall clock jumped ahead of the awake clock by WAKE_JUMP_S or more: the computer slept.

Each catch-up first sets the VM firewall to this computer's address now (it may have woken on
another network), then runs the environments' reaper and the VMs' reaper (after a wake, the VMs' reaper
keeps every environment's machine until a whole environments' pass that began after the wake is done:
openajar.vms.reaping), and writes what they
closed to local-catch-up.json in the home, which `openajar local status` reads. The environments'
reaper saves before it deletes (openajar.environments.saving): a keep "save" environment is saved, and
one with unsaved work gets a rescue save. One whose save failed stays (it is in `kept`, with its last
save), and the owner was told.

While it is awake and VMs run, the same loop looks at this computer's address once a minute
(FOLLOW_EVERY_S, openajar.vms.following): an address that moved without a tunnel noticing yet moves the
firewall, and the environments whose tunnels are down come back at once (openajar.environments.relink).
A dropped tunnel asks on its own, so this is the backstop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.core import fs
from openajar.local.paths import catch_up_path
from openajar.vms.following import FOLLOW_EVERY_S

log = logging.getLogger("openajar.local")

SERVICE_NAME = "local_catch_up"
TICK_S = 5.0
WAKE_JUMP_S = 30.0


class CatchUp:
    def __init__(self, runtime: Any, home: Path, *, tick_s: float = TICK_S, jump_s: float = WAKE_JUMP_S,
                 wall: Callable[[], float] = time.time, awake: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, follow_s: float = FOLLOW_EVERY_S):
        self.runtime = runtime
        self.home = Path(home)
        self.tick_s, self.jump_s, self.follow_s = tick_s, jump_s, follow_s
        self._wall, self._awake, self._sleep = wall, awake, sleep
        self._task: asyncio.Task | None = None
        self.last: dict | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="local-catch-up")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        await self.run("started")
        wall, awake = self._wall(), self._awake()
        looked = awake
        while True:
            await self._sleep(self.tick_s)
            now_wall, now_awake = self._wall(), self._awake()
            slept = (now_wall - wall) - (now_awake - awake)
            wall, awake = now_wall, now_awake
            if slept >= self.jump_s:
                await self.run("woke", slept_s=slept)
                looked = now_awake
            elif now_awake - looked >= self.follow_s:
                looked = now_awake
                await self.follow()

    async def follow(self) -> bool:
        """The minute's look at this computer's address while VMs run: when the firewall moved, the environments
        whose tunnels are down come back now. True when it moved. Never raises."""
        vms = self.runtime.get("vms")
        if vms is None or not callable(getattr(vms, "follow_address", None)) or not vms.store.live():
            return False
        try:
            out = await vms.follow_address("the minute's check while awake")
        except Exception as e:  # noqa: BLE001 - the network is down: the next minute looks again
            log.info("could not look at this computer's address: %s", getattr(e, "message", e))
            return False
        if not (out and out.get("changed")):
            return False
        envs = self.runtime.get("environments")
        if envs is not None and callable(getattr(envs, "address_moved", None)):
            envs.address_moved()
        return True

    async def run(self, reason: str, *, slept_s: float | None = None) -> dict:
        """One catch-up: the firewall, then both reapers. Never raises: what failed is recorded."""
        record: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "reason": reason,
                                  "slept_s": round(slept_s) if slept_s is not None else None,
                                  "closed": [], "saved": [], "kept": [], "vms_deleted": [], "errors": []}
        # written at once, then again when the reapers are done: they can take minutes (the firewall, a save),
        # and until now `openajar local status` read the record of the start before and named its time
        self._write({**record, "running": True})
        vms = self.runtime.get("vms")
        if reason == "woke" and vms is not None and callable(getattr(vms, "note_wake", None)):
            vms.note_wake()  # its own loop may tick before the pass below: it waits for that pass (S1-01)
        if vms is not None and vms.store.live():
            try:
                await vms.refresh_firewall()  # a move here: the reaper's pass below brings the tunnels back
            except Exception as e:  # noqa: BLE001 - the reapers still run; the tunnels retry on their own
                record["errors"].append(f"firewall: {getattr(e, 'message', e)}")
        envs = self.runtime.get("environments")
        if envs is not None:
            try:
                out = await envs.reap_once()
                for env_id in [*out.get("closed", []), *out.get("gone", [])]:
                    env = envs.get(env_id)
                    record["closed"].append({"id": env.id, "title": env.title, "reason": env.closed_reason or ""})
                record["saved"] = [s["save"] for s in out.get("saved", [])]
                for env_id in out.get("save_failed", []):
                    env = envs.get(env_id)
                    record["kept"].append({"id": env.id, "title": env.title, "why": env.save_error or "",
                                           "last_save": env.last_save_id, "machine": _machine(vms, env.vm_id),
                                           "last_save_at": env.last_save_at.isoformat() if env.last_save_at else None})
            except Exception as e:  # noqa: BLE001
                record["errors"].append(f"environments: {getattr(e, 'message', e)}")
        if vms is not None and vms.configured:
            try:
                record["vms_deleted"] = list((await vms.reap_once()).get("deleted", []))
            except Exception as e:  # noqa: BLE001
                record["errors"].append(f"vms: {getattr(e, 'message', e)}")
        self.last = record
        if record["closed"] or record["vms_deleted"] or record["errors"] or reason == "woke":
            log.info("catch-up after %s: %s", "a wake" if reason == "woke" else "the start", note(record))
        self._write(record)
        return record

    def _write(self, record: dict) -> None:
        try:
            fs.write_private(catch_up_path(self.home), json.dumps(record, indent=2) + "\n")
        except OSError as e:
            log.warning("could not write %s: %s", catch_up_path(self.home), e)


def _machine(vms: Any, vm_id: str | None) -> str:
    """What became of a machine whose save failed: "kept" only while its VM record is ready."""
    try:
        rec = vms.store.get(vm_id) if vms is not None and vm_id else None
    except Exception:  # noqa: BLE001 - not known is not kept
        rec = None
    return "kept" if rec is not None and rec.state == "ready" else (rec.state if rec is not None else "not known")


def read_last(home: Path) -> dict | None:
    try:
        data = json.loads(catch_up_path(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def note(record: dict | None) -> str:
    """The catch-up in one or two plain sentences, for `openajar local status`."""
    if not record:
        return ""
    when = _when(str(record.get("at") or ""))
    if record.get("reason") == "woke":
        slept = record.get("slept_s")
        head = f"The computer woke at {when}" + (f" after {_span(slept)} asleep" if slept else "")
    else:
        head = f"The service started at {when}"
    if record.get("running"):
        return f"{head}, and it is checking the environments now (their time limits and the VMs)."
    closed = record.get("closed") or []
    if closed:
        names = ", ".join(f"{c['id']} ({c.get('title') or 'no title'}: {c.get('reason') or 'closed'})" for c in closed)
        body = f"{head}, and it closed {len(closed)} environment{'s' if len(closed) != 1 else ''} at once: {names}."
    else:
        body = f"{head}. No environment was past its time limit."
    saved = record.get("saved") or []
    if saved:
        body += f" Saved first: {', '.join(saved)}."
    for k in record.get("kept") or []:
        last = f"; its last save is {k['last_save']}" if k.get("last_save") else "; it has no earlier save"
        machine = k.get("machine", "kept")  # a record from before 0.8.0 has none: it said kept
        how = "so its machine is kept" if machine == "kept" else f"and its machine is {machine}"
        body += (f" {k['id']} ({k.get('title') or 'no title'}) could not be saved, {how} "
                 f"({k.get('why') or 'no reason given'}{last}).")
    errors = record.get("errors") or []
    if errors:
        body += f" Some of the check failed: {'; '.join(errors)}."
    return body


def build(runtime: Any) -> CatchUp:
    cfg = runtime.get("config")
    svc = CatchUp(runtime, cfg.home)
    runtime.register(SERVICE_NAME, svc)
    return svc
