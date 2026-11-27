"""`openajar environments login ...`: the owner binds vault logins to agent environments, and signs one
in again (openajar.environments.logins). Only the owner runs these, at the node; no tool binds a login.

    openajar environments login add github.com --user me@example.com --title "Shop fix" [--totp]
    openajar environments login add https://github.com/login --repo github.com/me/shop
    openajar environments login add github.com --entry "github work" --all     (a login already in the vault)
    openajar environments login list
    openajar environments login remove github.com [--title "Shop fix"]
    openajar environments login "Shop fix" github.com                          (sign it in now)

The password (and a TOTP seed) is read from a hidden prompt, or from stdin with --stdin (the password
on the first line, the seed on the second), never from the command line.

Its group joins `openajar environments` (openajar.environments.cli's app, with prepared_cli's and
resume_cli's commands; openajar.commands.features mounts this module).
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from typing import Annotated, Any

import click
import typer
from typer.core import TyperGroup

from openajar.core import catalog
from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.environments import adopt_cli as _adopt  # noqa: F401  (its adopt joins the same app)
from openajar.environments import envfiles_cli as _envfiles  # noqa: F401  (its env group joins the same app)
from openajar.environments.resume_cli import _post, app


class _LoginGroup(TyperGroup):
    """`login TITLE SITE` signs in now: a first word that is not a subcommand goes to `now`."""

    def resolve_command(self, ctx: click.Context, args: list[str]) -> Any:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["now", *args]
        return super().resolve_command(ctx, args)


login_app = typer.Typer(cls=_LoginGroup, no_args_is_help=True, help=(
    "Logins an environment's browser signs in with, from the vault: bind them to computers by title, repository "
    "or all. `openajar environments login TITLE SITE` signs one in now."))

TOTP_PROMPT = "TOTP seed, or its otpauth:// link (not shown): "


def _fail(e: OpenajarError) -> typer.Exit:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    return typer.Exit(1)


def _vault() -> Any:
    from openajar.commands.base import cli_runtime

    vault = cli_runtime().get("vault")
    if vault is None:
        raise _fail(InvalidRequest("this node has no vault yet; run `openajar vault init` first"))
    return vault


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except OpenajarError as e:
        raise _fail(e) from None


async def _unsealed(vault: Any) -> None:
    unsealed = getattr(vault, "_unsealed", None)
    if callable(unsealed):
        await unsealed()


def _scope(title: str | None, repo: str | None, all_: bool) -> dict:
    from openajar.environments import logins

    try:
        return logins.scope_of(title, repo, all_)
    except OpenajarError as e:
        raise _fail(e) from None


def _title_scope(title: str) -> dict:
    """--title as the owner means it: that title, and the agent (grant family) whose computer has it now."""
    from openajar.environments import logins
    from openajar.environments.cli import _managers

    try:
        return logins.title_scope(_managers()[0], title)
    except OpenajarError as e:
        raise _fail(e) from None


def read_values(site: str, user: str | None, *, totp: bool, stdin: bool) -> tuple[str, str | None]:
    """(password, TOTP seed or None), from a hidden prompt or stdin: never from argv."""
    if stdin:
        password = sys.stdin.readline().strip()
        seed = sys.stdin.readline().strip() if totp else None
    else:
        who = f" for {user}" if user else ""
        password = getpass.getpass(f"Password{who} at {site} (not shown): ").strip()
        seed = getpass.getpass(TOTP_PROMPT).strip() if totp else None
    if not password:
        raise _fail(InvalidRequest("the password is empty; nothing was stored"))
    if totp and not seed:
        raise _fail(InvalidRequest("--totp was given but the seed is empty; nothing was stored"))
    return password, seed


Title = Annotated[str | None, typer.Option("--title", help="The title of a computer an agent has now (or its id): "
                                           "that agent's computer of that title, across saves and resumes.")]
Repo = Annotated[str | None, typer.Option("--repo", help="Every environment of this repository, like "
                                          "github.com/me/shop.")]
All = Annotated[bool, typer.Option("--all", help="Every environment.")]


@login_app.command("add")
def add(site: Annotated[str, typer.Argument(metavar="SITE", help="The site (github.com) or its login page's address.")],
        title: Title = None, repo: Repo = None, all_: All = False,
        user: Annotated[str | None, typer.Option("--user", help="The username (shown to the agent; not a secret).")] = None,
        totp: Annotated[bool, typer.Option("--totp", help="Also store its TOTP seed, so codes fill too.")] = False,
        entry: Annotated[str | None, typer.Option("--entry", help="Bind a login already in the vault (its name) "
                                                  "instead of typing it again.")] = None,
        name: Annotated[str | None, typer.Option("--name", help="The login's name (default: the site), for a "
                                                 "second login of one site.")] = None,
        stdin: Annotated[bool, typer.Option("--stdin", help="Read the password (and on the next line the seed) from "
                                            "stdin instead of a prompt.")] = False,
        replace: Annotated[bool, typer.Option("--replace", help="Replace the stored password of this login.")] = False
        ) -> None:
    """Bind a login to computers: their browser signs in with it, and nothing asks when it does.

    A binding by --title names the computer an agent has with that title now (open or saved): that agent's
    computer, across its saves and resumes, and never another agent's of the same title. --repo covers
    every environment of that repository, whichever agent opened it; --all every one. Storing it is the
    permission to use it."""
    from openajar.environments import logins

    scope = _scope(title, repo, all_)
    if title:
        scope = _title_scope(scope["title"])
    try:
        domain, url = logins.parse_site(site)
    except OpenajarError as e:
        raise _fail(e) from None
    vault = _vault()

    async def go() -> Any:
        if entry is not None:
            await _unsealed(vault)
            return await logins.bind(vault, entry, scope, user=user, url=url), "bound"
        found = await logins.existing(vault, site, user, name)
        if found is not None and not replace:
            if any(s == scope for s in found.scopes):
                return found, "already"
            await _unsealed(vault)
            return await logins.bind(vault, found.ref, scope, url=url), "bound"
        password, seed = read_values(domain, user, totp=totp, stdin=stdin)
        await _unsealed(vault)
        if found is not None:  # --replace: a new password, and the bindings it had stay
            out = await logins.add(vault, site, user=user, password=password, totp=seed, scope=scope, replace=True,
                                   name=name)
            for s in found.scopes:
                if not logins.same_scope(s, scope):
                    out = await logins.bind(vault, out.ref, s)
            return out, "replaced"
        return await logins.add(vault, site, user=user, password=password, totp=seed, scope=scope,
                                name=name), "stored"

    login, how = _run(go())
    words = {"stored": "Stored", "replaced": "Stored a new password for", "bound": "Bound",
             "already": "Already bound:"}[how]
    typer.echo(f"{words} {login.name} for {logins.scope_words(scope)}. Its browser signs in with it when the agent "
               "calls login, and nothing asks anyone.")
    if how == "already":
        typer.echo("To change its password: the same command with --replace.")
    typer.echo(f"Sign one in now: openajar environments login \"<title>\" {login.site}")


@login_app.command("list")
def list_cmd(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The logins bound to environments: name, site, username and which computers, never a secret."""
    from openajar.environments import logins

    rows = [{**b.owner_view(), "environments": [logins.scope_words(s) for s in b.scopes]}
            for b in _run(logins.all_bound(_vault()))]
    if json_out:
        typer.echo(json.dumps({"logins": rows}, indent=2))
        return
    if not rows:
        typer.echo("No login is bound to environments. Bind one: openajar environments login add <site> --user <name> "
                   "--title \"<environment title>\"")
        return
    for r in rows:
        totp = ", TOTP" if r["totp"] else ""
        user = f" ({r['user']})" if r["user"] else ""
        typer.echo(f"{r['name'] + user:<40} {r['site'] + totp:<22} {'; '.join(r['environments'])}")


@login_app.command("remove")
def remove(ref: Annotated[str, typer.Argument(metavar="SITE_OR_NAME")],
           title: Title = None, repo: Repo = None, all_: All = False) -> None:
    """Take a login off environments: one binding (--title, --repo or --all), or every one.

    A login `add` stored leaves the vault with its last binding; one bound with --entry stays in the vault."""
    from openajar.environments import logins

    scope = _scope(title, repo, all_) if (title or repo or all_) else None
    vault = _vault()

    async def go() -> Any:
        await _unsealed(vault)
        return await logins.remove(vault, ref, scope)

    for b in _run(go()):
        what = logins.scope_words(scope, stored=False) if scope else "every environment"
        typer.echo(f"Removed {b.name} from {what}. A browser signed in already stays signed in until the site "
                   "logs it out.")


@login_app.command("now")
def now(title: Annotated[str, typer.Argument(metavar="TITLE", help="The open environment's title.")],
        site: Annotated[str, typer.Argument(metavar="SITE", help="The bound login's site or name.")],
        json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Sign an environment's browser in now with its bound login (also: openajar environments login TITLE SITE)."""
    out = _post("/api/environments/login", {"title": title, "site": site})
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(signed_in_words(out))


NEEDS_WORDS = {"captcha": "a CAPTCHA", "code": "a code sent to you (email or text)", "totp": "a one-time code",
               "human": "a step only you can do"}


def signed_in_words(out: dict) -> str:
    """What the CLI says about a sign-in now."""
    env, login = out.get("environment") or {}, out.get("login") or {}
    where = f"\"{env.get('title')}\" ({env.get('id')})"
    if out.get("ok"):
        return f"Signed in to {login.get('name')} in {where}."
    link = (out.get("links") or {}).get("browser")
    tail = f" Live view: {link}" if link else ""
    if out.get("needs"):
        return (f"{login.get('name')} in {where} needs {NEEDS_WORDS.get(str(out['needs']), 'you')}: finish it in "
                f"the live view.{tail}")
    if out.get("step") == "no_login_form":
        return f"No login form on the page in {where}: the site may have it signed in already.{tail}"
    return f"The sign-in to {login.get('name')} in {where} stopped at {out.get('step')}.{tail}"


app.add_typer(login_app, name="login")
