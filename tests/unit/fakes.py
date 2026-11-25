"""In-memory implementations of the node Protocols, for tests that run without a browser.

The lane managers live in openajar.dev.lanes (the same classes `openajar dev up` serves); they are
re-exported here so the tests keep their import path.
"""

from __future__ import annotations

import os
from typing import Any

from openajar.core.errors import RefNotFound
from openajar.core.models import ActionKind, ActionResult, NavigateResult, Signal, SnapshotResult
from openajar.dev.lanes import (  # noqa: F401  (re-exported for the tests)
    ControlFakeLaneManager,
    FakeLaneManager,
    FakePage,
    LegacyLaneManager,
)

# A tiny valid PNG (1x1, transparent).
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def mcp_port_for_tests() -> int:
    """0 (any free port) by default; scripts/node-test.sh sets the agent's own range on the node."""
    return int(os.environ.get("OPENAJAR_TEST_MCP_PORT_BASE", "0"))


class FakeInteraction:
    """Interaction over FakePage. Known refs are e1 (a link) and e2 (an upvote button)."""

    def __init__(self):
        self.fail_with: BaseException | None = None
        self.acts: list[tuple[str, ActionKind, str | None]] = []
        self.followed_links: list[bool] = []  # per navigate: may it click the page's link
        self.page_has_link = False  # the page links to the target, so a navigate allowed to follows it
        self.signal_value = Signal.OK
        self.waits: list = []
        self.wait_result = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc

    async def navigate(self, page: FakePage, url: str, *, follow_link: bool = False,
                       on_click=None) -> NavigateResult:
        self.followed_links.append(follow_link)
        if follow_link and self.page_has_link and on_click is not None:
            on_click()  # the press comes first: what fails after it was still a click
        self._maybe_fail()
        page.url, page.title = url, f"Fake page at {url}"
        return NavigateResult(url=url, title=page.title, status=200, signal=self.signal_value)

    async def snapshot(self, page: FakePage) -> SnapshotResult:
        self._maybe_fail()
        text = '- link "Comments" [ref=e1]\n- button "Upvote" [ref=e2] pressed=false'
        return SnapshotResult(url=page.url, title=page.title, text=text, ref_count=2)

    async def act(self, page: FakePage, ref: str, action: ActionKind, text: str | None = None) -> ActionResult:
        self._maybe_fail()
        if ref not in ("e1", "e2"):
            raise RefNotFound(f"ref {ref} is gone; take a new snapshot")
        self.acts.append((ref, action, text))
        return ActionResult(ok=True, action=action, ref=ref, url=page.url, signal=self.signal_value,
                            changed=ref == "e2")

    async def screenshot(self, page: FakePage) -> bytes:
        self._maybe_fail()
        return PNG_1X1

    async def wait(self, page: FakePage, spec, *, should_stop=None):
        """Records the WaitFor; returns `wait_result` (a WaitResult) or "met" at once."""
        from openajar.node.interaction import WaitResult

        self._maybe_fail()
        self.waits.append(spec)
        if self.wait_result is not None:
            return self.wait_result
        return WaitResult(met=True, stopped="met", waited_s=0.0, url=page.url, conditions=[],
                          reread=spec.reads_tree, signal=self.signal_value, detail="met after 0 s")

    async def signal(self, page: FakePage) -> Signal:
        return self.signal_value

    async def report(self, page: FakePage) -> Any:
        """The page's signal with the rule that matched (none here), as the real interaction's report."""
        from openajar.node.sites import SignalReport

        return SignalReport(self.signal_value, None, page.url)
