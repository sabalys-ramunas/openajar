"""browser_challenge's steps, in this order, each only when the one before did not get the browser past the check:

1. wait: the check passes by itself (Cloudflare's often does within seconds), up to WAIT_S;
2. click: one click on its checkbox, as a person does (openajar.node.interaction.checks), unless one was made on
   this page in the last minute;
3. the owner's solver: when the owner set one up for this site, the widget's token from their service, put where
   the widget would put it (openajar.node.solver.widgets). Never for Cloudflare's full-page check, never on a
   sign-up, login or payment page, never past the month's cap;
4. the human: nothing got it past, so the browser is handed to the person (the tool holds it for them).

The result says which step ended it. Every solver try is in the record (openajar.node.solver.ledger), refused ones
too. Only where `checks.allowed`: an environment's browser on its machine. An account's browser never gets here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.core.errors import CheckForHuman
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


@dataclass
class Outcome:
    ended_by: str                    # NOTHING, WAITED, CLICK, SOLVER or HUMAN
    passed: bool
    report: SignalReport
    steps: list[str] = field(default_factory=list)


def is_check(report: SignalReport | Any) -> bool:
    return getattr(report, "signal", None) is Signal.CAPTCHA


async def get_past(ix: Any, page: Page, *, browser: str, home: Path | None) -> Outcome:
    """The steps above, on `page` of the environment's browser `browser` (its lane id, for the record)."""
    if not checks.allowed(page):
        raise CheckForHuman("this browser's checks are for its human: browser_challenge works only on an "
                            "environment's own browser on its machine")
    report = await ix.report(page)
    if not is_check(report):
        return Outcome(NOTHING, True, report, ["there is no check on this page now"])
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


async def _solver(ix: Any, page: Page, report: SignalReport, *, browser: str,
                  home: Path | None) -> tuple[SignalReport, bool, str]:
    """The owner's solving service, behind its guards. (report, passed, what happened)."""
    entries = settings.current(home)
    if not entries:
        return report, False, "solver: the owner has none set up"
    detail = getattr(report, "detail", None)
    if detail in FULL_PAGE_DETAILS:
        return report, False, ("solver: not for Cloudflare's full-page check. Its answer is a cookie tied to this "
                               "browser and its exit address, which a service cannot get without the proxy's "
                               "credentials, and openajar never sends those")
    if detail not in CHECKBOX_DETAILS | PUZZLE_DETAILS:
        return report, False, f"solver: not for this kind of check ({detail})"
    home_dir = home if home is not None else settings.home()
    url = getattr(page, "url", "") or ""
    host = (urlsplit(url).hostname or "").lower()
    widget, why = await widgets.find(page)
    kind = widget.kind if widget is not None else "unknown"

    def refuse(entry: settings.SolverEntry | None, note: str) -> tuple[SignalReport, bool, str]:
        if home_dir is not None:
            ledger.write(home_dir, ledger.Solve(settings.now_iso(), browser, host, kind, entry.name if entry else "",
                                                entry.provider if entry else "", ledger.REFUSED, note=note))
        return report, False, f"solver: {note}"

    if widget is None:
        return refuse(None, f"nothing to send ({why})")
    covering = [e for e in entries if e.covers(host)]
    if not covering:
        return refuse(None, f"{host} is not on the site list of any of the owner's solvers")
    taking = [e for e in covering if services.supports(e, widget.kind)]
    if not taking:
        names = ", ".join(e.name for e in covering)
        return refuse(covering[0], f"no solver of the owner's for this site takes {services.KIND_WORDS[widget.kind]} "
                                   f"({names})")
    stop = await widgets.sensitive(page, widget)
    if stop:
        return refuse(taking[0], f"never on a sign-up, login or payment page ({'; '.join(stop[:3])})")
    if home_dir is None:
        return report, False, "solver: the node has no home to keep the record in, so it does not solve"
    async with ledger.LOCK:
        entry = next((e for e in taking if ledger.spent(home_dir, e.name) + e.assumed_cost <= e.monthly_cap), None)
        if entry is None:
            first = taking[0]
            return refuse(first, f"the month's cap is reached ({first.name}: "
                                 f"{ledger.usd(ledger.spent(home_dir, first.name))} of {first.monthly_cap:.2f} USD)")
        job = services.Job(widget.kind, widget.site_key, services.page_address(url), widget.action, widget.cdata,
                           widget.invisible)
        record = ledger.Solve(settings.now_iso(), browser, host, widget.kind, entry.name, entry.provider, ledger.FAILED)
        try:
            answer = await services.solve(entry, job)
        except services.SolveError as e:
            ledger.write(home_dir, _with(record, note=str(e)[:300]))
            return report, False, f"solver: {entry.name} ({services.NAMES[entry.provider]}) gave no token: {e}"
        cost, cost_from = (answer.cost, "reported") if answer.cost is not None else (entry.assumed_cost, "assumed")
        record = _with(record, cost=round(cost, 6), cost_from=cost_from)
        if (urlsplit(getattr(page, "url", "") or "").hostname or "").lower() != host or page.is_closed():
            ledger.write(home_dir, _with(record, result=ledger.NOT_PASSED, note="the page moved on before the token came"))
            return await ix.report(page), False, "solver: the page moved on before the token came, so it was not used"
        handed = await widgets.hand(page, widget, answer.token)
        del answer
        report = await _settle(ix, page)
        passed = not is_check(report)
        note = f"token in {handed.get('filled', 0)} field(s); the page's callback: {handed.get('called', 'none')}"
        ledger.write(home_dir, _with(record, result=ledger.PASSED if passed else ledger.NOT_PASSED, note=note))
    words = "the check passed" if passed else "the check still shows"
    spent = f"{ledger.usd(cost)} USD ({cost_from})"
    return report, passed, (f"solver: {entry.name} ({services.NAMES[entry.provider]}) answered the "
                            f"{services.KIND_WORDS[widget.kind]} widget for {spent}: {words}; {note}")


def _with(solve: ledger.Solve, **changes: Any) -> ledger.Solve:
    from dataclasses import replace

    return replace(solve, **changes)


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
