"""What an environment shows its caller and the panel: ports, repositories and pushes, work not
saved yet, the summary, the detail and the links. A mixin of EnvironmentManager."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from openajar.core import agent_names, clock
from openajar.core.app_paths import lane_viewer_path
from openajar.core.errors import OpenajarError
from openajar.environments.models import (
    WORKSPACE,
    Call,
    Environment,
)
from openajar.environments.probes import CHECK_TIMEOUT_S, GIT_STATE, main_repo, parse_git_state, parse_ports
from openajar.node.viewer_state import env_agent_name

UNSAVED_LIST_MAX = 50


def only_modes(unsaved: dict) -> bool:
    """Every unsaved change is a file's mode (chmod +x by a setup script), its content as committed."""
    files, modes = unsaved.get("dirty_files") or [], set(unsaved.get("mode_only") or [])
    return (bool(files) and len(files) < UNSAVED_LIST_MAX and all(f in modes for f in files)
            and not any(unsaved.get(k) for k in ("unpushed", "stashes", "unreadable", "loose_files")))


class Views:
    """The read side of EnvironmentManager (it reads self.store, self.vms() and self.envs())."""

    # ================================================================ read
    async def ports(self, env: Environment) -> list[dict]:
        try:
            res = await self.probe_exec(  # type: ignore[attr-defined]  (Activity, on the same manager)
                env.terminal_lane, "ss -ltnpH 2>/dev/null || ss -ltnp", timeout_s=CHECK_TIMEOUT_S)
        except OpenajarError:
            return []
        from openajar.environments import ports as env_ports

        return env_ports.with_links(self, env.id, parse_ports(res.stdout))  # a forwarded one has its link

    async def git_repos(self, env: Environment) -> list[dict] | None:
        """Every repository under /work, the cloned one and any other the agent made: None when the
        machine does not answer."""
        try:
            res = await self.probe_exec(  # type: ignore[attr-defined]  (Activity, on the same manager)
                env.terminal_lane, GIT_STATE, timeout_s=CHECK_TIMEOUT_S)
        except OpenajarError:
            return None
        if res.exit_code not in (0, None) and not res.stdout:
            return None
        return parse_git_state(res.stdout)

    def pushes(self, env: Environment) -> list[dict]:
        """Each branch this environment pushed, as its last push left it on the remote, newest first:
        {branch, commit, at}, and for a branch it deleted {branch, commit: None, at, deleted: True}.
        From the git gateway's push traces and the record kept at close."""
        last: dict[str, dict] = {}

        def note(branch: str, commit: str, at: str | None, deleted: bool) -> None:
            had = last.get(branch)
            if had is None or (at or "") >= (had["at"] or ""):  # oldest first: a later push wins
                last[branch] = ({"branch": branch, "commit": None, "at": at, "deleted": True} if deleted else
                                {"branch": branch, "commit": commit[:12], "at": at})

        for p in env.pushed:
            note(p.branch, p.commit, clock.iso_or_none(p.at, z=True), p.deleted)
        for ev in reversed(self._push_traces(env)):
            args = ev.args if isinstance(ev.args, dict) else {}
            at = clock.iso_or_none(datetime.fromtimestamp(ev.at or 0, UTC), z=True)
            for ref in args.get("refs") or []:
                name = str(ref.get("ref") or "").removeprefix("refs/heads/")
                commit = str(ref.get("new") or "").split(":", 1)[-1]
                note(name, commit, at, ref.get("kind") == "delete" or not commit.strip("0"))
        return sorted(last.values(), key=lambda p: p["at"] or "", reverse=True)

    def _push_traces(self, env: Environment) -> list:
        """The git gateway's pushes from this environment that reached the remote, newest first."""
        traces = getattr(self._get("policy"), "traces", None)
        if traces is None:
            return []
        with contextlib.suppress(Exception):
            events, _ = traces.query(kind="git_push", lane=env.terminal_lane, limit=50)
            return [ev for ev in events if isinstance(ev.args, dict) and ev.args.get("result") == "pushed"]
        return []

    def push_lists(self, env: Environment) -> dict:
        """{pushed: what is on the remote now, deleted: the branches it deleted}, each newest first."""
        every = self.pushes(env)
        return {"pushed": [p for p in every if not p.get("deleted")],
                "deleted": [{"branch": p["branch"], "at": p["at"]} for p in every if p.get("deleted")]}

    @staticmethod
    def unsaved_of(repos: list[dict] | None) -> dict | None:
        """{dirty_files, unpushed, stashes} of every repository (each named by its folder when
        there are several), and `loose_files`, the files under /work in no repository (probes.GIT_STATE),
        or None when all of it is committed and on a remote. A repository git could not read counts as
        unsaved (`unreadable`)."""
        loose = next((r for r in repos or [] if r.get("loose")), None)
        repos = [r for r in repos or [] if not r.get("loose")]
        out = Views._unsaved_repos(repos) if repos else None
        if loose is not None and loose["dirty"]:
            out = out or {"dirty_files": [], "unpushed": [], "stashes": []}
            out["loose_files"] = loose["dirty_files"][:UNSAVED_LIST_MAX]
            out["loose_count"] = loose["dirty"]
        return out

    @staticmethod
    def _unsaved_repos(repos: list[dict]) -> dict | None:
        many = len(repos) > 1
        dirty = [f"{r['dir']}/{f}" if many else f for r in repos for f in r["dirty_files"]]
        unpushed = [f"{r['dir']}: {c}" if many else c for r in repos for c in r["unpushed"]]
        stashes = [f"{r['dir']}: {s}" if many else s for r in repos for s in r.get("stashes") or []]
        unreadable = [r["dir"] for r in repos if r.get("error")]
        if not (dirty or unpushed or stashes or unreadable) and not any(
                r["dirty"] or r["ahead"] or r.get("stash_count") for r in repos):
            return None
        out = {"dirty_files": dirty[:UNSAVED_LIST_MAX], "unpushed": unpushed[:UNSAVED_LIST_MAX],
               "stashes": stashes[:UNSAVED_LIST_MAX]}
        if modes := [f"{r['dir']}/{f}" if many else f for r in repos for f in r.get("mode_only") or []]:
            out["mode_only"] = modes[:UNSAVED_LIST_MAX]  # files whose content is as committed (unsaved_words)
        if unreadable:
            out["unreadable"] = unreadable[:UNSAVED_LIST_MAX]
        return out

    @staticmethod
    def unsaved_words(unsaved: dict, most: int = 10) -> str:
        """What unsaved_of found, for an error or a notification (`most` files and commits of each)."""
        parts = []

        def more(items: list) -> str:
            return f" and {len(items) - most} more" if len(items) > most else ""

        modes = set(unsaved.get("mode_only") or [])
        if files := [f for f in unsaved.get("dirty_files") or [] if f not in modes]:
            parts.append("changed or new files: " + ", ".join(files[:most]) + more(files))
        if chmods := [f for f in unsaved.get("dirty_files") or [] if f in modes]:
            parts.append(("only file modes changed, no content: " if only_modes(unsaved) else
                          "mode change only: ") + ", ".join(chmods[:most]) + more(chmods))
        if unsaved.get("unpushed"):
            commits = unsaved["unpushed"]
            parts.append("commits not on a remote: " + "; ".join(c[:80] for c in commits[:most]) + more(commits))
        if unsaved.get("stashes"):
            parts.append("stashes: " + "; ".join(unsaved["stashes"][:5]))
        if unsaved.get("unreadable"):
            parts.append("repositories git cannot read: " + ", ".join(unsaved["unreadable"][:5]))
        if unsaved.get("loose_files"):
            count = unsaved.get("loose_count") or len(unsaved["loose_files"])
            more = f" and {count - 10} more" if count > 10 else ""
            parts.append(f"files in /work in no git repository: {', '.join(unsaved['loose_files'][:10])}{more}")
        return "; ".join(parts) or "changes git counts but did not list"

    def summary(self, env: Environment) -> dict:
        """EnvSummary of the contract (docs/design/environments.md). One whose machine stopped carries
        `machine_gone` (when, why), so a list can say so without a read of each."""
        now = self._clock()
        out: dict[str, Any] = {"id": env.id, "title": env.title, "state": env.shown_state(now), "repo": env.repo,
                "branch": env.branch, "workdir": env.workdir, "created_at": clock.iso_or_none(env.created_at, z=True),
                "ready_at": clock.iso_or_none(env.ready_at, z=True), "last_call_at": clock.iso_or_none(env.last_call_at, z=True),
                "closed_at": clock.iso_or_none(env.closed_at, z=True), "expires_at": clock.iso_or_none(env.expires_at, z=True),
                "minutes_left": max(0, int((env.expires_at - now).total_seconds() // 60)) if env.open else None,
                "machine": env.machine.model_dump(), "price_hourly_eur": env.price_hourly_eur,
                "cost_eur": self.cost_now(env), "agent": agent_names.agent_view(env.agent, env.client, named=env_agent_name(env)),
                "lanes": env.lanes, "closed_reason": env.closed_reason, "error": env.error,
                "error_code": env.error_code, "keep": env.keep,
                "last_save": ({"id": env.last_save_id, "at": clock.iso_or_none(env.last_save_at, z=True)}
                              if env.last_save_id else None), "save_error": env.save_error}
        if env.gone_at is not None:
            out["machine_gone"] = self.gone_view(env)  # type: ignore[attr-defined]  (Liveness)
        return out

    def _commands(self, env: Environment) -> list[dict]:
        out = []
        envs = self._get("envs")
        if envs is not None and callable(getattr(envs, "job_list", None)):
            with contextlib.suppress(Exception):
                for j in envs.job_list(env.terminal_lane):
                    out.append({"id": j.id, "cmd": self._scrub(j.cmd, max_len=500), "state": j.state,
                                "started_at": clock.iso_or_none(j.started_at, z=True), "exit_code": j.exit_code})
        jobs = {self._scrub(r["cmd"]) for r in out}  # as record_call writes a summary
        for i, c in enumerate(env.calls):
            summary = self._scrub(c.summary)
            if c.tool == "terminal_exec":
                out.append({"id": f"exec-{i}", "cmd": summary, "state": "exited" if c.ok else "failed",
                            "started_at": clock.iso_or_none(c.at, z=True), "exit_code": c.exit_code})
            elif c.tool == "terminal_job_start" and c.ok and summary not in jobs:
                # a job whose record went with its lane (the machine was deleted, or the node restarted)
                out.append({"id": f"start-{i}", "cmd": summary, "state": "ended", "started_at": clock.iso_or_none(c.at, z=True),
                            "exit_code": None})
        out.sort(key=lambda r: r["started_at"] or "", reverse=True)
        return out[:50]

    def _viewer(self, env: Environment) -> dict | None:
        lanes = self._get("lanes")
        if lanes is None:
            return None
        try:
            if lanes.view(env.browser_lane) is None:
                return None
        except Exception:  # noqa: BLE001
            return None
        return {"path": lane_viewer_path(WORKSPACE, f"{env.id}-browser")}

    async def detail(self, env: Environment, *, calls: int = 50) -> dict:
        """EnvDetail of the contract, and what environment() returns to an agent. A machine that stopped for good
        (openajar.environments.liveness) shows as state gone, with `machine_gone`, and nothing is asked of it."""
        repos = None
        if env.gone_at is None and env.open and env.state != "starting" and env.vm_id and not self.link_down(env.id):  # type: ignore[attr-defined]
            repos = await self.git_repos(env)
            if repos is None and env.state == "ready" and await self.machine_gone(env, cause="environment()"):  # type: ignore[attr-defined]
                env = self.store.get(env.id)  # type: ignore[attr-defined]
        if env.gone_at is not None:
            return self._gone_detail(env, calls=calls)
        out = self.summary(env)
        main = main_repo(repos, env.workdir)
        out["ports"] = await self.ports(env) if env.state in ("ready",) else []
        out["git"] = {"branch": (main or {}).get("branch") or env.branch, "ahead": (main or {}).get("ahead", 0),
                      "dirty": (main or {}).get("dirty", 0), **self.push_lists(env)}
        out["branch"] = out["git"]["branch"]  # the branch checked out now, not the one it was cloned on
        out["calls"] = [self.call_view(c) for c in reversed(env.calls[-calls:])]
        out["commands"] = self._commands(env)
        out["viewer"] = self._viewer(env)
        out["unsaved"] = self.unsaved_of(repos)
        from openajar.environments.checkpoints import listed

        out["checkpoints"] = listed(repos)  # git's own undo points on the machine (openajar.environments.checkpoints)
        out["watched"] = self.watched(env)  # type: ignore[attr-defined]  (Watching, on the same manager)
        out["network"] = self.network_view(env)  # type: ignore[attr-defined]  (Network, on the same manager)
        out["logins"] = await self.login_names(env) if env.open else []  # type: ignore[attr-defined]  (Logins)
        out.update(self.prepared_view(env))  # type: ignore[attr-defined]  (Preparing, on the same manager)
        from openajar.environments import timings

        out["bringup"] = timings.view(self, env)  # each step of its bring-up and its seconds
        if (link := self.link_view(env)) is not None:  # type: ignore[attr-defined]  (Relink: it dropped, or came back)
            out["link"] = link
        busy = self.in_background(env.id)  # type: ignore[attr-defined]  (Saving, on the same manager)
        if busy:
            out["background"] = f"{busy}: an agent's environment_{'close' if busy == 'closing' else 'save'} goes on"
        return out

    def _gone_detail(self, env: Environment, *, calls: int) -> dict:
        """detail() of an environment whose machine stopped: what the node knows, nothing from the machine."""
        out = self.summary(env)
        out["machine_gone"] = self.gone_view(env)  # type: ignore[attr-defined]  (Liveness)
        out["ports"], out["checkpoints"], out["unsaved"] = [], [], None
        out["git"] = {"branch": env.branch, "ahead": 0, "dirty": 0, **self.push_lists(env)}
        out["calls"] = [self.call_view(c) for c in reversed(env.calls[-calls:])]
        out["commands"] = self._commands(env)
        out["viewer"] = None
        out["network"] = self.network_view(env)  # type: ignore[attr-defined]  (Network, on the same manager)
        return out

    def call_view(self, c: Call) -> dict:
        """One of the last calls: `ok` is the tool's answer; a command's `exit_code` (terminal_exec) and the
        `error` code of a refused or failed call are there when they are known."""
        out = {"at": clock.iso_or_none(c.at, z=True), "tool": c.tool, "summary": self._scrub(c.summary), "ok": c.ok}
        if c.exit_code is not None:
            out["exit_code"] = c.exit_code
        if c.error:
            out["error"] = c.error
        if c.client:
            out["client"] = c.client  # which MCP client made it: chats of one agent share a computer by its title
        return out

    def links(self, env: Environment) -> dict:
        """The links an agent gives the human: `browser`, the browser's live view on the node itself
        (openajar.environments.tools fills it with a signed take-over link once the browser lane
        exists). No page of the web app: a node from the public repository has none."""
        return {"browser": None}
