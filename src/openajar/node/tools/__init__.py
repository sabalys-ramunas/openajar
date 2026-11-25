"""The node's own MCP tools as ToolSpecs, plus the MCP
prompts for common jobs (the same recipes `howto` returns).

Browser tools use the `lanes` service, browser tools also `interaction`; both read `config`.
Every handler authorizes before it acts: READ for reads, DRAFT for browser_act. Browser
tools run inside `lanes.using(on, principal_kind=..., holder=..., mode=...)`, so a browser a human
has taken over refuses agent calls at the same gate as flows and brain tasks.

Shared browsers (openajar.node.claims): `holder` is the caller (its principal, grant and MCP session),
and `mode` says whether the tool reads or acts. Reads change nothing another caller would notice
and share the browser, also while someone else claims it: browser_snapshot, browser_screenshot,
browser_wait, browser_tabs, browser_dialog. Everything else acts and goes one at a time: navigate,
act, scroll, upload, a tab switch, a dialog answer, vault login and fill, browser_close. browser_open only warms
the browser and never waits in the queue. browser_claim, browser_release and browser_queue are the claim tools.

Egress (openajar.node.egress): every browser tool tells the browser's egress guard who drives the
browser (driven_session). browser_navigate checks the URL and browser_act checks the link or form its
click or key press would follow before anything is sent; a navigation the guard stops during the
call fails the call with egress_refused, and anything else it refused is listed in the result as
`egress_blocked`.

Snapshots: every outline an agent is shown starts with a line saying it is the page's own
content (AUD-31). browser_act, browser_navigate and browser_wait take `snapshot` ("none",
"delta", "full"); the last outline each agent session saw of each tab is kept (snapshot.OUTLINES) so
"delta" can send only what changed. With a snapshot the result is text: the JSON result on
one line, then the outline, because an outline inside JSON costs an escape per quote.
"""

from __future__ import annotations

import functools
from typing import Any

from openajar.core.models import ControlState, Effect
from openajar.core.runtime import Runtime
from openajar.core.tools import PromptSpec, ToolContext, ToolSpec
from openajar.node.handback import attach_note, claim_note, unclaim_note
from openajar.node.tools.browser import (
    browser_act,
    browser_navigate,
    browser_screenshot,
    browser_scroll,
    browser_snapshot,
    trace_read_back,
)
from openajar.node.tools.challenge import browser_challenge
from openajar.node.tools.form_check import (
    check_form_unchanged,
    form_before_submit,
    form_sent,
    ungated_post_host,
)
from openajar.node.tools.guarding import ActMeter
from openajar.node.tools.inputs import NEEDS_TEXT, lane_id, normalize_ref, normalize_url, url_host
from openajar.node.tools.lanes import (
    lane_claim,
    lane_close,
    lane_open,
    lane_queue,
    lane_release,
    lane_summary,
    lane_view,
    lanes_list,
    ssh_host,
    viewer_dict,
    whoami,
)
from openajar.node.tools.outline import (
    DELTA_REFS,
    EMPTY_OUTLINE_WAIT_S,
    FULL_NOT_DELTA,
    UNTRUSTED,
    active_page,
    full_outline,
    main_only_snap,
    page_result,
    page_text,
    render_snapshot,
    signal_lines,
)
from openajar.node.tools.recipes import INSTRUCTIONS, RECIPES, howto, prompt_specs, recipe_text
from openajar.node.tools.session import (
    _takes,
    caller,
    check_control,
    ctx_session,
    driven_session,
    grant_check,
    label_page_text,
    lane_asks_first,
    lane_session,
    who,
)
from openajar.node.tools.wait import WAIT_MAX_S, WAIT_REFS, RefStateName, browser_wait, control_stop

# Names other packages import from here; they live in the modules above.
__all__ = [
    "DELTA_REFS",
    "EMPTY_OUTLINE_WAIT_S",
    "FULL_NOT_DELTA",
    "INSTRUCTIONS",
    "NEEDS_TEXT",
    "RECIPES",
    "UNTRUSTED",
    "WAIT_MAX_S",
    "WAIT_REFS",
    "ActMeter",
    "RefStateName",
    "_takes",
    "active_page",
    "browser_act",
    "browser_navigate",
    "browser_scroll",
    "browser_snapshot",
    "caller",
    "check_control",
    "check_form_unchanged",
    "control_stop",
    "ctx_session",
    "driven_session",
    "form_before_submit",
    "form_sent",
    "full_outline",
    "grant_check",
    "label_page_text",
    "lane_asks_first",
    "lane_claim",
    "lane_id",
    "lane_queue",
    "lane_release",
    "lane_session",
    "lane_summary",
    "lane_view",
    "lanes_list",
    "main_only_snap",
    "normalize_ref",
    "normalize_url",
    "page_result",
    "page_text",
    "recipe_text",
    "render_snapshot",
    "signal_lines",
    "ssh_host",
    "trace_read_back",
    "ungated_post_host",
    "url_host",
    "viewer_dict",
    "who",
]




def with_handback(handler: Any) -> Any:
    """A browser tool whose first result for each agent session after a hand back carries the
    hand-back note (openajar.node.handback) as `handback`. The note is claimed before the tool acts,
    so it says what the human changed, not what the call did; a call that fails gives it back."""

    @functools.wraps(handler)
    async def wrapped(ctx: ToolContext, **kwargs: Any) -> Any:
        on = kwargs.get("on")
        lanes = getattr(getattr(ctx, "runtime", None), "lanes", None)
        if not isinstance(on, str) or lanes is None:
            return await handler(ctx, **kwargs)
        keeps = getattr(lanes, "handback", None) is not None  # only a manager that keeps notes is asked
        note = await claim_note(lanes, on, who(ctx)) if keeps and _may_act(lanes, on) else None
        try:
            result = await handler(ctx, **kwargs)
        except BaseException:
            if note is not None:
                unclaim_note(lanes, on, who(ctx))
            raise
        if note is None:
            return result
        label_page_text(ctx, "handback")
        return attach_note(result, note)

    return wrapped


def _may_act(lanes: Any, on: str) -> bool:
    """Only an agent that may act now gets the note (one waiting for the browser would lose it)."""
    try:
        return lanes.get(on).control.state is ControlState.AGENT
    except Exception:  # noqa: BLE001 - an unknown lane: the tool says so itself
        return False


def tool_specs(runtime: Runtime) -> list[ToolSpec | PromptSpec]:
    lanes, browser = ("lanes",), ("browser",)
    return [
        ToolSpec(name="whoami", handler=whoami, lane_arg=None, tags=("node",), hidden_without="lanes"),
        ToolSpec(name="browsers_list", handler=lanes_list, lane_arg=None, tags=lanes, hidden_without="lanes"),
        ToolSpec(name="browser_open", handler=lane_open, tags=(*lanes, "idempotent", "changes_state"), hidden_without="lanes"),
        ToolSpec(name="browser_close", handler=lane_close, tags=(*lanes, "idempotent", "changes_state"), hidden_without="lanes"),
        ToolSpec(name="browser_view", handler=lane_view, tags=lanes, hidden_without="lanes"),
        ToolSpec(name="browser_claim", handler=lane_claim, tags=(*lanes, "changes_state"), hidden_without="lanes"),
        ToolSpec(name="browser_release", handler=lane_release, tags=(*lanes, "idempotent", "changes_state"), hidden_without="lanes"),
        ToolSpec(name="browser_queue", handler=lane_queue, tags=lanes, hidden_without="lanes"),
        ToolSpec(name="browser_navigate", handler=with_handback(browser_navigate), tags=(*browser, "changes_state"),
                 hidden_without="interaction"),
        ToolSpec(name="browser_snapshot", handler=with_handback(browser_snapshot), tags=browser,
                 hidden_without="interaction"),
        ToolSpec(name="browser_act", handler=with_handback(browser_act), effect=Effect.DRAFT, tags=browser,
                 hidden_without="interaction"),
        ToolSpec(name="browser_scroll", handler=with_handback(browser_scroll), tags=(*browser, "changes_state"),
                 hidden_without="interaction"),
        ToolSpec(name="browser_screenshot", handler=browser_screenshot, tags=browser,
                 hidden_without="interaction"),
        ToolSpec(name="browser_wait", handler=with_handback(browser_wait), tags=browser,
                 hidden_without="interaction"),
        ToolSpec(name="browser_challenge", handler=with_handback(browser_challenge), effect=Effect.DRAFT,
                 tags=(*browser, "changes_state"), hidden_without="interaction"),
        ToolSpec(name="howto", handler=howto, lane_arg=None, tags=("node",), hidden_without="lanes"),
        *prompt_specs(runtime),
    ]
