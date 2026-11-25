"""Browser interaction for one page: navigate, snapshot, act, screenshot, signal.

Implements `openajar.node.interfaces.Interaction` on the Playwright async API (Camoufox).

- Refs come from Playwright's AI snapshot (see snapshot.py) and resolve with the `aria-ref`
  selector, which matches nothing once the ref is stale.
- Every action is human input from human.py (wheel scroll, pause, random point, real press,
  typing rhythm). No `locator.click()` / `fill()` shortcuts, no synthetic DOM events.
- Read-back (PoC lesson 1): the target's own state is captured before and after the action
  (aria-pressed, aria-checked, aria-expanded, value, selected option, accessible name...).
  `changed` reflects only that element, never a label somewhere nearby.
- `signal` is deterministic (sites.py has the table and the rules): the URL, the main document's
  HTTP status, challenge frames, scripts and marker elements, and the lane's session cookies, read
  with `context.cookies(url)` (read-only, invisible to the page). It never reads page text. Every
  navigate, act, upload and snapshot result carries the signal, a `signal_detail` naming the rule
  that matched, and `account` (whose session this is) when the site shows a user id.
- A lane manager binds each lane's context to its LaneSpec (`bind_lane`), so `logged_out` and
  `wrong_account` know what the lane expects; the latest report per context is kept for it to read
  when a lane use ends (`take_report`), which is where a hold is decided (holds.py).
- Nothing here mutates the page. Our scripts only read, and Camoufox runs them in an
  isolated world the page cannot see.
- Dialogs never freeze a lane. A `DialogGuard` attached to the lane's context owns every
  alert, confirm, prompt and beforeunload: it queues them per page, the agent answers with
  browser_dialog, and one left unanswered for `timeout` seconds is answered for it
  (alert accepted; confirm, prompt and beforeunload dismissed) and recorded. While a dialog is
  open the page's scripts are blocked (evaluate, snapshot, screenshot and input all hang), so
  every page call here races "a dialog opened": an action that opens one returns at once and
  says so, and snapshot shows the dialog instead of hanging. The guard also intercepts native
  file choosers (they never appear on the display; `upload` answers them).
- `upload` attaches files from the lane inbox to a file input, either the input itself or the
  button that opens its chooser, and reads back what the input holds (and what the page's own
  change event saw, for widgets that clear the input after reading it).
- `wait` holds the lane for a bounded time until conditions hold together: a text appears or
  goes away in the accessibility tree, a ref reaches a state, the network goes quiet, the URL
  matches. It only reads. Text waits re-read the tree the way a snapshot does (refs of
  unchanged elements stay valid); ref, network and URL waits never touch the refs. A dialog
  that opens stops the wait and is reported; a challenge signal, a human taking the lane or the
  tab closing stop it too.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable
from typing import Any

from openajar.core.errors import EngineError, OpenajarError
from openajar.core.models import Signal, SnapshotResult
from openajar.core.text import first_line
from openajar.node.human import quietly
from openajar.node.interaction.actions import ActMixin
from openajar.node.interaction.checks import check_lines
from openajar.node.interaction.dialogs import (
    _DIALOG_OPEN,
    DIALOG_TIMEOUT,
    DialogGuard,
    DialogPending,
    _DialogOpened,
    _pending_error,
    dialog_guard,
    dialog_notice,
)
from openajar.node.interaction.navigation import NavigateMixin
from openajar.node.interaction.pages import (
    _PAGES,
    _consume,
    _open_pages,
    _PageState,
    _read_cookies,
    _remember,
    _url_of,
    bind_lane,
    lane_expectation,
    last_report,
    take_report,
)
from openajar.node.interaction.readback import _MARKERS_JS, _SHOWN_JS, diff_state
from openajar.node.interaction.uploads import UploadMixin, UploadResult, accept_matches
from openajar.node.interaction.urls import (
    VM_PROXIED_LANES,
    _strip_fragment,
    check_url,
    is_challenge_url,
    normalize_ref,
    same_origin,
)
from openajar.node.interaction.waits import (
    WAIT_MAX_S,
    WaitFor,
    WaitMixin,
    WaitResult,
    _Watch,
    ref_state_met,
    url_matches,
)
from openajar.node.sites import (
    MARKER_SELECTORS,
    MIN_VISIBLE_PX,
    PASSED_FIELDS,
    Frame,
    PageFacts,
    SignalReport,
    classify_page,
    classify_signal,  # noqa: F401 - re-exported for callers of the older API
    is_challenge_frame,
)
from openajar.node.snapshot import MAX_CHARS, take_snapshot

# Names other packages import from here; they live in the modules above.
__all__ = [
    "DIALOG_TIMEOUT",
    "VM_PROXIED_LANES",
    "WAIT_MAX_S",
    "DialogGuard",
    "DialogPending",
    "UploadResult",
    "WaitFor",
    "WaitResult",
    "_Watch",
    "accept_matches",
    "bind_lane",
    "check_url",
    "dialog_guard",
    "dialog_notice",
    "diff_state",
    "is_challenge_url",
    "lane_expectation",
    "last_report",
    "normalize_ref",
    "ref_state_met",
    "same_origin",
    "take_report",
    "url_matches",
]


log = logging.getLogger("openajar.interaction")

Page = Any           # playwright.async_api.Page
ElementHandle = Any  # playwright.async_api.ElementHandle

NAVIGATE_TIMEOUT_MS = 45_000
SCREENSHOT_TIMEOUT_MS = 15_000
READBACK_TIMEOUT_MS = 2_500


class PlaywrightInteraction(NavigateMixin, ActMixin, UploadMixin, WaitMixin):
    """The Interaction protocol for Playwright pages (Camoufox lanes)."""

    def __init__(self, *, snapshot_max_chars: int = MAX_CHARS, navigate_timeout_ms: int = NAVIGATE_TIMEOUT_MS,
                 readback_timeout_ms: int = READBACK_TIMEOUT_MS, allow_private_targets: bool = False):
        self.allow_private_targets = allow_private_targets
        self.snapshot_max_chars = snapshot_max_chars
        self.navigate_timeout_ms = navigate_timeout_ms
        self.readback_timeout_ms = readback_timeout_ms

    # ---- main document status ----------------------------------------------------------

    def _track(self, page: Page) -> _PageState:
        """Remember the HTTP status of the page's main document, including navigations that an
        action (a click, Enter in a search box) starts."""
        state = _PAGES.get(page)
        if state is not None:
            return state
        state = _PageState()
        page_ref = weakref.ref(page)

        def on_response(response: Any) -> None:
            pg = page_ref()
            try:
                main_document = (pg is not None and response.frame == pg.main_frame
                                 and response.request.is_navigation_request())
            except Exception:  # noqa: BLE001 - e.g. service worker responses have no frame
                return
            if main_document:
                state.status, state.status_url = response.status, response.url

        page.on("response", on_response)
        # Plain functions, not bound methods: Playwright keeps a wrapper on a bound method's owner,
        # and a slots dataclass has nowhere to put it.
        page.on("request", lambda request: state.request_started(request))
        page.on("requestfinished", lambda request: state.request_ended(request))
        page.on("requestfailed", lambda request: state.request_ended(request))
        _PAGES[page] = state
        return state

    def _status_for_current(self, page: Page) -> int | None:
        state = _PAGES.get(page)
        if state is None or state.status_url is None:
            return None
        return state.status if _strip_fragment(state.status_url) == _strip_fragment(page.url) else None

    # ---- dialogs -----------------------------------------------------------------------

    async def _race(self, page: Page, work: Awaitable[Any], guard: DialogGuard | None) -> Any:
        """Await `work`, unless a dialog opens on the page first: then stop waiting (the page is
        frozen, so `work` could only hang) and raise _DialogOpened."""
        if guard is None:
            return await work
        waiter = guard.waiter(page)
        task = asyncio.ensure_future(work)
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise _DialogOpened(waiter.result())
        finally:
            guard.discard_waiter(waiter)
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume)

    def _header(self, page: Page, guard: DialogGuard | None) -> list[str]:
        """Lines on top of a snapshot: open dialogs (this tab first) and, with several tabs, which one
        this is."""
        lines: list[str] = []
        pages = _open_pages(page)
        if guard is not None:
            for d in sorted(guard.pending(), key=lambda d: d.page is not page):
                where = "on this tab" if d.page is page else (
                    f"on tab {pages.index(d.page)}" if d.page in pages else "in another tab")
                lines.append(dialog_notice(d, where))
        if len(pages) > 1 and page in pages:
            lines.append(f"- tabs: {len(pages)} open, this is tab {pages.index(page)} (browser_tabs lists them, "
                         "select=<index> switches)")
        return lines

    def _frozen_snapshot(self, page: Page, header: list[str]) -> SnapshotResult:
        frozen = "- (the page is frozen until the dialog is answered; nothing on it can be read or acted on)"
        text = "\n".join([*header, frozen])
        return SnapshotResult(url=_url_of(page), title="", text=text, ref_count=0, signal=Signal.UNKNOWN,
                              signal_detail=_DIALOG_OPEN, notices=header, frozen=True)

    async def snapshot(self, page: Page, *, max_chars: int | None = None) -> SnapshotResult:
        """`max_chars`: above the budget an agent's outline has, for a read that keeps none of it."""
        self._track(page)
        guard = dialog_guard(page)
        header = self._header(page, guard)
        if guard is not None and guard.pending_on(page) is not None:
            return self._frozen_snapshot(page, header)
        try:
            snap = await self._race(page, take_snapshot(page, max_chars=max_chars or self.snapshot_max_chars), guard)
        except _DialogOpened:
            return self._frozen_snapshot(page, self._header(page, guard))
        except OpenajarError:
            raise
        except Exception as e:
            raise EngineError(f"snapshot failed: {first_line(e, 300)}") from e
        report = await self.report(page)
        update: dict[str, Any] = report.public()
        checks = await check_lines(page, report)  # a site's "are you human" checkbox, with a ref (checks.py)
        if checks:
            update["text"] = "\n".join([*checks, snap.text])
            update["ref_count"] = snap.ref_count + len(checks)
        if header:
            update["text"] = "\n".join([*header, update.get("text", snap.text)])
            update["notices"] = header
        return snap.model_copy(update=update)

    async def screenshot(self, page: Page, *, full_page: bool = False) -> bytes:
        """A PNG of the viewport, or with `full_page` of the whole page (it scrolls nothing)."""
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, "take a screenshot")
        try:
            # caret="initial": the default hides the caret by injecting a style into the page.
            return await self._race(page, page.screenshot(type="png", full_page=full_page, caret="initial",
                                                          timeout=SCREENSHOT_TIMEOUT_MS), guard)
        except _DialogOpened as opened:
            raise _pending_error(opened.dialog, "take a screenshot") from None
        except Exception as e:
            raise EngineError(f"screenshot failed: {first_line(e, 300)}") from e

    async def signal(self, page: Page) -> Signal:
        return (await self.report(page)).signal

    async def report(self, page: Page) -> SignalReport:
        """The page's signal with the rule that matched and whose session it is (sites.classify_page).
        Remembered per lane context for the lane manager (`take_report`)."""
        url = _url_of(page)
        if not url or url == "about:blank":
            return _remember(page, SignalReport(Signal.UNKNOWN, "nothing is loaded", url))
        guard = dialog_guard(page)
        if guard is not None and guard.pending_on(page) is not None:
            # The page is frozen; reading it would hang.
            return _remember(page, SignalReport(Signal.UNKNOWN, _DIALOG_OPEN, url))
        try:
            facts = await self._race(page, self._facts(page, url), guard)
        except _DialogOpened:
            return _remember(page, SignalReport(Signal.UNKNOWN, _DIALOG_OPEN, url))
        return _remember(page, classify_page(facts, lane_expectation(page)))

    async def _facts(self, page: Page, url: str) -> PageFacts:
        self._track(page)
        status = self._status_for_current(page)
        markers = await quietly(page.evaluate(_MARKERS_JS, [list(MARKER_SELECTORS), MIN_VISIBLE_PX, list(PASSED_FIELDS)]))
        cookies = await _read_cookies(page, url)
        if markers is None:
            return PageFacts(url=url, status=status, readable=False, cookies=cookies)
        frames = [Frame(f.get("src") or "", bool(f.get("visible"))) for f in markers.get("frames") or []]
        frames += await self._frames_in_hidden_dom(page, {f.url for f in frames})
        return PageFacts(url=url, status=status, frames=tuple(frames), scripts=tuple(markers.get("scripts") or ()),
                         elements=dict(markers.get("elements") or {}), cookies=cookies,
                         passed=frozenset(str(f) for f in markers.get("passed") or ()))

    async def _frames_in_hidden_dom(self, page: Page, seen: set[str]) -> list[Frame]:
        """Challenge frames that a DOM query cannot see (closed shadow roots), found through the
        frame tree and judged by the same visibility check as the rest."""
        found: list[Frame] = []
        for frame in list(getattr(page, "frames", []))[1:]:
            if frame.url in seen or not is_challenge_frame(frame.url):
                continue
            element = await quietly(frame.frame_element())
            shown = await quietly(element.evaluate(_SHOWN_JS, MIN_VISIBLE_PX)) if element is not None else None
            found.append(Frame(frame.url, bool(shown)))
        return found


def create_interaction(**kwargs: Any) -> PlaywrightInteraction:
    return PlaywrightInteraction(**kwargs)
