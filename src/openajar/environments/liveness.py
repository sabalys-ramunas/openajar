"""A machine that stopped for good while its environment was open: the node finds out once, from the machine's
provider, and says so everywhere (evals t12, 2026-11-21: after the harness killed the container, the job said
the node "could not reach the machine over ssh to start it", environment() said working, terminal_exec came
back ok with ssh's exit 255, and environment_open with the same title handed back the dead machine).

What makes the node ask: a command whose ssh could not reach the machine (openajar.env's machine_lost hook,
which also ends a job on it), the reaper finding the machine's tunnel down, environment() finding that git on
the machine does not answer, and environment_open with a title whose machine is open. A failed ssh alone never
says a machine is gone: a network blip or a busy machine looks the same. The provider's own view decides (the
docker provider's `docker inspect`, Hetzner's API), twice, `gone_recheck_s` apart, and a machine it says runs
is not asked about again for ALIVE_AGAIN_S. A provider that does not answer decides nothing.

A machine that only stopped is not gone (the 0.7.0 audit, S1-04: a reboot or a Docker Desktop restart left
containers exited with their disks whole, and they were deleted). Docker's exited and Hetzner's off are
STOPPED_STATUSES: the node starts it again (openajar.vms start_machine; docker gives it a new SSH port, which the
lane follows) and the reaper's relink brings its tunnel back. One that does not start stays as it is, nothing is
deleted, and the owner is told once. Gone is only what the provider says is gone for good (no such server, or
GONE_STATUSES), and it is asked again before anything ends: a machine that runs again is not gone any more.

Once it is gone (Environment.gone_at, gone_why): every call that needs the machine fails with machine_gone,
whose message says when it stopped, why, and how the same title goes on (`gone_words`); environment() shows
state "gone" and `machine_gone`; environment_close ends it without a save (there is nothing to save from) and
says which save is left; environment_open with its title ends it and starts a new machine on the same title,
from the newest save when there is one, as an open by title does. Nothing here runs on the machine or touches
its lanes, so these checks are never the agent's use of it (the idle rule, as sshaccess.ssh_seen keeps it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import Any

from openajar.core import agent_names, clock
from openajar.core.errors import MachineGone, NotFound
from openajar.environments.models import Environment
from openajar.vms.models import provider_name

log = logging.getLogger("openajar.environments")

# a provider's word for a machine that is gone for good: docker's container states (dead, removing) and Hetzner's
# server states (deleting); a machine that stopped with its disk whole is STOPPED_STATUSES, started again
GONE_STATUSES = frozenset({"dead", "removing", "deleting", "deleted"})
STOPPED_STATUSES = frozenset({"exited", "created", "off", "stopping"})
RECHECK_S = 2.0         # the provider is asked again this long after it first said gone
ALIVE_AGAIN_S = 10.0    # a machine the provider said runs is not asked about again for this long
PROVIDER_S = 15.0       # one question to the provider, at most
STARTS_MAX, STARTS_WINDOW_S = 3, 1800.0  # a stopped machine started this often in this long that stops again is gone


def ran_nothing(words: str) -> str:
    """machine_gone's message for a call that did not run: the words, and that."""
    return f"Nothing ran: {words}"


def _clock_words(env: Environment) -> str:
    return env.gone_at.strftime("%H:%M UTC") if env.gone_at is not None else "a moment ago"


class Liveness:
    """The environments service's side of it (EnvironmentManager mixes it in)."""

    gone_recheck_s: float = RECHECK_S

    def _alive_seen(self) -> dict[str, float]:
        return self.__dict__.setdefault("_alive_at", {})

    async def machine_gone(self, env: Environment, *, cause: str = "") -> str | None:
        """Is `env`'s machine gone for good? Why, in words, once its provider said so twice (and the record
        says so from then on); None while it runs, while the provider does not answer, or for an environment
        that is not a ready one with a machine."""
        if env.gone_at is not None:
            return env.gone_why
        if not env.open or env.state != "ready" or not env.vm_id:
            return None
        alive = self._alive_seen()
        async with self._lock(f"gone:{env.id}"):  # type: ignore[attr-defined]
            env = self.store.get(env.id)  # type: ignore[attr-defined]
            if env.gone_at is not None:
                return env.gone_why
            seen = alive.get(env.id)
            if seen is not None and time.monotonic() - seen < ALIVE_AGAIN_S:
                return None
            why = await self._provider_says(env)
            if why is not None:  # once more, a moment later: a machine that restarts is not gone
                await asyncio.sleep(self.gone_recheck_s)
                why = await self._provider_says(env)
            if why is None:
                alive[env.id] = time.monotonic()
                return None
            return await self._mark_gone(env, why, cause)

    async def _provider_says(self, env: Environment) -> str | None:
        """Why the provider says the machine is gone, or None (it runs, it stopped and was started again, or there
        is no answer to go by)."""
        verdict, why = await self._ask(env)
        return why if verdict == "gone" else None

    async def _ask(self, env: Environment) -> tuple[str, str]:
        """("gone", why), ("runs", ""), or ("unknown", why): no answer, no record, or a stopped machine that did not
        start again. A stopped one is started here (S1-04)."""
        vms = self._get("vms")  # type: ignore[attr-defined]
        if vms is None or not env.vm_id:
            return "unknown", "this node has no VM service"
        try:
            rec = vms.store.get(env.vm_id)
        except (NotFound, ValueError):
            return "unknown", "its VM record is not there"
        if not rec.live:
            return "gone", f"the node deleted it ({rec.delete_reason or rec.state})"
        if not rec.provider_id:
            return "unknown", "it has no server yet"
        name = provider_name(rec.provider)
        what = "container" if rec.provider in ("docker", "own") else "server"
        try:
            server = await asyncio.wait_for(vms.provider().get_server(rec.provider_id), PROVIDER_S)
        except Exception as e:  # noqa: BLE001 - a provider that does not answer says nothing either way
            log.info("environment %s: %s did not answer about its machine: %s", env.id, name, e)
            return "unknown", f"{name} did not answer"
        if server is None and rec.provider == "own" and not rec.via:  # a machine handed over whole (no hop to it)
            return "gone", (f"{name} no longer holds this environment: its hold was ended outside openajar "
                            "(/var/lib/openajar-machine there), or the machine was installed again")
        if server is None:
            return "gone", f"{name} no longer has its {what}: it was deleted outside openajar, or lost with its host"
        status = str(getattr(server, "status", "") or "").lower()
        if status in GONE_STATUSES:
            return "gone", f"{name} says its {what} is {status}"
        if status in STOPPED_STATUSES:
            return await self._start_stopped(env, rec.id, f"{name} says its {what} is {status}")
        return "runs", ""

    async def _start_stopped(self, env: Environment, vm_id: str, said: str) -> tuple[str, str]:
        """A machine that stopped with its disk whole: started again. ("runs", "") once it is, else ("unknown",
        why), and the owner is told once; nothing is deleted. One that stopped again after each of STARTS_MAX
        starts in STARTS_WINDOW_S (it dies right after it starts) is not started again: ("gone", why), so the
        owner hears of it and the agent's next call says machine_gone (R1 #6)."""
        told: set[str] = self.__dict__.setdefault("_stopped_told", set())
        starts: dict[str, list[float]] = self.__dict__.setdefault("_starts", {})
        now = time.monotonic()
        recent = [t for t in starts.get(env.id, []) if now - t < STARTS_WINDOW_S]
        if len(recent) >= STARTS_MAX:
            starts[env.id] = recent
            return "gone", (f"{said} again, after each of its last {STARTS_MAX} starts in "
                            f"{int(STARTS_WINDOW_S // 60)} minutes, so openajar does not start it again")
        started = False
        with contextlib.suppress(Exception):
            started = await self._get("vms").start_machine(vm_id)  # type: ignore[attr-defined]
        if started:
            starts[env.id] = [*recent, now]
            told.discard(env.id)
            log.warning("environment %s (%s): %s, so it was started again with its disk as it was", env.id,
                        env.title, said)
            return "runs", ""
        if env.id not in told:
            told.add(env.id)
            with contextlib.suppress(Exception):
                await self._notify(f"Environment \"{env.title}\": its machine stopped",  # type: ignore[attr-defined]
                                   f"{said}, and it could not be started again. Its disk is as it was and nothing was "
                                   f"deleted: openajar tries again on each check. Start it yourself (environment "
                                   f"{env.id}, `openajar vm list` names its machine) or close it with `openajar "
                                   f"environments close {env.id}`.")
        return "unknown", f"{said}, and it could not be started again"

    def _not_gone(self, env: Environment) -> Environment:
        """The provider says the machine runs again: it is not gone any more (gone_at is never a one-way door)."""
        def back(e: Environment) -> bool:
            if e.gone_at is None:
                return False
            e.gone_at, e.gone_why = None, None
            return True

        env = self.store.update(env.id, back)  # type: ignore[attr-defined]
        self._alive_seen()[env.id] = time.monotonic()
        log.warning("environment %s (%s): its machine runs again, so it is not gone", env.id, env.title)
        return env

    async def _mark_gone(self, env: Environment, why: str, cause: str) -> str:
        now = self._clock()  # type: ignore[attr-defined]

        def mark(e: Environment) -> bool:
            if e.gone_at is not None or not e.open:
                return False
            e.gone_at, e.gone_why = now, why
            return True

        env = self.store.update(env.id, mark)  # type: ignore[attr-defined]
        log.warning("environment %s (%s): its machine is gone (%s), found by %s", env.id, env.title, why,
                    cause or "a check")
        tunnel = self._tunnels.pop(env.id, None)  # type: ignore[attr-defined]
        if tunnel is not None:  # nothing to reach any more; the reaper starts no new one (gone_at)
            with contextlib.suppress(Exception):
                await tunnel.stop()
        with contextlib.suppress(Exception):
            self.ssh_down(env)  # type: ignore[attr-defined]  (SshAccess: the agent's own ssh alias goes)
        with contextlib.suppress(Exception):
            await self._notify(f"Environment \"{env.title}\": its machine stopped",  # type: ignore[attr-defined]
                               f"{agent_names.said(env.agent, env.client)}'s environment {env.id} lost its machine: {why}. The agent is told "
                               f"so on its next call; environment_open with the same title starts a new one.")
        return why

    def gone_words(self, env: Environment) -> str:
        """machine_gone's message: when and why, and how the same title goes on (a save to resume, or none)."""
        title = env.title
        save = None
        with contextlib.suppress(Exception):
            save = self.saves().latest(env.family, title)  # type: ignore[attr-defined]  (Saving)
        if save is not None:
            then = (f"environment_open(title=\"{title}\") starts a new machine for it from its save of "
                    f"{clock.iso_or_none(save.saved_at, z=True)} ({save.id}); what changed after that save is lost")
        else:
            fresh = f" with {env.repo} cloned fresh" if env.repo else ""
            then = (f"environment_open(title=\"{title}\") starts a new machine for it{fresh}; it had no save, so "
                    "what was not pushed is lost")
        return (f"environment {env.id} (\"{title}\"): its machine stopped at {_clock_words(env)} and will not come "
                f"back ({env.gone_why or 'its provider says so'}). {then}. No other title is needed")

    def refuse_gone(self, env: Any) -> None:
        """machine_gone for a call that needs the machine of an environment whose machine is gone."""
        if getattr(env, "gone_at", None) is not None:
            raise MachineGone(ran_nothing(self.gone_words(env)))

    def gone_view(self, env: Environment) -> dict:
        """environment()'s `machine_gone`: when, why, and the one next step."""
        return {"at": clock.iso_or_none(env.gone_at, z=True), "why": env.gone_why, "next": self.gone_words(env)}

    async def lost_on_lane(self, lane_id: str) -> str | None:
        """openajar.env's machine_lost hook: a command on one of an environment's lanes could not reach its
        machine over ssh. machine_gone's message when the provider says the machine is gone, else None."""
        found = self.env_of_lane(lane_id)  # type: ignore[attr-defined]
        if found is None:
            return None
        env = found[0]
        if await self.machine_gone(env, cause="a command over ssh") is None:
            return None
        return self.gone_words(self.store.get(env.id))  # type: ignore[attr-defined]

    def liveness_install(self) -> None:
        """Give openajar.env the hook (at start, once its terminal lanes are there)."""
        envs = self._get("envs")  # type: ignore[attr-defined]
        if envs is not None and hasattr(envs, "machine_lost"):
            envs.machine_lost = self.lost_on_lane

    async def end_gone(self, env: Environment) -> Environment:
        """Close an environment whose machine is gone: no save and no check of its work (there is no machine
        to read), and nothing on the machine is waited for. Its saves stay."""
        async with self._lock(f"close:{env.id}"):  # type: ignore[attr-defined]
            env = self.store.get(env.id)  # type: ignore[attr-defined]
            if not env.open:
                return env
            task = self._tasks.get(env.id)  # type: ignore[attr-defined]
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            return await self._teardown(env, reason=f"its machine stopped ({env.gone_why})")  # type: ignore[attr-defined]

    async def replace_gone(self, title: str, family: str | None, repo: str | None) -> dict | None:
        """environment_open with a title whose open machine is gone: that environment is ended, so the open goes
        on as one with no live machine (a new machine, from the newest save of the title when there is one).
        What was replaced, or None (no open one with the title, or its machine runs)."""
        env = self._titled(title, family, repo)  # type: ignore[attr-defined]  (Picking)
        if env is None or env.state != "ready":
            return None
        if env.gone_at is not None and (await self._ask(env))[0] == "runs":  # it came back: the open attaches it
            self._not_gone(env)
            return None
        why = env.gone_why if env.gone_at is not None else await self.machine_gone(env, cause="environment_open")
        if why is None:
            return None
        gone = self.store.get(env.id)  # type: ignore[attr-defined]
        await self.end_gone(gone)
        return {"id": gone.id, "stopped_at": clock.iso_or_none(gone.gone_at, z=True), "why": why}

    async def reap_gone(self, env: Environment, now: Any, out: dict) -> bool:
        """The reaper's pass over an environment whose machine is gone: the provider is asked again (one that runs
        again is not gone any more), it waits for the agent (its next call says so) for idle_minutes, and then it
        is ended, only once the provider says again that it is gone. True while it is gone."""
        if env.gone_at is None:
            return False
        verdict, _why = await self._ask(env)
        if verdict == "runs":
            self._not_gone(env)
            return False
        if verdict == "gone" and now - env.gone_at >= timedelta(minutes=self.settings.idle_minutes):  # type: ignore[attr-defined]
            await self.end_gone(env)
            out.setdefault("gone", []).append(env.id)
        return True
