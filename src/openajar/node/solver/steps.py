"""browser_challenge's steps, in this order, each only when the one before did not get the browser past the check:

1. wait: the check passes by itself (Cloudflare's often does within seconds), up to WAIT_S;
2. click: one click on its checkbox, as a person does (openajar.node.interaction.checks), unless one was made on
   this page in the last minute;
3. the owner's solver: when the owner set one up for this site, the widget's token from their service, put where
   the widget would put it (openajar.node.solver.widgets). Never for Cloudflare's full-page check, never on a
   sign-up, login or payment page (checked before the request and again, with the page's address, before the
   token goes in), never past the month's cap (a reservation on disk before anything is sent: ledger);
4. the human: nothing got it past, so the browser is handed to the person (the tool holds it for them).

The result says which step ended it. Every solver try is in the record (openajar.node.solver.ledger), refused ones
too. Only where `checks.allowed`: an environment's browser on its machine. An account's browser never gets here.

`could_take` answers, for the signal watch (openajar.node.lanes.watch), whether a service of the owner's could take
the check on this page now; only then is an image puzzle left unheld for browser_challenge.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
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
    """The steps above, on `page` of the environment's browser `browser` (its lane id, for the record)."""
    if not checks.allowed(page):
        raise CheckForHuman("this browser's checks are for its human: browser_challenge works only on an "
                            "environment's own browser on its machine")
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

@dataclass(frozen=True)
class _Fit:
    """What the guards say for this page now: the widget and the entries that could take it, or why none."""
    widget: widgets.Widget | None
    entries: list[settings.SolverEntry]
    why: str
    refused_by: settings.SolverEntry | None = None   # the entry the refusal is recorded under
    record: bool = True                              # a refusal worth a line in the record


def _host(page: Page) -> str:
    return (urlsplit(getattr(page, "url", "") or "").hostname or "").lower()


async def _fit(page: Page, report: SignalReport, home: Path | None) -> _Fit:
    """The guards, in order, before anything is reserved or sent."""
    entries = settings.current(home)
    if not entries:
        return _Fit(None, [], "the owner has none set up", record=False)
    detail = getattr(report, "detail", None)
    if detail in FULL_PAGE_DETAILS or await widgets.full_page(page):
        return _Fit(None, [], FULL_PAGE, record=False)
    if detail not in CHECKBOX_DETAILS | PUZZLE_DETAILS:
        return _Fit(None, [], f"not for this kind of check ({detail})", record=False)
    widget, why = await widgets.find(page)
    if widget is None:
        return _Fit(None, [], f"nothing to send ({why})")
    host = _host(page)
    covering = [e for e in entries if e.covers(host)]
    if not covering:
        return _Fit(widget, [], f"{host} is not on the site list of any of the owner's solvers")
    taking = [e for e in covering if services.supports(e, widget.kind)]
    if not taking:
        names = ", ".join(e.name for e in covering)
        return _Fit(widget, [], f"no solver of the owner's for this site takes {services.KIND_WORDS[widget.kind]} "
                                f"({names})", covering[0])
    stop = await widgets.sensitive(page, widget)
    if stop:
        return _Fit(widget, [], f"never on a sign-up, login or payment page ({'; '.join(stop[:3])})", taking[0])
    return _Fit(widget, taking, "")


async def could_take(page: Page, report: SignalReport, home: Path | None = None) -> bool:
    """A service of the owner's could take this page's check now: it covers the site, takes the widget's kind, the
    page passes the guards, and its month's cap has room (a record that cannot be read: no). Reads only."""
    fit = await _fit(page, report, home)
    where = home if home is not None else settings.home()
    if not fit.entries or where is None:
        return False
    try:
        return any(ledger.spent(where, e.name) + e.assumed_cost <= e.monthly_cap + 1e-9 for e in fit.entries)
    except ledger.LedgerError:
        return False


async def _solver(ix: Any, page: Page, report: SignalReport, *, browser: str,
                  home: Path | None) -> tuple[SignalReport, bool, str]:
    """The owner's solving service, behind its guards. (report, passed, what happened)."""
    home_dir = home if home is not None else settings.home()
    host = _host(page)
    fit = await _fit(page, report, home)
    widget = fit.widget
    if not fit.entries or widget is None:
        if fit.record and home_dir is not None:
            by = fit.refused_by
            ledger.write(home_dir, ledger.Solve(settings.now_iso(), browser, host, widget.kind if widget else "unknown",
                                                by.name if by else "", by.provider if by else "", ledger.REFUSED,
                                                note=fit.why))
        return report, False, f"solver: {fit.why}"
    if home_dir is None:
        return report, False, "solver: the node has no home to keep the record in, so it does not solve"
    base = ledger.Solve(settings.now_iso(), browser, host, widget.kind, "", "", ledger.PENDING)
    entry, pending, why = await _reserve(home_dir, fit.entries, base)
    if pending is None:
        ledger.write(home_dir, replace(base, service=entry.name, provider=entry.provider, result=ledger.REFUSED,
                                       note=why))
        return report, False, f"solver: {why}"
    url = getattr(page, "url", "") or ""
    job = services.Job(widget.kind, widget.site_key, services.page_address(url), widget.action, widget.cdata,
                       widget.invisible)
    name = f"{entry.name} ({services.NAMES[entry.provider]})"
    try:
        answer = await services.solve(entry, job)
    except services.SolveError as e:
        cost, cost_from = (entry.assumed_cost, "assumed") if e.may_have_cost else (0.0, "")
        ledger.settle(pending, home_dir, result=ledger.FAILED, cost=cost, cost_from=cost_from, note=str(e))
        return report, False, f"solver: {name} gave no token: {e}"
    except BaseException:  # cut off (a deadline, a cancel) after the request went out: it may have been charged
        ledger.settle(pending, home_dir, result=ledger.CANCELLED, cost=entry.assumed_cost, cost_from="assumed",
                      note="cut off while the service worked")
        raise
    cost, cost_from = (answer.cost, "reported") if answer.cost is not None else (entry.assumed_cost, "assumed")
    try:
        moved = await _moved(page, url, widget)
        if moved:
            ledger.settle(pending, home_dir, result=ledger.NOT_PASSED, cost=cost, cost_from=cost_from,
                          note=f"not used: {moved}")
            return await ix.report(page), False, f"solver: the token was not used: {moved}"
        handed = await widgets.hand(page, widget, answer.token, url)
        del answer
        if handed.get("moved"):  # between the guards and the write: the document is no longer the one asked for
            moved = "the page moved on before the token came"
            ledger.settle(pending, home_dir, result=ledger.NOT_PASSED, cost=cost, cost_from=cost_from,
                          note=f"not used: {moved}")
            return await ix.report(page), False, f"solver: the token was not used: {moved}"
        report = await _settle(ix, page)
    except BaseException:
        ledger.settle(pending, home_dir, result=ledger.CANCELLED, cost=cost, cost_from=cost_from,
                      note="cut off while the token was handed over")
        raise
    passed = not is_check(report)
    note = f"token in {handed.get('filled', 0)} field(s); the page's callback: {handed.get('called', 'none')}"
    ledger.settle(pending, home_dir, result=ledger.PASSED if passed else ledger.NOT_PASSED, cost=cost,
                  cost_from=cost_from, note=note)
    words = "the check passed" if passed else "the check still shows"
    return report, passed, (f"solver: {name} answered the {services.KIND_WORDS[widget.kind]} widget for "
                            f"{ledger.usd(cost)} USD ({cost_from}): {words}; {note}")


async def _reserve(home: Path, entries: list[settings.SolverEntry],
                   base: ledger.Solve) -> tuple[settings.SolverEntry, ledger.Solve | None, str]:
    """The first entry whose cap has room, with its pending line on disk; or the first entry and why none."""
    first = entries[0]
    for entry in entries:
        solve = replace(base, service=entry.name, provider=entry.provider)
        try:
            pending = await asyncio.to_thread(ledger.reserve, home, solve, assumed=entry.assumed_cost,
                                              cap=entry.monthly_cap)
        except ledger.CapReached:
            continue
        except ledger.LedgerError as e:
            return entry, None, f"{e}, so nothing is sent"
        return entry, pending, ""
    try:
        now = ledger.usd(ledger.spent(home, first.name))
    except ledger.LedgerError:
        now = "?"
    return first, None, f"the month's cap is reached ({first.name}: {now} of {first.monthly_cap:.2f} USD)"


async def _moved(page: Page, url: str, widget: widgets.Widget) -> str:
    """Why the token must not go into the page as it is now ("" when it may): the page left the address it was
    asked for, or the guards now see a sign-up, login or payment page."""
    if page.is_closed():
        return "the page closed"
    now = getattr(page, "url", "") or ""
    if now.split("#")[0] != url.split("#")[0]:
        return "the page moved on before the token came"
    stop = await widgets.sensitive(page, widget)
    if stop:
        return f"the page is now a sign-up, login or payment page ({'; '.join(stop[:3])})"
    return ""


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
