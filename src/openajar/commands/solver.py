"""`openajar solver`: the owner's own solving service for a site's "are you human" widget in any browser
(openajar.node.solver). Off until the owner adds one; openajar ships no key and shares none."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from openajar.commands.base import fail, initialized_config
from openajar.core.config import NodeConfig
from openajar.node.solver import ledger, settings
from openajar.node.solver.settings import BUILT_IN, NAME_RE, PROVIDERS, SolverEntry

solver_app = typer.Typer(no_args_is_help=True, help="Your own captcha-solving service for every browser (off until "
                                                    "you add one).")

NOTICE = ("The service and what it costs are your choice. openajar asks it only when a check did not pass by itself "
          "or with one click, in any browser, your accounts' included.")


def _entries(cfg: NodeConfig) -> list[SolverEntry]:
    home = cfg.home
    try:
        return settings.load(home)
    except ValueError as e:
        raise fail(f"{e}. Fix or delete {settings.path(home)}, then run this again.") from None


@solver_app.command("add")
def solver_add(
    name: Annotated[str, typer.Argument(help="A short name for it, for example main.")],
    provider: Annotated[str, typer.Argument(help=f"One of: {', '.join(PROVIDERS)}.")],
    key: Annotated[str | None, typer.Argument(help="The service's API key (leave it out to be prompted; a command or "
                                                   "webhook may have none).")] = None,
    url: Annotated[str | None, typer.Option("--url", help="webhook: its https address. A built-in: another address "
                                                          "for its API (a service with the same API).")] = None,
    command: Annotated[str | None, typer.Option("--command", help="command: the command line to run (no shell). It "
                                                                  "reads the job as JSON on stdin and the key in "
                                                                  "OPENAJAR_SOLVER_KEY.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Replace an entry of the same name.")] = False,
) -> None:
    """Turn on a solving service of your own, with your key. The key is stored in secrets/solvers.json on this
    computer (chmod 600), and never goes to an environment's machine, a page, an agent, a log or a trace.

    An agent uses it only through browser_challenge, after the check did not pass by itself or with one click.
    Built in: capsolver, 2captcha and anticaptcha (Turnstile and reCAPTCHA v2; none of them takes hCaptcha now).
    command and webhook plug in any other service or your own script (docs/ENVIRONMENTS.md has the format)."""
    from openajar.node.solver import services

    if not NAME_RE.fullmatch(name):
        raise typer.BadParameter("lowercase letters, digits, dot, dash or underscore", param_hint="NAME")
    provider = provider.lower().replace("-", "")
    if provider not in PROVIDERS:
        raise typer.BadParameter(f"one of {', '.join(PROVIDERS)}", param_hint="PROVIDER")
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
    entry = SolverEntry(name=name, provider=provider, key=key or None, endpoint=url or None,  # type: ignore[arg-type]
                        command=command or None, added=settings.now_iso())
    entries = [entry if e.name == name else e for e in entries] if existed is not None else [*entries, entry]
    settings.save(cfg.home, entries)
    typer.echo(f"{'Replaced' if existed else 'Added'} solver {name}: {services.NAMES[provider]}"
               f"{', with a key' if entry.key else ''}.")
    if provider in BUILT_IN:
        typer.echo(f"It takes: {', '.join(services.KIND_WORDS[k] for k in services.TASK_TYPES[provider])}. "
                   "hCaptcha only through a command or webhook.")
    typer.echo(NOTICE)
    typer.echo(f"Check it with `openajar solver test {name}`.")


@solver_app.command("list")
def solver_list() -> None:
    """Your solving services, with this month's solves and the spend their services reported (keys are never
    shown)."""
    from openajar.node.solver import services

    cfg = initialized_config()
    entries = _entries(cfg)
    if not entries:
        typer.echo("No solver: openajar solves nothing, and a check an agent cannot pass goes to you. Add one with "
                   "`openajar solver add <name> <provider>`.")
        return
    month = ledger.this_month()
    record = ledger.read(cfg.home, month=month)
    for e in entries:
        mine = [s for s in record if s.service == e.name]
        how = settings.safe_url(e.endpoint) if e.endpoint else (e.command.split()[0] if e.command else "its own API")
        typer.echo(f"{e.name}  {services.NAMES[e.provider]} ({how})  {'key stored' if e.key else 'no key'}")
        typer.echo(f"{' ' * len(e.name)}  {month}: {len(mine)} solves ({sum(s.result == ledger.PASSED for s in mine)} "
                   f"passed), {ledger.usd(ledger.spent(mine))} USD reported")


@solver_app.command("remove")
def solver_remove(name: Annotated[str, typer.Argument(help="Its name, as `openajar solver list` shows it.")]) -> None:
    """Delete a solving service and its key from this computer. Its log stays (`openajar solver log`)."""
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
                                                        "(it costs one solve).")] = False,
) -> None:
    """Check each service with your key: a built-in's balance (free), or a hook's answer for Cloudflare's test widget
    (its key that forces the interactive check). Exits 1 when any fails. Nothing here touches a browser."""
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
    line = ledger.Solve(settings.now_iso(), "openajar solver test", "2captcha.com", "turnstile", e.name, e.provider,
                        ledger.FAILED)
    try:
        answer = await services.solve(e, job)
    except services.SolveError as err:
        ledger.write(home, replace(line, note=str(err)))
        typer.echo(f"{e.name}  failed: {err}")
        return False
    ledger.write(home, replace(line, result=ledger.PASSED, cost=answer.cost, note="a test token"))
    cost = f"{ledger.usd(answer.cost)} USD" if answer.cost is not None else "no cost reported"
    typer.echo(f"{e.name}  gave a token for Cloudflare's test widget ({len(answer.token)} characters), {cost}")
    return True


@solver_app.command("log")
def solver_log(
    month: Annotated[str | None, typer.Option("--month", help="A month, for example 2026-11 (default: this one).")]
    = None,
    limit: Annotated[int, typer.Option("--limit", help="The newest this many lines.")] = 50,
) -> None:
    """The log of every request to a service: when, which browser, the site, the widget, the service,
    the result and the cost it reported. Never a key or a token."""
    from openajar.node.solver import services

    cfg = initialized_config()
    month = month or ledger.this_month()
    record = ledger.read(cfg.home, month=month)
    if not record:
        typer.echo(f"No solves in {month}.")
        return
    for s in record[-max(1, limit):]:
        cost = f"{ledger.usd(s.cost)} USD" if s.cost is not None else "no cost reported"
        typer.echo(f"{s.at}  {s.site}  {services.KIND_WORDS.get(s.kind, s.kind)}  {s.service or '-'}  {s.result}  "
                   f"{cost}  {s.browser}" + (f"  {s.note}" if s.note else ""))
    typer.echo(f"{month}: {len(record)} solves, {ledger.usd(ledger.spent(record))} USD reported.")
