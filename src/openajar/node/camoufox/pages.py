"""The pages, popups and downloads of one lane's browser context (PageTracker): popups and new tabs
become the page the lane acts in, dialogs are owned by a DialogGuard, and downloads land in the lane
outbox.

A browser the node launched hands Playwright's file of a finished download to the outbox
(`download.path()`). A browser the node reaches with `connect()` (an environment's, on its machine)
has no such path here: its download is streamed with `save_as`, and the tracker's `machine`
(openajar.environments.vmdownloads) keeps the browser's own file on that machine as a plain file too,
reads a running download's size there for the size cap and the stall check, and finds what the
browser saved there by itself (the PDF viewer's Save), which becomes an outbox file as well.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.core.logs import url_for_log
from openajar.node.browser_output import browser_error, mask_urls
from openajar.node.devtools import DevtoolsCapture
from openajar.node.files import (
    FileStatus,
    LaneFiles,
    StoredFile,
    human_size,
    register_download,
    unregister_download,
)
from openajar.node.human import quietly
from openajar.node.interaction.dialogs import DIALOG_TIMEOUT, DialogGuard

log = logging.getLogger("openajar.engine.camoufox")


# ---- pages, popups and downloads of one lane -------------------------------------------------

TAB_TITLE_TIMEOUT = 1.5
DOWNLOAD_POLL = 0.25
REMOTE_POLL = 1.0       # a download on a machine: its size is asked this often (one command over SSH each)
RECENT_LENGTHS = 64
NOTE_WAIT = 3.0         # an action waits this long for a download it started to finish, for its result
SAVES_WAIT = 5.0        # and this long for the machine to say what the browser saved by itself
RECENT_BEGUN = 32


@dataclass
class _ActiveDownload:
    file_id: str
    name: str = ""
    machine_path: str | None = None
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

    def __init__(self, context: Any, *, files: LaneFiles | None = None, dialog_timeout: float = DIALOG_TIMEOUT,
                 machine: Any = None):
        self._ctx = context
        self._files = files
        self._machine = machine if files is not None else None
        self._seq = 0
        self._begun: deque[tuple[int, str]] = deque(maxlen=RECENT_BEGUN)  # (seq, file id) of recent downloads
        self._known: set[str] = set()  # names in the machine's folder that are not new saves of the browser
        self._pdfs: deque[str] = deque(maxlen=RECENT_LENGTHS)  # pages that opened as a PDF (the PDF viewer)
        self._machine_lock = asyncio.Lock()
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
        if self._machine is not None and files is not None:
            _BY_FILES[str(files.root)] = self

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
        """Remember announced sizes, so a download that ends short is recorded as partial, and (on a
        machine) which pages are a PDF in the browser's viewer, whose Save the browser does by itself."""
        try:
            headers = response.headers
            if self._machine is not None and headers.get("content-type", "").split(";")[0].strip().lower() \
                    == "application/pdf" and response.request.is_navigation_request():
                self._pdfs.append(response.url)
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

    def _note_begun(self, file_id: str) -> None:
        self._seq += 1
        self._begun.append((self._seq, file_id))

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
        state = _ActiveDownload(entry.id, entry.name)
        self._downloads[entry.id] = state
        self._note_begun(entry.id)

        async def cancel() -> None:
            state.cancelled = True
            await download.cancel()

        register_download(files.root, entry.id, cancel)
        watch = self._watch_size if self._machine is None else self._watch_remote
        watcher = self._spawn(watch(download, state, files.config.download_max_bytes,
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
            elif self._machine is None:
                path = Path(await download.path())
                await asyncio.to_thread(files.finish_download, entry.id, path,
                                        expected_size=self._lengths.get(download.url))
            else:
                await self._finish_remote(download, state, files)
        except Exception as e:  # noqa: BLE001 - whatever broke, the record must not say downloading
            log.warning("download %s: %s", entry.id, browser_error(e, f"download {entry.id}"))
            if not state.closing:
                await quietly(asyncio.to_thread(files.fail_download, entry.id,
                                                FileStatus.PARTIAL if state.bytes_seen else FileStatus.FAILED,
                                                f"the download could not be saved: {_first_line(e)}",
                                                size=state.bytes_seen, machine_path=state.machine_path))
        finally:
            unregister_download(files.root, entry.id)
            self._downloads.pop(entry.id, None)
            await quietly(download.delete())

    async def _finish_remote(self, download: Any, state: _ActiveDownload, files: LaneFiles) -> None:
        """A finished download of a browser on a machine: its own file there kept as a plain file in the
        machine's folder (no bytes cross the network for it), then streamed once into the outbox."""
        src = _artifact_path(download)
        if src is not None and self._machine is not None:
            async with self._machine_lock:
                try:
                    state.machine_path = await self._machine.keep(src.as_posix(), state.name)
                    self._known.add(state.machine_path.rsplit("/", 1)[-1])
                except Exception as e:  # noqa: BLE001 - the outbox copy still counts; the record says where it is
                    log.warning("download %s was not kept on the machine: %s", state.file_id, _first_line(e))
        target = await asyncio.to_thread(files.browser_copy, state.file_id)
        await download.save_as(target)
        await asyncio.to_thread(files.finish_download, state.file_id, target,
                                expected_size=self._lengths.get(download.url), machine_path=state.machine_path)

    async def _watch_size(self, download: Any, state: _ActiveDownload, cap: int, stall: float) -> None:
        """Cancel a download once the browser's temporary file grows past the cap, or stops growing
        for `stall` seconds (Firefox never finishes a download whose connection dropped)."""
        path = _artifact_path(download)
        if path is None:
            return  # checked when it finishes instead
        async def size() -> int:
            return max(_size(path), _size(path.with_name(path.name + ".part")))

        await self._watch(download, state, cap, stall, DOWNLOAD_POLL, size, wait_first=False)

    async def _watch_remote(self, download: Any, state: _ActiveDownload, cap: int, stall: float) -> None:
        """The same for a browser on a machine: the file is there, so its size is asked there. A size it
        cannot tell (the machine did not answer) is neither growth nor a stall."""
        path = _artifact_path(download)
        machine = self._machine
        if path is None or machine is None:
            return  # checked when it finishes instead

        async def size() -> int | None:
            return await quietly(machine.size(path.as_posix()), None)

        await self._watch(download, state, cap, stall, REMOTE_POLL, size, wait_first=True)

    async def _watch(self, download: Any, state: _ActiveDownload, cap: int, stall: float, poll: float,
                     read_size: Any, *, wait_first: bool) -> None:
        """`wait_first`: a download that finishes within `poll` is never asked about (on a machine, each
        ask is a command over SSH)."""
        loop = asyncio.get_running_loop()
        last_growth = loop.time()
        while True:
            if wait_first:
                await asyncio.sleep(poll)
            size = await read_size()
            if size is not None:
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
            if not wait_first:
                await asyncio.sleep(poll)

    # ---- what the browser saved on its machine by itself -----------------------------------

    async def pick_up_saves(self) -> None:
        """Bring what the browser saved into the machine's folder by itself (the PDF viewer's Save,
        Ctrl+S) into the outbox: each such file once, as a download the node did not see start."""
        machine, files = self._machine, self._files
        if machine is None or files is None:
            return
        async with self._machine_lock:
            found = await machine.saves(self._known)
            for item in found:
                self._known.add(item["name"])
                await self._import_save(machine, files, item)

    async def _import_save(self, machine: Any, files: LaneFiles, item: dict[str, Any]) -> None:
        name, size = item["name"], item["size"]
        machine_path = f"{machine.folder}/{name}"
        entry = await asyncio.to_thread(files.begin_download, name, None)
        state = _ActiveDownload(entry.id, entry.name, machine_path=machine_path)
        self._downloads[entry.id] = state
        self._note_begun(entry.id)
        cap = files.config.download_max_bytes
        try:
            if size > cap:
                await asyncio.to_thread(files.fail_download, entry.id, FileStatus.FAILED,
                                        f"larger than the {human_size(cap)} download limit, so it was not brought "
                                        "to the outbox; it is on the machine", size=size, machine_path=machine_path)
                return
            target = await asyncio.to_thread(files.browser_copy, entry.id)
            await machine.fetch(name, target, cap)
            await asyncio.to_thread(files.finish_download, entry.id, target, machine_path=machine_path)
        except Exception as e:  # noqa: BLE001 - the record must not say downloading
            log.warning("the browser's save %s: %s", entry.id, _first_line(e))
            if not state.closing:
                await quietly(asyncio.to_thread(
                    files.fail_download, entry.id, FileStatus.FAILED,
                    f"it is on the machine, but could not be brought to the outbox: {_first_line(e)}",
                    size=size, machine_path=machine_path))
        finally:
            self._downloads.pop(entry.id, None)

    # ---- what an action reports ------------------------------------------------------------

    def may_save(self, page: Any, key: str | None) -> bool:
        """Whether a click (`key` None) or a key press may make the browser save a file by itself, which
        no download event shows: a click in the PDF viewer (its Save button), or Ctrl+S / Cmd+S."""
        if self._machine is None:
            return False
        if key is None:
            return getattr(page, "url", None) in self._pdfs
        parts = key.lower().replace(" ", "").split("+")
        return parts[-1] == "s" and any(p in ("control", "ctrl", "meta", "controlormeta", "cmd") for p in parts)

    def mark(self, *, saves: bool = False) -> tuple[int, bool]:
        """Before an action: where the downloads stand. `saves` (may_save): its result also looks on the
        machine for a file the browser saved by itself (one command over SSH)."""
        return self._seq, saves and self._machine is not None

    async def notes_since(self, mark: tuple[int, bool]) -> list[str]:
        """After the action: a line for each download it started ("a download started: <name>", with how
        it ended when that was within NOTE_WAIT s) or that failed ("the download failed: <why>")."""
        seq, saves = mark
        if saves:
            pick = self._spawn(self.pick_up_saves())
            await asyncio.wait({pick}, timeout=SAVES_WAIT)  # never cancelled: an import half done stays right
        begun = [fid for s, fid in self._begun if s > seq]
        if saves and not begun:  # nothing saved: the browser may be asking where, in a window of its own
            return await self.window_notes()
        if not begun or self._files is None:
            return []
        loop = asyncio.get_running_loop()  # a browser on the node reports without waiting, as it always has
        deadline = loop.time() + (NOTE_WAIT if self._machine is not None else 0.0)
        while any(fid in self._downloads for fid in begun) and loop.time() < deadline:
            await asyncio.sleep(0.1)
        notes = []
        for fid in begun:
            meta = await quietly(asyncio.to_thread(self._files.get, fid), None)  # None: deleted meanwhile
            if meta is not None:
                notes.append(download_note(meta))
        return notes

    async def window_notes(self, titles: set[str] | None = None) -> list[str]:
        """A browser on a machine: each window of the browser open over its pages that is not one of
        them (a Save As or another native window, which no browser tool sees or answers), as a line
        that says how to answer it. `titles`: the tabs' titles, when the caller has them."""
        machine = self._machine
        if machine is None:
            return []
        if titles is None:
            titles = {await quietly(asyncio.wait_for(p.title(), TAB_TITLE_TIMEOUT), "") for p in self._open()}
        windows = await quietly(machine.browser_windows(), None) or []
        return [window_note(w) for w in outside_windows(windows, titles)]

    async def close(self) -> None:
        """The lane is closing: downloads still running are recorded as partial, dialogs as gone."""
        for state in list(self._downloads.values()):
            state.closing = True
            if self._files is not None:
                await quietly(asyncio.to_thread(
                    self._files.fail_download, state.file_id, FileStatus.PARTIAL,
                    "interrupted: the lane closed before the download finished", size=state.bytes_seen,
                    machine_path=state.machine_path))
                unregister_download(self._files.root, state.file_id)
        if self._files is not None and _BY_FILES.get(str(self._files.root)) is self:
            _BY_FILES.pop(str(self._files.root), None)
        await self.dialogs.close()
        self.devtools.close()
        for task in list(self._tasks):
            task.cancel()


_TRACKERS: weakref.WeakKeyDictionary[Any, PageTracker] = weakref.WeakKeyDictionary()
# the tracker of a browser on a machine, by its outbox: browser_files looks for new saves there first
_BY_FILES: weakref.WeakValueDictionary[str, PageTracker] = weakref.WeakValueDictionary()


def download_note(meta: StoredFile) -> str:
    """One download as an action's result says it."""
    if meta.status is FileStatus.FAILED or meta.status is FileStatus.PARTIAL:
        where = f"; it is on the machine at {meta.machine_path}" if meta.machine_path else ""
        why = meta.error or meta.status.value
        return f"the download failed: {why} ({meta.name}, {meta.id} in browser_files{where})"
    if meta.status is FileStatus.DOWNLOADING:
        return f"a download started: {meta.name} ({meta.id}); browser_files shows when it is done"
    where = f" and on the machine at {meta.machine_path}" if meta.machine_path else ""
    return f"a download started: {meta.name}; it finished ({human_size(meta.size)}): {meta.id} in browser_files{where}"


def outside_windows(windows: list[dict[str, Any]], titles: set[str]) -> list[dict[str, Any]]:
    """The browser's windows that show none of its pages: on top of the largest one (its own window),
    titled other than any tab."""
    browser = [w for w in windows if w.get("kind") == "browser" and not w.get("minimized")]
    if len(browser) < 2:
        return []
    main = max(browser, key=lambda w: int(w.get("w") or 0) * int(w.get("h") or 0))
    return [w for w in browser if w is not main and (w.get("title") or "") not in titles]


def window_note(window: dict[str, Any]) -> str:
    title, wid = window.get("title") or "untitled", window.get("id")
    return (f"the browser opened a window of its own over the page: {title!r} (window {wid}). No browser tool "
            "can answer it, and the browser waits for it. A Save As window saves into /work/downloads on Enter: "
            f'tools_call("screen_act", {{"action": "key", "keys": "Return", "window": {wid}}}) (keys "Escape" '
            "closes it); browser_files then lists the file")


async def pick_up_saves(files_root: Any, wait: float = SAVES_WAIT) -> None:
    """browser_files on a browser that runs on a machine: what it saved there by itself, into the
    outbox first (waits at most `wait` s; nothing when its browser is not open)."""
    tracker = _BY_FILES.get(str(files_root))
    if tracker is None or tracker._machine is None:
        return
    task = tracker._spawn(tracker.pick_up_saves())
    await asyncio.wait({task}, timeout=wait)


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
