"""MCP tools for agent environments: environment_open, environment, environment_close, environment_save,
environment_forget.

They are in the everyday list (the core toolset). One chat holds a live machine: environment_open with
a new title opens one and gives this chat its handle, `on`; only calls that carry it reach the machine
(openajar.environments.picking). A client that sends an MCP session id holds it by the session too, so
its calls without `on` go there. The title of a live machine another chat holds is environment_in_use
(take_over=true moves it once that chat has been quiet), and a saved one's title resumes it into a new
machine leased to the caller."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from openajar.core import progress
from openajar.core.errors import NotFound, OpenajarError, PolicyDenied
from openajar.core.models import Effect
from openajar.core.progress import StillWorking
from openajar.core.tools import ToolContext, ToolSpec, current_elicitor, frozen_off, refuse_frozen
from openajar.environments import caller
from openajar.environments.errors import EnvironmentFull, NoEnvironment, RepoNotConnected
from openajar.environments.manager import EnvironmentManager, check_ref, check_title, is_session
from openajar.environments.models import ID_RE, SIZES, WORKSPACE, Environment
from openajar.environments.picking import handle_env, lease_of, new_handle
from openajar.environments.routing import env_ref

WAIT_MAX_S = 200.0  # a call never runs past about 240 s: a client gives up at 300 (openajar.core.progress)
CALLS_SHOWN = 10


def _mgr(ctx: ToolContext) -> EnvironmentManager:
    return ctx.runtime.require("environments")


def _subject(ctx: ToolContext) -> Any:
    g = caller.grant_of(ctx)
    try:
        return g.subject if g is not None else None
    except Exception:  # noqa: BLE001 - a grant without a subject block reads as "an agent"
        return None


def _agent(ctx: ToolContext) -> str:
    name = getattr(_subject(ctx), "id", None) or getattr(getattr(ctx, "principal", None), "id", None)
    return str(name) if name and name != "owner" else "an agent"


def _member(ctx: ToolContext) -> str | None:
    """The member whose agent this is (the grant's subject owner), or None for the owner's agents."""
    sub = _subject(ctx)
    owner = getattr(sub, "owner", None)
    if getattr(sub, "kind", None) == "human" or not owner or owner == "owner":
        return None
    return str(owner)


def _client() -> str | None:
    e = current_elicitor()
    return getattr(e, "client", None) if e is not None else None


def _mine(ctx: ToolContext, env: Environment) -> bool:
    """Is it this caller's grant family's? Over MCP nobody else's is, the node's token included."""
    return EnvironmentManager.reaches(env, caller.grant_of(ctx), mcp=True)


def _find(ctx: ToolContext, on: str | None, *, need: bool = True) -> Environment | None:
    """The environment `on` names when this caller holds it (its handle; a session's own by id or
    env/<id>; a closed one of the family by its id, for what it cost), else the session's own. A
    client with no session and no `on`: which_environment while the agent has a machine open (`need`),
    or None (environment() then only lists). None: none."""
    mgr = _mgr(ctx)
    if on:
        ref = " ".join(str(on).split())
        env = mgr.reach(ref, caller.binding(ctx), caller.family(ctx))
        eid = ref.split("/", 1)[1].removesuffix("-browser") if ref.startswith(WORKSPACE + "/") else ref
        if env is None and ID_RE.match(eid):  # a closed one by its id: what it cost, what it pushed
            with contextlib.suppress(NotFound, ValueError):
                env = mgr.get(eid)
            if env is not None and (env.open or not _mine(ctx, env)):
                env = None
        if env is None:
            raise NotFound(f"no environment of yours is named \"{ref[:80]}\". on takes the on your environment_open "
                           "gave; environment() lists your agent's machines")
    elif need:
        env = mgr.pick_or_none(caller.binding(ctx), caller.family(ctx))
    else:
        env = mgr.live_bound(caller.binding(ctx), caller.family(ctx)) if is_session(caller.binding(ctx)) else None
    if env is not None:
        refuse_frozen(ctx, lanes=env.lanes.values())  # a freeze of its lanes or of `env` holds it as is
    return env


def _price(plan: dict, provider: str = "") -> str:
    price = plan.get("price_hourly_eur")
    if price is None:
        return ""
    if price == 0:
        return ", free (your own machine)" if provider == "own" else ", free (a machine on this computer)"
    hours = -(-plan["ttl_minutes"] // 60)
    worst = max([price, *[q or 0 for _n, q in plan.get("fallbacks") or []]])
    dearer = f"; at most EUR {worst * hours:.2f} if only a dearer type has room" if worst > price else ""
    return f", EUR {price:.4f} an hour (EUR {price * hours:.2f} for {plan['ttl_minutes']} min{dearer})"


LINK_REUSE_S = 120.0  # an environment's live-view link is handed out again while it has this long left


def _view_link(ctx: ToolContext, env: Environment) -> Any:
    """The environment's live-view link (a ViewLink): the one handed out before while it is still valid
    for at least LINK_REUSE_S, else a new one, kept for the next call. So environment() gives the same
    link call after call, and the tab the human opened is the one the agent keeps naming."""
    from openajar.node.viewer_tokens import TokenError, ViewMode, ViewTokens

    config = ctx.runtime.require("config")
    tokens = ctx.runtime.get("viewer_links") or ViewTokens.for_config(config)
    held = _mgr(ctx).view_links.get(env.browser_lane)
    if held is not None:
        try:
            tokens.verify(held.token, env.browser_lane)  # signed with today's key, and not expired
            if min(held.expires_at, held.hard_until) - tokens.clock() >= LINK_REUSE_S:
                return held
        except TokenError:
            pass
    link = tokens.issue(env.browser_lane, ViewMode.CONTROL)
    _mgr(ctx).view_links[env.browser_lane] = link
    return link


def link_note(link: Any) -> str:
    """How long links.browser works, in the words the agent passes on (docs/ENVIRONMENTS.md)."""
    from openajar.node.viewer_tokens import iso

    return (f"links.browser stays the same on every call until shortly before it expires ({link.expires_iso}); a "
            "link given earlier keeps working until its own expiry. A page the human has open renews itself "
            f"until {iso(link.hard_until)}, 30 minutes after its link was made; after that give them the new "
            "links.browser")


def _links(ctx: ToolContext, env: Environment) -> dict:
    """`browser`: a signed live-view link the human can open (in a browser or T3 Code's preview
    pane). The node's own viewer, so it works without the web app; before the browser's first page
    it says the browser is not running yet and shows it once it is. None until the browser lane
    exists (environment() gives it then). The same link comes back while it is valid (_view_link),
    with `browser_expires_at` and `browser_note`: how long it and the ones before it work."""
    links = _mgr(ctx).links(env)
    lanes = getattr(ctx.runtime, "lanes", None)
    if lanes is None or not callable(getattr(lanes, "take_control", None)):
        return links
    try:
        lanes.get(env.browser_lane)  # LaneNotFound: no browser lane yet
        from openajar.node.control_tools import take_over_link

        view = _view_link(ctx, env)
        link = take_over_link(ctx.runtime, env.browser_lane, control=True, link=view)
    except Exception:  # noqa: BLE001 - a link is a convenience; the environment works without one
        return links
    if link.get("url"):
        links["browser"] = link["url"]
        links["browser_expires_at"] = view.expires_iso
        links["browser_note"] = link_note(view)
    return links


def compare_link(env: Environment, branch: str) -> str | None:
    """GitHub's form for a pull request of a branch this environment pushed (environment_pull_request
    opens a real one), or None (not on
    GitHub, or the default branch it was opened on)."""
    host, _, repo = (env.repo or "").partition("/")
    if host != "github.com" or repo.count("/") != 1 or not branch or (env.ref is None and branch == env.branch):
        return None
    from urllib.parse import quote

    return f"https://github.com/{repo}/compare/{quote(branch, safe='/._-')}?expand=1"


def _with_compare(env: Environment, pushed: list[dict]) -> tuple[list[dict], str | None]:
    """`pushed` with each branch's compare link, and the newest one's (pushes are newest first)."""
    out, newest = [], None
    for p in pushed:
        link = compare_link(env, str(p.get("branch") or "")) if set(str(p.get("commit") or "")) != {"0"} else None
        out.append({**p, "compare": link} if link else p)
        newest = newest or link
    return out, newest


def _proofs(mgr: EnvironmentManager, env: Environment) -> list[dict]:
    try:
        return mgr.proofs(env)
    except Exception:  # noqa: BLE001 - the environment's state without its proofs rather than none
        return []


def _keep_next(env: Environment) -> str:
    if env.keep == "save":
        return (". keep is save: environment_close() when the task is done saves it (disk and files) and deletes the "
                "machine, as idle would later; environment_open with this title resumes it")
    if env.keep == "live":
        return ". keep is live: environment_close() only lets it go (the machine stays); final=true deletes it"
    return ""


def _differs(mgr: EnvironmentManager, env: Environment, *, ref: str | None, size: str | None,
             hours: float | None, keep: str | None) -> tuple[dict, list[str]]:
    """What an environment_open asked for that the environment it got does not have, in words, and
    what it has instead ({} and [] when all of it holds). An attach keeps how it was opened; a
    resume keeps the saved machine and branch."""
    from datetime import timedelta

    applied: dict = {}
    words: list[str] = []
    if keep is not None and keep != env.keep:
        applied["keep"] = env.keep
        words.append(f'keep: you asked for "{keep}", and it stays "{env.keep}" as it was opened')
    if hours is not None:
        wanted = mgr._clock() + timedelta(hours=float(hours))
        if abs((env.expires_at - wanted).total_seconds()) > 600:
            applied["expires_at"] = mgr.summary(env)["expires_at"]
            words.append(f"hours: you asked for {float(hours):g}, and it lives until {applied['expires_at']} as it was "
                         "opened (it closes sooner when idle)")
    if size:
        name = str(size).strip().lower()
        have = env.machine.size if name in SIZES else env.machine.server_type
        if have != name:
            applied["size"] = env.machine.size or env.machine.server_type
            words.append(f"size: you asked for {name}, and it has {env.machine.cpu} CPU and "
                         f"{env.machine.memory_gb:g} GB ({applied['size']})" if env.machine.memory_gb is not None
                         else f"size: you asked for {name}, and it is {applied['size']}")
    if ref is not None and ref not in (env.ref, env.branch):
        applied["ref"] = env.ref or env.branch
        words.append(f"ref: you asked for {ref}, and it was opened at {env.ref or 'the default branch'}; "
                     f"switch with git checkout in {env.workdir}")
    return applied, words


def _on_words(ctx: ToolContext, handle: str) -> str:
    """How the agent's calls reach this computer: with its handle. A client with no session id (MCP
    2026-07-28, stdio) looks like every other chat of the agent, so only the handle reaches it."""
    if is_session(caller.binding(ctx)):
        return (f"calls without `on` go here (this chat's session holds it); on=\"{handle}\" works too.")
    return (f"keep on=\"{handle}\" and pass it on every call: only calls that carry it reach this machine, and "
            "a call without `on` is refused.")


def _with_ssh(mgr: EnvironmentManager, env: Environment, out: dict) -> None:
    """Local mode: the holder's own shell reaches the machine (openajar.environments.sshaccess). Only in
    results for the environment's holder; a server node gives none."""
    ssh = mgr.ssh_of(env)
    if ssh is None:
        return
    out["ssh"] = ssh
    if "next" in out:
        out["next"] += (f". You may also use your own shell: {ssh['command']}, and scp, rsync and git over it "
                        "(ssh.examples); what you run there is not in openajar's call log")


def _open_result(ctx: ToolContext, mgr: EnvironmentManager, env: Environment, handle: str,
                 bigger: list[dict] | None = None, *, opened: str = "new", applied: dict | None = None,
                 differs: list[str] | None = None) -> dict:
    s = mgr.summary(env)
    machine = {**env.machine.model_dump(), "price_hourly_eur": env.price_hourly_eur, "bigger": bigger or [],
               **mgr.machine_notes(env)}  # fell_back: another type had room, and what it costs
    if bigger:
        machine["next"] = (f"environment_open(title=<a new title>, size=\"{bigger[0]['size']}\") opens a bigger "
                           "machine")
    out = {"id": env.id, "title": env.title, "opened": opened, "state": "ready" if env.state == "ready" else env.state,
           "on": handle,
           "workdir": env.workdir, "repo": env.repo, "branch": env.branch, "machine": machine,
           "expires_at": s["expires_at"], "links": _links(ctx, env), "keep": env.keep}  # no lane names: `on` is the way in
    if differs:
        out["applied"], out["differs"] = applied or {}, differs
    if env.resumed:
        out["resumed"] = env.resumed
    out.update(mgr.prepared_view(env))  # prepared and app (openajar.environments.preparing)
    app = env.app or {}
    if env.state == "ready":
        out["next"] = ("Give the human links.browser, the live view of this environment's browser; if your client "
                       "can open a page beside the chat (T3 Code: preview_open), open it there. "
                       "file_read, file_edit and file_write work on files, not cat or sed (a relative path starts in "
                       f'{env.workdir}); terminal_exec(cmd="git status") runs a command, in {env.workdir} too '
                       "(cwd=\"sub/folder\" starts under it), and terminal_job_start(cmd=<the dev server's or the "
                       "long build's command>) runs one in the background past any call's 200 s (terminal_job_read "
                       "reads its output). They are in your list; " + _on_words(ctx, handle) + " The other "
                       "terminal tools: tools_more(\"terminals\"). "
                       "http://localhost:<port> in browser_navigate and in ui_check (tools_more(\"studio\")) is the "
                       "machine's own port; browser_screenshot(proof=\"...\") keeps a shot with a link for the human. "
                       "Push a branch, environment_pull_request(title) opens its pull request (give the human the url), then "
                       "environment_close() as your last call unless the human asked to keep the machine open. "
                       "environment_port(3000) gives the human a link to a dev server. "
                       "Nothing here waits for a person, a push to any branch included; a delete or a force "
                       "push of a branch this environment did not create is refused, so push your work to a new "
                       "branch")
        # code from the agent's own checkout, uncommitted work included (openajar.environments.uploads)
        out["next"] += ('. Code in your own checkout: environment_upload() gives a link and the '
                        "commands that pack and send it")
        if env.resumed:
            out["next"] = ("Resumed: read resumed.note (git, files, services) and resumed.missing first. " + out["next"])
        out["next"] += _keep_next(env)
        if app.get("listening"):
            out["next"] = (f"The app runs already: `{app.get('command')}` is job {app.get('job_id')}, listening on port "
                           f"{app.get('port')}" + (f"; give the human app.link ({app['link']})" if app.get("link") else "")
                           + ". " + out["next"])
        elif app:
            out["next"] = f"The app did not come up: read app.why and app.log_tail and fix it. {out['next']}"
        _with_ssh(mgr, env, out)
    if opened in ("attached", "taken_over"):
        if opened == "taken_over":
            words = (f"Taken over: another chat of your agent held it and made no call for at least "
                     f"{mgr.settings.take_over_minutes} min. Its work is on it as that chat left it: read environment"
                     f"(on=\"{handle}\") and git status before you change anything. That chat's on no longer works. ")
        else:
            words = "Attached: this machine was already open with this title, and it is this chat's now, as it was. "
        if differs:
            words += ("Not as you asked: " + "; ".join(differs) + ". For a computer with what you asked, call "
                      "environment_open with another title. ")
        out["next"] = words + out.get("next", "")
    elif differs:
        out["next"] = "Resumed as it was saved. Not as you asked: " + "; ".join(differs) + ". " + out.get("next", "")
    if env.state == "starting" and env.restore_from:
        how = "the paused machine wakes" if (env.resumed or {}).get("from") == "paused" else \
            "a new machine, then the saved work"
        out["next"] = out.get("next", "") + (f"resuming from save {env.restore_from} ({how}): "
                                             f"environment(on=\"{handle}\") waits up to a minute for ready and "
                                             "then shows resumed.note")
    elif env.state == "starting":
        out["next"] = out.get("next", "") + ("still starting (the machine boots and installs its tools): "
                                             f"environment(on=\"{handle}\") waits up to a minute for ready, then gives "
                                             "links.browser for the human. Keep this on and pass it on every call")
    return out


async def environment_open(ctx: ToolContext, *, title: str | None = None, repo: str | None = None,
                           ref: str | None = None, size: str | None = None, hours: float | None = None,
                           wait_s: float = 240, keep: str | None = None, signins: bool | None = None,
                           take_over: bool = False, network: str | None = None) -> dict:
    """Your own cloud computer: a VM with a terminal, a browser and the repo, to build and test on
    instead of the user's laptop. It returns `on`, this machine's whole handle: keep it and pass
    on="<it>" on every call (not its id), since only calls that carry it reach the machine. A new
    title opens one; a saved one's title resumes it (`resumed`). localhost there is the VM. Nothing
    in it waits for a person. State starting: environment(on=...) waits for ready.
    links.browser is its live view, for the human; machine.bigger lists sizes and prices; `differs`
    says what is not as you asked. A private repo the node cannot read: approval_pending with a
    review_url for the human.

    Args:
        title: A few words people see, like "Black Friday page"; it names the computer. Without it,
            repo finds that repo's environment (several: a list to pick from).
        repo: To clone, like github.com/me/shop. Leave it out for an empty /work.
        ref: A branch, tag or commit (default: the default branch).
        size: small (2 CPU, 4 GB), medium (4, 8), large (8, 16), xlarge (16, 32), or a server type
            like cpx42. Only when the work needs more.
        hours: How long it may live (default 2); it closes sooner when idle.
        wait_s: Seconds to wait for ready, 0 to 200.
        keep: save (default: saved when idle or closed, its title resumes it), none (deleted when done),
            live (kept running if allowed).
        signins: false: saves keep no sign-ins.
        take_over: Take a machine another chat of yours holds but has not used for 10 minutes, once
            that chat is done with it.
        network: limited or none (only narrows the node's).
    """
    mgr = _mgr(ctx)
    title = check_title(title if title else title_for_repo(ctx, mgr, repo))
    ref = check_ref(ref)
    binding = caller.binding(ctx)
    family = caller.family(ctx)
    wait = max(0.0, min(float(wait_s), WAIT_MAX_S))
    if repo:
        from openajar.git.wire import parse_remote

        repo = parse_remote(repo).slug
    # the call's own argument errors first: a size past the limit is never hidden by an attach
    await mgr.check_open_args(size=size, hours=hours, keep=keep)
    level = mgr.network_for_open(network, repo)  # narrowed only, never widened (openajar.environments.network)
    loop = asyncio.get_running_loop()
    began, last = loop.time(), False
    gone: dict = {}  # a machine of this title that stopped, ended by this open
    while True:
        try:
            async with mgr.opening(family, title):  # one open at a time per title: a second one is environment_in_use
                got = await _open_locked(ctx, mgr, binding, family, title=title, repo=repo, ref=ref, size=size,
                                         hours=hours, keep=keep, signins=signins, take_over=take_over, network=level,
                                         gone=gone)
            break
        except EnvironmentFull:  # the node runs all the machines it may: wait for one to free, up to wait_s
            waited, slot_wait = loop.time() - began, min(wait, mgr.slot_wait_s)
            if waited >= slot_wait:
                if slot_wait > 0:
                    mgr.check_count(waited_s=waited)  # still full: the words say how long it waited
                    if not last:  # one freed just now: one more try
                        last = True
                        continue
                raise
            progress.step(f"waiting for a free machine: the node runs as many as it may ({waited:.0f} s so far)")
            await asyncio.sleep(min(mgr.slot_poll_s, slot_wait - waited))
    if isinstance(got, dict):  # a card for the human to connect the repository: nothing was rented
        return got
    env, opened, handle = got
    was_open = opened in ("attached", "taken_over")
    applied, differs = ({}, []) if opened == "new" else _differs(
        mgr, env, ref=ref, size=size, hours=hours if was_open else None, keep=keep if was_open else None)
    if env.state == "starting":
        env = await mgr.wait_ready(env.id, max(0.0, wait - (loop.time() - began)))
    if env.state == "failed":  # an error, never a result that looks like an open (environment() lists it)
        from openajar.environments.failures import error_for

        raise error_for(env)
    bigger: list[dict] = []
    with contextlib.suppress(Exception):  # a convenience: the environment works without it
        bigger = await mgr.bigger_sizes(env)
    out = _open_result(ctx, mgr, env, handle, bigger, opened=opened, applied=applied, differs=differs)
    if gone:
        out["replaced"] = gone
        how = ("resumed the title's newest save" if opened == "resumed" else
               f"has {env.repo} cloned fresh" if env.repo else "starts empty")
        out["next"] = (f"New machine: the one this title had stopped ({gone['why']}), so it was ended, and this one "
                       f"{how}; redo what was lost since. " + out.get("next", ""))
    if logins := await mgr.login_names(env):  # the owner's bound logins, by name (openajar.environments.logins)
        out["logins"] = logins
        out["next"] = (f"Signed-in accounts: login(on=\"{handle}\") signs this browser in to "
                       f"{', '.join(repr(x['name']) for x in logins)} (entry=<name> when several), again "
                       "whenever a site logs it out; it asks nobody. " + out.get("next", ""))
    return out


async def _open_locked(ctx: ToolContext, mgr: EnvironmentManager, binding: str, family: str | None, *, title: str,
                       repo: str | None, ref: str | None, size: str | None, hours: float | None,
                       keep: str | None = None, signins: bool | None = None, take_over: bool = False,
                       network: str = "full", gone: dict | None = None) -> tuple[Environment, str, str] | dict:
    # a machine that stopped is never handed back: it is ended, and the open goes on as one with no live
    # machine, from the title's newest save when there is one (openajar.environments.liveness)
    replaced = await mgr.replace_gone(title, family, repo)
    if replaced is not None and gone is not None:
        gone.update(replaced)
    # the live one with this title (starting or ready, only with the repository asked for): this chat's
    # own, one let go, or another chat's with take_over once it is quiet; else environment_in_use
    found = mgr.reattach(binding, title, family, repo, take_over=take_over)
    if found is not None:
        env, how = found
        refuse_frozen(ctx, lanes=env.lanes.values())
        await ctx.authorize(Effect.READ, summary=("take over" if how == "taken_over" else "use")
                            + f" environment {env.id} ({env.title})")
        env, handle = mgr.give(env, binding, take=how == "taken_over")
        ctx.trace("environment_open", lane=env.terminal_lane, environment=env.id, opened=how)
        return env, how, handle
    env_id = mgr.planned_id(binding, title)
    saved = mgr.save_to_resume(family or caller.grant_id(ctx), title, repo)
    if saved is not None:  # this family saved this title: resume it (its repository, branch and machine)
        keep = mgr.check_keep(keep if keep is not None else saved.keep)
        repo, ref = saved.repo, saved.ref
        plan = await mgr.plan_restore(env_id, saved, hours=hours, keep=keep)
    else:
        keep = mgr.check_keep(keep)
        plan = await mgr.plan(env_id, size=size, hours=hours, keep=keep)
    mgr.check_network_plan(network, plan)
    mgr.check_limits(plan)
    await mgr.check_room(plan)
    sp = plan.get("specs") or {}
    specs = f" ({sp.get('cores')} vCPU, {sp.get('memory_gb'):g} GB)" if sp.get("cores") and sp.get("memory_gb") else ""
    prov = mgr.vms().provider()
    provider = getattr(prov, "name", "")
    whole = getattr(prov, "whole", None)
    what = (f"take your machine {plan['location']} as a whole{specs}" if callable(whole) and whole(plan["location"])
            else f"start a container{specs} on your machine {plan['location']}" if provider == "own"
            else f"rent a {plan['server_type']} machine{specs} in {plan['location']}")
    summary = (("Resume" if saved is not None else "Open") +
               f" environment {env_id} \"{title}\": {what} for up to {plan['ttl_minutes']} min"
               f"{_price(plan, provider)}, with a terminal and a browser"
               + (f", and clone {repo}" + (f" at {ref}" if ref else "") if repo else "") + ".")
    # Opening one never asks a person (the owner's decision of 2026-11-13, docs/design/environments.md):
    # the node's limits above bound it instead (how many at once, the longest life, the month's budget),
    # and they hold whatever the agent read. The grant must still hold the vm special for this lane
    # (and its VM profiles); the call is then recorded as a read of the lane, with what it rents.
    lane = f"env/{env_id}"
    check = getattr(ctx, "precheck", None)
    if callable(check):
        check(Effect.VM, lane_id=lane, facts={"profile": plan["profile"]})
    if repo:  # after the limits and the grant (both local), before anything is rented: repo_not_connected
        from openajar.environments import connect

        with contextlib.suppress(Exception):  # a card this agent raised for another repo: a name it got wrong
            connect.withdraw(mgr.store, ctx.runtime.get("approvals"), caller.grant_id(ctx), instead=repo)
        try:
            await mgr.check_repo(repo)
        except RepoNotConnected as e:  # a card the human answers in the panel (openajar.environments.connect)
            from openajar.environments.connect import card_result

            card = await card_result(ctx, mgr, repo, e)
            if card is None:
                raise
            return card
    prepared = None
    if repo and saved is None:  # its .openajar/environment.toml, and a prepared machine to boot (openajar.environments.preparing)
        prepared = await mgr.prepared_for(env_id, repo, ref, grant_id=caller.grant_id(ctx))
        plan = await mgr.plan_prepared(env_id, plan, prepared, size=size, hours=hours, keep=keep)
    await ctx.authorize(Effect.READ, lane_id=lane, summary=summary,
                        content={"environment_open": title, "repo": repo, "ref": ref,
                                 "server_type": plan["server_type"], "ttl_minutes": plan["ttl_minutes"],
                                 "price_hourly_eur": plan.get("price_hourly_eur"), "image": plan["image"]})
    acted = getattr(ctx, "acted", None)
    if callable(acted):
        acted(Effect.VM)  # the call's record says what it did: it rented a machine
    g = caller.grant_of(ctx)
    handle = new_handle(env_id)  # this chat's lease: only calls that carry it reach the machine
    env = await mgr.create(env_id, plan, binding=binding, title=title, grant_id=caller.grant_id(ctx),
                           lease=lease_of(handle), grant_root=getattr(g, "root_id", None), agent=_agent(ctx), client=_client(),
                           repo=repo, ref=ref, subject=getattr(_subject(ctx), "id", None), member=_member(ctx),
                           keep=keep, resumed=mgr.resuming(saved, plan) if saved is not None else None,
                           prepared=prepared)
    keep_signins = signins if signins is not None else (saved.keep_signins if saved is not None else True)
    if not keep_signins:  # before its bring-up runs: a resume then puts no sign-ins back either
        env = mgr.store.update(env.id, lambda e: setattr(e, "keep_signins", False))
    ctx.trace("environment_open", lane=env.terminal_lane, environment=env.id, server_type=plan["server_type"])
    return env, "resumed" if saved is not None else "new", handle


def title_for_repo(ctx: ToolContext, mgr: EnvironmentManager, repo: str | None) -> str:
    """environment_open's title when it names none: this chat's environment's, else the one environment
    of the caller's family for `repo`, open or saved (openajar.environments.comeback.by_repo), else the
    repository's name for a new one. Raises environment_choice with several."""
    from openajar.environments import comeback

    # a client with a session id: its own computer; without one, every chat looks the same, so only the repo
    mine = mgr.live_bound(caller.binding(ctx), caller.family(ctx)) if is_session(caller.binding(ctx)) else None
    if mine is not None:
        return mine.title
    if not repo:
        from openajar.core.errors import InvalidRequest

        raise InvalidRequest('environment_open needs a title, like "Black Friday page", or a repo to find its '
                             "environment by: environment() lists the open and saved ones")
    from openajar.git.wire import parse_remote

    return comeback.by_repo(mgr, caller.family(ctx), parse_remote(repo).slug, saves_of=caller.family(ctx) or caller.grant_id(ctx),
                            reaches=lambda e: _mine(ctx, e) and not frozen_off(ctx, lanes=e.lanes.values()))


def _family_lists(ctx: ToolContext, mgr: EnvironmentManager, here: Environment | None = None) -> dict:
    """The caller's family's open environments and newest saves. A live machine the caller does not
    hold shows no handle and no id: its title, state and who has it (`use`). The one it holds (`here`,
    reached with its handle or its session) is `this_chat`."""
    mine = [e for e in mgr.open_for_family(caller.family(ctx))
            if _mine(ctx, e) and not frozen_off(ctx, lanes=e.lanes.values())]
    saved = [{k: r[k] for k in ("title", "repo", "branch", "saved_at", "snapshot_gb", "paused", "cost_eur_month",
                                "note")}
             for r in mgr.saved_for(caller.family(ctx) or caller.grant_id(ctx)) if r["latest"]]
    open_, now, binding = [], mgr._clock(), caller.binding(ctx)
    for e in mine:
        row = {"title": e.title, "repo": e.repo, "state": e.shown_state(now), "last_call_at": mgr.summary(e)["last_call_at"]}
        if (here is not None and e.id == here.id) or (is_session(binding) and e.binding == binding):
            row = {"id": e.id, **row, "this_chat": True}
        else:
            row["use"] = mgr.use_words(e, now, session=is_session(binding))
        open_.append(row)
    from openajar.environments.failures import failed_rows

    out: dict = {"open": open_, "saved": saved}
    failed = failed_rows(mgr, caller.family(ctx), lambda e: _mine(ctx, e))
    if failed:
        out["failed"] = failed  # opens that did not start in the last hour, with why (environment_open raised it)
    return out


async def _wait_ready(mgr: EnvironmentManager, env: Environment) -> Environment:
    """environment(on) of a machine that is starting: wait for ready up to mgr.ready_wait_s, saying
    what the bring-up does in the call's progress notifications."""
    from openajar.environments.bringup import starting_words

    loop = asyncio.get_running_loop()
    end = loop.time() + mgr.ready_wait_s
    while env.state == "starting" and (left := end - loop.time()) > 0:
        progress.step(f"waiting for ready: {starting_words(env, mgr._clock())}")
        env = await mgr.wait_ready(env.id, min(left, 5.0))
        if env.state == "starting" and not mgr.coming_up(env.id):
            break  # nothing here brings it up (another process, or the reaper starts it again): answer now
    return env


async def environment(ctx: ToolContext, *, on: str | None = None) -> dict:
    """Your environment now: state, minutes_left, cost_eur, ports listening on the VM, git (branch,
    ahead, dirty, pushed, pull_requests), proofs (fresh links), replay, unsaved work, your note and
    the last 10 calls. Cheap: call it to see if it is ready or what is left to push. It always lists
    your agent's open and saved ones (`open`, `saved`).

    Args:
        on: The on environment_open gave. Without it, a client with a session id sees its own.
    """
    await ctx.authorize(Effect.READ, summary="read the state of an environment")
    mgr = _mgr(ctx)
    env = _find(ctx, on, need=False)
    if env is None:
        lists = _family_lists(ctx, mgr)
        out = {"environment": None, **lists}
        if lists["open"] and not is_session(caller.binding(ctx)):
            out["note"] = ("this client sends no session id, so this list cannot tell which machine is yours: yours is "
                           "the one whose on you hold. environment(on=\"<that on>\") shows it in full")
        out["next"] = ("environment_open(title=...) with a new title opens your own" if lists["open"] else
                       "environment_open(title=...)" + (" with one of these titles resumes it; a new title opens "
                                                        "your own" if lists["saved"] else ""))
        return out
    if env.state == "starting":  # a machine coming up: wait here for ready (about a minute), never a poll loop
        env = await _wait_ready(mgr, env)
    elif mgr.link_down(env.id) is not None:  # its link dropped: wait here for it, as for ready
        progress.step("waiting for the link to the machine")
        await mgr.wait_link(env.id, mgr.ready_wait_s)
    out = await mgr.detail(env, calls=CALLS_SHOWN)
    out.pop("viewer", None)
    out.pop("commands", None)
    out["links"] = _links(ctx, env)
    git = out.get("git") or {}
    git["pushed"], compare = _with_compare(env, git.get("pushed") or [])
    if compare:
        git["compare"] = compare
    out["proofs"] = _proofs(mgr, env)
    if (recording := mgr.recording_view(env)) is not None:
        out["recording"] = recording  # openajar.environments.recording: one runs now
    out["replay"] = mgr.replay_summary(env)  # {frames, since, bytes}: the owner watches it, not the agent
    if env.pull_requests:
        git["pull_requests"] = env.pull_requests
    if env.resumed:
        out["resumed"] = env.resumed
    out["note"] = env.note
    out.update(_family_lists(ctx, mgr, env))
    if (ssh := mgr.ssh_of(env)) is not None:
        out["ssh"] = ssh
    if handle_env(on) == env.id:  # it came with its handle: the caller holds it, so it may see it again
        out["on"] = " ".join(str(on).split())
    if out.get("machine_gone"):
        out["next"] = out["machine_gone"]["next"]
    elif (out.get("link") or {}).get("state") == "down":
        out["next"] = out["link"]["next"]
    elif env.state == "starting":
        from openajar.environments.bringup import starting_words

        now = starting_words(env, mgr._clock())
        out["doing"] = now
        out["next"] = (f"{now}. Call environment(on=\"<your on>\") again: it waits up to a minute for ready. "
                       "Terminal and browser calls run only once it is ready (environment_not_ready)")
    elif out.get("unsaved") and env.keep == "none":
        out["next"] = ("save the work before environment_close: push it (git push origin <branch>), or "
                       "environment_download to keep it without a push (unsaved_work's fix says how)")
    elif git.get("compare") and not env.pull_requests:
        out["next"] = ("environment_pull_request(title) opens the pull request for your pushed branch and returns "
                       "its url (git.compare is only GitHub's form); give the human that url and the proofs' links, "
                       "then environment_close()")
    return out


async def environment_close(ctx: ToolContext, *, on: str | None = None, discard: bool = False,
                            final: bool = False, note: str | None = None, forget: bool = False) -> dict:
    """Stop or finish your environment, as the `keep` it was opened with says. Saved: it is saved
    (`pause` says what it costs and how fast it resumes), then the VM is deleted; its title resumes
    it. Live: only let go (final=true deletes it). Single use: the VM is deleted, refused with
    unsaved_work while work is not pushed (push, or discard=true if the user agreed). A long save goes
    on in the background (state closing).

    Args:
        on: The on environment_open gave.
        discard: Delete it even with work that is not pushed (no save).
        final: A live one: delete it too.
        note: A saved one: what you did and what is next, for the next chat (resumed.note).
        forget: Done for good: the VM and every save of its title go.
    """
    mgr = _mgr(ctx)
    env = _find(ctx, on)  # a client with no session id names it with its handle (which_environment without)
    if env is None:
        raise NoEnvironment("no environment is open to close; environment() lists the open and saved ones")
    what = ", forgetting its saves" if forget else (", discarding unsaved work" if discard else "")
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"close environment {env.id} ({env.title}){what}")
    if not _mine(ctx, env):
        raise PolicyDenied(f"environment {env.id} is not yours")
    if note is not None:
        from openajar.environments.saves import NOTE_MAX

        env = mgr.store.update(env.id, lambda e: setattr(e, "note", note[:NOTE_MAX] or None))
    if forget and not discard and env.state == "ready":
        await _refuse_unsaved(mgr, env, "forget=true deletes every save too")
    try:
        env = await mgr.close_bounded(env.id, discard=discard or forget, final=final or forget,
                                      reason="closed by the agent" + (" (forgotten)" if forget else
                                                                      " (discarded)" if discard else ""))
    except StillWorking as still:  # a save with a snapshot goes on in the background
        return still.result
    except OpenajarError as e:  # a refusal is one of its last calls too (environment() lists it)
        mgr.record_call(env.terminal_lane, "environment_close", ok=False, error=e.code,
                        summary="forget" if forget else "discard" if discard else "")
        raise
    ctx.trace("environment_close", lane=env.terminal_lane, environment=env.id, discard=discard, forget=forget)
    if env.state == "failed" and env.dismissed_at is None:  # the agent is done with a failed open: off its list
        env = mgr.store.update(env.id, lambda e: setattr(e, "dismissed_at", mgr._clock()))
    if env.open and env.keep == "live":
        price = env.price_hourly_eur
        from openajar.vms.providers import FREE

        free = env.machine.provider in FREE  # nothing rented: a container on a computer of the owner's
        return {"id": env.id, "title": env.title, "state": env.state, "keep": "live", "machine": "kept",
                "cost_eur": mgr.cost_now(env),
                "pause": ("kept running until a final close (it costs nothing: the computer is the owner's own)" if free
                          else "kept running: it bills" + (f" EUR {price:.4f} an hour" if price else "")
                          + " until a final close"),
                "next": (f"this chat let it go and its machine stays{'' if free else ' (it bills)'}; its on no longer "
                         "works. "
                         f"environment_open(title=\"{env.title}\") gets it back in any chat, with a new on; "
                         "environment_close(on=..., final=true) with that on deletes it")}
    end = env.closed_at or mgr._clock()
    lists = mgr.push_lists(env)
    pushed, _ = _with_compare(env, lists["pushed"])
    out = {"id": env.id, "title": env.title, "state": env.state, "machine": env.vm_state or "unknown",
           "cost_eur": env.cost_eur,
           "minutes": int((end - env.created_at).total_seconds() // 60),
           "pushed": pushed, "deleted": lists["deleted"], "proofs": _proofs(mgr, env)}
    if forget:
        try:
            gone = await mgr.forget(env.family, env.title)
        except NotFound:
            gone = {"forgot": [], "snapshots_deleted": 0}
        out["saves"] = {"made": None, **gone}
        out["next"] = (f"done for good: {_machine_words(mgr, env)} and no save of \"{env.title}\" is left. "
                       f"environment_open(title=\"{env.title}\") would open a new one")
        return out
    if env.gone_at is not None:  # its machine had stopped: this close saved nothing (openajar.environments.liveness)
        left = [r["id"] for r in mgr.saved_for(env.family) if r["title"].lower() == env.title.lower()]
        out["saves"] = {"made": None, "left": left}
        out["next"] = (f"its machine had stopped ({env.gone_why}), so nothing was saved now. environment_open(title="
                       f"\"{env.title}\") starts a new machine on this title"
                       + (f", from its newest save ({left[0]})" if left else ""))
        return out
    if env.keep == "save" and not discard and env.last_save_id:
        out["keep"] = "save"
        out["saved"] = _saved_result(mgr, env.last_save_id)
        out["pause"] = _pause(mgr, out["saved"])
        out["saves"] = {"made": env.last_save_id}
        out["next"] = (f"saved as {env.last_save_id}, and {_machine_words(mgr, env)}. {out['pause']}. "
                       f"Tell the human: say \"continue {env.title} in openajar\"; environment_open(title=\"{env.title}\") "
                       "resumes it in any chat of your agent")
        return out
    left = [{"id": r["id"], "saved_at": r["saved_at"], "snapshot_gb": r["snapshot_gb"],
             "cost_eur_month": r["cost_eur_month"]}
            for r in mgr.saved_for(env.family) if r["title"].lower() == env.title.lower()]
    out["saves"] = {"made": None, "left": left}
    if left:
        out["next"] = (f"deleted without a save{' (discard)' if discard else ''}. {len(left)} earlier "
                       f"save{'s' if len(left) != 1 else ''} of \"{env.title}\" stay, and environment_open with this "
                       f"title resumes the newest; environment_forget(title=\"{env.title}\") removes "
                       f"{'them' if len(left) != 1 else 'it'}")
    return out


async def _refuse_unsaved(mgr: EnvironmentManager, env: Environment, why: str) -> None:
    """forget=true without discard: the work that is not pushed would be lost, so it is refused as a close is."""
    from openajar.environments.errors import UnsavedWork

    repos = await mgr.git_repos(env)
    unsaved = mgr.unsaved_of(repos) if repos is not None else None
    if unsaved is not None:
        raise UnsavedWork(f"environment {env.id} has work that is not pushed: {mgr.unsaved_words(unsaved)}. "
                          f"{why}: push it first, or pass discard=true as well if the user agreed to lose it",
                          unsaved)


def _machine_words(mgr: EnvironmentManager, env: Any) -> str:
    """What became of a closed environment's machine: "the machine is deleted", or, for a machine of the
    owner's handed over whole (openajar.vms.whole), that it is free again (nothing of theirs is deleted)."""
    state = env.vm_state or "unknown"
    whole = getattr(mgr.vms().provider(), "whole", None)
    if state == "deleted" and callable(whole) and whole(env.machine.location):
        return f"your machine {env.machine.location} is free again (its /work is emptied)"
    return f"the machine is {state}"


def _pause(mgr: EnvironmentManager, saved: dict) -> str:
    from openajar.environments import comeback

    if saved.get("paused"):  # the machine itself, asleep (openajar.environments.pausing)
        from openajar.environments.pausing import paused_words

        return paused_words(mgr.saves().find(saved["save_id"])[0])
    snap = saved.get("snapshot") or None
    gb = (snap or {}).get("gb")
    provider = getattr(mgr.vms().provider(), "name", "")
    provider = provider if snap or provider == "own" else ""  # a save of files there goes back onto that machine
    from openajar.vms.providers import FREE

    cost = round(gb * mgr.settings.snapshot_eur_per_gb_month, 4) if gb and provider not in FREE else 0.0
    return comeback.pause_words(cost, snapshot=snap is not None, provider=provider)


def _saved_result(mgr: EnvironmentManager, save_id: str) -> dict:
    """What environment_save returns, and environment_close's `saved`."""
    from openajar.core import clock

    m, _ = mgr.saves().find(save_id)
    snap = {"id": m.snapshot.id, "gb": m.snapshot.gb} if m.snapshot is not None else None
    out = {"save_id": m.id, "saved_at": clock.iso_or_none(m.saved_at, z=True), "snapshot": snap, "files": m.files,
           "bytes": m.bytes, "commits": m.commits, "verified": True}
    if m.paused is not None:  # the machine paused with its memory, in place of a snapshot
        out["paused"] = {"machine": m.paused.vm_id, "cost_eur_month": m.paused.eur_month, "estimate": True}
    return out


async def environment_save(ctx: ToolContext, *, note: str | None = None, on: str | None = None) -> dict:
    """Save the environment now, without closing it: commits and stashes no remote has, changed and
    new files git does not ignore, running services, and with keep "save" a disk snapshot (services
    pause for it). Its title resumes the newest save in any chat. After save_failed nothing was lost:
    the machine works on.

    Args:
        note: For the next chat that resumes it: what you were doing and what is next.
        on: The on environment_open gave.
    """
    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("no environment is open to save; environment() lists the open and saved ones")
    mgr.refuse_gone(env)
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"save environment {env.id} ({env.title})")
    try:
        m = await mgr.save_bounded(env, kind="save", reason="saved by the agent", note=note)
    except StillWorking as still:
        return still.result
    except OpenajarError as e:
        mgr.record_call(env.terminal_lane, "environment_save", ok=False, error=e.code)
        raise
    mgr.record_call(env.terminal_lane, "environment_save", ok=True, summary=m.id)
    ctx.trace("environment_save", lane=env.terminal_lane, environment=env.id, save=m.id)
    out = {**_saved_result(mgr, m.id), "environment": env_ref(env)}
    if out.get("snapshot"):
        out["pause"] = _pause(mgr, out)
    out["next"] = (f"saved. Work on; environment_open(title=\"{env.title}\") resumes the newest save in a new chat"
                   + (" once this one is closed" if env.keep != "save" else ""))
    return out


async def environment_forget(ctx: ToolContext, *, title: str, save_id: str | None = None) -> dict:
    """Delete saves and their disk snapshots (they bill each month): every save of `title`, or one
    save_id. Its title then opens a new environment. Returns forgot and snapshots_deleted.

    Args:
        title: The environment's title, as environment() lists it under saved.
        save_id: Only this save (s and 10 letters or digits).
    """
    mgr = _mgr(ctx)
    title = check_title(title)
    family = caller.family(ctx) or caller.grant_id(ctx)
    # a freeze of an environment's lanes (or of `env`) holds its saves as they are
    refuse_frozen(ctx, lanes=[f"{WORKSPACE}/{m.env_id}" for m in mgr.saves().of_title(family, title)])
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"forget the saves of \"{title}\""
                        + (f" ({save_id})" if save_id else ""))
    out = await mgr.forget(family, title, save_id)
    ctx.trace("environment_forget", title=title, forgot=out["forgot"])
    out["next"] = f"forgotten. environment_open(title=\"{title}\") now opens a new environment"
    return out


async def environment_port(ctx: ToolContext, *, port: int | None = None, ports: list[int] | None = None,
                           same_numbers: bool = False, on: str | None = None) -> dict:
    """A dev server on your VM as a link on the human's own computer, forwarded over the environment's
    SSH connection until it closes. On a server node the human first runs `tunnel` (an ssh -L line).

    Args:
        port: Your server's port in the VM, like 3000 or 5173 (environment() lists them).
        ports: Several, for a page that calls another localhost port (its API).
        same_numbers: The VM's port numbers here too, so those calls reach the VM.
        on: The on environment_open gave.
    """
    from openajar.environments import ports as env_ports

    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("this chat has no environment; environment_open first, then start your server")
    mgr.refuse_gone(env)
    what = ", ".join(str(p) for p in [*([port] if port is not None else []), *(ports or [])])
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"forward port {what} of environment {env.id} ({env.title})")
    out = await env_ports.share(mgr, env, port, ports, same_numbers)
    mgr.record_call(env.terminal_lane, "environment_port", ok=True, summary=what)
    ctx.trace("environment_port", lane=env.terminal_lane, environment=env.id, port=what,
              local=out.get("local_port") or [f["local_port"] for f in out.get("forwards") or []])
    return out


async def environment_app_restart(ctx: ToolContext, *, on: str | None = None) -> dict:
    """Restart the app the repository's .openajar/environment.toml starts: its job and every process it
    started stop, then `start` runs again as at environment_open (the file as it is now). Returns `app`
    as the open does.

    Args:
        on: The on environment_open gave.
    """
    from openajar.environments.errors import EnvironmentNotReady

    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("this chat has no environment; environment_open first")
    mgr.refuse_gone(env)
    if env.state != "ready":
        raise EnvironmentNotReady(f"environment {env.id} is {env.state}; call environment() until it is ready")
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"restart the app of environment {env.id} ({env.title})")
    app = await mgr.restart_app(env)
    mgr.record_call(env.terminal_lane, "environment_app_restart", ok=True, summary=str(app.get("job_id") or ""))
    ctx.trace("environment_app_restart", lane=env.terminal_lane, environment=env.id, job=app.get("job_id"))
    if app.get("listening"):
        words = f"it runs again as job {app.get('job_id')}, listening on port {app.get('port')}"
        nxt = words + (f"; the human's link is app.link ({app['link']})" if app.get("link") else "")
    else:
        nxt = "it did not come up: read app.why and app.log_tail, fix it and restart again"
    return {"app": app, "next": nxt}


async def environment_pull_request(ctx: ToolContext, *, title: str, body: str | None = None,
                                   base: str | None = None, draft: bool = False,
                                   branch: str | None = None, proofs: bool | list[str] | None = None,
                                   on: str | None = None) -> dict:
    """Open a pull request on GitHub for a branch your environment pushed, with the node's GitHub
    token (the VM has no gh and no credential). Only a branch this environment created; it never
    merges. Your proofs (screenshots, recordings) go in its Proof section, stored on the repository's
    agentos-proofs branch, never in the diff. One already open for the branch comes back with its
    Proof section brought up to date. Give the human its url. pull_request_failed: no token on the
    node, or GitHub refused.

    Args:
        title: The pull request's title.
        body: Its description (Markdown): what changed, how it was tested.
        base: The branch to merge into (default: the repository's default branch).
        draft: Open it as a draft.
        branch: Which pushed branch (default: the newest one this environment pushed).
        proofs: false leaves the proofs out; a list of proof ids or titles shows only those.
        on: The on environment_open gave.
    """
    from openajar.environments import pulls

    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("this chat has no environment; environment_open first, push a branch, then call this")
    await ctx.authorize(Effect.READ, lane_id=None,
                        summary=f"open a pull request for environment {env.id}'s branch on {env.repo}")
    try:
        out = await pulls.open_pull_request(mgr, env, title=title, body=body, base=base, draft=draft, branch=branch,
                                            proofs=proofs)
    except OpenajarError as e:
        mgr.record_call(env.terminal_lane, "environment_pull_request", ok=False, error=e.code)
        raise
    row = {"url": out["url"], "number": out["number"], "branch": out["branch"], "base": out["base"]}

    def add(e: Environment) -> None:
        e.pull_requests = [p for p in e.pull_requests if p.get("url") != row["url"]][-9:] + [row]

    mgr.store.update(env.id, add)
    mgr.record_call(env.terminal_lane, "environment_pull_request", ok=True, summary=str(out["url"]))
    ctx.trace("environment_pull_request", lane=env.terminal_lane, environment=env.id, url=out["url"])
    out["next"] = (("opened. " if out["created"] else "it was open already. ") +
                   f"Give the human {out['url']}; do not delete the branch {out['branch']} while it is open")
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    from openajar.environments.checkpoint_tools import tool_specs as checkpoint_specs

    t = ("environments",)
    return [*checkpoint_specs(runtime),
        ToolSpec("environment_open", environment_open, Effect.VM, lane_arg=None, tags=(*t, "changes_state"),
                 hidden_without="environments"),
        ToolSpec("environment", environment, Effect.READ, lane_arg=None, tags=t, hidden_without="environments"),
        ToolSpec("environment_close", environment_close, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
        ToolSpec("environment_save", environment_save, Effect.READ, lane_arg=None, tags=(*t, "changes_state"),
                 hidden_without="environments"),
        ToolSpec("environment_forget", environment_forget, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
        ToolSpec("environment_port", environment_port, Effect.READ, lane_arg=None, tags=(*t, "idempotent"),
                 hidden_without="environments"),
        ToolSpec("environment_app_restart", environment_app_restart, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state"), hidden_without="environments"),
        ToolSpec("environment_pull_request", environment_pull_request, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
    ]
