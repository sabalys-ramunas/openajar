"""The form a submit would send, read before browser_act presses it, and checked again right
before the press (openajar.node.fields, openajar.node.human).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openajar.core.errors import FormChanged, OpenajarError, RefNotFound
from openajar.core.models import ActionKind, ControlState, LaneState
from openajar.core.tools import ToolContext
from openajar.node import egress, human
from openajar.node.claims import READ
from openajar.node.fields import FormUnreadable, read_form_diff
from openajar.node.snapshot import is_check_ref
from openajar.node.tools.outline import active_page
from openajar.node.tools.session import driven_session

log = logging.getLogger(__name__)


REF_COUNT_S = 2.0          # how long a stale-ref check may take before a click


async def form_before_submit(ctx: ToolContext, on: str, ref: str, action: ActionKind,
                             text: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """The form an act on `ref` that can submit one would submit, as openajar.node.fields.form_diff: the
    fields it sends and how each differs from what the page loaded with (never a secret value),
    and, when it posts to a host the commit gate does not read (ungated_post_host), that host.
    Read only on a warm browser the caller may use and agents control (a ref needs a snapshot, so
    the browser is warm); (None, None) for anything else, and when the click submits no form.

    When the page cannot be read, nobody knows whether the click submits a form or what it would
    send, so the call is refused (form_changed, nothing clicked) rather than asked for or run with
    an approval that binds no form: the agent takes a snapshot and calls again, which reads afresh.

    Which acts can submit is the commit gate's own definition (egress _press_mode, which ActGate's
    submit_like reads too): a click, a press of Enter, NumpadEnter, Space or any of them with a
    modifier, a type or fill whose text holds a newline, and a select. One definition, so no key the
    gate counts as a submit gets past Ask me first unread (review of the fix wave, finding 2)."""
    mode = egress._press_mode(action.value, text)
    if mode is None or is_check_ref(ref):  # a check's checkbox is in its vendor's frame: no form of the page
        return None, None
    allowed = getattr(ctx, "can_access_lane", None)
    if callable(allowed) and not allowed(on):
        return None, None
    lanes = ctx.runtime.lanes
    try:
        info = lanes.get(on)
    except OpenajarError:
        raise
    except Exception:  # no lane to read: the act itself says why
        log.debug("could not look up the browser before a submit", exc_info=True)
        return None, None
    if info.state not in (LaneState.WARM, LaneState.ACTIVE) or info.control.state is not ControlState.AGENT:
        return None, None
    try:
        async with driven_session(ctx, on, mode=READ) as session:  # a read: it shares with other reads
            page = active_page(session)
            target = page.locator(f"aria-ref={ref}")
            try:
                form = await read_form_diff(page, target, mode)
            except FormUnreadable:
                if await ref_gone(target):  # an older snapshot's ref: the element left the page
                    raise RefNotFound(f"ref {ref} is no longer on the page (it is from an older snapshot); nothing "
                                      "was clicked. Take a new snapshot and pick the ref from it.") from None
                raise
            return form, ungated_post_host(egress.guard_for(session), getattr(page, "url", "") or "", form)
    except OpenajarError:
        raise
    except Exception:
        log.debug("could not read the form before a submit", exc_info=True)
        raise FormChanged(f"the form {ref} may submit could not be read for the approval; nothing was clicked. "
                          "Take a new snapshot and call browser_act again") from None


async def ref_gone(target: Any) -> bool:
    """Whether a ref's locator matches nothing on the page now (a page that does not answer is not gone)."""
    count = getattr(target, "count", None)
    if not callable(count):
        return False
    return await human.quietly(count(), None, timeout=REF_COUNT_S) == 0


def ungated_post_host(guard: Any, page_url: str, form: dict[str, Any] | None) -> str | None:
    """The host a form posts to when the commit gate does not read it (no effect rule names it), so
    Ask me first can hold the submit before the click (browser_act); None for a GET form, a host
    the gate reads, and a host the vault's login window is open for (a login is no publish, 8.11).
    A target the node cannot read is treated as one the gate does not read (fail closed)."""
    from urllib.parse import urljoin

    if form is None or str(form.get("method") or "").strip().lower() != "post":
        return None
    try:
        url = urljoin(page_url, str(form.get("action") or ""))
    except ValueError:
        url = ""
    host = egress.host_of(url)
    if not host:
        return "an address the node cannot read"
    rules = guard.rules if guard is not None else egress.EFFECT_RULES
    if egress.unknown_rule(rules, host, url, "POST") is not None:
        return None  # the commit gate holds what it sends there, and asks when the lane asks first
    if guard is not None and guard.login_open(host):
        return None
    return host


FORM_SHOWN_ONLY = ("changed", "was", "was_checked")  # what the page loaded with: shown, never sent


def form_sent(form: dict[str, Any]) -> dict[str, Any]:
    """What a form diff says the submit sends: where to, how, and each field's value, compared by
    the keyed digest of the whole raw value (`bind`: past the shown text, unmasked, a hidden or
    password field too) and the form's own digest (the whole target URL, method and encoding). A
    re-render that keeps every value, or only changes what the page loaded with, sends the same."""
    rows = [{k: v for k, v in row.items() if k not in FORM_SHOWN_ONLY} for row in form.get("fields") or []]
    return {"action": form.get("action"), "method": form.get("method"), "bind": form.get("bind"),
            "fields": sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))}


# Acts that write the value of the field they act on: that field's new value is the act itself.
WRITES_OWN_FIELD = frozenset({ActionKind.TYPE, ActionKind.FILL, ActionKind.SELECT})


def _without(form: dict[str, Any], name: str | None) -> dict[str, Any]:
    if not name:
        return form
    return {**form, "fields": [row for row in form.get("fields") or [] if row.get("name") != name]}


def _form_change(was: dict[str, Any], now: dict[str, Any] | None, own: str | None = None) -> str | None:
    """How `now` sends something other than `was`, in words (labels, never values); None when it
    sends the same. `own`: the name of the field the act itself writes, left out of the comparison
    (a select's arrow keys change it before its Enter, a typed newline comes after the text)."""
    if now is None:
        return "the form could not be read again, or the click no longer submits it"
    before, after = form_sent(_without(was, own)), form_sent(_without(now, own))
    if before == after:
        return None
    if (before["action"], before["method"], before["bind"]) != (after["action"], after["method"], after["bind"]):
        return "it now goes somewhere else"

    def rows(form: dict[str, Any]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in form["fields"]:
            out.setdefault(str(row.get("label")), []).append(json.dumps(row, sort_keys=True, default=str))
        return out

    a, b = rows(before), rows(after)
    labels = sorted(label for label in a.keys() | b.keys() if sorted(a.get(label, [])) != sorted(b.get(label, [])))
    named = ", ".join(f'"{label[:40]}"' for label in labels[:5]) + (" and more" if len(labels) > 5 else "")
    return f"fields changed: {named}"


async def check_form_unchanged(page: Any, ref: str, action: ActionKind, approved: dict[str, Any],
                               text: str | None = None) -> None:
    """Read the form again inside the act's own browser session, right before the click, and refuse
    when it sends other than `approved` (the form the approval bound): between a read in its own
    session and the click, a page could change a field the owner never saw (AUD-10). Runs again
    just before the press (openajar.node.human.before_press), after the scroll, pause and glide."""
    try:
        now = await read_form_diff(page, page.locator(f"aria-ref={ref}"),
                                   egress._press_mode(action.value, text) or "")
    except FormUnreadable:
        now = None
    own = approved.get("own") if ActionKind(action) in WRITES_OWN_FIELD else None
    change = _form_change(approved, now, own)
    if change is not None:
        raise FormChanged(f"the form {ref} submits changed after it was read for the approval ({change}); nothing "
                          "was clicked. Take a new snapshot and call browser_act again")
