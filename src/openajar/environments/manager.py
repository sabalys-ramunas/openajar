"""EnvironmentManager: an agent's own cloud computer (runtime service "environments").

An environment is a VM from openajar.vms (its terminal lane is `env/<id>`), a scratch browser lane on
the node (`env/<id>-browser`) whose traffic leaves from the VM through an SSH SOCKS tunnel, the
node's git gateway reached from the VM through a reverse tunnel, and optionally a repository
cloned into /work/<name>. One chat holds a live machine: environment_open gives it the machine's
handle (`on`), and only calls that carry it reach the machine; a client with an MCP session id holds
its own by the session too, so its calls without `on` go there (openajar.environments.picking and
routing). The title of a live machine another chat holds is environment_in_use (take_over=true moves
it once that chat has been quiet), and the title of a saved one resumes it.

Opening: plan (the VM the node would rent, the cheapest that fits), the node's limits (check_limits:
open at once, the longest life, the month's budget; opening never asks a person) and the caller's
`vm` special, then the VM, its setup (openajar.environments.machine), the tunnels, the browser lane and the clone.
The environment is `starting` until all of that is done, then `ready`.

Closing: refused while the repository has changes that are not committed or commits that are not
pushed (unsaved_work), unless the caller discards them; then the tunnels, the browser lane and the
VM go. The reaper closes an environment idle for `idle_minutes` (never one with unsaved work: the
owner is told and it lives to its time limit) and one past its time limit, and marks one whose VM
is gone. A restart of the node keeps every environment: the reaper starts their tunnels again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from openajar.core import agent_names, clock
from openajar.core.errors import InvalidRequest, LaneExists, NotFound, OpenajarError
from openajar.core.models import ProxyConfig, utcnow
from openajar.environments.activity import Activity
from openajar.environments.bringup import BringUp
from openajar.environments.browserproxy import BrowserProxies
from openajar.environments.caller import binding_of
from openajar.environments.checkpoints import Checkpoints
from openajar.environments.downloads import Downloads
from openajar.environments.errors import (
    EnvironmentFailed,
    EnvironmentLimit,
    SaveFailed,
    SaveLimit,
    UnsavedWork,
)
from openajar.environments.fleet import Fleet
from openajar.environments.liveness import Liveness
from openajar.environments.logins import Logins
from openajar.environments.models import (
    BRANCHES_KEPT,
    CALLS_KEPT,
    LIVE_STATES,
    WORKSPACE,
    Call,
    Environment,
    EnvSettings,
    Machine,
    Push,
    load_settings,
)
from openajar.environments.networking import Network
from openajar.environments.pausing import Pausing
from openajar.environments.picking import Picking, current_client, is_session, same_title
from openajar.environments.ports import ended
from openajar.environments.preparing import Preparing
from openajar.environments.prepbuild import PrepBuild
from openajar.environments.proofs import Proofs
from openajar.environments.recording import Recording
from openajar.environments.relink import Relink
from openajar.environments.replay import Replay
from openajar.environments.saving import Saving
from openajar.environments.sizing import Sizing
from openajar.environments.sshaccess import SshAccess
from openajar.environments.store import EnvironmentStore
from openajar.environments.tunnels import Tunnel
from openajar.environments.updates import Updates
from openajar.environments.uploads import Uploads
from openajar.environments.views import Views, only_modes
from openajar.environments.waking import Waking
from openajar.environments.warmpool import WarmPool
from openajar.environments.watching import Watching

log = logging.getLogger("openajar.environments")

TITLE_MAX = 80
SUMMARY_MAX = 200
OPEN_NEXT = "environment_open(title=...)"
# Ids are e + ID_LENGTH characters people can read aloud (no 0/o, 1/l/i): 31^10, about 8e14, so one
# cannot be guessed, and a lane name (env/<id>-browser) stays short.
_ID_CHARS = "".join(c for c in string.ascii_lowercase + string.digits if c not in "0o1li")
ID_LENGTH = 10
OPENING_KEPT = 256
GONE_CHECKS, GONE_WAIT_S = 5, 2.0              # asking the provider whether a deleted machine is gone
LANE_DELETE_TRIES, LANE_DELETE_WAIT_S = 3, 1.0  # a browser still exiting holds its lane a moment
SWEEP_RECENT, SWEEP_WINDOW = 100, timedelta(days=1)
REPO_CHECK_WAITS_S = (2.0, 4.0, 8.0)  # between tries of a repository check the host did not answer: ~15 s
REPO_RETRY_TIMEOUT_S = 10.0           # each try after the first
SLOT_POLL_S = 5.0                     # a full node: environment_open looks again this often, up to its wait_s
READY_WAIT_S = 60.0                   # environment(on) of a machine still starting waits for ready this long


BUNDLE = "/work/unpushed.bundle"


def unsaved_fix(unsaved: dict, workdir: str | None = None) -> str:
    """What to do about unsaved work, for the files git has and for the ones it has not. Commits that
    are only local can also go home without a push (the human said not to push): a git bundle of them,
    through environment_download."""
    if only_modes(unsaved):  # a chmod +x from a setup script, say: nothing of the content is lost
        return ("Nothing but file modes (chmod) differ from the commit: close with discard=true to drop them, or "
                "commit and push them if the repository needs them")
    git = unsaved.get("dirty_files") or unsaved.get("unpushed") or unsaved.get("stashes") or unsaved.get("unreadable")
    steps = ["commit it and push every branch (git push origin <branch>; a stash goes onto a branch first)"] if git else []
    if unsaved.get("unpushed"):
        steps.append("to keep the commits without a push, run git bundle create "
                     f"{BUNDLE} --branches --not --remotes" + (f" in {workdir}" if workdir else "")
                     + f", take it home with environment_download(path=\"{BUNDLE}\"), then close with discard=true")
    elif unsaved.get("dirty_files"):
        steps.append("to keep the changes without a push, take them home with environment_download(path=\"<file or "
                     "folder>\"), then close with discard=true")
    if unsaved.get("loose_files"):
        steps.append("take the files in no repository home with environment_download(path=\"<file or folder>\"), "
                     "or push them in a repository")
    words = "; ".join(steps) + ", or close with discard=true when the human agreed to lose it"
    return words[0].upper() + words[1:]


def new_id() -> str:
    return "e" + "".join(secrets.choice(_ID_CHARS) for _ in range(ID_LENGTH))


def check_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    if not t:
        raise InvalidRequest('give the environment a short title people will see, like "Black Friday page"')
    if len(t) > TITLE_MAX:
        raise InvalidRequest(f"the title is at most {TITLE_MAX} characters")
    return t


def check_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$", ref) or ".." in ref:
        raise InvalidRequest("ref is a branch, tag or commit, like main or v1.2.0")
    return ref


class EnvironmentManager(Picking, Sizing, BringUp, Views, Proofs, Uploads, Downloads, Pausing, Saving, Activity,
                         SshAccess, Updates, Network, BrowserProxies, Preparing, PrepBuild, WarmPool, Replay,
                         Checkpoints, Watching, Waking, Recording, Fleet, Logins, Liveness, Relink):
    """The environments service. Which environment a call means and attaching by title (Picking), its
    sizing and cost (Sizing), its bring-up (BringUp), its read side (Views), its saves (Saving) and what
    counts as busy (Activity), and in local mode the agent's own ssh (SshAccess) are mixins in their own
    modules; lane lookups, opening, closing and the reaper are here."""

    def __init__(self, home: Path, *, runtime: Any = None, settings: EnvSettings | None = None, clock=utcnow):
        self.home = Path(home)
        self.runtime = runtime
        self.settings = settings or load_settings(self.home)
        self.store = EnvironmentStore(self.home / "environments.db")
        self._clock = clock
        self._tunnels: dict[str, Tunnel] = {}
        self._nets: dict = {}  # each environment's network on the node (openajar.environments.networking)
        self._vm_browsers: Any = None  # openajar.environments.vmbrowser.VmBrowsers, once a "vm" browser is made
        self._tasks: dict[str, asyncio.Task] = {}
        self._warming: set[asyncio.Task] = set()  # mirror warm-ups while VMs boot (BringUp._warm_mirror)
        self._fetch_direct: set[str] = set()  # environments whose public repository the VM fetches itself
        self.view_links: dict[str, Any] = {}  # browser lane -> the live-view link handed out (tools._view_link)
        self._planned: dict[tuple[str, str], str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._loader_wrapped = False
        self.last_reap: dict = {}
        self._reap_lock = asyncio.Lock()  # the reaper's loop and a catch-up after a wake never overlap
        self.repo_probe: Any = None  # how check_repo reads a repository (None: `git ls-remote`; tests set one)
        self.repo_check_waits: tuple[float, ...] = REPO_CHECK_WAITS_S
        self.slot_poll_s = SLOT_POLL_S  # environment_open looks for a free machine this often while the node is full
        self.slot_wait_s = 200.0  # and waits at most this long (its wait_s, up to tools.WAIT_MAX_S; tests set less)
        self.ready_wait_s = READY_WAIT_S  # environment(on) of a starting machine waits this long for ready
        self.on_laptop = False  # openajar local sets it: the agent is on this computer (ports, sshaccess)
        self._ssh_failed: set[str] = set()

    # ================================================================ wiring
    def _get(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def vms(self) -> Any:
        vms = self._get("vms")
        if vms is None:
            raise EnvironmentFailed("this node has no VMs (openajar.vms), so it cannot open environments")
        return vms

    def envs(self) -> Any:
        envs = self._get("envs")
        if envs is None:
            raise EnvironmentFailed("this node has no terminal lanes (openajar.env), so it cannot open environments")
        return envs

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def proxies(self) -> dict[str, ProxyConfig]:
        """The SOCKS proxy of each open environment's tunnel, by the label its browser lane names."""
        return {f"env-{eid}": ProxyConfig(label=f"env-{eid}", host="127.0.0.1", port=self.browser_socks_port(eid, t),
                                           scheme="socks5",
                                           check_exit=False)
                for eid, t in self._tunnels.items()}

    def tunnel_of(self, env_id: str) -> Tunnel | None:
        return self._tunnels.get(env_id)

    def proxy_for(self, env: Environment) -> ProxyConfig | None:
        return self.proxies().get(f"env-{env.id}")

    def live_proxy_for(self, env: Environment) -> ProxyConfig | None:
        """proxy_for, only while its tunnel is up (studio's checks must never fall back to the node)."""
        tunnel = self._tunnels.get(env.id)
        return self.proxy_for(env) if tunnel is not None and tunnel.alive else None

    def _wrap_proxies_loader(self) -> None:
        """Let the lane manager find each environment's tunnel as a proxy label (`env-<id>`), next to
        the owner's own proxies. Nothing is written to the owner's proxies file."""
        lanes = self._get("lanes")
        if self._loader_wrapped or lanes is None or not callable(getattr(lanes, "proxies_loader", None)):
            return
        own = lanes.proxies_loader

        def loader() -> dict[str, ProxyConfig]:
            return {**own(), **self.proxies()}

        lanes.proxies_loader = loader
        self._loader_wrapped = True

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._wrap_proxies_loader()
        lanes = self._get("lanes")
        if lanes is not None:  # a slow or stuck browser call says how loaded its machine is
            hints = getattr(lanes, "slow_hints", None)
            if hints is None:
                with contextlib.suppress(AttributeError, TypeError):
                    lanes.slow_hints = hints = []
            if isinstance(hints, list) and self.load_hint not in hints:
                hints.append(self.load_hint)
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="environment-reaper")
        self.replay_start()  # frames of a person's take-over and of a changing page (openajar.environments.replay)
        self.logins_install()  # the vault reads which environments a bound login covers (openajar.environments.logins)
        self.liveness_install()  # a command that cannot reach its machine asks its provider (openajar.environments.liveness)

    async def shutdown(self) -> None:
        """Stop the reaper, the bring-ups and the tunnels. The VMs stay: a restart of the node must
        not end an agent's work. The next start brings the tunnels back."""
        tasks = [t for t in (self._reaper, *self._tasks.values(), *self._warming, *self.side_tasks(),
                                       *self.recording_tasks()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._reaper = None
        self._tasks.clear()
        self._warming.clear()
        await self.replay_stop()
        for tunnel in list(self._tunnels.values()):
            await tunnel.stop()
        self._tunnels.clear()
        await self.net_shutdown()

    async def stop(self) -> None:
        await self.shutdown()

    async def _reap_loop(self) -> None:
        await asyncio.sleep(1)
        while True:
            try:
                self.last_reap = await self.reap_once()
            except Exception as e:  # noqa: BLE001
                log.warning("environment reaper: %s", getattr(e, "message", e))
                self.last_reap = {"at": clock.iso_or_none(self._clock(), z=True), "error": str(getattr(e, "message", e))}
            await asyncio.sleep(self.settings.reap_interval_s)

    # ================================================================ lookups
    def get(self, env_id: str) -> Environment:
        return self.store.get(env_id)

    def env_of_lane(self, lane_id: str) -> tuple[Environment, str] | None:
        """(environment, "terminal" | "browser") for any environment's lane, open or not, else None."""
        if not isinstance(lane_id, str) or not lane_id.startswith(WORKSPACE + "/"):
            return None
        name = lane_id.split("/", 1)[1]
        kind = "browser" if name.endswith("-browser") else "terminal"
        eid = name.removesuffix("-browser")
        try:
            env = self.store.get(eid)
        except (NotFound, ValueError):
            return None
        return env, kind

    def lane_env(self, lane_id: str) -> tuple[Environment, str] | None:
        """(environment, "terminal" | "browser") for an open environment's lane, else None."""
        found = self.env_of_lane(lane_id)
        return found if found is not None and found[0].open else None

    @staticmethod
    def reaches(env: Environment, grant: Any, *, mcp: bool) -> bool:
        """May this grant act on, read or close this environment? Its own grant family (the grant
        that opened it, and every grant narrowed from the same root: a brain or a routine of that
        agent). Besides them only a person outside MCP: the owner (the web app, the CLI, the node's
        token or an admin grant over the API), or the member whose agent opened it."""
        if grant is None:
            return True  # no policy on this node: every caller is the owner
        if getattr(grant, "root_id", None) == env.family or env.grant_id in (getattr(grant, "chain_ids", None) or ()):
            return True
        if mcp:
            return False
        if getattr(grant, "owner_token", False) or getattr(grant, "admin", False):
            return True
        try:
            sub = grant.subject
        except Exception:  # noqa: BLE001 - a grant without a subject is nobody's
            return False
        return getattr(sub, "kind", None) == "human" and env.member is not None and getattr(sub, "id", None) == env.member

    def may_reach(self, lane_id: str, grant: Any, *, mcp: bool) -> bool:
        """The policy asks this for every lane a call names (openajar.policy.service): False for an
        environment's lane outside the caller's family, which it then refuses as not found. Any other
        lane: True (the usual rules hold)."""
        found = self.env_of_lane(lane_id)
        return found is None or self.reaches(found[0], grant, mcp=mcp)

    def holds_terminal(self, lane_id: str) -> bool:
        """Is this the terminal lane of an open environment? The git gateway reads it: a VM's token
        dies with its environment."""
        found = self.lane_env(lane_id)
        return (found is not None and found[1] == "terminal") or self.building_lane(lane_id)  # a prepared machine's build

    def branches_created(self, lane_id: str) -> list[str]:
        """The branches the open environment of terminal lane `lane_id` created on its remote (it
        pushed them first): the only existing branches the git gateway lets it delete or force-push."""
        found = self.lane_env(lane_id)
        return list(found[0].own_branches) if found is not None and found[1] == "terminal" else []

    def note_branches(self, lane_id: str, *, created: list[str], deleted: list[str]) -> None:
        """The git gateway pushed from this environment: `created` are its own branches from now on,
        `deleted` no longer are (another may create the name again)."""
        found = self.lane_env(lane_id)
        if found is None or found[1] != "terminal":
            return

        def change(env: Environment) -> bool:
            have = [b for b in env.own_branches if b not in deleted]
            have += [b for b in dict.fromkeys(created) if b not in have]
            if have == env.own_branches:
                return False
            env.own_branches = have[-BRANCHES_KEPT:]
            env.own_heads = {b: sha for b, sha in env.own_heads.items() if b in env.own_branches}
            return True

        self.store.update(found[0].id, change)

    def branch_heads(self, lane_id: str) -> dict[str, str]:
        """{branch: the commit this environment last pushed there} for the branches it created. The git
        gateway lets it rewrite or delete one only while the remote still has that commit there."""
        found = self.lane_env(lane_id)
        return dict(found[0].own_heads) if found is not None and found[1] == "terminal" else {}

    def note_heads(self, lane_id: str, heads: dict[str, str]) -> None:
        """The git gateway pushed these commits to branches this environment owns."""
        found = self.lane_env(lane_id)
        if found is None or found[1] != "terminal" or not heads:
            return

        def change(env: Environment) -> bool:
            new = {**env.own_heads, **{b: sha for b, sha in heads.items() if b in env.own_branches}}
            if new == env.own_heads:
                return False
            env.own_heads = new
            return True

        self.store.update(found[0].id, change)

    def own_lane(self, lane_id: str, grant: Any) -> bool:
        """Is this a lane of an open environment (its terminal or its browser) that `grant` reaches:
        its grant family's, or the owner's and the member's outside MCP (over MCP the policy has
        already refused another family's as not found)? The policy reads it as a node fact, and
        nothing aimed at such a lane waits for a person. The VM holds no account and no key, its
        browser only the logins the owner bound to it (openajar.environments.logins), and its one
        token fetches and pushes the environment's own repository through the git gateway, whose
        refusals still hold (docs/design/environments.md)."""
        found = self.lane_env(lane_id)
        return found is not None and self.reaches(found[0], grant, mcp=False)

    def _scrub(self, text: str, extra: list[str] | tuple[str, ...] = (), *, max_len: int = SUMMARY_MAX) -> str:
        """A call's summary (or a command) as the audit log keeps it: vault values and secret shapes
        redacted, on one line."""
        from openajar.policy.redact import redact_text

        policy = self._get("policy")
        value_scrub = getattr(getattr(policy, "traces", None), "value_scrub", None)
        if callable(value_scrub):
            text = value_scrub(text)
        else:
            from openajar.policy.scrub import runtime_scrub

            text = runtime_scrub(self.runtime)(text)
        return redact_text(" ".join(text.split()), extra=extra, max_len=max_len)

    def record_call(self, lane_id: str, tool: str, *, ok: bool, summary: str = "",
                    scrub: list[str] | tuple[str, ...] = (), exit_code: int | None = None,
                    error: str | None = None) -> None:
        """One call on an environment's lane, for environment()'s last calls: `ok` is the tool's own
        answer, `exit_code` a command's (terminal_exec), `error` the code of a refusal or a failure."""
        found = self.lane_env(lane_id)
        if found is None:
            return
        now = self._clock()
        call = Call(at=now, tool=tool, summary=self._scrub(summary, scrub), ok=ok, exit_code=exit_code, error=error,
                    client=current_client())

        def change(env: Environment) -> bool:
            if not env.open:
                return False
            env.last_call_at = now
            env.calls.append(call)
            del env.calls[:-CALLS_KEPT]
            if env.unsaved_noticed and ok:
                env.unsaved_noticed = False  # someone works on it again: a later idle stretch tells the owner again
            return True

        self.store.update(found[0].id, change)
        self.replay_noted(found[0], lane_id, call)  # its call log, and a frame after a page call

    # ================================================================ open
    async def repo_access(self, repo: str, *, timeout_s: float | None = None) -> Any:
        """What GitService.access finds for `repo`: declared for env with the vault's credential
        (checked as the git gateway fetches it), or public, or neither."""
        from openajar.git.service import public_access

        git = self._get("git")
        more = {"timeout_s": timeout_s} if timeout_s else {}
        if git is not None and callable(getattr(git, "access", None)):
            return await git.access(repo, [WORKSPACE], probe=self.repo_probe, **more)
        return await public_access(repo, self.home, probe=self.repo_probe, **more)  # no git gateway: public only

    async def _reach_repo(self, repo: str) -> Any:
        """repo_access, tried again over about 15 s (REPO_CHECK_WAITS_S) while the host does not answer:
        a laptop's network drops for a few seconds, and one lost answer must not fail an open."""
        access = await self.repo_access(repo)
        for wait in self.repo_check_waits:
            if not access.unreachable:
                break
            log.info("environments: %s did not answer the check of %s; trying again in %g s",
                     access.slug.split("/", 1)[0], access.slug, wait)
            await asyncio.sleep(wait)
            access = await self.repo_access(repo, timeout_s=REPO_RETRY_TIMEOUT_S)
        return access

    async def check_repo(self, repo: str) -> Any:
        """Can a machine clone `repo`? Asked before anything is rented: declared for env with the
        vault's credential (checked as the git gateway fetches it), or public. Else
        repo_not_connected, with the fix the human runs on the node (openajar.environments.repos).
        A repository that can be cloned closes the cards that asked a person to connect it."""
        from openajar.environments import connect, repos

        access = await self._reach_repo(repo)
        if access.ok:
            with contextlib.suppress(Exception):  # a card left open is closed by the reaper's sweep
                connect.close(self.store, self._get("approvals"), access.slug)
            return access
        if access.unreachable:
            raise repos.unreachable(access, tries=len(self.repo_check_waits) + 1)
        if access.declared and access.wide and access.not_found:  # a token that should see it: no card helps
            raise repos.not_found(access)
        raise repos.not_connected(access)

    def planned_id(self, binding: str, title: str) -> str:
        """The id an environment will get, the same for a chat and title until one with it exists (a
        call that failed, on a limit or a boot, and is tried again keeps the id it named)."""
        key = (binding, title.lower())
        eid = self._planned.get(key)
        while eid is None or self.store.has(eid):
            eid = new_id()
            self._planned[key] = eid
        return eid

    def opening(self, family: str | None, title: str) -> asyncio.Lock:
        """One open at a time per title of a family: a second environment_open of the same title waits
        for the first, then is environment_in_use (never a second machine; create() checks it too). Opens of
        other titles go on meanwhile."""
        if len(self._opening) > OPENING_KEPT:  # chats come and go: keep only the locks in use
            self._opening = {k: v for k, v in self._opening.items() if v.locked()}
        return self._opening.setdefault(f"{family}|{' '.join(title.split()).lower()}", asyncio.Lock())

    async def create(self, env_id: str, plan: dict, *, binding: str, title: str, grant_id: str,
                     grant_root: str | None, agent: str, client: str | None, repo: str | None,
                     ref: str | None, subject: str | None = None, member: str | None = None,
                     keep: str = "none", resumed: dict | None = None, lease: str | None = None,
                     prepared: dict | None = None) -> Environment:
        """Record the environment and start bringing it up (after the limits and the grant's check). An open
        one of the family with this title (made meanwhile, by another process) is environment_in_use: never
        a second machine for one title, and never another chat's. `resumed` (Saving.resuming): it resumes
        that save. `lease`: the hash of the handle the opening chat gets (openajar.environments.picking)."""
        if repo:
            from openajar.git.wire import parse_remote

            parse_remote(repo)
        now = self._clock()
        specs = plan.get("specs") or {}
        env = Environment(
            id=env_id, title=title, grant_id=grant_id, grant_root=grant_root, binding=binding, agent=agent,
            subject=subject, member=member, client=client, bound_at=now, lease=lease, repo=repo, ref=ref,
            image=plan["image"], created_at=now,
            browser=self.settings.browser, network=plan.get("network") or "full",
            expires_at=now + timedelta(minutes=plan["ttl_minutes"]), price_hourly_eur=plan.get("price_hourly_eur"),
            keep=keep, restore_from=(resumed or {}).get("save_id"), resumed=resumed, prepared=prepared,
            machine=Machine(provider=getattr(self.vms().provider(), "name", ""), server_type=plan["server_type"],
                            cpu=specs.get("cores"), memory_gb=specs.get("memory_gb"), location=plan["location"],
                            size=self.size_of(specs.get("cores"), specs.get("memory_gb"))))
        async with self._lock("create"):
            live = [e for e in self.open_for_family(env.family) if same_title(e.title, title) and e.state in LIVE_STATES]
            if live:  # opened meanwhile with this title (another process): never a second machine
                raise self.in_use(live[0], now, session=is_session(binding))
            if self.store.has(env_id):
                raise LaneExists(f"environment {env_id} exists already")
            if keep == "live":
                self.check_keep("live")
            self.check_limits(plan)
            self.assign_browser_proxy(env)  # the owner's proxy its browser exits through (browserproxy)
            self.store.put(env)
        self._planned = {k: v for k, v in self._planned.items() if v != env_id}
        with contextlib.suppress(Exception):  # the warm pool boots the image the newest open used
            self.prepared_store().set_last_image(plan["image"])
        self._spawn(env_id, self._bring_up(env_id, plan))
        log.info("environment %s (%s) opening for %s", env_id, title, agent)
        return env


    # ---------------------------------------------------------------- bring-up


    # ================================================================ close
    async def close(self, env_id: str, *, discard: bool = False, reason: str = "closed", final: bool = False
                    ) -> Environment:
        """Close an environment. keep "live" without `final`: the chat only lets it go (Saving.unbind).
        keep "save" without `discard`: saved first (SaveFailed keeps the machine), then deleted; the save
        is env.last_save_id. Else refused with unsaved_work unless `discard`."""
        env = self.store.get(env_id)
        if not env.open:
            return env
        if env.gone_at is not None:  # nothing to save or check on a machine that stopped (openajar.environments.liveness)
            return await self.end_gone(env)
        if env.keep == "live" and not final:
            return self.unbind(env)
        async with self._lock(f"close:{env_id}"):
            env = self.store.get(env_id)
            if not env.open:
                return env
            if env.keep == "save" and not discard and env.state == "ready":
                try:
                    saved = await self.save_env(env, kind="save", reason=reason, then_delete=True)
                except (SaveFailed, SaveLimit) as e:
                    await self._tell_failed(env, e.message)
                    raise
                reason = f"{reason}, saved as {saved.id}"
            elif not discard and env.state == "ready":
                repos = await self.git_repos(env)
                if repos is None:
                    raise UnsavedWork(f"environment {env.id}'s machine does not answer, so its work cannot be "
                                      "checked; close it with discard=true to delete it anyway",
                                      {"dirty_files": [], "unpushed": [], "unreachable": True})
                unsaved = self.unsaved_of(repos)
                if unsaved is not None:
                    raise UnsavedWork(f"environment {env.id} has work that is not saved: "
                                      f"{self.unsaved_words(unsaved, most=3)}. {unsaved_fix(unsaved, env.workdir)}", unsaved)
            task = self._tasks.get(env_id)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            return await self._teardown(self.store.get(env_id), reason=reason)

    async def _teardown(self, env: Environment, *, reason: str, final: str = "closed") -> Environment:
        pushed = [Push(branch=p["branch"], commit=p["commit"] or "", at=datetime.fromisoformat(p["at"]),
                       deleted=bool(p.get("deleted"))) for p in self.pushes(env)][:50]
        why = env.closed_reason

        def closing(e: Environment) -> None:
            e.pushed, e.state = pushed, "closing"
            e.closed_reason = e.closed_reason or why

        env = self.store.update(env.id, closing)
        self.ssh_down(env)  # the agent's `ssh openajar-<id>` goes first: its machine is going
        self.drop_downloads(env.id)  # its download links end, and their archives go (openajar.environments.downloads)
        await self.stop_browser_start(env.id)  # a browser still starting (bringup.start_browser) is not waited for
        gone = env.gone_at is not None  # its machine stopped: nothing on it answers, so nothing there is waited for
        if not gone:
            await self.last_looks(env, "close")  # its recording kept, the browser's last frame (recording, replay)
        if env.browser == "vm" and not gone:  # its browser and screen stop on the machine while the tunnel still reaches it
            await self._remove_browser(env)
            if self._vm_browsers is not None:
                await self._vm_browsers.drop_screen(env)
        tunnel = self._tunnels.pop(env.id, None)
        if tunnel is not None:
            await tunnel.stop()
        await self.net_down(env.id)  # its proxy and swap stop; the CA's key and the placeholders go
        git = self._get("git")
        if git is not None and callable(getattr(git, "vm_down", None)):
            try:  # its gateway token ends here, with the listener its tunnel reached
                await git.vm_down(env.terminal_lane)
            except Exception:  # the gateway checks that the environment is open as well
                log.exception("environment %s: could not close its git listener", env.id)
        from openajar.node.interaction import VM_PROXIED_LANES

        VM_PROXIED_LANES.discard(env.browser_lane)
        await self._remove_browser(env)
        await ended(self, env.id)  # its shared ports close, and the node's copy of its sign-ins goes
        cost, vm_state = await self._delete_machine(env, reason)
        await self._remove_terminal(env)

        def closed(e: Environment) -> None:
            e.state = final  # type: ignore[assignment]
            e.closed_at = self._clock()
            e.closed_reason = e.closed_reason or reason
            e.cost_eur = cost if cost is not None else (0.0 if e.price_hourly_eur == 0 else e.cost_eur)
            e.vm_state = vm_state

        env = self.store.update(env.id, closed)
        log.info("environment %s %s (%s), its machine %s", env.id, final, reason, vm_state)
        return env

    async def _delete_machine(self, env: Environment, reason: str) -> tuple[float | None, str]:
        """Delete the environment's VM: (its cost, what became of it). "deleted" only when the provider
        says the server is gone; "deleting" while the provider still has it (the node retries the
        delete); "unknown" when the provider could not be asked; "none" when no machine was rented;
        "paused" when its save paused it (openajar.environments.pausing): it is kept, asleep."""
        if not env.vm_id:
            return None, "none"
        vms = self.vms()
        try:
            rec = vms.store.get(env.vm_id)
        except NotFound:
            return None, "unknown"
        if rec.live:
            rec = await vms.destroy(rec.id, reason=f"environment {env.id}: {reason}")
        cost = rec.cost_eur(self._clock())
        if rec.state == "paused":
            return cost, "paused"
        if rec.state not in ("deleted", "failed"):
            return cost, "deleting"
        if not rec.provider_id:
            return cost, "none"
        for attempt in range(GONE_CHECKS):
            try:
                if await vms.provider().get_server(rec.provider_id) is None:
                    return cost, "deleted"
            except Exception as e:  # noqa: BLE001 - not confirmed is not deleted
                log.warning("environment %s: could not ask the provider about its machine: %s", env.id, e)
                return cost, "unknown"
            if attempt < GONE_CHECKS - 1:
                await asyncio.sleep(GONE_WAIT_S)
        return cost, "deleting"

    async def _remove_browser(self, env: Environment) -> bool:
        """Delete the environment's browser lane (close it first). A browser that is still exiting
        holds it a moment, so the delete is tried a few times. True when it is gone."""
        lanes = self._get("lanes")
        if lanes is None:
            return True
        try:
            lanes.get(env.browser_lane)
        except Exception:  # noqa: BLE001 - no such lane: nothing to remove
            return True
        with contextlib.suppress(Exception):
            await lanes.close(env.browser_lane)
        for attempt in range(LANE_DELETE_TRIES):
            try:
                await lanes.delete(env.browser_lane, by="node")
                return True
            except Exception as e:  # noqa: BLE001
                if getattr(e, "code", None) == "lane_not_found":
                    return True
                if attempt == LANE_DELETE_TRIES - 1:
                    log.warning("environment %s: its browser lane stays for now: %s", env.id, getattr(e, "message", e))
                    return False
                await asyncio.sleep(LANE_DELETE_WAIT_S)
        return False

    async def _remove_terminal(self, env: Environment) -> bool:
        """Delete the environment's terminal lane once its machine is handled (openajar.vms removes it
        with the VM; this covers one it left). True when it is gone."""
        envs = self._get("envs")
        if envs is None:
            return True
        try:
            if envs.has(env.terminal_lane):
                await envs.remove(env.terminal_lane, vm_ok=True)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("environment %s: its terminal lane stays for now: %s", env.id, getattr(e, "message", e))
            return False

    async def sweep_lanes(self, now: datetime | None = None) -> list[str]:
        """The lanes of environments closed in the last day that are still there (a delete that a
        browser still exiting refused): removed now. Returns the environments it cleaned."""
        now = now or self._clock()
        out = []
        for env in self.store.recent(SWEEP_RECENT):
            if env.open or env.closed_at is None or now - env.closed_at > SWEEP_WINDOW:
                continue
            if not (self._lane_left(env.browser_lane) or self._terminal_left(env)):
                continue
            await self._remove_browser(env)
            await self._remove_terminal(env)
            out.append(env.id)
        return out

    def _lane_left(self, lane_id: str) -> bool:
        lanes = self._get("lanes")
        if lanes is None:
            return False
        try:
            lanes.get(lane_id)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _terminal_left(self, env: Environment) -> bool:
        envs = self._get("envs")
        try:
            return envs is not None and bool(envs.has(env.terminal_lane))
        except Exception:  # noqa: BLE001
            return False

    async def extend(self, env_id: str, minutes: int) -> Environment:
        env = self.store.get(env_id)
        if env.state not in ("starting", "ready"):
            raise InvalidRequest(f"environment {env.id} is {env.state}")
        if minutes < 1:
            raise InvalidRequest("extend by at least one minute")
        vms = self.vms()
        longest = self._longest()
        cap = env.created_at + timedelta(minutes=longest)
        new = min(max(env.expires_at, self._clock()) + timedelta(minutes=minutes), cap)
        if new <= env.expires_at:
            raise EnvironmentLimit(f"environment {env.id} is already at this node's longest life of {longest / 60:g} "
                                   f"hours; {self._longest_raise()}")
        budget = self.settings.monthly_eur
        if budget:
            booked = self.booked_eur(until={env.id: new})
            if booked > budget:
                raise EnvironmentLimit(f"with environment {env.id} booked that long, environments could cost EUR "
                                       f"{booked:.2f} this month, past the monthly budget of EUR {budget:.2f}. Extend "
                                       "it by less, or close another; the owner raises the budget with "
                                       "`openajar environments allow --monthly-eur E`")
        if env.vm_id:
            rec = vms.store.get(env.vm_id)
            if rec.live and rec.expires_at < new:
                try:
                    await vms.extend_to(rec.id, new)  # the node's own VM limits: max_ttl_minutes, monthly budget
                except OpenajarError as e:
                    if e.code == "vm_limit":
                        raise EnvironmentLimit(e.message) from None
                    raise
        env = self.store.get(env_id)
        env.expires_at = new
        env.unsaved_noticed = False
        self.store.put(env)
        return env

    # ================================================================ reaper
    async def _notify(self, title: str, body: str) -> None:
        notifier = self._get("notifier")
        fn = getattr(notifier, "notify", None)
        if fn is None:
            log.warning("%s: %s", title, body)
            return
        try:
            out = fn(title=title, body=body, level="warning", urgent=False, data={})
            if asyncio.iscoroutine(out):
                await out
        except Exception:
            log.exception("could not send an environment notification")

    async def reap_once(self, now: datetime | None = None) -> dict:
        """One pass of the reaper (its loop runs one every reap_interval_s). Past its time limit means
        past it by the wall clock, so the first pass after a laptop slept closes it at once."""
        async with self._reap_lock:
            return await self._reap(now)

    async def _reap(self, now: datetime | None) -> dict:
        now = now or self._clock()
        out: dict[str, Any] = {"at": clock.iso_or_none(now, z=True), "closed": [], "kept_unsaved": [], "tunnels": [], "gone": []}
        vms = self._get("vms")
        with contextlib.suppress(Exception):  # proofs past their retention (openajar.environments.proofs)
            self.prune_proofs(now)
        with contextlib.suppress(Exception):  # replays past replay_days (openajar.environments.replay)
            self.prune_replays(now)
        with contextlib.suppress(Exception):  # download archives past their link's life
            self.sweep_downloads()
        with contextlib.suppress(Exception):  # a repository connected at the node closes its cards
            from openajar.environments import connect

            await connect.sweep(self)
        for listed in self.store.open():
            # the list was read before this pass awaited anything: an agent's close may have finished since
            # (field report 2026-11-16: a stale "closing" here undid a close that had saved and deleted the
            # machine, and kept the title held by that chat). Read each one fresh.
            env = self.store.get(listed.id)
            if not env.open:
                continue
            if env.id in self._tasks or self._lock(f"close:{env.id}").locked() or self.in_background(env.id):
                continue  # still coming up, or an agent's close or save runs (its teardown marks it closing)
            if env.state == "closing":
                await self._closing(env, now, out)
                continue
            if env.state == "starting":
                self._spawn(env.id, self._bring_up(env.id, None))  # the node restarted while it came up
                continue
            rec = None
            if vms is not None and env.vm_id:
                with contextlib.suppress(NotFound):
                    rec = vms.store.get(env.vm_id)
            if rec is None or not rec.live:
                env.closed_reason = "its machine was deleted"
                await self._teardown(env, reason="its machine was deleted")
                out["gone"].append(env.id)
                continue
            if env.gone_at is not None:  # its machine stopped: no tunnel, screen or save; ended once it waited
                await self.reap_gone(env, now, out)
                continue
            env = await self.renew_live(env, now)
            if now >= env.expires_at:
                await self._end_of_life(env, now, out)
                continue
            tunnel = self._tunnels.get(env.id)
            if (rec.state == "ready" and (tunnel is None or not tunnel.alive) and env.state == "ready"
                    and await self.machine_gone(env, cause="its tunnel went down")):
                continue  # gone: the agent's next call says so (openajar.environments.liveness)
            # down: the firewall follows this computer, then the tunnel, the browser lane and the screen (relink)
            if (rec.state == "ready" and (tunnel is None or not tunnel.alive)
                    and await self.relink_once(env, cause="the reaper found its tunnel down")):
                out["tunnels"].append(env.id)
            if rec.state == "ready":
                await self.ssh_keep(env, now)  # local mode: the agent's own ssh, and its use counted as use
                await self.quiet_upgrade(env, now)  # a keep "live" machine's security updates (openajar.environments.updates)
            idle = (now - self._last_used(env, now) >= timedelta(minutes=self.settings.idle_minutes)
                    and not await self._busy_for_idle(env))
            if rec.state == "ready":  # after the idle rule read the lane: the check's command never looks like work
                self._screen_check(env)  # a desktop's screen that died on its own starts again, in the background
            if env.keep == "live" or env.state != "ready":
                continue  # a kept machine is never closed for idleness
            if env.keep == "save":
                if idle:
                    await self.save_and_close(env, now, out, reason=f"idle for {self.settings.idle_minutes} minutes")
                else:
                    await self.autosave(env, now, out)
                continue
            near_end = env.expires_at - now <= timedelta(minutes=self.settings.unsaved_notice_minutes)
            if not idle and not near_end:
                continue
            repos = await self.git_repos(env)
            unsaved = self.unsaved_of(repos) if repos is not None else {"unreachable": True}
            if unsaved is None and idle:
                await self._teardown(env, reason=f"idle for {self.settings.idle_minutes} minutes")
                out["closed"].append(env.id)
                continue
            if unsaved is not None and not env.unsaved_noticed:
                def noticed(e: Environment) -> None:
                    e.unsaved_noticed = True

                env = self.store.update(env.id, noticed)
                left = max(0, int((env.expires_at - now).total_seconds() // 60))
                await self._notify(f"Environment \"{env.title}\" has work that is not pushed",
                                   f"{agent_names.said(env.agent, env.client)} left environment {env.id} with changes that are not committed or "
                                   f"pushed. It stays open for {left} more minutes, to its time limit; then openajar "
                                   "saves that work and deletes the machine. Open it to look, or extend it.")
                out["kept_unsaved"].append(env.id)
        out["swept"] = await self.sweep_lanes(now)  # lanes a closed environment left behind
        with contextlib.suppress(Exception):  # saves past saved_days (the owner is told a day before)
            out["saves"] = await self.prune_saves(now)
        with contextlib.suppress(Exception):  # snapshots of saves that did not finish
            swept = await self.sweep_snapshots(now)
            if swept.get("deleted"):
                out["snapshots_deleted"] = swept["deleted"]
        with contextlib.suppress(Exception):  # prepared machines past their retention (openajar.environments.prepbuild)
            out["prepared"] = await self.prepared_reap(now)
        with contextlib.suppress(Exception):  # warm machines past warm_minutes (openajar.environments.warmpool)
            out["warm_deleted"] = await self.pool_reap(now)
        return out

    async def _end_of_life(self, env: Environment, now: datetime, out: dict) -> None:
        """Past its time limit (a laptop that woke late included): keep "save" saves first; any other
        with unsaved work gets a rescue save first. Never a silent discard: a save that fails keeps the
        machine and tells the owner (openajar.vms gives it ENV_SAVE_GRACE before its own reaper deletes it)."""
        if env.state != "ready":
            await self._teardown(env, reason="time limit")
            out["closed"].append(env.id)
            return
        if env.keep == "save":
            await self.save_and_close(env, now, out, reason="time limit")
            return
        repos = await self.git_repos(env)
        unsaved = self.unsaved_of(repos) if repos is not None else {"unreachable": True}
        if unsaved is None:
            await self._teardown(env, reason="time limit")
            out["closed"].append(env.id)
            return
        await self.rescue_and_close(env, now, out, reason="time limit")

    async def _closing(self, env: Environment, now: datetime, out: dict) -> None:
        """One the CLI marked closing: a keep "save" one is saved first (unless it was discarded); if that
        fails it goes back to ready, and the owner is told."""
        reason = env.closed_reason or "closed"
        if env.keep == "save" and "(discarded)" not in reason and env.vm_id:
            def ready(e: Environment) -> bool:
                if e.state != "closing":
                    return False  # closed (or opened again) since it was read: never undo that
                e.state, e.closed_reason = "ready", None
                return True

            env = self.store.update(env.id, ready)
            if env.state == "ready":
                await self.save_and_close(env, now, out, reason=reason)
            return
        await self._teardown(env, reason=reason)
        out["closed"].append(env.id)

    # ================================================================ report
    def report(self) -> dict:
        return {"open": len(self.store.open()), "tunnels": sum(1 for t in self._tunnels.values() if t.alive),
                "month_spend_eur": self.month_spend(), "last_reap": self.last_reap,
                "saves": len(self.saves().all()), "snapshots_eur_month": self.snapshots_eur_month(),
                "prepared": self.prepared_rows(), "prepared_eur_month": self.prepared_eur_month(),
                "pool": self.pool_view(), "settings": self.settings.model_dump()}

    def binding_of(self, session: str | None, grant_id: str | None) -> str:
        return binding_of(session, grant_id)


__all__ = ["OPEN_NEXT", "EnvironmentManager", "binding_of", "check_ref", "check_title", "current_client", "is_session",
           "same_title"]
