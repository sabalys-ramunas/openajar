"""The pages, popups and downloads of one lane's browser context (PageTracker): popups and new tabs
become the page the lane acts in, dialogs are owned by a DialogGuard, and downloads land in the lane
outbox."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.core.logs import url_for_log
from openajar.node.browser_output import browser_error, mask_urls
from openajar.node.devtools import DevtoolsCapture
from openajar.node.files import FileStatus, LaneFiles, human_size, register_download, unregister_download
from openajar.node.human import quietly
from openajar.node.interaction.dialogs import DIALOG_TIMEOUT, DialogGuard

log = logging.getLogger("openajar.engine.camoufox")


# ---- pages, popups and downloads of one lane -------------------------------------------------

TAB_TITLE_TIMEOUT = 1.5
DOWNLOAD_POLL = 0.25
RECENT_LENGTHS = 64


@dataclass
class _ActiveDownload:
    file_id: str
    bytes_seen: int = 0
    too_large: bool = False
    stalled: bool = False
    cancelled: bool = False
    closing: bool = False


class PageTracker:
    """Every page of one lane's browser context, and which one the lane acts in.

    A lane is one identity (one context) that can show several pages: a "Continue with Google"
    popup, a target=_blank tab. A page that opens becomes active (it is what a person would be
    looking at). When the active page closes (an OAuth window finishing) the page that opened it
    becomes active again, or else the most recently active page still open. `page()` returns the
    active page, so every tool follows along without knowing about tabs.

    The tracker also attaches the context's DialogGuard (dialogs and file choosers) and its
    DevtoolsCapture (console messages and requests, for browser_console and browser_network), and
    sends every download to the lane outbox, capped in size; a download that fails or stops part way
    is recorded as failed or partial, never as complete.
    """

    def __init__(self, context: Any, *, files: LaneFiles | None = None, dialog_timeout: float = DIALOG_TIMEOUT):
        self._ctx = context
        self._files = files
        self._opener: dict[Any, Any] = {}  # page -> the page that opened it (or was active then)
        self._mru: list[Any] = []
        self._active: Any = None
        self._downloads: dict[str, _ActiveDownload] = {}
        self._lengths: OrderedDict[str, int] = OrderedDict()  # url -> announced Content-Length
        self._tasks: set[asyncio.Task] = set()
        self.dialogs = DialogGuard(timeout=dialog_timeout).attach(context)
        self.devtools = DevtoolsCapture().attach(context)
        context.on("page", self._on_page)
        if files is not None:
            context.on("response", self._on_response)
        for page in list(context.pages):
            self._adopt(page, opener=None, activate=self._active is None)
        _TRACKERS[context] = self

    # ---- events --------------------------------------------------------------------------

    def _on_page(self, page: Any) -> None:
        self._adopt(page, opener=self._active, activate=True)

    def _adopt(self, page: Any, *, opener: Any, activate: bool) -> None:
        if page in self._opener:
            if activate:
                self._activate(page)
            return
        if page.is_closed():
            return
        self._opener[page] = opener
        page.on("close", lambda *_: self._on_close(page))
        page.on("popup", lambda popup: self._on_popup(page, popup))
        page.on("download", lambda download: self._spawn(self._download(download)))
        if activate:
            self._activate(page)

    def _on_response(self, response: Any) -> None:
        """Remember announced sizes, so a download that ends short is recorded as partial."""
        try:
            headers = response.headers
            length = headers.get("content-length", "")
            if not length.isdigit() or headers.get("content-encoding"):
                return
            self._lengths[response.url] = int(length)
            self._lengths.move_to_end(response.url)
            while len(self._lengths) > RECENT_LENGTHS:
                self._lengths.popitem(last=False)
        except Exception:  # noqa: BLE001 - a size hint is optional
            return

    def _on_popup(self, opener: Any, popup: Any) -> None:
        if popup in self._opener:
            self._opener[popup] = opener

    def _activate(self, page: Any) -> None:
        self._active = page
        if page in self._mru:
            self._mru.remove(page)
        self._mru.append(page)

    def _on_close(self, page: Any) -> None:
        if page not in self._opener:
            return
        opener = self._opener.pop(page)
        if page in self._mru:
            self._mru.remove(page)
        for child, parent in list(self._opener.items()):
            if parent is page:
                self._opener[child] = opener
        if self._active is page:
            back = opener if opener in self._opener and not opener.is_closed() else None
            back = back or next((p for p in reversed(self._mru) if not p.is_closed()), None)
            self._active = None
            if back is not None:
                self._activate(back)
                self._spawn(quietly(asyncio.wait_for(back.bring_to_front(), 5.0)))
            log.info("tab closed; the lane now acts in %s", url_for_log(getattr(back, "url", None)))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- pages ---------------------------------------------------------------------------

    def _open(self) -> list[Any]:
        """Open pages in the order they opened (the context's own order), adopting any we missed."""
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        for p in pages:
            self._adopt(p, opener=None, activate=False)
        return pages

    @property
    def active(self) -> Any:
        if self._active is not None and not self._active.is_closed():
            return self._active
        pages = self._open()
        back = next((p for p in reversed(self._mru) if not p.is_closed()), None) or (pages[-1] if pages else None)
        self._active = None
        if back is not None:
            self._activate(back)
        return back

    async def page(self) -> Any:
        page = self.active
        if page is None:
            page = await self._ctx.new_page()
            self._adopt(page, opener=None, activate=True)
        return page

    async def tabs(self) -> list[dict[str, Any]]:
        pages = self._open()
        active = self.active
        out = []
        for i, p in enumerate(pages):
            opener = self._opener.get(p)
            dialog = self.dialogs.pending_on(p)
            title = "" if dialog else await quietly(asyncio.wait_for(p.title(), TAB_TITLE_TIMEOUT), "")
            tab: dict[str, Any] = {"index": i, "url": p.url, "title": title, "active": p is active,
                                   "opened_by": pages.index(opener) if opener in pages else None}
            if dialog is not None:
                tab["dialog"] = dialog.type
            out.append(tab)
        return out

    async def switch(self, index: int) -> Any:
        pages = self._open()
        if not pages:
            raise InvalidRequest("this lane has no open tabs")
        if not 0 <= index < len(pages):
            raise InvalidRequest(f"no tab {index}: there are {len(pages)} (0 to {len(pages) - 1}); see browser_tabs")
        page = pages[index]
        self._activate(page)
        await quietly(asyncio.wait_for(page.bring_to_front(), 5.0))
        return page

    # ---- downloads -----------------------------------------------------------------------

    async def _download(self, download: Any) -> None:
        files = self._files
        if files is None:
            return
        try:
            entry = await asyncio.to_thread(files.begin_download, download.suggested_filename, download.url)
        except Exception:
            log.exception("could not record a download in the outbox; cancelling it")
            await quietly(download.cancel())
            return
        state = _ActiveDownload(entry.id)
        self._downloads[entry.id] = state

        async def cancel() -> None:
            state.cancelled = True
            await download.cancel()

        register_download(files.root, entry.id, cancel)
        watcher = self._spawn(self._watch_size(download, state, files.config.download_max_bytes,
                                               files.config.download_stall_seconds))
        cap = human_size(files.config.download_max_bytes)
        try:
            try:
                failure = await download.failure()
            except Exception as e:  # noqa: BLE001 - the browser went away
                failure = _first_line(e)
            watcher.cancel()
            if state.closing:
                return  # close() already recorded it as partial
            if state.too_large:
                await asyncio.to_thread(files.fail_download, entry.id, FileStatus.FAILED,
                                        f"larger than the {cap} download limit; cancelled", size=state.bytes_seen)
            elif state.stalled:
                await asyncio.to_thread(files.fail_download, entry.id, FileStatus.PARTIAL,
                                        f"stalled: no data for {files.config.download_stall_seconds:g} s (the "
                                        "connection probably dropped); cancelled", size=state.bytes_seen)
            elif state.cancelled:
                await asyncio.to_thread(files.fail_download, entry.id, FileStatus.FAILED, "cancelled",
                                        size=state.bytes_seen)
            elif failure:
                status = FileStatus.PARTIAL if state.bytes_seen else FileStatus.FAILED
                await asyncio.to_thread(files.fail_download, entry.id, status,
                                        f"the download did not finish: {failure}", size=state.bytes_seen)
            else:
                path = Path(await download.path())
                await asyncio.to_thread(files.finish_download, entry.id, path,
                                        expected_size=self._lengths.get(download.url))
        except Exception as e:  # noqa: BLE001 - whatever broke, the record must not say downloading
            log.warning("download %s: %s", entry.id, browser_error(e, f"download {entry.id}"))
            if not state.closing:
                await quietly(asyncio.to_thread(files.fail_download, entry.id,
                                                FileStatus.PARTIAL if state.bytes_seen else FileStatus.FAILED,
                                                f"the download could not be saved: {_first_line(e)}",
                                                size=state.bytes_seen))
        finally:
            unregister_download(files.root, entry.id)
            self._downloads.pop(entry.id, None)
            await quietly(download.delete())

    async def _watch_size(self, download: Any, state: _ActiveDownload, cap: int, stall: float) -> None:
        """Cancel a download once the browser's temporary file grows past the cap, or stops growing
        for `stall` seconds (Firefox never finishes a download whose connection dropped)."""
        path = _artifact_path(download)
        if path is None:
            return  # checked when it finishes instead
        loop = asyncio.get_running_loop()
        last_growth = loop.time()
        while True:
            size = max((_size(path), _size(path.with_name(path.name + ".part"))))
            if size > state.bytes_seen:
                state.bytes_seen, last_growth = size, loop.time()
            if size > cap:
                state.too_large = True
                log.warning("download %s passed the %s limit; cancelling", state.file_id, human_size(cap))
                await quietly(download.cancel())
                return
            if loop.time() - last_growth > stall:
                state.stalled = True
                log.warning("download %s stalled for %g s; cancelling", state.file_id, stall)
                await quietly(download.cancel())
                return
            await asyncio.sleep(DOWNLOAD_POLL)

    async def close(self) -> None:
        """The lane is closing: downloads still running are recorded as partial, dialogs as gone."""
        for state in list(self._downloads.values()):
            state.closing = True
            if self._files is not None:
                await quietly(asyncio.to_thread(
                    self._files.fail_download, state.file_id, FileStatus.PARTIAL,
                    "interrupted: the lane closed before the download finished", size=state.bytes_seen))
                unregister_download(self._files.root, state.file_id)
        await self.dialogs.close()
        self.devtools.close()
        for task in list(self._tasks):
            task.cancel()


_TRACKERS: weakref.WeakKeyDictionary[Any, PageTracker] = weakref.WeakKeyDictionary()


def forget_context(context: Any) -> int:
    """Drop every registry entry for a closed context and its pages. The registries are weak on
    their keys, but their values (a tracker, a dialog or egress guard, a page's requests in flight)
    hold the context or page again, so an entry never goes by itself: each closed lane kept its
    whole Playwright connection alive (found by the soak run, docs/BENCH.md). Returns how many."""
    from openajar.node import devtools, egress, human, typist
    from openajar.node.interaction import dialogs, pages

    registries = (_TRACKERS, egress._GUARDS, dialogs._GUARDS, pages._PAGES, pages._EXPECT,
                  pages._REPORTS, typist._NETWORKS, human.X_KEYBOARDS, human.X_POINTERS, human.IDLE_LANES,
                  human._IDLE, human.POINTERS, human._GROUND, devtools._CAPTURES)
    dropped = 0
    for registry in registries:
        for key in list(registry.keys()):
            if key is context or getattr(key, "context", None) is context:
                registry.pop(key, None)
                dropped += 1
    return dropped


def tracker_for(page_or_context: Any) -> PageTracker | None:
    """The tracker of a page's (or a context's) lane, if one is attached."""
    context = getattr(page_or_context, "context", page_or_context)
    try:
        return _TRACKERS.get(context)
    except TypeError:
        return None


def _artifact_path(download: Any) -> Path | None:
    """Where the browser writes a download while it runs (Playwright keeps it on the Artifact)."""
    impl = getattr(download, "_impl_obj", download)
    value = getattr(getattr(impl, "_artifact", None), "absolute_path", None)
    return Path(value) if isinstance(value, str) and value else None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _first_line(e: BaseException) -> str:
    """The error's first line, URLs without their query (a download URL can carry a token)."""
    return mask_urls((str(e).strip() or type(e).__name__).splitlines()[0])[:300]
