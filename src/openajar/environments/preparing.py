"""Prepared machines on an open's side, and the app started for the agent. A mixin of EnvironmentManager.

Before anything is rented (prepared_for): the repository's `.openajar/environment.toml`
(openajar.environments.repoconfig) is read on the node, from its mirror of a declared repository or
from GitHub's raw files for a public one, and the key of its prepared machine is worked out. A ready
prepared machine with that key is the image the open boots (plan_prepared).

In the bring-up, after the clone (prepare_work):

- from a prepared machine: the checkout it holds is brought to the asked ref (a fetch, not a clone),
  then `refresh` runs;
- else: `setup` runs on the agent's own machine (its dependencies are installed either way), and the
  node starts a build of the prepared machine for next time, within its limits
  (openajar.environments.prepbuild);
- then `start` runs as a job (as terminal_job_start does, so it shows in jobs, a save records it as a
  service and a resume starts it again), and the open waits for `port` to listen, up to
  ready_timeout_s. A setup that fails or an app that does not listen never fails the open: the
  result says what happened, with the end of the log (prepared_view, app_view).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import shlex
import time
from datetime import timedelta
from typing import Any

from openajar.core import clock
from openajar.core.errors import OpenajarError
from openajar.environments import machine
from openajar.environments.models import WORKSPACE, Environment
from openajar.environments.prepared import PreparedStore, age_words
from openajar.environments.repoconfig import (
    CHECKOUT_READ,
    CONFIG_MAX_BYTES,
    CONFIG_PATH,
    CONFIG_PATHS,
    DEFAULT_INPUTS,
    LEGACY_CONFIG_PATH,
    ConfigRead,
    RepoConfig,
    blob_id,
    checkout_blobs_cmd,
    from_text,
    parse_ls_tree,
    prepared_key,
    wanted,
)

log = logging.getLogger("openajar.environments")

CONFIG_WARM_S = 10.0           # reading the file waits this long for the node's mirror to catch up
RAW_TIMEOUT_S = 10.0           # each file read from GitHub
RAW_MAX_BYTES = 64 * 2**20     # a lockfile bigger than this is not read (its key is then unknown)
RAW_BASE = "https://raw.githubusercontent.com"
SETUP_TIMEOUT_S = 1800.0       # setup on the agent's machine or the build machine
REFRESH_TIMEOUT_S = 900.0
CHECKOUT_TIMEOUT_S = 600.0
TAIL = 1500                    # characters of a failed command's output kept for the agent
FAILED_RETRY = timedelta(hours=6)  # a failed build is not tried again by an open for this long
# How the repository's commands run: in its folder, under bash, with CI set so package managers never
# wait for an answer (pnpm asks before it removes node_modules when there is no terminal).
RUN = "cd {dir} && CI=true COREPACK_ENABLE_DOWNLOAD_PROMPT=0 bash -c {cmd}"
NO_CONFIG = (f"the repository has no {CONFIG_PATH}. Propose one in your branch (setup, and start with port; "
             "the skill shows it): once it is there, the next open of that branch installs and starts the app "
             "by itself, and later ones start from a prepared machine")

# Fetch the asked ref into a prepared machine's checkout: the default branch at its head (the branch the
# build checked out), a branch at origin's head, or a tag or commit, detached. Prints the result.
CHECKOUT = r"""
set -u
cd "$1" || { echo "error=no checkout in $1"; exit 3; }
R=${2:-}
git fetch -q --prune --tags origin 2>&1 | tail -n 5 || true
if [ -z "$R" ]; then
  B=$(git symbolic-ref -q --short HEAD || true)
  if [ -n "$B" ] && git rev-parse -q --verify "refs/remotes/origin/$B" >/dev/null; then git checkout -q -f -B "$B" "origin/$B"; fi
elif git rev-parse -q --verify "refs/remotes/origin/$R" >/dev/null; then
  git checkout -q -f -B "$R" "origin/$R"
else
  git checkout -q -f --detach "$R" 2>/dev/null || { git fetch -q origin "$R" && git checkout -q -f --detach FETCH_HEAD; } \
    || { echo "error=no branch, tag or commit $R"; exit 5; }
fi
echo "branch=$(git rev-parse --abbrev-ref HEAD)"
echo "commit=$(git rev-parse HEAD)"
"""


async def raw_get(url: str) -> tuple[int, bytes]:
    """(HTTP status, body) of a public file, with no credential. (0, b"") when the host did not answer."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=RAW_TIMEOUT_S, follow_redirects=True) as c, \
                c.stream("GET", url, headers={"User-Agent": "openajar"}) as r:
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > RAW_MAX_BYTES:
                    return 0, b""
            return r.status_code, bytes(body)
    except httpx.HTTPError:
        return 0, b""


def _lines(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out.setdefault(k, []).append(v)
    return out


class Preparing:
    """For EnvironmentManager: its store, settings, clock, _get, vms(), envs(), ports() and _image()."""

    def prepared_store(self) -> PreparedStore:
        store = self.__dict__.get("_prepared_store")
        if store is None:
            store = self.__dict__["_prepared_store"] = PreparedStore(self.home)
        return store

    # ================================================================ before the rent
    async def prepared_for(self, env_id: str, repo: str, ref: str | None, *, grant_id: str) -> dict:
        """What the open does about the repository's prepared machine: the file as read, its key, and
        `state` (ready: `image` boots it; none, building, failed: setup runs on the machine; absent,
        invalid, no_setup, unreadable: nothing is prepared). Never raises: an open works without it."""
        try:
            read = await self._read_config(env_id, repo, ref, grant_id)
        except Exception as e:  # noqa: BLE001 - only the prepared part is lost
            log.warning("environment %s: reading %s of %s failed: %s", env_id, CONFIG_PATH, repo, e)
            read = ConfigRead("unreadable", why=f"the node could not read {CONFIG_PATH}")
        base_name, base_id = await self._base_image()
        out: dict[str, Any] = {"state": read.state, "source": read.source, "why": read.why, "commit": read.commit,
                               "base_image": base_name, "base_id": base_id}
        if read.state != "found" or read.config is None:
            return out
        return self._match_prepared(out, repo, read, ref)

    def _match_prepared(self, out: dict, repo: str, read: ConfigRead, ref: str | None) -> dict:
        cfg = read.config
        assert cfg is not None
        out["config"] = cfg.model_dump()
        if not cfg.setup:
            out["state"] = "no_setup"
            return out
        key = prepared_key(repo, out["base_id"], cfg, read.inputs, ref)
        out["key"] = key
        rec = self.prepared_store().get(key)
        if rec is None:
            out["state"] = "none"
        elif rec.state == "ready" and rec.image:
            out.update(state="ready", image=rec.image, built={
                "commit": rec.commit, "built_at": clock.iso_or_none(rec.built_at, z=True), "setup_s": rec.setup_s})
        elif rec.state == "building":
            out.update(state="building", started_at=clock.iso_or_none(rec.started_at, z=True))
        else:
            out.update(state="failed", error=rec.error, failed_at=clock.iso_or_none(rec.started_at, z=True))
        return out

    async def plan_prepared(self, env_id: str, plan: dict, prepared: dict, *, size: str | None,
                            hours: float | None, keep: str) -> dict:
        """The plan, made again for the prepared machine's image when one is ready (a type whose disk fits
        it). An image the provider no longer has: the open goes on from the usual image, and says so."""
        if prepared.get("state") != "ready" or not prepared.get("image"):
            return plan
        try:
            gone = await self.vms().image(prepared["image"]) is None
        except Exception:  # noqa: BLE001 - the provider cannot say now: the rent will
            gone = False
        if gone:  # deleted outside openajar: forget it, so setup runs here and a new one is built
            with contextlib.suppress(Exception):
                self.prepared_store().remove(prepared.get("key") or "")
            prepared.update(state="none", why="its prepared machine's image is gone, so setup ran here and a new "
                                              "one is built")
            prepared.pop("image", None)
            return plan
        try:
            again = await self.plan(env_id, size=size, hours=hours, keep=keep, image=prepared["image"])
        except OpenajarError as e:
            prepared.update(state="none", why=f"its prepared machine could not be planned ({e.message[:200]})")
            prepared.pop("image", None)
            return plan
        self.check_limits(again)
        return again

    async def _base_image(self) -> tuple[str, str]:
        """(the image an open boots without a prepared machine, its provider id): the key's base."""
        name = await self._image()
        try:
            return name, str(await self.vms().provider().resolve_image(name))
        except Exception:  # noqa: BLE001 - the name stands for it
            return name, name

    async def _read_config(self, env_id: str, repo: str, ref: str | None, grant_id: str) -> ConfigRead:
        probe = getattr(self, "config_probe", None)  # tests read the file their own way
        if callable(probe):
            return await probe(repo, ref)
        from openajar.git.wire import parse_remote

        remote = parse_remote(repo)
        git = self._get("git")
        lane = f"{WORKSPACE}/{env_id}"
        rules = getattr(git, "remotes", None)
        if rules is not None and rules.find(remote, [lane, WORKSPACE]) is not None:
            return await self._read_mirror(git, lane, grant_id, repo, ref)
        if remote.host == "github.com":
            return await self._read_github(remote.repo, ref)
        return ConfigRead("unreadable", why=f"the node reads {CONFIG_PATH} before the rent only from GitHub or a "
                                            "connected repository; it reads it in the checkout instead")

    async def _read_mirror(self, git: Any, lane: str, grant_id: str, repo: str, ref: str | None) -> ConfigRead:
        """The file in the node's mirror, once it is up to date (a warm-up the bring-up would run anyway,
        waited for at most CONFIG_WARM_S; it goes on in the background past that). A mirror not made yet
        is not waited for: its first fetch of a big repository takes minutes, which the boot hides, so
        the file is read in the checkout instead."""
        from openajar.git.wire import parse_remote

        mirror = git.gateway.mirror(parse_remote(repo))
        made = (mirror.path / "HEAD").is_file()
        warm = asyncio.ensure_future(git.warm(lane, grant_id=grant_id, repo=repo))
        self._warming.add(warm)
        warm.add_done_callback(self._warming.discard)
        if not made:
            return ConfigRead("unreadable", source="mirror", why="the node's copy of the repository is not fetched "
                                                                  "yet, so the file is read in the checkout")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(warm), timeout=CONFIG_WARM_S)
        commit = None
        for cand in ([ref, f"refs/heads/{ref}", f"refs/tags/{ref}"] if ref else ["HEAD"]):
            rc, out, _ = await mirror.git("rev-parse", "-q", "--verify", f"{cand}^{{commit}}")
            if rc == 0 and out.strip():
                commit = out.decode().strip()
                break
        if commit is None:
            return ConfigRead("unreadable", source="mirror", why=f"the node's copy has no {ref or 'default branch'} yet")
        rc, out, _ = await mirror.git("ls-tree", commit, "--", *CONFIG_PATHS)
        found = parse_ls_tree(out.decode(errors="replace"), list(CONFIG_PATHS)) if rc == 0 else {}
        blob = next((found[p] for p in CONFIG_PATHS if found.get(p)), None)  # the new name first
        if blob is None:
            return from_text(None, source="mirror", commit=commit)
        rc, text, _ = await mirror.git("cat-file", "blob", blob)
        read = from_text(text[:CONFIG_MAX_BYTES + 1].decode(errors="replace"), source="mirror", commit=commit)
        if read.config is not None:
            rc, names, _ = await mirror.git("ls-tree", "--name-only", commit)
            paths = wanted(read.config.inputs, names.decode(errors="replace").splitlines())
            rc, listed, _ = await mirror.git("ls-tree", commit, "--", *paths) if paths else (0, b"", b"")
            read.inputs = parse_ls_tree(listed.decode(errors="replace"), paths)
        return read

    async def _read_github(self, slug: str, ref: str | None) -> ConfigRead:
        """The file from GitHub's raw files at the ref (HEAD: the default branch), with no credential."""
        fetch = getattr(self, "raw_fetch", None) or raw_get
        base = f"{RAW_BASE}/{slug}/{ref or 'HEAD'}/"
        status, body = await fetch(base + CONFIG_PATH)
        if status == 404:  # legacy-name: a repository that has it where 0.4.0 read it
            status, body = await fetch(base + LEGACY_CONFIG_PATH)
        if status == 404:
            return from_text(None, source="github")
        if status != 200:
            return ConfigRead("unreadable", source="github", why="GitHub did not answer for the repository's files")
        read = from_text(body.decode(errors="replace"), source="github")
        if read.config is None:
            return read
        named = read.config.inputs
        paths = named if named is not None else [*(p for p in DEFAULT_INPUTS if "*" not in p),
                                                 "requirements.txt", "requirements-dev.txt"]
        got = await asyncio.gather(*(fetch(base + p) for p in paths))
        inputs: dict[str, str | None] = {}
        for p, (st, data) in zip(paths, got, strict=True):
            if st == 200:
                inputs[p] = blob_id(data)
            elif st == 404:
                if named is not None:
                    inputs[p] = None
            else:
                return ConfigRead("unreadable", source="github", why=f"GitHub did not answer for {p}")
        read.inputs = inputs
        return read

    # ================================================================ in the bring-up
    async def prepare_work(self, env: Environment, steps: Any) -> None:
        """After the clone: the prepared machine's checkout and refresh, or setup and a build for next time,
        then the app. Never raises (an open never fails for these); what happened is on env.prepared/app."""
        p = dict(env.prepared or {})
        if not p or not env.repo or not env.workdir.startswith(machine.WORK + "/"):
            return
        try:
            if p.get("state") == "unreadable":
                p = await self._read_in_checkout(env, p)
            cfg = RepoConfig(**p["config"]) if p.get("config") else None
            if cfg is not None and p.get("image"):
                p["checkout"] = await steps.run("checkout", self._checkout_ref(env))
                if cfg.refresh:
                    p["refresh"] = await steps.run("refresh", self._run_in_repo(env, cfg.refresh, REFRESH_TIMEOUT_S))
                self._note_used(p.get("key") or "")
            elif cfg is not None and cfg.setup:
                p["setup"] = await steps.run("setup", self._run_in_repo(env, cfg.setup, SETUP_TIMEOUT_S))
                p["build"] = await self._build_for_next_time(env, p, cfg)
            self._keep_prepared(env, p)
            if cfg is not None and cfg.start and cfg.port:
                app = await steps.run("app", self._start_app(env, cfg))
                self.store.update(env.id, lambda e: setattr(e, "app", app))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("environment %s: the repository's setup step failed", env.id, exc_info=True)
            p["error"] = f"{type(e).__name__}: {str(getattr(e, 'message', e))[:300]}"
            self._keep_prepared(env, p)

    def _keep_prepared(self, env: Environment, p: dict) -> None:
        self.store.update(env.id, lambda e: setattr(e, "prepared", p))

    def _note_used(self, key: str) -> None:
        now = self._clock()

        def used(r: Any) -> None:
            r.last_used_at, r.uses = now, r.uses + 1

        with contextlib.suppress(Exception):
            self.prepared_store().update(key, used)

    async def _build_for_next_time(self, env: Environment, p: dict, cfg: RepoConfig) -> dict:
        if not (p.get("setup") or {}).get("ok"):
            return {"started": False, "why": "setup failed here, so no prepared machine is built from it"}
        if not p.get("key") or p.get("state") == "building":
            return {"started": False, "why": "one is being built already" if p.get("state") == "building" else
                    "its key is unknown"}
        if p.get("state") == "failed" and (p.get("failed_at") or "") > clock.iso_or_none(
                self._clock() - FAILED_RETRY, z=True):
            return {"started": False, "why": "the last build failed less than 6 hours ago; the owner builds it "
                                             "again with `openajar environments prepare " + (env.repo or "") + "`"}
        return await self.start_build(repo=env.repo or "", ref=env.ref, key=p["key"], config=cfg,
                                      base_image=p.get("base_image") or "", base_id=p.get("base_id") or "",
                                      grant_id=env.grant_id)

    async def _read_in_checkout(self, env: Environment, p: dict) -> dict:
        """The node could not read the file before the rent: read it in the clone (the same commit the
        agent works on). A ready prepared machine cannot be used any more, so its state is none."""
        res = await self.envs().exec(env.terminal_lane, f"cd {shlex.quote(env.workdir)} && {{ {CHECKOUT_READ}; }}",
                                     timeout_s=60)
        got = _lines(res.stdout or "")
        raw = (got.get("config") or ["-"])[0]
        text = None if raw == "-" else base64.b64decode(raw or "").decode(errors="replace")
        commit = (got.get("commit") or [""])[0] or None
        read = from_text(text, source="checkout", commit=commit)
        p.update(state=read.state, source="checkout", why=read.why, commit=commit)
        if read.config is None:
            return p
        paths = wanted(read.config.inputs, got.get("name") or [])
        listed = await self.envs().exec(env.terminal_lane,
                                        f"cd {shlex.quote(env.workdir)} && {checkout_blobs_cmd(paths)}", timeout_s=60)
        read.inputs = parse_ls_tree(listed.stdout or "", paths)
        p = self._match_prepared(p, env.repo or "", read, env.ref)
        if p.get("state") == "ready":  # booted from the base image already
            p["state"] = "none_now"
            p.pop("image", None)
        return p

    async def _checkout_ref(self, env: Environment) -> dict:
        t = time.monotonic()
        res = await self.envs().exec(env.terminal_lane, f"bash -s -- {shlex.quote(env.workdir)} "
                                     f"{shlex.quote(env.ref or '')}", stdin=CHECKOUT, timeout_s=CHECKOUT_TIMEOUT_S)
        got = _lines(res.stdout or "")
        out: dict[str, Any] = {"ok": res.exit_code == 0 and "error" not in got, "seconds": round(time.monotonic() - t, 1)}
        if out["ok"]:
            branch, commit = (got.get("branch") or [None])[-1], (got.get("commit") or [None])[-1]
            out["commit"] = (commit or "")[:12] or None
            if branch:
                self.store.update(env.id, lambda e: setattr(e, "branch", branch))
        else:
            out["tail"] = self._tail((got.get("error") or [""])[-1] or (res.stderr or res.stdout or ""))
        return out

    async def _run_in_repo(self, env: Environment, cmd: str, timeout_s: float) -> dict:
        """One of the repository's commands in its folder: {command, ok, exit_code, seconds, tail on a failure}."""
        t = time.monotonic()
        out: dict[str, Any] = {"command": cmd}
        try:
            res = await self.envs().exec(env.terminal_lane, RUN.format(dir=shlex.quote(env.workdir),
                                                                       cmd=shlex.quote(cmd)), timeout_s=timeout_s)
            out.update(ok=res.exit_code == 0, exit_code=res.exit_code)
            if res.exit_code != 0:
                out["tail"] = self._tail((res.stdout or "") + "\n" + (res.stderr or ""))
        except OpenajarError as e:
            out.update(ok=False, exit_code=None, tail=self._tail(e.message))
        out["seconds"] = round(time.monotonic() - t, 1)
        return out

    def _tail(self, text: str) -> str:
        """The end of a log, with vault values and secret shapes redacted, lines kept."""
        from openajar.policy.redact import redact_text
        from openajar.policy.scrub import runtime_scrub

        text = runtime_scrub(self.runtime)(text.strip()[-4 * TAIL:])
        return redact_text(text, max_len=10 * TAIL)[-TAIL:]

    async def _start_app(self, env: Environment, cfg: RepoConfig) -> dict:
        """`start` as a job in the repository's folder, then up to ready_timeout_s for `port` to listen."""
        envs = self.envs()
        port = int(cfg.port or 0)
        app: dict[str, Any] = {"command": cfg.start, "port": port, "listening": False}
        t = time.monotonic()
        try:
            job = await envs.job_start(env.terminal_lane, cfg.start or "", cwd=env.workdir, by="openajar (app)",
                                       grant_id=env.grant_id)
        except OpenajarError as e:
            app["why"] = f"it did not start: {e.message[:300]}"
            return app
        app["job_id"] = job.id
        try:
            app["listening"] = bool(await envs.job_wait_port(job.id, port, float(cfg.ready_timeout_s)))
        except OpenajarError as e:
            app["why"] = f"its port could not be checked: {e.message[:200]}"
        app["seconds"] = round(time.monotonic() - t, 1)
        if not app["listening"]:
            await self._why_not_listening(env, app, job.id, cfg)
        return app

    async def _why_not_listening(self, env: Environment, app: dict, job_id: str, cfg: RepoConfig) -> None:
        envs = self.envs()
        info = None
        with contextlib.suppress(Exception):
            info = next((j for j in envs.job_list(env.terminal_lane) if j.id == job_id), None)
        if info is not None and info.state != "running":
            code = f" with exit code {info.exit_code}" if info.exit_code is not None else ""
            app["why"] = app.get("why") or f"the command {info.state}{code} before port {cfg.port} listened"
        else:
            app["why"] = app.get("why") or (f"it runs, but port {cfg.port} did not listen within {cfg.ready_timeout_s} "
                                            "s (check it listens on that port, on 0.0.0.0 or 127.0.0.1)")
        tail = ""
        with contextlib.suppress(Exception):
            for stream in ("stdout", "stderr"):
                size = getattr(info, f"{stream}_bytes", 0) or 0
                tail += (await envs.job_log(job_id, stream, max(0, size - 3000), 3000)).decode(errors="replace")
        if tail.strip():
            app["log_tail"] = self._tail(tail)
        with contextlib.suppress(Exception):
            app["listening_instead"] = [p["port"] for p in await self.ports(env) if p.get("port") not in (None, 22)]
        app["next"] = f"terminal_job_read(job=\"{job_id}\") shows its whole output"

    # ================================================================ restart (environment_app_restart)
    app_free_wait_s = 10.0  # how long a restart waits for the old process to let go of the port

    async def restart_app(self, env: Environment) -> dict:
        """`start` again, as the open runs it: the running jobs of the app (its job, or the same command
        started by hand) end with their process trees (job_cancel ends the process group), the port is
        given up to app_free_wait_s to close, then _start_app. The file is read again in the checkout, so
        a changed `start` or `port` counts. Returns env.app with `stopped` (the job ids ended)."""
        from openajar.core.errors import InvalidRequest

        cfg = await self._app_config(env)
        if cfg is None or not cfg.start or not cfg.port:
            raise InvalidRequest(f"this repository's {CONFIG_PATH} has no start command (with port), so there is no "
                                 "app to restart: add them there, or run your server with terminal_job_start")
        envs = self.envs()
        async with self._lock(f"app:{env.id}"):
            old = self.store.get(env.id).app or {}
            stopped = []
            for j in envs.job_list(env.terminal_lane):
                if j.state == "running" and (j.id == old.get("job_id") or j.cmd in (cfg.start, old.get("command"))):
                    with contextlib.suppress(OpenajarError):
                        await envs.job_cancel(j.id)
                        stopped.append(j.id)
            held = await self._port_held(env, int(cfg.port))
            app = await self._start_app(env, cfg)
            app["stopped"] = stopped
            if held:
                app["why"] = (f"port {cfg.port} still listened after the old app stopped ({held}), so `listening` "
                              f"may be that process, not the new one: stop it (terminal_exec(cmd=\"fuser -k "
                              f"{cfg.port}/tcp\")) and restart again")
            self.store.update(env.id, lambda e: setattr(e, "app", app))
        await self._app_link(self.store.get(env.id))  # its link, as at the open
        return self.prepared_view(self.store.get(env.id))["app"]

    async def _app_config(self, env: Environment) -> RepoConfig | None:
        """The file as it is in the checkout now, else as the open read it."""
        from openajar.core.errors import InvalidRequest

        read = None
        with contextlib.suppress(OpenajarError):
            res = await self.envs().exec(env.terminal_lane, f"cd {shlex.quote(env.workdir)} && {{ {CHECKOUT_READ}; }}",
                                         timeout_s=60)
            raw = (_lines(res.stdout or "").get("config") or ["-"])[0]
            if raw != "-":
                read = from_text(base64.b64decode(raw).decode(errors="replace"), source="checkout")
        if read is not None and read.config is None:
            raise InvalidRequest(f"{CONFIG_PATH} in {env.workdir}: {read.why}")
        if read is not None:
            return read.config
        stored = (env.prepared or {}).get("config")
        return RepoConfig(**stored) if stored else None

    async def _port_held(self, env: Environment, port: int) -> str | None:
        """What still listens on `port` after app_free_wait_s (None: it closed)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + self.app_free_wait_s
        while True:
            row = next((p for p in await self.ports(env) if p.get("port") == port), None)
            if row is None:
                return None
            if loop.time() >= end:
                return row.get("process") or "a process"
            await asyncio.sleep(min(0.5, max(0.0, end - loop.time())))

    async def after_ready(self, env: Environment) -> None:
        """Once the environment is ready: the app's port link (as environment_port gives), and the warm
        pool filled again for the next open. Never raises."""
        await self._app_link(env)
        with contextlib.suppress(Exception):
            self.pool_refill_soon()

    async def _app_link(self, env: Environment) -> None:
        from openajar.environments import ports as env_ports

        app = env.app or {}
        if app.get("listening") and app.get("port"):
            try:
                link = await env_ports.forward(self, env, int(app["port"]))
                got = {"link": link["url"], **({"tunnel": link["tunnel"]} if "tunnel" in link else {})}
                self.store.update(env.id, lambda e: setattr(e, "app", {**(e.app or {}), **got}))
            except Exception as e:  # noqa: BLE001 - environment_port makes it later
                log.info("environment %s: no link for port %s yet: %s", env.id, app["port"], e)

    # ================================================================ what the agent reads
    def prepared_view(self, env: Environment) -> dict:
        """environment_open's and environment()'s `prepared` and `app` (nothing when no repository)."""
        out: dict[str, Any] = {}
        p = env.prepared
        if p:
            out["prepared"] = self._prepared_words(p)
        if env.app:
            out["app"] = {k: v for k, v in env.app.items()}
            out["app"]["restart"] = 'tools_call("environment_app_restart", {"on": "<your on>"}) after a change'
        if getattr(env, "warm_claimed", False):
            out.setdefault("prepared", {})["warm"] = "this machine came from the node's warm pool"
        return out

    def _prepared_words(self, p: dict) -> dict[str, Any]:
        used = bool(p.get("image"))
        view: dict[str, Any] = {"used": used}
        if used:
            built = p.get("built") or {}
            view["from"] = {"commit": (built.get("commit") or "")[:12] or None, "built_at": built.get("built_at")}
            when = clock.parse(built.get("built_at")) if built.get("built_at") else None
            if when is not None:
                view["from"]["age"] = age_words((self._clock() - when).total_seconds())
            for k in ("checkout", "refresh"):
                if p.get(k):
                    view[k] = p[k]
            if built.get("setup_s") is not None:
                spent = (p.get("refresh") or {}).get("seconds") or 0
                view["setup_s"] = {"build": built["setup_s"], "here": spent,
                                   "saved": round(max(0.0, built["setup_s"] - spent), 1)}
            return view
        view["why"] = self._why_not_prepared(p)
        if p.get("setup"):
            view["setup"] = p["setup"]
        if p.get("build"):
            view["build"] = p["build"]
        if p.get("error"):
            view["error"] = p["error"]
        return view

    def _why_not_prepared(self, p: dict) -> str:
        state = p.get("state")
        if state == "absent":
            return NO_CONFIG
        if state in ("invalid", "unreadable"):
            return p.get("why") or f"{CONFIG_PATH} could not be read"
        if state == "no_setup":
            return f"its {CONFIG_PATH} has no setup, so there is nothing to prepare"
        if state == "building":
            return "a prepared machine for this repository is being built; the next open starts from it"
        if state == "failed":
            return f"the last build of its prepared machine failed: {(p.get('error') or 'no reason given')[-600:]}"
        if state == "none_now":
            return "the node read the file only after the rent, so this open could not use its prepared machine"
        if p.get("why"):
            return str(p["why"])
        return "no prepared machine for this repository and its lockfiles yet, so setup ran here"
