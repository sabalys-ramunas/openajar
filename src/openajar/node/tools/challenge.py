"""browser_challenge: one explicit call to get an environment's browser past a site's "are you human" check, in the
order openajar.node.solver.steps gives (wait, click, the owner's solver, the human). In the `browser` toolset, not
the default list: an agent learns of it from the snapshot's line for a check, when the owner set up a solver.

An account's browser never gets here: the handler refuses a lane that is not an environment's (check_for_human),
and the steps refuse a page whose context is not an environment's browser on its machine (checks.allowed).
When nothing got the browser past the check, or the call ended early on a page that showed one (its deadline, a
cancel, an error), the handler holds it for the human, as any check on a browser without a solver does, so
browser_wait_handback waits for the person.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from openajar.core.errors import CheckForHuman
from openajar.core.models import Effect, Signal
from openajar.core.tools import ToolContext
from openajar.node.tools.guarding import guarded, with_blocks
from openajar.node.tools.inputs import lane_id
from openajar.node.tools.session import driven_session, label_page_text

log = logging.getLogger(__name__)

NOT_HERE = ("this browser's checks are for its human: browser_challenge works only on an environment's own browser "
            "on its machine")
NEXT = {  # by openajar.node.solver.steps' names for the step that ended it
    "nothing": "Go on with your task.",
    "wait": "Go on with your task.",
    "click": "Go on with your task.",
    "solver": ("Go on with your task. The token is in the page's form: the site checks it when the form is sent, "
                   "so read the page after you send it."),
    "human": ("Tell your human the browser needs them for a site's check, give them its live view (links.browser), "
                  "and wait with browser_wait_handback. Do not call browser_challenge again for this check."),
}


async def browser_challenge(ctx: ToolContext, *, on: str) -> dict[str, Any]:
    """Get your environment's browser past a site's "are you human" check, in this order: wait for it to pass by
    itself, click its checkbox once, ask the owner's solving service (only when the owner set one up), else hand
    the browser to your human. `ended_by` says which step ended it (nothing, wait, click,
    solver or human) and `steps` what each did. Call it once per check, never in a loop. Effect: draft.
    """
    from openajar.node.interaction import checks  # the browser stack, only when it runs
    from openajar.node.solver import steps

    on = lane_id(on)
    if not on.startswith("env/"):
        raise CheckForHuman(NOT_HERE)
    await ctx.authorize(Effect.DRAFT, lane_id=on, summary=f"get past a site's check on {on}", content={})
    interaction = ctx.runtime.interaction
    home = getattr(getattr(ctx.runtime, "config", None), "home", None)
    progress = steps.Progress()
    page = None
    try:
        async with driven_session(ctx, on) as page:
            if not checks.allowed(page):
                raise CheckForHuman(NOT_HERE)
            async with guarded(ctx, on, page) as blocks:
                outcome = await steps.get_past(interaction, page, browser=on, home=home, progress=progress)
    except BaseException:
        if progress.saw_check:  # cut off (its deadline, a cancel) or failed on a page with a check: the human's
            await asyncio.shield(_hold(ctx, on, page))
        raise
    if outcome.ended_by == steps.HUMAN:
        await _hold(ctx, on, page)
    label_page_text(ctx, "browser_challenge", getattr(page, "url", None))
    report = outcome.report
    out: dict[str, Any] = {"on": on, "ended_by": outcome.ended_by, "passed": outcome.passed, "steps": outcome.steps,
                           "url": getattr(page, "url", None), **report.public(), "next": NEXT[outcome.ended_by]}
    out["signal"] = getattr(out.get("signal"), "value", out.get("signal"))
    if out.get("signal_detail") is None:
        out.pop("signal_detail", None)
    if out.get("account") is None:
        out.pop("account", None)
    return with_blocks(out, blocks)


async def _hold(ctx: ToolContext, on: str, page: Any) -> None:
    """Hold the browser for its human, as a check on a browser with no solver does (openajar.node.holds)."""
    lanes = getattr(ctx.runtime, "lanes", None)
    report_signal = getattr(lanes, "report_signal", None)
    if report_signal is None:
        return
    try:
        await report_signal(on, Signal.CAPTCHA, detail=urlsplit(getattr(page, "url", "") or "").hostname)
    except Exception:
        log.exception("%s: could not hold the browser for its human after a check", on)
