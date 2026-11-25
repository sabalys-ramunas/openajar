"""How long one tool call may hold its lane: the overall deadline of every browser tool, in one place.

Playwright's own timeouts (a goto's 45 s, an aria snapshot's 15 s) cover single steps. A content
process that stops answering (the 2026-11-10 re-test: a LinkedIn tab at 1430 MB of its 1536 MB
ceiling) hangs a step that has none (an evaluate, a title read, a settle wait, the read-back), and
the call then held the lane for every other caller until a person killed the browser.

So every tool call that takes a lane through openajar.node.tools.session.ctx_session gets a deadline from
this table, and LaneManagerImpl.using() enforces it (openajar.node.lanes.access): when it passes,
the call is interrupted at its next await, its turn on the lane is released, and the event is
logged with the lane, the tool and how long it ran. Then the page gets a trivial probe
(PROBE_S); a browser that does not answer it is closed (gracefully, then its process group is
killed) and the lane goes cold, so the next call opens it fresh.

The table, in seconds (what each tool legitimately needs, with room to spare):

  browser_navigate        120   goto (45) + the outline after it (15) + settle and read-back
  browser_snapshot         60   the aria snapshot (15) + the signal read
  browser_act             120   + TYPE_S_PER_CHAR for each character it types (human-paced)
  browser_scroll           60
  browser_screenshot       60   the screenshot (15) + scaling
  browser_wait             60   + its own wait_s (at most 60)
  browser_tabs             60
  browser_dialog           60
  browser_console          60
  browser_network          60
  browser_upload          600   large files go through the file chooser
  vault_fill              120   + TYPE_S_PER_CHAR for the secret it types
  login                  1080   a code card waits up to 900 s for the owner's code (vault relay limit)
  browser_challenge       240   a wait (20) + a click and its answer (20) + the owner's solver (120) + settling;
                                under the 300 s an MCP client waits
  any other tool          120   (DEFAULT_S)

Not on the table, so no overall deadline: flow and connector runs (mode "run", they hold the lane
for the whole run under their own step timeouts), the node's own work (the exit IP probe,
quarantine, shutdown), and a person's own calls (the panel, assisted login).
"""

from __future__ import annotations

DEFAULT_S = 120.0
TYPE_S_PER_CHAR = 0.6   # human-paced typing with its pauses, with room (openajar.node.human)
PROBE_S = 5.0           # a page that does not evaluate `1` in this long has stopped answering
CLOSE_WAIT_S = 30.0     # at most this long for a stuck call to end after it was interrupted
# A call whose deadline is at most WALL_FOR_S also ends WALL_S after it began, counting its wait in the
# queue and the browser's start: an MCP client gives up on a call at 300 s (openajar.core.progress). A
# browser_navigate on an overloaded 2-CPU machine once waited 300 s before its own deadline even began.
WALL_S = 210.0
WALL_FOR_S = 150.0

TOOL_DEADLINES_S: dict[str, float] = {
    "browser_navigate": 120.0,
    "browser_snapshot": 60.0,
    "browser_act": 120.0,
    "browser_scroll": 60.0,
    "browser_screenshot": 60.0,
    "browser_wait": 60.0,
    "browser_tabs": 60.0,
    "browser_dialog": 60.0,
    "browser_console": 60.0,
    "browser_network": 60.0,
    "browser_upload": 600.0,
    "vault_fill": 120.0,
    "login": 1080.0,
    "browser_challenge": 240.0,
}


def deadline_for(tool: str | None, *, extra_s: float = 0.0) -> float:
    """The overall deadline of one call of `tool`, plus `extra_s` the call itself needs (browser_wait's
    wait_s, browser_act's typing)."""
    base = TOOL_DEADLINES_S.get(tool or "", DEFAULT_S)
    return base + max(0.0, float(extra_s or 0.0))


def typing_s(text: str | None) -> float:
    """What typing `text` at a person's pace may take."""
    return len(text or "") * TYPE_S_PER_CHAR
