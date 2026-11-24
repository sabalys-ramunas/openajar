"""Saves that keep the machine: on a provider that pauses with memory (Morph: the "pause"
capability), a save that ends the machine pauses it instead of a disk snapshot and a delete. A mixin
of EnvironmentManager, placed before Saving: it wraps Saving's save, snapshot, drop and restore.

- When: a keep "save" environment that goes idle, is closed (with a note or not), reaches its time
  limit or wakes past it on a laptop (the reaper's catch-up), and a rescue save of any other keep at
  its time limit. `environment_save` (the machine works on) still takes a disk snapshot, and
  `pause_saves = false` in environments.toml turns pausing off.
- The save: the file part is made as always (openajar.environments.saving: the fallback if the paused
  machine is ever gone), with the browser's sign-ins. Then the gateway token's files are removed (the
  token itself is revoked when the environment closes), a browser on the machine is closed, and the
  machine pauses (openajar.vms.pausing). The save records it in place of a snapshot (saves.Paused:
  its VM, the provider's server, and what pausing costs a month), with the agent's note. Nothing is
  stopped: the dev server, the containers and every other process sleep with the machine.
- A save that fails after the pause resumes the machine in place, so a failed save keeps the machine
  working, as before.
- Coming back: environment_open with the title resumes the same machine (seconds), then the tunnels,
  a new gateway token (the old one was revoked) and the browser are made again. The services are
  checked, not started: `resumed.services` says each is running when its port listens, and
  `resumed.from` is "paused". A paused machine the provider no longer has: the files, on a fresh one.
- Forgetting and retention as for snapshots: environment_close(forget=true), environment_forget,
  the owner's Forget, keep_last and saved_days delete the paused machine with its save. A paused
  machine no save points to (a node that stopped mid save) is deleted after SNAPSHOT_GRACE.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core import clock
from openajar.core.errors import NotFound, OpenajarError
from openajar.environments.errors import RestoreFailed, SaveFailed
from openajar.environments.models import Environment
from openajar.environments.saves import NOTE_MAX, Paused, SaveCorrupt, SaveManifest, open_payload

log = logging.getLogger("openajar.environments")

# The gateway token's files on the machine (bringup.GIT_SETUP writes them; the token is revoked at close)
TOKEN_SCRUB = "rm -f ~/.config/agentos/gitconfig ~/.git-credentials; sync; echo ok=1"
STEP_TIMEOUT_S = 120.0
PORT_WAIT_S = 10.0        # a resumed machine's services listen already; this covers a slow first answer
PAUSED_SWEEP_S = 900.0


class Pausing:
    """For EnvironmentManager (Saving's save and restore, its store, settings, clock and services)."""

    # ================================================================ when
    def _pausing(self) -> dict[str, Paused | None]:
        """Environments whose save pauses their machine now: the pause once it is made."""
        return self.__dict__.setdefault("_pausing_now", {})

    def pauses_saves(self) -> bool:
        """Does a save that ends the machine pause it here (the provider can, and the owner did not turn it off)?"""
        if not self.settings.pause_saves:
            return False
        try:
            return bool(self.vms().can_pause())
        except OpenajarError:
            return False

    async def save_env(self, env: Environment, *, kind: str = "save", reason: str = "", note: str | None = None,
                       snapshot: bool | None = None, then_delete: bool = False) -> SaveManifest:
        wants = snapshot if snapshot is not None else env.keep == "save" and self.settings.snapshots
        if not (then_delete and (wants or kind == "rescue") and self.pauses_saves()):
            return await super().save_env(env, kind=kind, reason=reason, note=note,
                                          snapshot=snapshot, then_delete=then_delete)
        now = self._pausing()
        now[env.id] = None
        try:
            return await super().save_env(env, kind=kind, reason=reason, note=note,
                                          snapshot=True, then_delete=True)
        except BaseException:
            made = now.get(env.id)
            if made is not None:  # the save failed after the pause: the machine works on, as after any failed save
                await self._unpause(env, made)
            raise
        finally:
            now.pop(env.id, None)

    async def _snapshot(self, env: Environment, sid: str, found: dict, *, resume_after: bool) -> Any:
        """Saving's disk snapshot, or the pause in its place (found["paused"]: the manifest records it)."""
        now = self._pausing()
        if env.id not in now or resume_after:
            return await super()._snapshot(env, sid, found, resume_after=resume_after)
        found["paused"] = now[env.id] = await self._pause_machine(env, sid)
        return None

    # ================================================================ pause
    async def _pause_machine(self, env: Environment, sid: str) -> Paused:
        from openajar.environments.saving import snapshot_reason

        envs = self.envs()
        if env.browser == "vm":  # its browser and screen stop on the machine while it still answers
            await self._remove_browser(env)
        with contextlib.suppress(OpenajarError):
            await envs.exec(env.terminal_lane, TOKEN_SCRUB, timeout_s=STEP_TIMEOUT_S)
        try:
            got = await self.vms().pause(env.vm_id or "", why=f"paused for save {sid} of environment {env.id}",
                                         keep_days=self.settings.saved_days)
        except OpenajarError as e:
            await self._working_again(env)
            raise SaveFailed(f"pausing environment {env.id}'s machine failed: {e.message}. Its machine is kept",
                             reason=snapshot_reason(e)) from None
        log.info("environment %s: its machine %s paused for save %s", env.id, got["vm_id"], sid)
        return Paused(vm_id=got["vm_id"], provider_id=got["provider_id"], provider=got["provider"],
                      server_type=got["server_type"], location=got["location"], eur_month=got.get("eur_month"))

    async def _working_again(self, env: Environment) -> None:
        """After a pause that failed or was undone: the gateway token and a browser on the machine again."""
        with contextlib.suppress(Exception):
            await self._git_setup(env)
        if env.browser == "vm":
            with contextlib.suppress(Exception):
                await self._make_browser(env)

    async def _unpause(self, env: Environment, paused: Paused) -> None:
        try:
            await self.vms().unpause(paused.vm_id)
        except Exception as e:  # noqa: BLE001 - the owner hears of the failed save; the paused machine is listed
            log.warning("environment %s: its save failed and its paused machine %s did not resume: %s", env.id,
                        paused.vm_id, getattr(e, "message", e))
            return
        await self._working_again(env)

    # ================================================================ costs, retention, forgetting
    async def _drop(self, m: SaveManifest) -> bool:
        """Saving's drop, the paused machine first: one the provider did not delete keeps its save."""
        if m.paused is not None and not await self.vms().forget_paused(m.paused.vm_id, why=f"save {m.id} removed"):
            return False
        return await super()._drop(m)

    def snapshots_eur_month(self) -> float:
        paused = sum(m.paused.eur_month or 0 for m in self.saves().all() if m.paused is not None)
        return round(super().snapshots_eur_month() + paused, 4)

    async def sweep_snapshots(self, now: datetime | None = None, *, force: bool = False) -> dict:
        """Saving's sweep, and paused machines no save points to (a node that stopped mid save)."""
        from openajar.environments.saving import SNAPSHOT_GRACE

        out = await super().sweep_snapshots(now, force=force)
        now = now or self._clock()
        last = self.__dict__.get("_paused_swept")
        if not force and last is not None and (now - last).total_seconds() < PAUSED_SWEEP_S:
            return out
        self.__dict__["_paused_swept"] = now
        vms = self.vms()
        if not callable(getattr(vms, "paused", None)):
            return out
        known = {m.paused.vm_id for m in self.saves().all() if m.paused is not None}
        known |= {p.vm_id for p in self._pausing().values() if p is not None}
        gone = []
        for rec in vms.paused():
            if rec.id in known or now - (rec.deleted_at or rec.created_at) < SNAPSHOT_GRACE:
                continue
            if await vms.forget_paused(rec.id, why="no save points to it"):
                gone.append(rec.id)
        if gone:
            await self._notify("Leftover paused machines deleted",
                               f"openajar found {len(gone)} paused machine{'s' if len(gone) != 1 else ''} of "
                               f"environment saves that did not finish ({', '.join(gone[:5])}). No save used them, "
                               "and they billed their storage, so they were deleted. Nothing you saved was touched.")
            out = {**out, "paused_deleted": gone}
        return out

    # ================================================================ resume
    async def plan_restore(self, env_id: str, m: SaveManifest, *, hours: float | None, keep: str) -> dict:
        """Saving's plan, or the paused machine itself while the provider still has it paused."""
        if m.paused is not None:
            there = False
            with contextlib.suppress(Exception):
                there = await self.vms().paused_there(m.paused.vm_id)
            if there:
                plan = await self.plan(env_id, size=m.paused.server_type or m.server_type, hours=hours, keep=keep)
                plan["restore"] = {"save_id": m.id, "from": "paused", "paused_vm": m.paused.vm_id,
                                   "snapshot_missing": False}
                return plan
        plan = await super().plan_restore(env_id, m, hours=hours, keep=keep)
        if m.paused is not None:
            plan["restore"]["paused_missing"] = True
        return plan

    def resuming(self, m: SaveManifest, plan: dict) -> dict:
        out = super().resuming(m, plan)
        if plan["restore"].get("paused_missing"):
            out["paused_missing"] = True
        return out

    def resume_note(self, m: SaveManifest, source: str) -> str:
        if source != "paused":
            return super().resume_note(m, source)
        from openajar.environments.saving import UNTRUSTED, _when

        note = (f"Resumed from save {m.id} of {_when(m.saved_at)} (the same machine, paused with its memory: the "
                "processes that ran, the dev server among them, kept running). ") + m.summary
        if m.agent_note:
            note += f"\n\n{UNTRUSTED}\n<<<\n{m.agent_note[:NOTE_MAX]}\n>>>"
        return note

    async def _resume_paused(self, env: Environment, plan: dict) -> Any:
        """The bring-up's rent, for a paused save: the same machine, resumed (openajar.vms.pausing)."""
        return await self.vms().resume(plan["restore"]["paused_vm"], env.terminal_lane, ttl_minutes=plan["ttl_minutes"],
                                       purpose=f"environment {env.id}: {env.title}", created_by=env.grant_id,
                                       environment=env.id, kept=env.keep == "live")

    async def _restore_any(self, env: Environment) -> Environment:
        """The bring-up's restore: a paused machine's (_restore_paused), else Saving's. `missing` then
        says when a paused machine was gone at the provider, and when the browser's proxy is not the one
        the save had (openajar.environments.browserproxy)."""
        if (env.resumed or {}).get("from") == "paused":
            env = await self._restore_paused(env)
        else:
            env = await self._restore(env)
        got = env.resumed or {}
        gone = "the paused machine is gone at the provider, so this is a fresh machine with the saved files"
        said = [n for n in (gone if got.get("paused_missing") else "", got.get("browser_proxy_note")) if n]
        if not said:
            return env

        def say(e: Environment) -> None:
            r = dict(e.resumed or {})
            r.pop("paused_missing", None)
            r.pop("browser_proxy_note", None)
            r["missing"] = [*said, *(r.get("missing") or [])]
            e.resumed = r

        return self.store.update(env.id, say)

    async def _restore_paused(self, env: Environment) -> Environment:
        """The machine is the one that was paused: its disk, processes and files are there. Its clone is
        found (the branch it is on), the browser's sign-ins go back into the node's browser, and the
        services are checked."""
        from openajar.environments import comeback, signins

        try:
            m, path = self.saves().find(env.restore_from or "")
        except NotFound:
            raise RestoreFailed(f"the save {env.restore_from} is gone (forgotten meanwhile); open a new "
                                "environment with environment_open") from None
        resumed = dict(env.resumed or {})
        if env.repo:
            await self._clone(env)  # the clone is there: this finds it, and the branch it is on
        state = await self._saved_signins(m, path, env) if (m.signins or m.fingerprint) else None
        await signins.restore(self, env, state)
        services = await self._check_services(env, m)
        resumed.update({"files_restored": 0, "commits_restored": 0, "services": services, "branch": m.branch,
                        "missing": [f"the service {s['cmd']} did not survive the pause: {s['why']}"
                                    for s in services if not s["running"]],
                        "head": (m.head or "")[:12] or None,
                        "expires_at": clock.iso_or_none(self.store.get(env.id).expires_at, z=True),
                        "signins": signins.restored_words(m, state, env)})
        lost = comeback.lost_jobs(services)
        if lost:
            resumed["running_jobs_lost"] = lost
        resumed.pop("state", None)

        def back(e: Environment) -> None:
            e.resumed, e.branch, e.note = resumed, m.branch or e.branch, m.agent_note
            e.own_branches = list(dict.fromkeys([*e.own_branches, *m.own_branches]))
            e.own_heads = {**m.own_heads, **e.own_heads}

        return self.store.update(env.id, back)

    async def _saved_signins(self, m: SaveManifest, path: Path, env: Environment | None = None) -> dict | None:
        """The save's browser sign-ins; with `env`, its browser's pinned device goes back too (vmpin)."""
        from openajar.environments import signins, vmpin

        key = await self._saves_key(create=False) if m.encrypted else None
        with tempfile.TemporaryDirectory(prefix="openajar-restore-") as t:
            try:
                files = await asyncio.to_thread(open_payload, m, path, Path(t), key)
            except (SaveCorrupt, SaveFailed) as e:
                raise RestoreFailed(f"the save {m.id} does not open: {e.message}") from None
            if env is not None:
                vmpin.restore(self, env, files.get(vmpin.PAYLOAD))
            return signins.read_saved(files.get(signins.PAYLOAD)) if m.signins else None

    async def _check_services(self, env: Environment, m: SaveManifest) -> list[dict]:
        """Each saved service, checked on the resumed machine (nothing is started: it never stopped). One
        with a port runs when the port listens; one without a port was not stopped, so it runs."""
        from openajar.environments.comeback import restart_call

        want = {s.port for s in m.services if s.port}
        listening: set[int] = set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (PORT_WAIT_S if want else 0)
        while True:
            with contextlib.suppress(Exception):
                listening = {p["port"] for p in await self.ports(env) if p.get("port")}
            if want <= listening or loop.time() >= deadline:
                break
            await asyncio.sleep(1.0)
        out = []
        for s in m.services:
            up = s.port in listening if s.port else True
            row: dict = {"cmd": s.cmd, "cwd": s.cwd, "port": s.port, "running": up,
                         "listening": up if s.port else None, "kept": True}
            if not up:
                row["why"] = f"port {s.port} does not listen after the resume"
                row["restart"] = restart_call(s.cmd, s.cwd)
            out.append(row)
        return out


def paused_words(m: SaveManifest) -> str:
    """environment_close's `pause` for a paused save."""
    cost = m.paused.eur_month if m.paused is not None else None
    money = (f"about EUR {cost:.2f} a month" if cost and cost >= 0.01 else "under a cent a month") if cost else \
        "its storage only"
    return f"paused with its memory: {money} (an estimate), resumes in seconds with its processes running"
