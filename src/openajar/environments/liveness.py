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

log = logging.getLogger("openajar.environments")

# a provider's word for a machine that does not run and will not run again by itself: docker's container states
# (exited, dead, removing) and Hetzner's server states (off, stopping, deleting)
GONE_STATUSES = frozenset({"exited", "dead", "removing", "off", "stopping", "deleting", "deleted"})
RECHECK_S = 2.0         # the provider is asked again this long after it first said gone
ALIVE_AGAIN_S = 10.0    # a machine the provider said runs is not asked about again for this long
PROVIDER_S = 15.0       # one question to the provider, at most
PROVIDERS = {"docker": "Docker", "hetzner": "Hetzner", "morph": "Morph", "own": "Your machine"}


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
        """Why the provider says the machine is gone, or None (it runs, or there is no answer to go by)."""
        vms = self._get("vms")  # type: ignore[attr-defined]
        if vms is None or not env.vm_id:
            return None
        try:
            rec = vms.store.get(env.vm_id)
        except (NotFound, ValueError):
            return None
        if not rec.live:
            return f"the node deleted it ({rec.delete_reason or rec.state})"
        if not rec.provider_id:
            return None
        name = PROVIDERS.get(rec.provider, rec.provider or "the provider")
        what = "container" if rec.provider in ("docker", "own") else "server"
        try:
            server = await asyncio.wait_for(vms.provider().get_server(rec.provider_id), PROVIDER_S)
        except Exception as e:  # noqa: BLE001 - a provider that does not answer says nothing either way
            log.info("environment %s: %s did not answer about its machine: %s", env.id, name, e)
            return None
        if server is None and rec.provider == "own" and not rec.via:  # a machine handed over whole (no hop to it)
            return (f"{name} no longer holds this environment: its hold was ended outside openajar "
                    "(/var/lib/openajar-machine there), or the machine was installed again")
        if server is None:
            return f"{name} no longer has its {what}: it was deleted outside openajar, or lost with its host"
        status = str(getattr(server, "status", "") or "").lower()
        if status in GONE_STATUSES:
            return f"{name} says its {what} is {status}"
        return None

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
        why = env.gone_why if env.gone_at is not None else await self.machine_gone(env, cause="environment_open")
        if why is None:
            return None
        gone = self.store.get(env.id)  # type: ignore[attr-defined]
        await self.end_gone(gone)
        return {"id": gone.id, "stopped_at": clock.iso_or_none(gone.gone_at, z=True), "why": why}

    async def reap_gone(self, env: Environment, now: Any, out: dict) -> None:
        """The reaper's pass over an environment whose machine is gone: it waits for the agent (its next call
        says so) for idle_minutes, then it is ended."""
        if env.gone_at is not None and now - env.gone_at >= timedelta(minutes=self.settings.idle_minutes):  # type: ignore[attr-defined]
            await self.end_gone(env)
            out.setdefault("gone", []).append(env.id)
