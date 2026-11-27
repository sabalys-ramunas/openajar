"""The environments' reaper: one pass over every open environment, every reap_interval_s. A mixin of
EnvironmentManager (its loop, `_reap_loop`, starts with the service).

Each pass prunes what is past its retention (proofs, replays, download archives), then looks at each open
environment on its own (`_reap_one`): one whose machine was deleted is ended, one whose machine is gone
waits for the agent's next call (openajar.environments.liveness), and one past its time limit is saved
first when it keeps saves or has unsaved work, then closed (`_end_of_life`; a save that fails keeps the
machine). Before its limit a keep "save" one is autosaved, or saved and closed once idle; any other idle one
is closed when nothing is unsaved, and kept to its limit with the owner told when something is. One
environment's error is its own (S1-16, S6-01): logged, in the pass's `errors`, the owner told once, and its
machine held from the VM reaper (`holds_machine`), while the others are still checked. With no VM service
on the node (vms.toml did not read) nothing is marked deleted (S2-01). Then the lanes a closed environment
left, saves past saved_days, snapshots no save points to, and prepared and warm machines past their time.

The VM reaper (openajar.vms.reaping) keeps an environment's machine past its limit until a whole pass of
this one has finished since the node started or woke (`reap_mark`, `reaped_since`), and while
`holds_machine` says why.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any

from openajar.core import agent_names, clock
from openajar.core.errors import NotFound
from openajar.environments.models import Environment

log = logging.getLogger("openajar.environments")


class Reaping:
    """For EnvironmentManager (its store, settings, clock, locks, tunnels and tasks, and the mixins a pass
    calls)."""

    async def reap_once(self, now: datetime | None = None) -> dict:
        """One pass of the reaper (its loop runs one every reap_interval_s). Past its time limit means
        past it by the wall clock, so the first pass after a laptop slept closes it at once."""
        async with self._reap_lock:
            n = self._passes_begun = self._passes_begun + 1
            out = await self._reap(now)
            self._passes_done = n
            return out

    def reap_mark(self) -> int:
        """The reaper's passes begun so far (openajar.vms.reaping: the VM reaper waits for one begun after it)."""
        return self._passes_begun

    def reaped_since(self, mark: int) -> bool:
        """Has a whole pass that began after `mark` finished?"""
        return self._passes_done > mark

    def holds_machine(self, env_id: str) -> str | None:
        """Why this environment's machine must not be deleted now (openajar.vms.reaping), or None: a save or close
        of it runs, the reaper's last look at it failed (so nothing saved it), or its last save missed files git
        lists (it waits for a save that holds them, or for the owner's close)."""
        if (self._lock(f"save:{env_id}").locked() or self._lock(f"close:{env_id}").locked()
                or env_id in self._tasks or self.in_background(env_id) is not None):
            return "a save or close of its environment runs"
        failed = self._reap_failed.get(env_id)
        if failed:
            return f"the environments' reaper could not check it ({failed[:200]})"
        with contextlib.suppress(NotFound):
            env = self.store.get(env_id)
            if env.open and env.save_unheld:
                return (f"its last save did not hold {len(env.save_unheld)} changed file"
                        f"{'s' if len(env.save_unheld) != 1 else ''} git lists ({', '.join(env.save_unheld[:3])})")
        return None

    async def _reap(self, now: datetime | None) -> dict:
        now = now or self._clock()
        out: dict[str, Any] = {"at": clock.iso_or_none(now, z=True), "closed": [], "kept_unsaved": [], "tunnels": [], "gone": []}
        vms = self._get("vms")
        with contextlib.suppress(Exception):  # proofs past their retention (openajar.environments.proofs)
            self.prune_proofs(now)
        with contextlib.suppress(Exception):  # replays past replay_days (openajar.environments.replay)
            self.prune_replays(now)
        with contextlib.suppress(Exception):  # download archives past their link's life
            self.sweep_downloads()
        with contextlib.suppress(Exception):  # a repository connected at the node closes its cards
            from openajar.environments import connect

            await connect.sweep(self)
        if vms is None and any(e.vm_id for e in self.store.open()):  # S2-01: no answer is not a deleted machine
            log.warning("environment reaper: no VM service on this node (does vms.toml read?): no environment is checked")
        for listed in self.store.open():
            # one environment's error is that environment's alone (S1-16, S6-01): the others are still checked
            try:
                await self._reap_one(listed.id, now, out, vms)
                self._reap_failed.pop(listed.id, None)
            except Exception as e:  # noqa: BLE001
                await self._reap_failed_on(listed, e, out)
        out["swept"] = await self.sweep_lanes(now)  # lanes a closed environment left behind
        with contextlib.suppress(Exception):  # saves past saved_days (the owner is told a day before)
            out["saves"] = await self.prune_saves(now)
        with contextlib.suppress(Exception):  # snapshots of saves that did not finish
            swept = await self.sweep_snapshots(now)
            if swept.get("deleted"):
                out["snapshots_deleted"] = swept["deleted"]
        with contextlib.suppress(Exception):  # prepared machines past their retention (openajar.environments.prepbuild)
            out["prepared"] = await self.prepared_reap(now)
        with contextlib.suppress(Exception):  # warm machines past warm_minutes (openajar.environments.warmpool)
            out["warm_deleted"] = await self.pool_reap(now)
        return out

    async def _reap_one(self, env_id: str, now: datetime, out: dict, vms: Any) -> None:
        # the list was read before this pass awaited anything: an agent's close may have finished since
        # (field report 2026-11-16: a stale "closing" here undid a close that had saved and deleted the
        # machine, and kept the title held by that chat). Read each one fresh.
        env = self.store.get(env_id)
        if not env.open:
            return
        if env.id in self._tasks or self._lock(f"close:{env.id}").locked() or self.in_background(env.id):
            return  # still coming up, or an agent's close or save runs (its teardown marks it closing)
        if vms is None and env.vm_id:
            out.setdefault("held", []).append(env.id)  # S2-01: its machine is not known, so nothing is done to it
            return
        if env.state == "closing":
            await self._closing(env, now, out)
            return
        if env.state == "starting":
            self._spawn(env.id, self._bring_up(env.id, None))  # the node restarted while it came up
            return
        rec = None
        if vms is not None and env.vm_id:
            with contextlib.suppress(NotFound):
                rec = vms.store.get(env.vm_id)
        if rec is None or not rec.live:
            env.closed_reason = "its machine was deleted"
            await self._teardown(env, reason="its machine was deleted")
            out["gone"].append(env.id)
            return
        if env.gone_at is not None and await self.reap_gone(env, now, out):
            return  # its machine is gone: no tunnel, screen or save; ended once it waited (openajar.environments.liveness)
        env = await self.renew_live(self.store.get(env.id), now)
        if now >= env.expires_at:
            await self._end_of_life(env, now, out)
            return
        tunnel = self._tunnels.get(env.id)
        if (rec.state == "ready" and (tunnel is None or not tunnel.alive) and env.state == "ready"
                and await self.machine_gone(env, cause="its tunnel went down")):
            return  # gone: the agent's next call says so (openajar.environments.liveness)
        # down: the firewall follows this computer, then the tunnel, the browser lane and the screen (relink)
        if (rec.state == "ready" and (tunnel is None or not tunnel.alive)
                and await self.relink_once(env, cause="the reaper found its tunnel down")):
            out["tunnels"].append(env.id)
        if rec.state == "ready":
            await self.ssh_keep(env, now)  # local mode: the agent's own ssh, and its use counted as use
            await self.quiet_upgrade(env, now)  # a keep "live" machine's security updates (openajar.environments.updates)
        idle = (now - self._last_used(env, now) >= timedelta(minutes=self.settings.idle_minutes)
                and not await self._busy_for_idle(env))
        if rec.state == "ready":  # after the idle rule read the lane: the check's command never looks like work
            self._screen_check(env)  # a desktop's screen that died on its own starts again, in the background
        if env.keep == "live" or env.state != "ready":
            return  # a kept machine is never closed for idleness
        if env.keep == "save":
            if idle:
                await self.save_and_close(env, now, out, reason=f"idle for {self.settings.idle_minutes} minutes")
            else:
                await self.autosave(env, now, out)
            return
        near_end = env.expires_at - now <= timedelta(minutes=self.settings.unsaved_notice_minutes)
        if not idle and not near_end:
            return
        repos = await self.git_repos(env)
        unsaved = self.unsaved_of(repos) if repos is not None else {"unreachable": True}
        if unsaved is None and idle:
            await self._teardown(env, reason=f"idle for {self.settings.idle_minutes} minutes")
            out["closed"].append(env.id)
            return
        if unsaved is not None and not env.unsaved_noticed:
            def noticed(e: Environment) -> None:
                e.unsaved_noticed = True

            env = self.store.update(env.id, noticed)
            left = max(0, int((env.expires_at - now).total_seconds() // 60))
            await self._notify(f"Environment \"{env.title}\" has work that is not pushed",
                               f"{agent_names.said(env.agent, env.client)} left environment {env.id} with changes that are not committed or "
                               f"pushed. It stays open for {left} more minutes, to its time limit; then openajar "
                               "saves that work and deletes the machine. Open it to look, or extend it.")
            out["kept_unsaved"].append(env.id)

    async def _reap_failed_on(self, env: Environment, e: Exception, out: dict) -> None:
        """One environment's step raised: logged, in the pass's errors, and the owner told once per error."""
        msg = str(getattr(e, "message", e)) or type(e).__name__
        log.exception("environment reaper: environment %s (%s): %s", env.id, env.title, msg)
        out.setdefault("errors", []).append({"id": env.id, "error": msg[:300]})
        if self._reap_failed.get(env.id) == msg:
            return
        self._reap_failed[env.id] = msg
        await self._notify(f"Environment \"{env.title}\": openajar could not check it",
                           f"The reaper's look at environment {env.id} failed: {msg[:300]}. Its idle close, time limit "
                           "and saves wait until this passes; the other environments were checked. Its machine is "
                           "left as it is. openajar tries again every minute.")

    async def _end_of_life(self, env: Environment, now: datetime, out: dict) -> None:
        """Past its time limit (a laptop that woke late included): keep "save" saves first; any other
        with unsaved work gets a rescue save first. Never a silent discard: a save that fails keeps the
        machine and tells the owner (openajar.vms gives it ENV_SAVE_GRACE before its own reaper deletes it)."""
        if env.state != "ready":
            await self._teardown(env, reason="time limit")
            out["closed"].append(env.id)
            return
        if env.keep == "save":
            await self.save_and_close(env, now, out, reason="time limit")
            return
        repos = await self.git_repos(env)
        unsaved = self.unsaved_of(repos) if repos is not None else {"unreachable": True}
        if unsaved is None:
            await self._teardown(env, reason="time limit")
            out["closed"].append(env.id)
            return
        await self.rescue_and_close(env, now, out, reason="time limit")

    async def _closing(self, env: Environment, now: datetime, out: dict) -> None:
        """One the CLI marked closing: a keep "save" one is saved first (unless it was discarded); if that
        fails it goes back to ready, and the owner is told."""
        reason = env.closed_reason or "closed"
        if env.keep == "save" and "(discarded)" not in reason and env.vm_id:
            def ready(e: Environment) -> bool:
                if e.state != "closing":
                    return False  # closed (or opened again) since it was read: never undo that
                e.state, e.closed_reason = "ready", None
                return True

            env = self.store.update(env.id, ready)
            if env.state == "ready":
                await self.save_and_close(env, now, out, reason=reason)
            return
        await self._teardown(env, reason=reason)
        out["closed"].append(env.id)
