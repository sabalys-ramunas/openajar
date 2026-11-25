"""An environment's link to its machine, brought back by itself when it drops. A mixin of EnvironmentManager.

The link is the SSH connection that carries the environment's tunnels (openajar.environments.tunnels): its
browser on the machine, the live view, the git gateway, the network's ports. It drops when this computer's
network does: the Wi-Fi changes, a VPN goes on, or (openajar local) this computer's public address moves, after
which the VM firewall refuses it until it names the new address (openajar.vms.following).

- The signal: a tunnel's ssh that ends by itself (Tunnel.on_exit) starts a relink at once (`tunnel_dropped`).
  The reaper's pass, once a minute, does the same for one it finds down (`relink_once`).
- A relink: first the firewall follows this computer's address when it moved (VmManager.follow_address: free
  when it did not), then the dead tunnel is stopped and a new one started on its ports (BringUp._start_tunnel),
  the browser lane made if a restart lost it, and the screen checked. Tries at RELINK_WAITS_S, then the reaper
  goes on each minute. A machine its provider says is gone ends it (openajar.environments.liveness).
- The browser: its connection went with the link, so a browser that ran is started again at once
  (start_browser), with the sign-ins it last kept; its open tabs do not come back.
- Meanwhile a call that needs the machine runs nothing and gets environment_not_ready (`refuse_link_down`), whose
  words say what happened and that the node is on it, with the catalog's retry_after; environment(on) waits for
  the link up to its usual minute (`wait_link`) and shows `link` while it is down and for a while after.
- openajar local's minute check (openajar.local.wake) calls `address_moved` when the firewall moved without a
  tunnel noticing yet: every ready environment whose tunnel is down gets a relink.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from openajar.core import clock
from openajar.environments.models import Environment

log = logging.getLogger("openajar.environments")

RELINK_WAITS_S = (0.0, 3.0, 10.0, 20.0, 30.0)  # before each try after a drop; then the reaper's pass, each minute
BACK_SHOWN_S = 600.0                            # environment() says the link came back this long after


def link_words(env: Environment, state: dict) -> str:
    """environment_not_ready's message while the link is down: what happened, that nothing ran, what to do."""
    since = state.get("since")
    when = since.strftime("%H:%M UTC") if since is not None else "a moment ago"
    why = state.get("why") or "its connection dropped"
    tries = int(state.get("tries") or 0)
    doing = (f"the node is bringing it back (try {tries + 1})" if state.get("running") else
             "the node tries again on its next pass, within a minute")
    return (f"environment {env.id}'s link to its machine dropped at {when} ({why}), and {doing}. Nothing ran. "
            "The machine and its work are as they were. Call environment(on=\"<your on>\"): it waits up to a "
            "minute for the link and says when it is back, then call again. Do not open another environment")


class Relink:
    """The link of each environment (EnvironmentManager's self.store, self._tunnels, self._get, self._clock)."""

    store: Any
    _tunnels: dict[str, Any]

    def _link_state(self) -> dict[str, dict]:
        return self.__dict__.setdefault("_links_down", {})

    def _link_back(self) -> dict[str, dict]:
        return self.__dict__.setdefault("_links_back", {})

    def _relinking(self) -> dict[str, asyncio.Task]:
        return self.__dict__.setdefault("_relink_tasks", {})

    def watch_link(self, env: Environment, tunnel: Any) -> None:
        """Give a new tunnel its drop handler (BringUp._start_tunnel)."""
        with contextlib.suppress(AttributeError):
            tunnel.on_exit = lambda i=env.id: self.tunnel_dropped(i)

    def tunnel_dropped(self, env_id: str, why: str = "its connection dropped") -> None:
        """A tunnel's ssh ended by itself: bring the link back now, in the background (once per environment)."""
        try:
            env = self.store.get(env_id)
        except Exception:  # noqa: BLE001 - closed and forgotten meanwhile
            return
        if not env.open or env.state != "ready" or env.gone_at is not None:
            return
        state = self._link_state().setdefault(env_id, {"since": self._clock(), "tries": 0, "why": why})  # type: ignore[attr-defined]
        task = self._relinking().get(env_id)
        if task is not None and not task.done():
            return
        log.info("environment %s: its link to its machine dropped (%s); bringing it back", env_id, state["why"])
        task = asyncio.create_task(self._relink(env_id), name=f"environment-{env_id}-relink")
        self._relinking()[env_id] = task
        task.add_done_callback(lambda t, i=env_id: self._relinking().pop(i, None) if self._relinking().get(i) is t else None)

    async def _relink(self, env_id: str) -> None:
        state = self._link_state().get(env_id) or {}
        state["running"] = True
        try:
            for wait in RELINK_WAITS_S:
                await asyncio.sleep(wait)
                env = self.store.get(env_id)
                if not env.open or env.state != "ready" or env.gone_at is not None:
                    self._link_state().pop(env_id, None)
                    return
                if await self.relink_once(env, cause="its tunnel dropped"):
                    return
                if state.get("tries") == 1 and await self.machine_gone(env, cause="its tunnel went down"):  # type: ignore[attr-defined]
                    self._link_state().pop(env_id, None)
                    return
        finally:
            state["running"] = False

    async def relink_once(self, env: Environment, *, cause: str) -> bool:
        """One try at the link: the firewall follows this computer, then the tunnel, the browser lane and the
        screen. True when it is up (it was, or it came back)."""
        async with self._lock(f"link:{env.id}"):  # type: ignore[attr-defined]
            tunnel = self._tunnels.get(env.id)
            if tunnel is not None and tunnel.alive:
                self._link_state().pop(env.id, None)
                return True
            noticed = env.id in self._link_state() or tunnel is not None  # not the node's own restart
            state = self._link_state().setdefault(env.id, {"since": self._clock(), "tries": 0,  # type: ignore[attr-defined]
                                                           "why": "its connection dropped" if noticed else
                                                           "the node restarted"})
            began = time.monotonic()
            if await self._follow_address(cause, fresh=bool(state.get("tries"))):  # a retry looks again
                state["why"] = "this computer's address changed, and the VM firewall now lets in the new one"
            had_browser = self._browser_ran(env)
            try:
                await self._start_tunnel(env)  # type: ignore[attr-defined]  (BringUp: the dead one is stopped first)
                await self._make_browser(env)  # type: ignore[attr-defined]  (a restart of the node lost the lane)
                self._screen_soon(env)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001 - the next try, or the reaper's pass
                state["tries"] = int(state.get("tries") or 0) + 1
                state["error"] = str(getattr(e, "message", e))[:300]
                log.warning("environment %s: its link did not come back (try %d): %s", env.id, state["tries"],
                            state["error"])
                return False
            self._link_state().pop(env.id, None)
            if had_browser:
                self.start_browser(env)  # type: ignore[attr-defined]  (its browser went with the old link)
            down_s = (self._clock() - state["since"]).total_seconds() if state.get("since") else 0.0  # type: ignore[attr-defined]
            if noticed:
                self._link_back()[env.id] = {"at": self._clock(), "down_s": round(down_s), "why": state.get("why"),  # type: ignore[attr-defined]
                                             "browser_restarted": had_browser}
            log.info("environment %s: its link to its machine is back (%s; down %.0f s, this try %.1f s)", env.id,
                     state.get("why"), down_s, time.monotonic() - began)
            return True

    def _browser_ran(self, env: Environment) -> bool:
        """Was the environment's browser running (a lane that is warm or in use)? It is started again after."""
        lanes = self._get("lanes")  # type: ignore[attr-defined]
        try:
            return str(getattr(lanes.get(env.browser_lane), "state", "")) in ("warm", "active")
        except Exception:  # noqa: BLE001 - no browser lane: a terminal only
            return False

    async def _follow_address(self, why: str, *, fresh: bool = False) -> bool:
        """The VM firewall follows this computer's address when it moved (local mode); True when it moved."""
        vms = self._get("vms")  # type: ignore[attr-defined]
        follow = getattr(vms, "follow_address", None)
        if not callable(follow):
            return False
        try:
            out = await follow(why, fresh=fresh)
        except Exception as e:  # noqa: BLE001 - no address now (the network is down): the tunnel's try says so
            log.info("could not check this computer's address for the VM firewall: %s", getattr(e, "message", e))
            return False
        return bool(out and out.get("changed"))

    def address_moved(self) -> list[str]:
        """The VM firewall moved to this computer's new address (openajar.local.wake's minute check): a relink for
        every ready environment whose tunnel is down. The ids it started."""
        out = []
        for env in self.store.open():
            tunnel = self._tunnels.get(env.id)
            if env.state == "ready" and env.gone_at is None and (tunnel is None or not tunnel.alive):
                self.tunnel_dropped(env.id, "this computer's address changed, and the VM firewall now lets in "
                                    "the new one")
                out.append(env.id)
        return out

    def link_down(self, env_id: str) -> dict | None:
        return self._link_state().get(env_id)

    def refuse_link_down(self, env: Any) -> None:
        """environment_not_ready for a call that needs the machine while its link is down and being brought back."""
        state = self._link_state().get(getattr(env, "id", ""))
        if state is None:
            return
        tunnel = self._tunnels.get(env.id)
        if tunnel is not None and tunnel.alive:
            self._link_state().pop(env.id, None)
            return
        from openajar.environments.errors import EnvironmentNotReady

        raise EnvironmentNotReady(link_words(env, state))

    async def wait_link(self, env_id: str, wait_s: float) -> None:
        """environment(on) while the link is down: wait for it, up to wait_s."""
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_s
        while self._link_state().get(env_id) is not None and loop.time() < end:
            task = self._relinking().get(env_id)
            if task is None or task.done():
                break  # nothing brings it back now: the reaper's pass will
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=min(5.0, max(0.0, end - loop.time())))

    def link_view(self, env: Environment) -> dict | None:
        """environment()'s `link`: down (since when, why, the tries), or back (when, after how long) for a while."""
        state = self._link_state().get(env.id)
        if state is not None:
            return {"state": "down", "since": clock.iso_or_none(state.get("since"), z=True), "why": state.get("why"),
                    "tries": state.get("tries", 0), "next": link_words(env, state)}
        back = self._link_back().get(env.id)
        if back is None or (self._clock() - back["at"]).total_seconds() > BACK_SHOWN_S:  # type: ignore[attr-defined]
            return None
        words = f"the link to the machine dropped ({back.get('why')}) and came back after {back['down_s']} s"
        if back.get("browser_restarted"):
            words += ": the browser started again, with the sign-ins it had kept, and its open tabs are gone"
        return {"state": "back", "at": clock.iso_or_none(back["at"], z=True), "note": words}
