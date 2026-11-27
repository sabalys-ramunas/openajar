"""One lane's running browser (CamoufoxSession): its context, pages, routes and guard, whether it
still runs, and closing it so that no process outlives it."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from openajar.core.errors import EngineError
from openajar.core.progress import abandon_after
from openajar.node.browser_output import browser_error, forget_browser
from openajar.node.camoufox.pages import PageTracker, forget_context
from openajar.node.camoufox.procs import browser_pids
from openajar.node.display import kill_processes, pid_alive, process_group
from openajar.node.egress import LaneGuard
from openajar.node.files import LaneFiles
from openajar.node.interaction.dialogs import DIALOG_TIMEOUT
from openajar.node.routes import RouteDispatcher

log = logging.getLogger("openajar.engine.camoufox")


SIGNED_OUT_LAUNCH_MS = 30_000  # how long the signed-out check's throwaway browser may take to start


async def _close_browser(browser: Any) -> None:
    """Close a throwaway browser (Playwright deletes its temporary profile); never raises."""
    try:
        await asyncio.wait_for(browser.close(), 15.0)
    except Exception as e:  # noqa: BLE001 - the check's result stands; the log says what was left
        log.warning("the signed-out browser did not close cleanly: %s", browser_error(e, "signed-out browser"))


class CamoufoxSession:
    def __init__(self, playwright: Any, context: Any, profile_dir: Path, pid: int | None,
                 close_timeout: float, *, files: LaneFiles | None = None, dialog_timeout: float = DIALOG_TIMEOUT,
                 machine: Any = None):
        self._pw = playwright
        self._ctx = context
        self._profile_dir = profile_dir
        self._pid = pid
        self._close_timeout = close_timeout
        self._closed = False
        self._ctx_closed = False
        # What the launch did to the lane's pin (and its identity), as (event kind, detail) for the lane's events.
        self.pin_notes: list[tuple[str, dict[str, Any]]] = []
        self.signer: Any = None  # the lane's webbotauth.route.LaneSigner while it signs
        context.on("close", lambda *_: setattr(self, "_ctx_closed", True))
        # machine: where a browser that runs elsewhere keeps its downloads (openajar.environments.vmdownloads)
        self.tracker = PageTracker(context, files=files, dialog_timeout=dialog_timeout, machine=machine)
        self.routes = RouteDispatcher(context)  # the only owner of this context's routes
        self.guard: LaneGuard | None = None     # the lane's egress guard (set by the engine)
        # what its browser was launched with (the pinned config, prefs and proxy): the signed-out
        # check starts its throwaway browser with the same (openajar.node.signed_out). Never logged.
        self.launch_options: dict[str, Any] | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def alive(self) -> bool:
        if self._closed or self._ctx_closed:
            return False
        if self._pid is None:
            # The pid was not found at launch: look again rather than assume the browser runs.
            if not Path("/proc").is_dir():
                return True  # no way to tell here; the context close event still applies
            found = browser_pids(self._profile_dir)
            if not found:
                return False
            self._pid = min(found)
        return pid_alive(self._pid)

    async def read_signed_out(self, url: str, text: str, *, display: str, timeout_ms: int, lane_id: str = "",
                              sites: tuple[str, ...] | list[str] = (), strict: bool = False) -> dict[str, Any]:
        """`url` as a signed-out visitor sees it, in a throwaway browser with this lane's identity
        and proxy and a temporary profile (openajar.node.signed_out). The browser is always closed
        before this returns, also when the caller is cancelled."""
        from camoufox.async_api import AsyncNewBrowser

        from openajar.node.signed_out import read_visibility, throwaway_options

        if not self.launch_options:
            raise EngineError("this lane's launch options are not known, so a signed-out browser cannot match it")
        opts = {**throwaway_options(self.launch_options, display), "timeout": SIGNED_OUT_LAUNCH_MS}
        browser = await AsyncNewBrowser(self._pw, from_options=opts, persistent_context=False)
        try:
            context = await browser.new_context(no_viewport=True)  # the window's own size, as the lane's page
            LaneGuard(lane_id, sites=sites, strict=strict).attach(context, RouteDispatcher(context))
            page = await context.new_page()
            return await read_visibility(page, url, text, timeout_ms=timeout_ms)
        finally:
            await asyncio.shield(_close_browser(browser))

    @property
    def pids(self) -> list[int]:
        """Every process of this lane's browser: the main process, which leads its own process
        group, and its content processes (they share the group, not the profile argument)."""
        found = set(browser_pids(self._profile_dir, main_only=False))
        if self._pid:
            found.update(process_group(self._pid))
        return sorted(found)

    @property
    def context(self) -> Any:
        return self._ctx

    async def page(self) -> Any:
        """The page the lane acts in: the newest popup or tab until it closes, else the one before."""
        if not self.alive:
            raise EngineError("the browser for this lane is not running")
        return await self.tracker.page()

    async def close(self) -> None:
        """Graceful, then force, then sweep. Every step runs even when the caller is cancelled
        part way (the cancellation is raised at the end), so a browser never outlives its
        session because a close was interrupted."""
        if self._closed:
            return
        self._closed = True
        cancelled = False
        try:
            await abandon_after(self.tracker.close(), self._close_timeout)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as e:  # noqa: BLE001 - never block closing the browser
            log.warning("closing the page tracker of %s: %s", self._profile_dir, browser_error(e, str(self._profile_dir)))
        # 1. graceful: closing the context flushes cookies and session state to the profile
        if not cancelled:
            try:
                # never waits on a close stuck on a dead connection (wait_for does, and one took 13 min)
                await abandon_after(self._ctx.close(), self._close_timeout)
            except asyncio.CancelledError:
                cancelled = True
                log.warning("closing %s was cancelled; forcing it", self._profile_dir)
            except Exception as e:  # noqa: BLE001 (timeout or a browser that already died)
                log.warning("graceful close of %s did not finish: %s", self._profile_dir,
                            browser_error(e, str(self._profile_dir)))
        # 2. force: the browser leads its own process group (Playwright starts it detached)
        if self._pid and pid_alive(self._pid):
            log.warning("browser %d still running after close; killing its process group", self._pid)
            try:
                await kill_processes([self._pid], grace=2.0)
            except asyncio.CancelledError:
                cancelled = True
                await kill_processes([self._pid], grace=0)  # grace 0 never awaits
        try:
            await asyncio.wait_for(self._pw.stop(), 10.0)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as e:  # noqa: BLE001
            log.warning("playwright driver did not stop cleanly: %s", browser_error(e, str(self._profile_dir)))
        # 3. sweep any stray process still holding this profile
        stray = browser_pids(self._profile_dir, main_only=False)
        if stray:
            log.warning("sweeping %d stray camoufox-bin processes of %s", len(stray), self._profile_dir)
            try:
                await kill_processes(stray, grace=1.0)
            except asyncio.CancelledError:
                cancelled = True
                await kill_processes(stray, grace=0)
        forget_context(self._ctx)
        forget_browser(self._pid, self._profile_dir)  # its driver has stopped: no more lines of this pid
        if cancelled:
            raise asyncio.CancelledError
