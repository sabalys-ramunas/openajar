"""`openajar solver`: the owner's own solving service for a site's "are you human" widget in an environment's browser
(openajar.node.solver). Off until the owner adds one; openajar ships no key and shares none."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from openajar.commands.base import fail, initialized_config
from openajar.core.config import NodeConfig
from openajar.node.solver import ledger, settings
from openajar.node.solver.settings import BUILT_IN, NAME_RE, PROVIDERS, SolverEntry

solver_app = typer.Typer(no_args_is_help=True, help="Your own captcha-solving service for environments' browsers "
                                                    "(off until you add one).")

NOTICE = ("A solving service may be against a site's terms; the choice and the cost are yours. openajar uses it only "
          "in environments' browsers, never on an account's browser, a sign-up, login or payment page, or Cloudflare's "
          "full-page check, and never past your cap.")


ANY_SITE_NOTE = ("With --sites '*' any page an agent opens can have your service answer a widget: the page chooses "
                 "the site key, so a hostile page can get another site's check answered on your account and read "
                 "the token. Your cap bounds what that costs. Name the sites instead when you can.")


def _record(cfg: NodeConfig, month: str) -> list[ledger.Solve]:
    try:
        return ledger.read(cfg.home, month=month)
    except ledger.LedgerError as e:
        raise fail(f"{e}. Until then no solve is sent.") from None


def _entries(cfg: NodeConfig) -> list[SolverEntry]:
    home = cfg.home
    try:
        return settings.load(home)
    except ValueError as e:
        raise fail(f"{e}. Fix or delete {settings.path(home)}, then run this again.") from None


def _sites(raw: str) -> list[str]:
    sites = [s for s in (x.strip() for x in raw.split(",")) if s]
    bad = [s for s in sites if settings.clean_site(s) is None]
    if not sites or bad:
        raise typer.BadParameter(f"not a site: {bad[0]!r}" if bad else "name at least one site",
                                 param_hint="--sites")
    return sites


@solver_app.command("add")
def solver_add(
    name: Annotated[str, typer.Argument(help="A short name for it, for example main.")],
    provider: Annotated[str, typer.Argument(help=f"One of: {', '.join(PROVIDERS)}.")],
    key: Annotated[str | None, typer.Argument(help="The service's API key (leave it out to be prompted; a command or "
                                                   "webhook may have none).")] = None,
    cap: Annotated[float, typer.Option("--cap", help="The most it may spend in a calendar month, in US dollars.",
                                       show_default=False)] = 0.0,
    sites: Annotated[str, typer.Option("--sites", help="The sites it may answer for, comma separated (example.com "
                                                       "covers its subdomains; * is any site).",
                                       show_default=False)] = "",
    url: Annotated[str | None, typer.Option("--url", help="webhook: its https address. A built-in: another address "
                                                          "for its API (a service with the same API).")] = None,
    command: Annotated[str | None, typer.Option("--command", help="command: the command line to run (no shell). It "
                                                                  "reads the job as JSON on stdin and the key in "
                                                                  "OPENAJAR_SOLVER_KEY.")] = None,
    assumed_cost: Annotated[float, typer.Option("--assumed-cost", help="What one solve counts against the cap when "
                                                                       "the service does not report its cost (USD).")]
    = settings.DEFAULT_ASSUMED_COST,
    yes: Annotated[bool, typer.Option("--yes", help="Replace an entry of the same name.")] = False,
) -> None:
    """Turn on a solving service of your own, with your key, your month's cap and the sites it may answer for.
    The key is stored in secrets/solvers.json on this computer (chmod 600), and never goes to an environment's
    machine, a page, an agent, a log or a trace.

    An agent uses it only through browser_challenge, after the check did not pass by itself or with one click.
    Built in: capsolver, 2captcha and anticaptcha (Turnstile and reCAPTCHA v2; none of them takes hCaptcha now).
    command and webhook plug in any other service or your own script (docs/ENVIRONMENTS.md has the format)."""
    from openajar.node.solver import services

    if not NAME_RE.fullmatch(name):
        raise typer.BadParameter("lowercase letters, digits, dot, dash or underscore", param_hint="NAME")
    provider = provider.lower().replace("-", "")
    if provider not in PROVIDERS:
        raise typer.BadParameter(f"one of {', '.join(PROVIDERS)}", param_hint="PROVIDER")
    if cap <= 0:
        raise fail("Set the month's cap in US dollars with --cap (for example --cap 5): a looping agent then cannot "
                   "spend more than that. Nothing changed.")
    site_list = _sites(sites) if sites else []
    if not site_list:
        raise fail("Name the sites it may answer for with --sites (for example --sites example.com,shop.example.org, "
                   "or --sites '*' for any site). Nothing changed.")
    if provider == "command" and not (command or "").strip():
        raise fail("A command hook needs --command \"the command line\". Nothing changed.")
    if provider == "webhook" and not url:
        raise fail("A webhook needs --url https://... Nothing changed.")
    if url and (why := services.check_address(url)) is not None:
        raise fail(f"--url is refused: {why}. Nothing changed.")
    if provider in BUILT_IN and not key:
        key = typer.prompt(f"API key for {services.NAMES[provider]}", hide_input=True).strip()
    cfg = initialized_config()
    entries = _entries(cfg)
    existed = next((e for e in entries if e.name == name), None)
    if existed is not None and not yes:
        raise fail(f"solver {name} already exists ({services.NAMES[existed.provider]}). Nothing changed. To replace it: "
                   f"openajar solver add {name} ... --yes")
    try:
        entry = SolverEntry(name=name, provider=provider, key=key or None, endpoint=url or None,  # type: ignore[arg-type]
                            command=command or None, monthly_cap=cap, sites=site_list, assumed_cost=assumed_cost,
                            added=settings.now_iso())
    except ValueError as e:
        fields = sorted({".".join(str(x) for x in err["loc"]) for err in getattr(e, "errors", list)()}) or ["the entry"]
        raise fail(f"solver {name} is not valid: check {', '.join(fields)}. Nothing changed.") from None
    entries = [entry if e.name == name else e for e in entries] if existed is not None else [*entries, entry]
    settings.save(cfg.home, entries)
    where = "any site" if settings.ANY_SITE in entry.sites else ", ".join(entry.sites)
    typer.echo(f"{'Replaced' if existed else 'Added'} solver {name}: {services.NAMES[provider]}, for {where}, at most "
               f"{cap:.2f} USD a month{', with a key' if entry.key else ''}.")
    if provider in BUILT_IN:
        typer.echo(f"It takes: {', '.join(services.KIND_WORDS[k] for k in services.TASK_TYPES[provider])}. "
                   "hCaptcha only through a command or webhook.")
    typer.echo(NOTICE)
    if settings.ANY_SITE in entry.sites:
        typer.echo(ANY_SITE_NOTE)
    typer.echo(f"Check it with `openajar solver test {name}`.")


@solver_app.command("list")
def solver_list() -> None:
    """Your solving services, with this month's spend and solves (keys are never shown)."""
    from openajar.node.solver import services

    cfg = initialized_config()
    entries = _entries(cfg)
    if not entries:
        typer.echo("No solver: openajar solves nothing, and a check an agent cannot pass goes to you. Add one with "
                   "`openajar solver add <name> <provider> --cap 5 --sites example.com`.")
        return
    month = ledger.this_month()
    record = _record(cfg, month)
    for e in entries:
        mine = [s for s in record if s.service == e.name]
        tried = [s for s in mine if s.result != ledger.REFUSED]
        where = "any site" if settings.ANY_SITE in e.sites else ", ".join(e.sites)
        how = settings.safe_url(e.endpoint) if e.endpoint else (e.command.split()[0] if e.command else "its own API")
        typer.echo(f"{e.name}  {services.NAMES[e.provider]} ({how})  sites: {where}  "
                   f"{'key stored' if e.key else 'no key'}")
        spent = round(sum(s.cost for s in mine), 6)
        typer.echo(f"{' ' * len(e.name)}  {month}: {ledger.usd(spent)} of "
                   f"{e.monthly_cap:.2f} USD, {len(tried)} solves ({sum(s.result == ledger.PASSED for s in tried)} "
                   f"passed), {len(mine) - len(tried)} refused")


@solver_app.command("remove")
def solver_remove(name: Annotated[str, typer.Argument(help="Its name, as `openajar solver list` shows it.")]) -> None:
    """Delete a solving service and its key from this computer. Its record stays (`openajar solver log`)."""
    cfg = initialized_config()
    entries = _entries(cfg)
    if not any(e.name == name for e in entries):
        raise fail(f"no solver {name!r}; `openajar solver list` shows the ones there are")
    settings.save(cfg.home, [e for e in entries if e.name != name])
    left = len(entries) - 1
    typer.echo(f"Removed solver {name} and its key." + ("" if left else " No solver is left: openajar solves "
                                                                         "nothing, and checks go to you."))


@solver_app.command("test")
def solver_test(
    name: Annotated[str | None, typer.Argument(help="One solver's name (default: every one).")] = None,
    solve: Annotated[bool, typer.Option("--solve", help="A built-in: also buy one token for Cloudflare's test widget "
                                                        "(it costs one solve and counts against the cap).")] = False,
) -> None:
    """Check each service with your key: a built-in's balance (free), or a hook's answer to Cloudflare's always-pass
    test widget. Exits 1 when any fails. Nothing here touches a browser."""
    cfg = initialized_config()
    entries = _entries(cfg)
    if name is not None:
        entries = [e for e in entries if e.name == name]
        if not entries:
            raise fail(f"no solver {name!r}; `openajar solver list` shows the ones there are")
    if not entries:
        typer.echo("No solver to test. Add one with `openajar solver add`.")
        return
    failed = 0
    for e in entries:
        ok = asyncio.run(_test_one(cfg.home, e, solve))
        failed += not ok
    if failed:
        raise typer.Exit(1)


async def _test_one(home: Path, e: SolverEntry, solve: bool) -> bool:
    from openajar.node.solver import services

    if e.provider in BUILT_IN:
        try:
            money = await services.balance(e)
        except services.SolveError as err:
            typer.echo(f"{e.name}  failed: {err}")
            return False
        typer.echo(f"{e.name}  {services.NAMES[e.provider]} answers: balance {money:.4f} USD")
        if not solve:
            return True
    job = services.Job("turnstile", services.TEST_SITE_KEY, services.TEST_PAGE)
    base = ledger.Solve(settings.now_iso(), "openajar solver test", "example.com", "turnstile", e.name, e.provider,
                        ledger.PENDING)
    try:  # the same reservation as the node's, under the same lock: this process and the node take turns
        pending = ledger.reserve(home, base, assumed=e.assumed_cost, cap=e.monthly_cap)
    except ledger.CapReached as full:
        typer.echo(f"{e.name}  not tried: this month's cap is reached ({ledger.usd(full.spent)} of "
                   f"{e.monthly_cap:.2f} USD)")
        return False
    except ledger.LedgerError as err:
        typer.echo(f"{e.name}  not tried: {err}")
        return False
    try:
        answer = await services.solve(e, job)
    except services.SolveError as err:
        cost, cost_from = (e.assumed_cost, "assumed") if err.may_have_cost else (0.0, "")
        ledger.settle(pending, home, result=ledger.FAILED, cost=cost, cost_from=cost_from, note=str(err))
        typer.echo(f"{e.name}  failed: {err}")
        return False
    except BaseException:
        ledger.settle(pending, home, result=ledger.CANCELLED, cost=e.assumed_cost, cost_from="assumed",
                      note="cut off while the service worked")
        raise
    cost, cost_from = (answer.cost, "reported") if answer.cost is not None else (e.assumed_cost, "assumed")
    ledger.settle(pending, home, result=ledger.PASSED, cost=cost, cost_from=cost_from, note="a test token")
    typer.echo(f"{e.name}  gave a token for Cloudflare's test widget ({len(answer.token)} characters), "
               f"{ledger.usd(cost)} USD ({cost_from})")
    return True


@solver_app.command("log")
def solver_log(
    month: Annotated[str | None, typer.Option("--month", help="A month, for example 2026-11 (default: this one).")]
    = None,
    limit: Annotated[int, typer.Option("--limit", help="The newest this many lines.")] = 50,
) -> None:
    """The record of every solve and every refused try: when, which environment's browser, the site, the widget,
    the service, the result and the cost. Never a key or a token."""
    from openajar.node.solver import services

    cfg = initialized_config()
    month = month or ledger.this_month()
    record = _record(cfg, month)
    if not record:
        typer.echo(f"No solves in {month}.")
        return
    for s in record[-max(1, limit):]:
        cost = f"{ledger.usd(s.cost)} USD ({s.cost_from})" if s.cost_from else "no cost"
        typer.echo(f"{s.at}  {s.site}  {services.KIND_WORDS.get(s.kind, s.kind)}  {s.service or '-'}  {s.result}  "
                   f"{cost}  {s.browser}" + (f"  {s.note}" if s.note else ""))
    total = sum(s.cost for s in record)
    typer.echo(f"{month}: {len(record)} lines, {ledger.usd(total)} USD in all.")
