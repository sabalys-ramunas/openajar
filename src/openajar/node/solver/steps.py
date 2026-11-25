"""browser_challenge's steps, in this order, each only when the one before did not get the browser past the check:

1. wait: the check passes by itself (Cloudflare's often does within seconds), up to WAIT_S;
2. click: one click on its checkbox, as a person does (openajar.node.interaction.checks), unless one was made on
   this page in the last minute;
3. the owner's solver: when the owner set one up, the widget's token from their service, put where the widget
   would put it (openajar.node.solver.widgets), only while the page is at the address it was asked for. Never
   for Cloudflare's full-page check, reCAPTCHA Enterprise or a widget inside another site's frame: no token from
   a service works there;
4. the human: nothing got it past, so the browser is handed to the person (the tool holds it for them).

The result says which step ended it. Every request to a service is a line in the log (openajar.node.solver.ledger).
The same in every browser, an environment's or an account's.

`could_take` answers, for the signal watch (openajar.node.lanes.watch), whether a service of the owner's could take
the check on this page; only then is an image puzzle left unheld for browser_challenge.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.core.models import Signal
from openajar.node.human import quietly
from openajar.node.interaction import autocheck, checks
from openajar.node.sites import CHECKBOX_DETAILS, FULL_PAGE_DETAILS, PUZZLE_DETAILS, SignalReport
from openajar.node.solver import ledger, services, settings, widgets

log = logging.getLogger("openajar.solver")
Page = Any

WAIT_S = 20.0            # as a person waits on "Just a moment" (autocheck.AUTO_CHECK_S)
WAIT_OTHER_S = 5.0       # an hCaptcha or reCAPTCHA widget passes by itself only when it is invisible: a short look
CLICKED_S = 60.0         # a click this recent on the page counts as the click step
SETTLE_S = 8.0           # after the token is handed: the page's callback and what it loads

# which step ended it
NOTHING, WAITED, CLICK, SOLVER, HUMAN = "nothing", "wait", "click", "solver", "human"

PUZZLE_WORDS = {"closed": "; the image puzzle left over the page was closed with one click outside it",
                "open": "; the image puzzle still covers part of the page: a click outside it closes it"}
FULL_PAGE = ("not for Cloudflare's full-page check. Its answer is a cookie tied to this browser and its exit "
             "address, which a service cannot get without the proxy's credentials, and openajar never sends those")


@dataclass
class Outcome:
    ended_by: str                    # NOTHING, WAITED, CLICK, SOLVER or HUMAN
    passed: bool
    report: SignalReport
    steps: list[str] = field(default_factory=list)


@dataclass
class Progress:
    """What a caller can read when the steps end early (a deadline, an error): whether the page showed a check."""
    saw_check: bool = False


def is_check(report: SignalReport | Any) -> bool:
    return getattr(report, "signal", None) is Signal.CAPTCHA


async def get_past(ix: Any, page: Page, *, browser: str, home: Path | None,
                   progress: Progress | None = None) -> Outcome:
    """The steps above, on `page` of the browser `browser` (its lane id, for the log)."""
    report = await ix.report(page)
    if not is_check(report):
        return Outcome(NOTHING, True, report, ["there is no check on this page now"])
    if progress is not None:
        progress.saw_check = True
    steps: list[str] = []

    report, waited = await _wait(ix, page, report)
    if not is_check(report):
        return Outcome(WAITED, True, report, [f"waited {waited:.0f} s: the check passed by itself"])
    steps.append(f"waited {waited:.0f} s: it did not pass by itself")

    report, passed, said = await _click(ix, page, report)
    steps.append(said)
    if passed:
        return Outcome(CLICK, True, report, steps)

    report, passed, said = await _solver(ix, page, report, browser=browser, home=home)
    steps.append(said)
    if passed:
        return Outcome(SOLVER, True, report, steps)
    steps.append("handed to your human")
    return Outcome(HUMAN, False, report, steps)


async def _wait(ix: Any, page: Page, report: SignalReport) -> tuple[SignalReport, float]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    if autocheck.cloudflare_check(report):
        report = await autocheck.past_automatic_check(page, report, ix.report, wait_s=WAIT_S)
    else:
        deadline = started + WAIT_OTHER_S
        while loop.time() < deadline and not page.is_closed() and is_check(report):
            await asyncio.sleep(autocheck.AUTO_POLL_S)
            report = await ix.report(page)
    return report, loop.time() - started


async def _click(ix: Any, page: Page, report: SignalReport) -> tuple[SignalReport, bool, str]:
    if getattr(report, "detail", None) not in CHECKBOX_DETAILS:
        return report, False, "click: no checkbox to click (an image puzzle shows)"
    if checks.clicked_within(page, CLICKED_S):
        return report, False, "click: its checkbox was clicked once already, so not again"
    for check in await checks.find_checks(page):
        click = await checks.click_once(ix, page, check)
        if click is None:
            continue
        if click.how is None:
            return click.report, False, f"click: the {check.vendor} check is out of view, so it was not clicked"
        words = {checks.PASSED: "it passed", checks.PUZZLE: "an image puzzle appeared",
                 checks.NOT_PASSED: "it did not pass"}[click.outcome]
        return click.report, click.ok, f"click: clicked the {check.vendor} check's checkbox once {click.how}: {words}"
    return await ix.report(page), False, "click: no checkbox showed to click"


# ---- the owner's solver -----------------------------------------------------------------------------------------

def _host(page: Page) -> str:
    return (urlsplit(getattr(page, "url", "") or "").hostname or "").lower()


async def _pick(page: Page, report: SignalReport,
                home: Path | None) -> tuple[settings.SolverEntry | None, widgets.Widget | None, str]:
    """The owner's service that takes this page's widget, and the widget; or why none can be sent (only what keeps a
    token from working: no service, a kind of check no token answers, no widget in the page's own document, no
    service that takes its kind)."""
    entries = settings.current(home)
    if not entries:
        return None, None, "the owner has none set up"
    detail = getattr(report, "detail", None)
    if detail in FULL_PAGE_DETAILS or await widgets.full_page(page):
        return None, None, FULL_PAGE
    if detail not in CHECKBOX_DETAILS | PUZZLE_DETAILS:
        return None, None, f"not for this kind of check ({detail})"
    widget, why = await widgets.find(page)
    if widget is None:
        return None, None, f"nothing to send ({why})"
    entry = next((e for e in entries if services.supports(e, widget.kind)), None)
    if entry is None:
        names = ", ".join(e.name for e in entries)
        return None, widget, f"no solver of the owner's takes {services.KIND_WORDS[widget.kind]} ({names})"
    return entry, widget, ""


async def could_take(page: Page, report: SignalReport, home: Path | None = None) -> bool:
    """A service of the owner's could take this page's check: one is set up, a widget is found in the page's own
    document, and the service takes its kind. Reads only."""
    entry, _, _ = await _pick(page, report, home)
    return entry is not None


async def _solver(ix: Any, page: Page, report: SignalReport, *, browser: str,
                  home: Path | None) -> tuple[SignalReport, bool, str]:
    """The owner's solving service. (report, passed, what happened)."""
    entry, widget, why = await _pick(page, report, home)
    if entry is None or widget is None:
        return report, False, f"solver: {why}"
    home_dir = home if home is not None else settings.home()
    url = getattr(page, "url", "") or ""
    line = ledger.Solve(settings.now_iso(), browser, _host(page), widget.kind, entry.name, entry.provider,
                        ledger.FAILED)
    job = services.Job(widget.kind, widget.site_key, services.page_address(url), widget.action, widget.cdata,
                       widget.invisible)
    name = f"{entry.name} ({services.NAMES[entry.provider]})"
    try:
        answer = await services.solve(entry, job)
    except services.SolveError as e:
        ledger.write(home_dir, replace(line, note=str(e)))
        return report, False, f"solver: {name} gave no token: {e}"
    except BaseException:  # cut off (a deadline, a cancel) after the request went out
        ledger.write(home_dir, replace(line, result=ledger.CANCELLED, note="cut off while the service worked"))
        raise
    line = replace(line, cost=answer.cost)
    cost = f"{ledger.usd(answer.cost)} USD" if answer.cost is not None else "a cost it did not report"
    moved = "the page moved on before the token came"
    try:
        if page.is_closed() or (getattr(page, "url", "") or "").split("#")[0] != url.split("#")[0]:
            handed: dict[str, Any] = {"moved": True}
        else:  # the address is read again in the step that writes the token
            handed = await widgets.hand(page, widget, answer.token, url)
        del answer
        if handed.get("moved"):
            ledger.write(home_dir, replace(line, result=ledger.NOT_PASSED, note=f"not used: {moved}"))
            return await ix.report(page), False, f"solver: the token was not used: {moved}"
        report = await _settle(ix, page)
        puzzle = await widgets.close_puzzle(page) if handed.get("filled") else "none"
    except BaseException:
        ledger.write(home_dir, replace(line, result=ledger.CANCELLED, note="cut off while the token was handed over"))
        raise
    passed = not is_check(report)
    note = f"token in {handed.get('filled', 0)} field(s); the page's callback: {handed.get('called', 'none')}"
    ledger.write(home_dir, replace(line, result=ledger.PASSED if passed else ledger.NOT_PASSED, note=note))
    note += PUZZLE_WORDS.get(puzzle, "")
    words = "the check passed" if passed else "the check still shows"
    return report, passed, (f"solver: {name} answered the {services.KIND_WORDS[widget.kind]} widget for {cost}: "
                            f"{words}; {note}")


async def _settle(ix: Any, page: Page) -> SignalReport:
    """The page's report once the handed token has taken (the check no longer shows), or after SETTLE_S."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_S
    report = await ix.report(page)
    while is_check(report) and loop.time() < deadline and not page.is_closed():
        await asyncio.sleep(0.5)
        report = await ix.report(page)
    if not is_check(report):
        await quietly(page.wait_for_load_state("domcontentloaded", timeout=5_000))
        report = await ix.report(page)
    return report
