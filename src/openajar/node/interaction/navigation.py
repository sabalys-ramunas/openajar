"""navigate: open a URL directly, or, mid-visit, follow the page's own link to it with a click.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from openajar.core.errors import EngineError, OpenajarError
from openajar.core.logs import url_for_log
from openajar.core.models import NavigateResult
from openajar.core.text import first_line
from openajar.node import human
from openajar.node.human import quietly
from openajar.node.interaction.autocheck import cloudflare_check, past_automatic_check
from openajar.node.interaction.dialogs import _DialogOpened, _pending_error, dialog_guard
from openajar.node.interaction.pages import _Navigations, _title, _url_of
from openajar.node.interaction.urls import _strip_fragment, check_url, same_origin, through_vm

log = logging.getLogger("openajar.interaction")
Page = Any           # playwright.async_api.Page
ElementHandle = Any  # playwright.async_api.ElementHandle


LINK_START_S = 4.0  # a followed link that starts no navigation by then was taken by a script
LINK_QUIET_S = 0.5   # then no request for this long: the script is done, so a goto cannot race it
LINK_SETTLE_S = 6.0  # at most this long waiting for that quiet


# The link a person would click to reach `target`: an <a href> of the main document whose address,
# without its fragment, is the target's; shown (not hidden, transparent or tiny), inside the
# viewport's width (the pointer can only wheel up and down to it), opening in this tab (its own
# target, or else the document's <base target>), not a download and with no inline onclick (a
# script that takes the click could do something else). An exact match before one that differs in
# its fragment, then the one in view or nearest to it. _link_leads_to checks it again in Python
# right before the press.
_LINK_TO_JS = """
(target) => {
  const parse = (u) => { try { return new URL(u, document.baseURI); } catch (e) { return null; } };
  const want = parse(target);
  if (!want) return null;
  const vw = window.innerWidth, vh = window.innerHeight;
  const base = document.querySelector('base[target]');
  const baseTarget = base ? base.getAttribute('target') : '';
  let best = null, bestScore = Infinity;
  for (const a of document.querySelectorAll('a[href]')) {
    if (typeof a.href !== 'string' || a.hasAttribute('download') || a.hasAttribute('onclick')) continue;
    const t = (a.hasAttribute('target') ? a.getAttribute('target') : baseTarget).trim().toLowerCase();
    if (t && t !== '_self' && t !== '_top') continue;
    const url = parse(a.href);
    if (!url || url.href !== want.href) continue;
    if (a.checkVisibility && !a.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const r = a.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.right <= 0 || r.left >= vw) continue;
    const off = r.bottom < 0 ? -r.bottom : (r.top > vh ? r.top - vh : 0);
    if (off < bestScore) { best = a; bestScore = off; }
  }
  return best;
}
"""


# Where a link leads, read right before the press (the page may have changed it since it was found):
# what _link_leads_to decides on. `want` is the target as the browser writes it, without fragment.
_LINK_FACTS_JS = """
(a, target) => {
  let want = null;
  try { want = new URL(target, document.baseURI).href; } catch (e) {}
  const base = document.querySelector('base[target]');
  return {
    want,
    connected: a.isConnected,
    href: typeof a.href === 'string' ? a.href : '',
    target: a.hasAttribute('target') ? a.getAttribute('target') : null,
    baseTarget: base ? base.getAttribute('target') : null,
    download: a.hasAttribute('download'),
    onclick: a.hasAttribute('onclick'),
  };
}
"""


def _link_leads_to(facts: Any) -> str | None:
    """The address a link with these facts (_LINK_FACTS_JS) opens in this tab, when that is exactly
    the target, fragment included; None when it leads anywhere else, opens another tab (its own target or
    the document's <base target>), downloads, carries an inline onclick, left the page, or could not
    be read."""
    if not isinstance(facts, dict) or not facts.get("connected") or facts.get("download") \
            or facts.get("onclick"):
        return None
    want = facts.get("want")
    where = facts.get("target")
    if where is None:
        where = facts.get("baseTarget")
    if not isinstance(want, str) or not want or str(where or "").strip().lower() not in ("", "_self", "_top"):
        return None
    return want if str(facts.get("href") or "") == want else None


class _LinkChanged(Exception):  # internal control flow: the link no longer leads to the target
    pass


class NavigateMixin:
    """navigate: open a URL, or follow a link to it the way a person would."""

    # ---- Interaction -------------------------------------------------------------------

    async def navigate(self, page: Page, url: str, *, follow_link: bool = False,
                       on_click: Callable[[], None] | None = None) -> NavigateResult:
        """Open `url` directly. With `follow_link`, and mid-visit when the page shows a link to
        exactly that address, follow it with a click as a person would (the site then sees a Referer
        and a same-origin fetch, not an address typed into the bar); otherwise, or when the click
        does not take the page there, go there directly once whatever the click started has finished
        (never two navigations at once). Either way the page ends on the checked address, or the
        error says where it is. On Cloudflare's check ("Just a moment...") it waits up to
        autocheck.AUTO_CHECK_S for the check to let the browser through by itself, as a person waits,
        before it reads the signal. A click runs the page's own handlers, so it is an act: only a caller
        allowed to act on the lane may ask for one (tools.browser_navigate). `on_click` runs right
        before the mouse goes down, so the caller can record the click whatever comes of it (the page
        may land elsewhere, or the navigation fail); it does not run when no press was made."""
        target = check_url(url, allow_private=self.allow_private_targets or through_vm(page))
        state = self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, f"navigate to {target}")
        await human.still(page)
        await human.place_pointer(page)  # a fresh tab: the pointer leaves the corner before a site can see it
        link = await self._link_to(page, target) if follow_link else None
        led_to: str | None = None
        try:
            followed = False
            if link is not None:
                followed, led_to = await self._race(page, self._follow(page, target, link, on_click), guard)
            if followed:
                status = self._status_for_current(page)
            else:
                if led_to is not None:
                    log.info("the link to %s led to %s; opening the address directly", url_for_log(target),
                             url_for_log(led_to))
                response = await self._race(
                    page, page.goto(target, wait_until="domcontentloaded", timeout=self.navigate_timeout_ms), guard)
                status = response.status if response is not None else None
                if response is not None:
                    state.status, state.status_url = status, response.url
        except _DialogOpened as opened:
            raise _pending_error(opened.dialog, f"finish the navigation to {target} yet") from None
        except OpenajarError:
            raise
        except Exception as e:
            # the click on the page's link took the page elsewhere first: say so, it was never checked
            elsewhere = f" (the page's link had taken it to {url_for_log(led_to)})" if led_to is not None else ""
            raise EngineError(f"navigation to {target} failed: {first_line(e, 300)}{elsewhere}") from e
        finally:
            if link is not None:
                await quietly(link.dispose(), timeout=2.0)
        report = await self.report(page)
        if cloudflare_check(report):  # "Just a moment...": give it the seconds a person gives it
            report = await past_automatic_check(page, report, self.report)
            status = self._status_for_current(page) or status
        title = await _title(page)
        human.begin_idle(page)
        return NavigateResult(url=page.url, title=title, status=status, **report.public())

    async def _link_to(self, page: Page, target: str) -> ElementHandle | None:
        """The link on the page to `target` (_LINK_TO_JS), when the target is another page of the
        site the tab shows. None otherwise, or when the page cannot be read."""
        current = _url_of(page)
        if not same_origin(current, target) or _strip_fragment(current) == _strip_fragment(target):
            return None
        handle = await quietly(page.evaluate_handle(_LINK_TO_JS, target), None, timeout=3.0)
        if handle is None:
            return None
        element = handle.as_element()
        if element is None:
            await quietly(handle.dispose(), timeout=2.0)
        return element

    async def _follow(self, page: Page, target: str, link: ElementHandle,
                      on_click: Callable[[], None] | None = None) -> tuple[bool, str | None]:
        """Click `link` like a person and wait until the page it leads to has loaded: (True, None).
        (False, None) when the click was not made (the link changed, is covered or out of sight) or
        started nothing; (False, url) when it took the page somewhere else, now loaded at `url`. In
        both the caller then goes to the address directly, after anything the click started has
        finished. A navigation to the target that fails raises, as page.goto would."""
        want = _link_leads_to(await quietly(link.evaluate(_LINK_FACTS_JS, target), None, timeout=3.0))
        if want is None:
            return False, None
        pressed = False

        async def still_leads_there() -> None:
            # right before the mouse down: page scripts had the scroll, the pause and the glide to
            # change the link's href or target, or the document's <base target>
            nonlocal pressed
            now = await quietly(link.evaluate(_LINK_FACTS_JS, target), None, timeout=3.0)
            if _link_leads_to(now) != want:
                raise _LinkChanged
            pressed = True
            if on_click is not None:
                on_click()

        navs = _Navigations(page, target, _strip_fragment(want))
        page.on("request", navs.on_request)
        page.on("framenavigated", navs.on_navigated)
        page.on("requestfailed", navs.on_failed)
        try:
            try:
                with human.before_press(still_leads_there):
                    await human.click(page, link)
            except _LinkChanged:
                log.info("the link to %s changed before the press; opening the address directly",
                         url_for_log(target))
            except OpenajarError as e:
                if not isinstance(e, human.TargetNotVisible | human.TargetCovered):
                    raise
                log.info("could not click the link to %s (%s); opening it directly", url_for_log(target), e.code)
            except Exception as e:  # noqa: BLE001 - the link left the page under the pointer
                log.info("could not click the link to %s (%s); opening it directly", url_for_log(target),
                         first_line(e, 300))
            if not pressed and not navs.began.done():
                return False, None
            if not navs.began.done():
                try:
                    await asyncio.wait_for(asyncio.shield(navs.began), LINK_START_S)
                except TimeoutError:
                    # nothing yet: a script may still be at work on the click (a fetch, then a
                    # navigation). Let the network go quiet first, so a late navigation and the
                    # caller's goto do not race.
                    await self._quiet_unless(page, navs.began)
                    if not navs.began.done():
                        return False, None
            async with asyncio.timeout(self.navigate_timeout_ms / 1000):
                try:
                    await navs.landed
                except EngineError:
                    if navs.ours:
                        raise
                    return False, None  # it went elsewhere and failed there, so the page stayed: the goto follows
                if not navs.ours:
                    return False, _url_of(page)
                await page.wait_for_load_state("domcontentloaded")
            return True, None
        finally:
            page.remove_listener("request", navs.on_request)
            page.remove_listener("framenavigated", navs.on_navigated)
            page.remove_listener("requestfailed", navs.on_failed)
            if navs.landed.done() and not navs.landed.cancelled():
                navs.landed.exception()  # retrieved, so asyncio does not warn about a failure nobody awaited

    async def _quiet_unless(self, page: Page, began: asyncio.Future[None]) -> None:
        """Wait until no request has started or ended for LINK_QUIET_S, or `began` resolves, at most
        LINK_SETTLE_S."""
        state = self._track(page)
        deadline = time.monotonic() + LINK_SETTLE_S
        while not began.done() and time.monotonic() < deadline:
            busy, idle = state.network()
            if busy == 0 and idle >= LINK_QUIET_S:
                return
            await asyncio.wait({began}, timeout=0.1)
