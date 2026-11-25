"""act: one human action on a ref (human.py does the input), then the read-back of that element and
what else it opened (a tab, a file chooser, a dialog).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openajar.core.errors import EngineError, InvalidRequest, OpenajarError, RefNotFound
from openajar.core.models import ActionKind, ActionResult, Signal
from openajar.core.text import first_line
from openajar.node import human, human_fields
from openajar.node.human import quietly
from openajar.node.interaction.autocheck import past_automatic_check
from openajar.node.interaction.checks import click_check
from openajar.node.interaction.dialogs import (
    _DIALOG_OPEN,
    DialogGuard,
    PageDialog,
    _a,
    _DialogOpened,
    _pending_error,
    dialog_guard,
)
from openajar.node.interaction.pages import _open_pages, _url_of
from openajar.node.interaction.readback import _state, diff_state
from openajar.node.interaction.urls import _strip_fragment, normalize_ref
from openajar.node.snapshot import is_check_ref

Page = Any           # playwright.async_api.Page
ElementHandle = Any  # playwright.async_api.ElementHandle


RESOLVE_TIMEOUT_MS = 2_000
READBACK_POLL_MS = 200
READBACK_SETTLE_MS = 300
NEW_TAB_SETTLE_MS = 3_000


class ActMixin:
    """act: one human action on a ref, and the read-back of what it changed."""

    async def act(self, page: Page, ref: str, action: ActionKind | str, text: str | None = None) -> ActionResult:
        try:
            kind = ActionKind(action)
        except ValueError:
            raise InvalidRequest(f"unknown action {action!r}; use one of: "
                                 + ", ".join(a.value for a in ActionKind)) from None
        ref = normalize_ref(ref)
        if is_check_ref(ref):  # a site's "are you human" checkbox (checks.py)
            return await click_check(self, page, ref, kind)
        await human.still(page)
        if kind in (ActionKind.TYPE, ActionKind.FILL, ActionKind.SELECT, ActionKind.PRESS) and not text:
            what = {"select": "the option label", "press": "the key, for example Enter"}.get(kind.value, "the text")
            raise InvalidRequest(f"{kind.value} needs text: {what}")
        self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, f"{kind.value} on {ref}")
        pages_before = set(_open_pages(page))
        choosers_before = guard.chooser_count(page) if guard is not None else 0
        target = await self._resolve(page, ref)
        url_before = page.url
        before = await _state(target)
        if before is None:
            raise RefNotFound(f"ref {ref} is no longer on the page. Take a new snapshot.")

        in_view = None
        try:
            in_view = await self._race(page, self._perform(page, target, kind, text), guard)
        except _DialogOpened as opened:
            return await self._after(page, kind, ref, pages_before, choosers_before, guard, opened=opened.dialog)
        except human.TargetCovered as e:  # say what covers it, with its ref (interaction.covered)
            from openajar.node.interaction.covered import explain

            raise await explain(page, e) from None
        except OpenajarError:
            raise
        except Exception as e:
            if page.is_closed():
                return await self._after(page, kind, ref, pages_before, choosers_before, guard)
            if await _state(target) is None:
                raise RefNotFound(f"ref {ref} left the page during {kind.value}. Take a new snapshot.") from e
            raise EngineError(f"{kind.value} on {ref} failed: {first_line(e, 300)}") from e
        if page.is_closed():
            return await self._after(page, kind, ref, pages_before, choosers_before, guard)

        ok = True
        try:
            if kind is ActionKind.SCROLL:
                changed = None
                ok = bool(in_view)
                detail = "in view" if in_view else "could not bring it fully into view"
            else:
                changed, detail, after = await self._race(
                    page, self._read_back(page, target, before, kind, url_before), guard)
                if kind is ActionKind.SELECT and after is not None:
                    ok = human_fields.match_option([{"label": after.get("selected", "")}], text or "") == 0
                    if not ok:
                        detail += f"; the selected option is {after.get('selected')!r}, not {text!r}"
            if _strip_fragment(page.url) != _strip_fragment(url_before):
                await self._race(page, quietly(page.wait_for_load_state("domcontentloaded", timeout=10_000)), guard)
        except _DialogOpened as opened:
            return await self._after(page, kind, ref, pages_before, choosers_before, guard, opened=opened.dialog)
        if page.is_closed():
            return await self._after(page, kind, ref, pages_before, choosers_before, guard)
        notes = await self._side_effects(page, pages_before, choosers_before, guard, ref)
        report = await self.report(page)
        if _strip_fragment(page.url) != _strip_fragment(url_before):  # it led to Cloudflare's check: as navigate
            report = await past_automatic_check(page, report, self.report)
        human.begin_idle(page)
        return ActionResult(ok=ok, action=kind, ref=ref, url=page.url, changed=changed,
                            detail="; ".join([detail, *notes]), **report.public())

    async def _after(self, page: Page, kind: ActionKind, ref: str, pages_before: set[Any], choosers_before: int,
                     guard: DialogGuard | None, *, opened: PageDialog | None = None) -> ActionResult:
        """The result of an action that opened a dialog or closed its own tab (a popup finishing)."""
        if opened is not None:
            detail = (f"the {kind.value} opened {_a(opened.type)} {opened.type} dialog: "
                      f"{json.dumps(opened.message[:300], ensure_ascii=False)}. The page is frozen until it is "
                      f"answered: call browser_dialog(accept=...). Unanswered, it is {opened.automatic} automatically "
                      f"in {opened.seconds_left()} s")
        else:
            if not _open_pages(page):
                # No tab is left: the browser itself went away (it crashed or was stopped), not a
                # popup finishing. What was typed or clicked may have only partly happened.
                raise EngineError(f"the browser closed during the {kind.value} on {ref}, so it may be half "
                                  "done. Take a snapshot to see what reached the page before you go on")
            detail = f"the tab closed during the {kind.value} (a popup or sign-in window finishing)"
        notes = await self._side_effects(page, pages_before, choosers_before, guard, ref)
        why = _DIALOG_OPEN if opened is not None else "the tab closed; take a snapshot of the tab the browser acts in now"
        return ActionResult(ok=True, action=kind, ref=ref, url=_url_of(page), signal=Signal.UNKNOWN, changed=None,
                            detail="; ".join([detail, *notes]), signal_detail=why)

    async def _side_effects(self, page: Page, pages_before: set[Any], choosers_before: int,
                            guard: DialogGuard | None, ref: str) -> list[str]:
        notes: list[str] = []
        new = [p for p in _open_pages(page) if p not in pages_before]
        if new:
            await quietly(asyncio.wait_for(new[-1].wait_for_load_state("domcontentloaded"),
                                           NEW_TAB_SETTLE_MS / 1000))
            notes.append(f"a new tab opened ({_url_of(new[-1]) or 'loading'}) and the browser now acts in it; "
                         "browser_tabs lists the tabs")
        elif page.is_closed():
            rest = _open_pages(page)
            if rest:
                notes.append("the browser now acts in the tab that opened it (or the last one used); "
                             "take a snapshot to see it")
        if guard is not None and guard.chooser_count(page) > choosers_before:
            notes.append(f"it opened a file chooser (not shown on screen): attach files with "
                         f"browser_upload on ref {ref} and file ids from browser_files")
        return notes

    # ---- act internals -----------------------------------------------------------------

    async def _resolve(self, page: Page, ref: str) -> ElementHandle:
        stale = RefNotFound(f"ref {ref} is not on the page (stale after a navigation or a newer snapshot). "
                            "Take a new snapshot.")
        locator = page.locator(f"aria-ref={ref}")
        try:
            if await locator.count() == 0:
                raise stale
            handle = await locator.element_handle(timeout=RESOLVE_TIMEOUT_MS)
        except RefNotFound:
            raise
        except Exception as e:
            raise stale from e
        if handle is None:
            raise stale
        return handle

    async def _perform(self, page: Page, target: ElementHandle, kind: ActionKind, text: str | None) -> bool | None:
        if kind is ActionKind.CLICK:
            await human.click(page, target)
        elif kind is ActionKind.HOVER:
            await human.hover(page, target)
        elif kind is ActionKind.SCROLL:
            return await human.scroll_into_view(page, target)
        elif kind is ActionKind.SELECT:
            await human_fields.select_option(page, target, text or "")
        elif kind in (ActionKind.TYPE, ActionKind.FILL):
            if await human_fields.editable_kind(target) is None:
                raise InvalidRequest(f"{kind.value} needs a text field; this ref is not one")
            await human_fields.focus_field(page, target, caret_to_end=kind is ActionKind.TYPE)
            if kind is ActionKind.FILL:
                await human_fields.clear_field(page, target)
            await human.type_text(page, text or "", typos=await human_fields.typos_fit(target))
        elif kind is ActionKind.PRESS:
            if not await human_fields.is_focused(target):
                if await human_fields.editable_kind(target) is not None:
                    await human_fields.focus_field(page, target)
                else:
                    await target.focus()  # like arriving with Tab: focus without a click
            await human.press(page, text or "")
        return None

    async def _read_back(self, page: Page, target: ElementHandle, before: dict[str, str], kind: ActionKind,
                         url_before: str) -> tuple[bool | None, str, dict[str, str] | None]:
        """Poll the target's own state until it changes (then let it settle) or time runs out."""
        loop = asyncio.get_running_loop()
        budget = self.readback_timeout_ms if kind is not ActionKind.HOVER else min(self.readback_timeout_ms, 1_000)
        deadline = loop.time() + budget / 1000
        after: dict[str, str] | None = before
        while True:
            after = await _state(target)
            if after is None or diff_state(before, after):
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(READBACK_POLL_MS / 1000)
        if after is not None and diff_state(before, after):
            await asyncio.sleep(READBACK_SETTLE_MS / 1000)
            after = await _state(target) or after
        if after is None:
            if _strip_fragment(page.url) != _strip_fragment(url_before):
                return None, f"the page navigated to {page.url}", None
            return None, "the target left the page, so its state cannot be read back", None
        changes = diff_state(before, after)
        if changes:
            return True, "; ".join(changes), after
        return False, "no change in the target's own state", after
