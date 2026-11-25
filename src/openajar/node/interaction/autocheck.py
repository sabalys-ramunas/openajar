"""Cloudflare's automatic check, given the seconds a person gives it.

Cloudflare's challenge page ("Just a moment...", "Verifying...") and its Turnstile widget often pass a browser by
themselves after a few seconds of work in the page, with no click: measured on real sites through a residential
address (glassdoor.com cleared in about 15 s, a crunchbase.com company page in about 8 s). Read at once, the page
is a challenge, and the lane is held for a human (openajar.node.holds) though nothing was left to do. So a
navigation, and an action that lands on such a page, wait for it the way a person waits for "Just a moment":
until the signal is no longer Cloudflare's check (the page moved on, or the widget holds its token), or
AUTO_CHECK_S have passed. The page is only read, never touched. A check that still wants a person after that
(the "Verify you are human" checkbox) is reported as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from openajar.core.models import Signal
from openajar.node.human import quietly
from openajar.node.sites import CLOUDFLARE_DETAILS, SignalReport

Page = Any  # playwright.async_api.Page

AUTO_CHECK_S = 20.0   # the longest a person waits on "Just a moment" before thinking it is stuck
AUTO_POLL_S = 1.0
LOAD_AFTER_S = 10.0   # the page the check lets through: its document, before the signal is read again


def cloudflare_check(report: SignalReport) -> bool:
    """A captcha signal from Cloudflare's own check (its challenge page or its Turnstile widget)."""
    return getattr(report, "signal", None) is Signal.CAPTCHA and getattr(report, "detail", None) in CLOUDFLARE_DETAILS


async def past_automatic_check(page: Page, report: SignalReport, read: Callable[[Page], Awaitable[SignalReport]],
                               *, wait_s: float = AUTO_CHECK_S) -> SignalReport:
    """`report` when it is not Cloudflare's check; else the page's report once the check let the browser through
    by itself, or after `wait_s`."""
    if not cloudflare_check(report):
        return report
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while loop.time() < deadline and not page.is_closed():
        await asyncio.sleep(AUTO_POLL_S)
        report = await read(page)
        if not cloudflare_check(report):
            if report.signal is not Signal.OK:  # mid-way to the page it lets through: let that load first
                await quietly(page.wait_for_load_state("domcontentloaded", timeout=LOAD_AFTER_S * 1000))
                report = await read(page)
            return report
    return report
