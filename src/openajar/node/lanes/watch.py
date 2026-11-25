"""SignalWatch: the Interaction wrapper that tells the lane manager every signal a page read
carries, so a captcha, a checkpoint or a rate limit holds the lane whoever caused it (openajar.node.lanes,
"Holds"). Installed once where the runtime is composed: `interaction = lanes.watch(interaction)`.
A bad signal the manager could not record a hold for fails the call that read it (agent_paused)
instead of passing its result on: it never goes on as if the page had shown nothing.

One exception, and only where an agent may click a check (interaction.checks.allowed: an environment's
browser on its machine, never an account's): a check that asks for one click on a checkbox (sites.CHECKBOX_DETAILS)
is not held, because the snapshot gives the agent its ref. And when a solving service the owner set up
(openajar.node.solver) could take the page's widget (solver.steps.could_take: a widget in the page's own document,
a service that takes its kind), an image puzzle of hCaptcha or reCAPTCHA (sites.PUZZLE_DETAILS) is not held either,
so the agent can call browser_challenge, which holds the browser for the human itself when the service does not get
it past. Every other signal, Cloudflare's full-page check included, holds as everywhere."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openajar.core.errors import AgentPaused
from openajar.core.models import Signal
from openajar.core.signals import QUIET
from openajar.node.interaction import checks
from openajar.node.interfaces import Page
from openajar.node.lanes.base import _host
from openajar.node.sites import CHECKBOX_DETAILS, PUZZLE_DETAILS

if TYPE_CHECKING:
    from openajar.node.lanes.manager import LaneManagerImpl

log = logging.getLogger("openajar.lanes")

# ---- the signal watch -----------------------------------------------------------------

class SignalWatch:
    """An Interaction that tells the lane manager every signal it reads, so a bad signal holds
    the lane whoever caused it: an agent's tool call, a flow step or a schedule. Install it
    once, where the runtime is composed: `interaction = lanes.watch(interaction)`. Everything
    else passes through to the wrapped Interaction."""

    def __init__(self, inner: Any, lanes: LaneManagerImpl):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_lanes", lanes)

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "_lanes"):  # not set yet (copy, pickle): never recurse
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    @property
    def inner(self) -> Any:
        return self._inner

    async def navigate(self, page: Page, url: str, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.navigate(page, url, *args, **kwargs)
        await self._seen(page, getattr(result, "signal", None), getattr(result, "url", None) or url, _detail(result))
        return result

    async def act(self, page: Page, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.act(page, *args, **kwargs)
        await self._seen(page, getattr(result, "signal", None), getattr(result, "url", None), _detail(result))
        return result

    async def signal(self, page: Page, *args: Any, **kwargs: Any) -> Any:
        value = await self._inner.signal(page, *args, **kwargs)
        await self._seen(page, value, getattr(page, "url", None))
        return value

    # Every read that tells the agent what the page is holds the lane the same way: a challenge an
    # agent can see is one it could act on (QA 2026-11-07, finding 1).

    async def snapshot(self, page: Page, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.snapshot(page, *args, **kwargs)
        await self._seen(page, getattr(result, "signal", None), getattr(result, "url", None) or getattr(page, "url", None),
                         _detail(result))
        return result

    async def wait(self, page: Page, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.wait(page, *args, **kwargs)
        await self._seen(page, getattr(result, "signal", None), getattr(result, "url", None) or getattr(page, "url", None),
                         _detail(result))
        return result

    async def upload(self, page: Page, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.upload(page, *args, **kwargs)
        await self._seen(page, getattr(result, "signal", None), getattr(result, "url", None) or getattr(page, "url", None))
        return result

    async def screenshot(self, page: Page, *args: Any, **kwargs: Any) -> Any:
        """A picture carries no signal of its own: the page's is read beside it (a read only)."""
        png = await self._inner.screenshot(page, *args, **kwargs)
        if self._lanes.lane_for_page(page) is not None:
            detail = None
            try:
                if checks.allowed(page):  # its detail says whether it is a check the agent may click
                    report = await self._inner.report(page)
                    value, detail = report.signal, report.detail
                else:
                    value = await self._inner.signal(page)
            except Exception:  # noqa: BLE001 - a dialog or a closing tab: the picture still stands
                value = None
            await self._seen(page, value, getattr(page, "url", None), detail)
        return png

    async def _seen(self, page: Page, signal: Any, url: Any, detail: str | None = None) -> None:
        if signal is None:
            return
        if detail in CHECKBOX_DETAILS and checks.allowed(page):
            return  # an environment's browser: the agent has the check's ref (interaction.checks)
        if detail in PUZZLE_DETAILS and checks.allowed(page) and await checks.solver_could_take(page, signal, detail):
            return  # an environment's browser where the owner's solver could take it now: browser_challenge holds
        lane_id = self._lanes.lane_for_page(page)
        if lane_id is None:
            return
        try:
            await self._lanes.report_signal(lane_id, signal, detail=_host(url))
        except Exception:
            log.exception("%s: could not record signal %s", lane_id, signal)
            if _quiet(signal):
                return
            # A bad signal the lane could not be held for: the call that read it stops here, so no
            # agent, flow step or schedule goes on past a challenge as if nothing was seen.
            value = str(getattr(signal, "value", signal))[:40]
            raise AgentPaused(f"{lane_id} shows {value}, and openajar could not hold the browser for a human, so "
                              "this call stops here. Do not retry; tell your user.") from None


def _detail(result: Any) -> str | None:
    detail = getattr(result, "signal_detail", None)
    return detail if isinstance(detail, str) else None


def _quiet(signal: Any) -> bool:
    """A signal that never holds a lane (QUIET). One that cannot be read as a Signal is not quiet."""
    try:
        return Signal(getattr(signal, "value", signal)) in QUIET
    except ValueError:
        return False
