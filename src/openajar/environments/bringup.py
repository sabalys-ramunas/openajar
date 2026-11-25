"""Bringing an environment up after it is created: the VM, its setup, the tunnels and git, then the
browser lane, the clone and the tools top-up at once, until it is `ready` or `failed`. Each step's
time is logged at INFO (Steps). A mixin of EnvironmentManager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import time
from typing import Any

from openajar.core.errors import LaneExists, OpenajarError
from openajar.core.models import LaneSpec
from openajar.environments import machine, timings
from openajar.environments.errors import (
    EnvironmentFailed,
    EnvironmentLimit,
)
from openajar.environments.models import (
    WORKSPACE,
    Environment,
)
from openajar.environments.probes import CHECK_TIMEOUT_S
from openajar.environments.tunnels import Tunnel

log = logging.getLogger("openajar.environments")

SETUP_POLL_S = 1.0  # between setup checks; each check itself waits for cloud-init (machine.SETUP_WAIT_S)


VM_POLL_S = 0.25


CLONE_TIMEOUT_S = 900.0


# Writes the VM's git config for the gateway, read from stdin (openajar.git.service.vm_gitconfig), to
# a file of its own that ~/.gitconfig includes once. Nothing secret is on the command line.
GIT_SETUP = ("umask 077 && mkdir -p ~/.config/agentos && cat > ~/.config/agentos/gitconfig && "
             "(git config --global --get-all include.path 2>/dev/null | grep -qx '~/.config/agentos/gitconfig' || "
             "git config --global --add include.path '~/.config/agentos/gitconfig')")


# Is the repository public? `git ls-remote` on the VM with no git config of its own (so not the
# gateway's rewrite) and no credential or prompt: "public" when the host answers.
PUBLIC_CHECK = ("if GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false "
                "timeout 20 git -c credential.helper= ls-remote {url} HEAD >/dev/null 2>&1; then echo public; "
                "else echo private; fi")


# What each bring-up is doing right now (environment id -> step -> when it began), so a call that
# comes while a machine starts can say what it waits for. Steps.run keeps it.
NOW: dict[str, dict[str, float]] = {}
STEP_WORDS = {"VM create": "renting the VM", "VM ready": "the VM boots", "setup check": "the VM sets up its tools",
              "apt ready": "the VM's package manager finishes", "tools top-up": "installing tools",
              "tunnels and git": "connecting to the VM", "browser": "starting the browser",
              "clone": "cloning the repository", "restore": "restoring the saved work",
              "ssh": "setting up ssh", "network": "setting up the network", "checkout": "checking out the ref", "refresh": "running the repository's refresh",
              "setup": "running the repository's setup", "app": "starting the app and waiting for its port"}


def doing(env_id: str) -> str | None:
    """What the bring-up of `env_id` is doing now, in words with how long so far ("cloning the
    repository (3 min 10 s so far)"), or None when no step runs (it is not starting here)."""
    steps = NOW.get(env_id)
    if not steps:
        return None
    now = time.monotonic()
    return ", ".join(f"{STEP_WORDS.get(name, name)} ({_so_far(now - began)} so far)" for name, began in steps.items())


def starting_words(env: Any, now: Any) -> str:
    """A machine that is starting, in words: how long since the open, and the step now with its own
    time ("starting for 62 s: the VM boots (19 s so far)"). The steps before the one now (renting the
    VM) took the rest."""
    since = max(0.0, (now - env.created_at).total_seconds()) if env.created_at is not None else None
    head = f"starting for {_so_far(since)}" if since is not None else "starting"
    step = doing(env.id)
    return f"{head}: {step}" if step else head


def _so_far(seconds: float) -> str:
    s = int(seconds)
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60} s"


class Steps:
    """How long each step of a bring-up took, logged at INFO as it ends (a failed one too), so a slow
    open says where its time went. The steps running now are in NOW."""

    def __init__(self, env_id: str):
        self.env_id = env_id
        self.took: list[tuple[str, float]] = []
        self.done: list[dict] = []  # {step, at_s, seconds, ok} as each ends (openajar.environments.timings)
        self.started = time.monotonic()

    async def run(self, name: str, coro: Any) -> Any:
        t = time.monotonic()
        NOW.setdefault(self.env_id, {})[name] = t
        ok = False
        try:
            out = await coro
            ok = True
            return out
        finally:
            running = NOW.get(self.env_id, {})
            running.pop(name, None)
            if not running:
                NOW.pop(self.env_id, None)
            took = time.monotonic() - t
            self.took.append((name, took))
            self.done.append({"step": name, "at_s": round(t - self.started, 1), "seconds": round(took, 1), "ok": ok})
            log.info("environment %s: %s took %.1f s", self.env_id, name, took)

    def running(self) -> list[dict]:
        """The steps in progress: {step, at_s, so_far_s}."""
        now = time.monotonic()
        return [{"step": name, "at_s": round(began - self.started, 1), "so_far_s": round(now - began, 1)}
                for name, began in (NOW.get(self.env_id) or {}).items()]

    def total(self) -> float:
        return time.monotonic() - self.started

    def words(self) -> str:
        return ", ".join(f"{name} {took:.1f} s" for name, took in self.took)


async def together(*coros: Any) -> None:
    """Run the steps at once; the first that fails cancels the others and raises."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BringUp:
    """The steps from `starting` to `ready`, for EnvironmentManager (its store, tunnels, tasks and services)."""

    def _spawn(self, env_id: str, coro: Any) -> None:
        task = asyncio.create_task(coro, name=f"environment-{env_id}")
        self._tasks[env_id] = task
        task.add_done_callback(lambda _t, i=env_id: self._tasks.pop(i, None) if self._tasks.get(i) is _t else None)

    def coming_up(self, env_id: str) -> bool:
        """Is this node's bring-up of it running (a task that wait_ready can wait on)?"""
        task = self._tasks.get(env_id)
        return task is not None and not task.done()

    async def wait_ready(self, env_id: str, wait_s: float) -> Environment:
        task = self._tasks.get(env_id)
        if task is not None and wait_s > 0:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
        return self.store.get(env_id)

    async def _bring_up(self, env_id: str, plan: dict | None) -> None:
        env = self.store.get(env_id)
        steps = timings.LIVE[env_id] = Steps(env_id)
        warm = self._warm_mirror(env)
        try:
            if env.vm_id is None:
                if plan is None:
                    raise EnvironmentFailed("the node restarted before the machine was rented")
                env = await steps.run("VM create", self._rent(env, plan))
            await steps.run("VM ready", self._wait_vm(env))
            tools = await steps.run("setup check", self._wait_setup(env))
            await self._set_up_work(env, steps, tools=tools)
            await self.prepare_work(self.store.get(env_id), steps)  # .openajar/environment.toml (openajar.environments.preparing)

            def ready(e: Environment) -> bool:
                if e.state != "starting":
                    return False
                e.state, e.ready_at, e.bringup = "ready", self._clock(), timings.record(steps, ok=True)
                return True

            env = self.store.update(env_id, ready)
            if env.state == "ready":
                self._screen_soon(env)
                log.info("environment %s ready (%s) in %.0f s: %s", env.id, env.workdir, steps.total(), steps.words())
                await self.after_ready(env)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            from openajar.environments.failures import classify, is_capacity

            room = None
            if is_capacity(e):
                with contextlib.suppress(Exception):  # only an offer: the failure stands without it
                    room = await self.sizes_with_room(env, e)
            reason, msg = classify(e, env, self.settings.max_size, room)
            log.warning("environment %s failed to come up%s: %s", env_id, f" ({reason})" if reason else "", msg)
            code = e.code if isinstance(e, OpenajarError) else None
            if code == "vm_error":
                code = None  # the provider's refusal is the environment's failure, with its reason
            if env.restore_from and code in (None, "environment_failed"):
                code = "restore_failed"  # its save is untouched: the next open with the title tries again
            if warm is not None and not warm.done():
                warm.cancel()  # no clone waits for it any more
            await self._fail(env_id, msg, code=code, reason=reason, bringup=timings.record(steps, ok=False))
        finally:
            timings.ended(env_id, steps)

    async def _rent(self, env: Environment, plan: dict) -> Environment:
        """Rent the machine (openajar.vms; its own fallbacks when the provider has no room) and record it."""
        paused = (plan.get("restore") or {}).get("from") == "paused"  # the same machine (openajar.environments.pausing)
        claimed = None if paused else await self.claim_warm(env, plan)  # a warm one (openajar.environments.warmpool)
        if claimed is not None:
            return self.store.get(env.id)
        await self.make_room()
        cloud = machine.cloud_config() if plan["image"] == machine.BASE_IMAGE else ""
        try:
            if paused:
                rec = await self._resume_paused(env, plan)
            else:
                rec = await self.vms().create(env.terminal_lane, plan=plan, purpose=f"environment {env.id}: {env.title}",
                                              created_by=env.grant_id, wait_s=0, cloud_config=cloud,
                                              environment=env.id, kept=env.keep == "live")
        except OpenajarError as e:
            from openajar.vms.models import VmFull

            if isinstance(e, VmFull):  # the node's count, in the words every open hears (Sizing.check_count)
                self.check_count(besides=env.id)
                open_, others = self.machines(besides=env.id)
                raise self.full_error(open_, others, max(1, len(open_) + len(others))) from None
            if e.code == "vm_limit":
                raise EnvironmentLimit(e.message) from None
            raise

        def rented(e: Environment) -> None:
            e.vm_id = rec.id
            if rec.price_hourly_eur is not None:
                e.price_hourly_eur = rec.price_hourly_eur
            e.machine.server_type, e.machine.location = rec.server_type, rec.location

        return self.store.update(env.id, rented)

    def _warm_mirror(self, env: Environment) -> asyncio.Task | None:
        """Bring the node's mirror of env.repo up to date now, while the VM is rented and boots (a minute
        or two), so the clone after it is served from a warm mirror. A cold mirror of a big repository
        is minutes of fetching from the remote (a 241 MiB one took 254 s), which the clone otherwise waits
        for. It runs on its own: a failure is the clone's to report, and a bring-up that fails stops it.
        The task, or None when there is nothing to warm."""
        git = self._get("git")
        warm = getattr(git, "warm", None)
        if not env.repo or not callable(warm):
            return None

        async def run() -> None:
            try:
                await warm(env.terminal_lane, grant_id=env.grant_id, repo=env.repo)
            except Exception:
                log.warning("environment %s: the warm-up of the mirror of %s failed", env.id, env.repo, exc_info=True)

        task = asyncio.create_task(run(), name=f"environment-{env.id}-warm")
        self._warming.add(task)  # held until done: a task nobody holds may be collected mid-fetch
        task.add_done_callback(self._warming.discard)
        return task

    async def _top_up(self, env: Environment) -> None:
        with contextlib.suppress(OpenajarError):  # pnpm on an older snapshot (machine.TOOLS_TOP_UP)
            await self.envs().exec(env.terminal_lane, machine.TOOLS_TOP_UP, timeout_s=machine.TOOLS_TOP_UP_S)

    async def _apt_ready(self, env: Environment) -> None:
        with contextlib.suppress(OpenajarError):  # package lists, fetched in the background (machine.apt_ready)
            await self.envs().exec(env.terminal_lane, machine.apt_ready(), timeout_s=machine.APT_READY_S)

    async def _swap(self, env: Environment) -> None:
        """Swap on the machine (machine.SWAP_ON: zram, else a swapfile), and what it got in its notes."""
        try:
            res = await self.envs().exec(env.terminal_lane, machine.SWAP_ON, timeout_s=machine.SWAP_ON_S)
        except OpenajarError:
            return
        words = machine.swap_words(res.stdout or "")
        if words:
            def noted(e: Environment) -> None:
                e.machine.swap = words

            self.store.update(env.id, noted)

    async def _set_up_work(self, env: Environment, steps: Steps, *, tools: bool = True) -> None:
        """The tunnels first (the browser's traffic and the clone through the git gateway go over them),
        then the browser, the clone and the rest at once: each is a few seconds to a minute of its own.
        What needs no tunnel starts with them: the apt wrappers, swap, the pnpm top-up (only where the setup
        check found no pnpm) and, in local mode, the agent's own ssh (openajar.environments.sshaccess).
        A resume waits for the browser and the tools first: its services start in it."""
        early = [asyncio.ensure_future(steps.run("apt ready", self._apt_ready(env))),
                 asyncio.ensure_future(steps.run("swap", self._swap(env)))]
        if tools:
            early.append(asyncio.ensure_future(steps.run("tools top-up", self._top_up(env))))
        if self.ssh_wanted():
            early.append(asyncio.ensure_future(steps.run("ssh", self.ssh_up(env))))
        try:
            await steps.run("tunnels and git", self._start_tunnel(env))
            await steps.run("network", self.net_setup(env))  # its proxy, placeholders and CA (networking)
            if env.restore_from:
                await together(steps.run("browser", self._make_browser(env)), *early)
                await steps.run("restore", self._restore_any(env))
                self.start_browser(env)  # after the restore: its sign-ins go into the browser as it starts
            elif env.repo:
                await together(steps.run("browser", self._make_browser(env, start=True)),
                               steps.run("clone", self._clone(env)), *early)
            else:
                from openajar.environments.identity import committer, git_identity_cmd

                who = git_identity_cmd(await committer(self), env.agent)  # a repository made here commits as them too
                mkdir = self.envs().exec(env.terminal_lane, f"mkdir -p {machine.WORK} && {{ {who}; }} || true",
                                         timeout_s=CHECK_TIMEOUT_S)
                await together(steps.run("browser", self._make_browser(env, start=True)), mkdir, *early)
        finally:
            for t in early:
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(BaseException):
                        await t

    async def _fail(self, env_id: str, why: str, *, code: str | None = None, reason: str | None = None,
                    bringup: dict | None = None) -> None:
        def failed(e: Environment) -> None:
            e.error = why[:1200]
            e.bringup = bringup or e.bringup
            e.error_code = code or "environment_failed"
            e.error_reason = reason if e.error_code == "environment_failed" else None

        env = self.store.update(env_id, failed)
        await self._teardown(env, reason=f"failed to start: {why[:200]}", final="failed")

    async def _wait_vm(self, env: Environment) -> None:
        vms = self.vms()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + vms.settings.boot_timeout_s + 30
        while True:
            rec = vms.store.get(env.vm_id)
            if rec.state == "ready":
                return
            if not rec.live:
                raise EnvironmentFailed(f"the machine did not come up: {rec.error or rec.state}", reason="setup")
            if loop.time() > deadline:
                raise EnvironmentFailed(f"the machine did not answer SSH within {vms.settings.boot_timeout_s} s", reason="setup")
            await asyncio.sleep(VM_POLL_S)

    async def _wait_setup(self, env: Environment) -> bool:
        """Until the machine's setup is done. True when pnpm is missing there (the tools top-up runs). A
        provider without cloud-init (Morph) never runs it on its own: the node runs it at once."""
        from openajar.vms.provider import capable

        envs = self.envs()
        no_cloud_init = not capable(self.vms().provider(), "cloud_init")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.setup_timeout_s
        said = ""
        ran = False
        while loop.time() < deadline:
            answered = False  # the check ran on the machine (it always exits 0 there; ssh's own failure is 255)
            try:
                res = await envs.exec(env.terminal_lane, machine.SETUP_STATE, timeout_s=CHECK_TIMEOUT_S)
                said, answered = (res.stdout or res.stderr or "").strip(), res.exit_code == 0
            except OpenajarError as e:
                said = e.message
            first = said.split("\n", 1)[0]
            if first == "ready":
                return machine.needs_pnpm(said)
            if "error" in first:
                raise EnvironmentFailed(f"the machine's setup failed: {said[-600:]}", reason="setup")
            if not ran and ((no_cloud_init and answered) or machine.boot_done_without_setup(first)):
                # booted and done, but nothing ran the setup: a snapshot made another way (`openajar vm
                # image build` without the environment's script) or an image with no cloud-init
                ran = True
                left = max(60.0, deadline - loop.time())
                try:
                    out = await envs.exec(env.terminal_lane, "sh -s", stdin=machine.setup_script(), timeout_s=left)
                except OpenajarError as e:
                    raise EnvironmentFailed(f"the machine's setup could not run: {e.message}", reason="setup") from None
                if out.exit_code != 0:
                    tail = (out.stderr or out.stdout or "").strip()[-600:]
                    raise EnvironmentFailed(f"the machine's setup failed (exit {out.exit_code}): {tail}", reason="setup")
                continue
            await asyncio.sleep(SETUP_POLL_S)
        raise EnvironmentFailed(f"the machine's setup did not finish within {self.settings.setup_timeout_s} s "
                                f"({said[-200:] or 'no answer'})", reason="setup")

    async def _start_tunnel(self, env: Environment) -> None:
        vms = self.vms()
        rec = vms.store.get(env.vm_id)
        old = self._tunnels.get(env.id)
        if old is not None and old.alive:
            return
        if old is not None:  # a dead one still holds its ports (its guards), its sockets and any ssh of it still running
            await old.stop()
        git = self._get("git")
        gateway = None
        if git is not None and callable(getattr(git, "vm_listener", None)):
            try:  # the gateway's listener for this VM alone: its token is good only through its own tunnel
                gateway = await git.vm_listener(env.terminal_lane)
            except Exception as e:  # noqa: BLE001 - the environment works without git through the node
                log.warning("environment %s: no git gateway listener: %s", env.id, e)
        on_vm = {"vm_browser": True, "browser_port": old.browser_port if old else None} if env.browser == "vm" else {}
        tunnel = Tunnel(vms.remote(rec), gateway_port=gateway, socks_port=old.socks_port if old else None, **on_vm)
        tunnel.free_first = bool(getattr(old, "remote_taken", False))  # the machine refused its -R: free it first
        tunnel.net_forwards = await self.net_forwards(env)  # a closed network's proxy, the secret swap
        self.watch_link(env, tunnel)  # type: ignore[attr-defined]  (Relink: a drop brings it back at once)
        await tunnel.start()
        self._tunnels[env.id] = tunnel
        self._wrap_proxies_loader()
        from openajar.node.interaction import VM_PROXIED_LANES

        VM_PROXIED_LANES.add(env.browser_lane)  # its localhost is the VM's: a page there may open it
        if gateway is not None:
            await self._git_setup(env)

    async def _git_setup(self, env: Environment) -> None:
        """Give the VM the one token it holds: a gateway token that fetches and pushes env.repo and
        nothing else, good only through this VM's own tunnel, revoked when the environment closes.
        No declared repository: no token at all. It reaches the VM's git config through the
        command's stdin, never a command line (which `ps` on the node shows)."""
        git = self._get("git")
        if git is None or not env.repo or not callable(getattr(git, "vm_token", None)):
            return
        from openajar.git.service import vm_gitconfig

        token = git.vm_token(env.terminal_lane, grant_id=env.grant_id, repo=env.repo)
        if token is None:
            return  # not declared for env: a public repository, cloned straight from the VM
        public = await self._public_from_vm(env)
        res = await self.envs().exec(env.terminal_lane, GIT_SETUP,
                                     stdin=vm_gitconfig(env.repo, token, fetch_direct=public),
                                     timeout_s=CHECK_TIMEOUT_S)
        if res.exit_code != 0:
            raise EnvironmentFailed(f"setting up git on the machine failed: {(res.stderr or res.stdout).strip()[-300:]}")
        (self._fetch_direct.add if public else self._fetch_direct.discard)(env.id)

    async def _public_from_vm(self, env: Environment) -> bool:
        """Can the VM read env.repo straight from its host with no credential (a public repository)? Then
        it clones and fetches from there (the cloud's own bandwidth: seconds, where the pack through this
        computer's upload took 110 s), and only its pushes go through the gateway, with its rules. Asked
        on the VM, without its git config, so nothing it holds is used. No answer: through the gateway."""
        from openajar.git.wire import parse_remote

        if env.network != "full":
            return False  # a closed machine reaches no host straight (and its proxy is not set up yet)
        remote = parse_remote(env.repo or "")
        url = shlex.quote(f"https://{remote.host}/{remote.repo}.git")
        try:
            res = await self.envs().exec(env.terminal_lane, PUBLIC_CHECK.format(url=url), timeout_s=CHECK_TIMEOUT_S)
        except OpenajarError:
            return False
        return res.exit_code == 0 and (res.stdout or "").strip().endswith("public")

    async def _make_browser(self, env: Environment, *, start: bool = False) -> None:
        """A scratch browser lane made by the node: no account, no vault entry, no keepalive; its
        proxy is the environment's tunnel, so every request leaves from the VM. With browser = "vm" the
        lane's browser runs on the machine instead (openajar.environments.vmbrowser). `start`: and its
        browser starts now, beside the clone (start_browser)."""
        await self._make_browser_lane(env)
        if start:
            self.start_browser(env)

    def start_browser(self, env: Environment) -> None:
        """Start the environment's browser in the background, so the agent's first browser call finds it
        running: it was the largest wait after a cold open (10 to 14 s on a docker machine, evals
        2026-11-18). Nothing waits for it, the open neither: a browser call meanwhile waits for the
        start in progress, and one that failed is started again by the first call, as before."""
        lanes = self._get("lanes")
        opener = getattr(lanes, "open", None)
        if not callable(opener) or not callable(getattr(lanes, "get", None)):
            return
        try:
            lanes.get(env.browser_lane)
        except Exception:  # noqa: BLE001 - no browser lane (a node with no browser, a terminal only)
            return

        async def start() -> None:
            began = time.monotonic()
            try:
                await opener(env.browser_lane)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the first browser call starts it, and says why it fails
                log.info("environment %s: its browser did not start ahead (%s); the first browser call starts "
                         "it", env.id, str(e)[:200] or type(e).__name__)
                return
            log.info("environment %s: its browser started ahead in %.1f s", env.id, time.monotonic() - began)

        task = asyncio.create_task(start(), name=f"environment-{env.id}-browser")
        self._warming.add(task)  # held until done, and cancelled at shutdown
        task.add_done_callback(self._warming.discard)
        starts = self.__dict__.setdefault("_browser_starts", {})
        starts[env.id] = task
        task.add_done_callback(lambda t, i=env.id: starts.pop(i, None) if starts.get(i) is t else None)

    async def stop_browser_start(self, env_id: str) -> None:
        """A close does not wait for a browser nobody used to finish starting: its start is cancelled
        (the lane manager tears a half-started browser down). An agent that pushed and closed within
        seconds of the open waited 2 to 3 s for it (evals, 2026-11-18)."""
        task = (self.__dict__.get("_browser_starts") or {}).pop(env_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _make_browser_lane(self, env: Environment) -> None:
        lanes = self._get("lanes")
        if lanes is None:
            return
        if env.browser == "vm":
            env = await self._browser_on_vm(env, lanes)
        if env.browser != "vm" and getattr(lanes, "makes_browsers", True) is False:
            return  # a node with no browser of its own (openajar local) and no browser on the machine: a terminal only
        from openajar.environments import signins

        signins.listen(self, lanes)  # a resume's sign-ins go into the browser when it starts
        with contextlib.suppress(Exception):
            lanes.get(env.browser_lane)
            return
        spec = LaneSpec(id=env.browser_lane, proxy_label=f"env-{env.id}")
        try:
            await lanes.create(spec)
        except LaneExists:
            return

    def _screen_soon(self, env: Environment) -> None:
        """An environment with its browser on the machine: its screen and desktop start now, in the background,
        and stay until it closes (openajar.environments.vmbrowser.VmBrowsers.screen_soon)."""
        if env.browser == "vm" and self._vm_browsers is not None:
            self._vm_browsers.screen_soon(env)

    def _screen_check(self, env: Environment) -> None:
        """The reaper's pass: an environment whose desktop is up gets its screen started again if it died
        (openajar.environments.vmbrowser.VmBrowsers.check_screen)."""
        if env.browser == "vm" and self._vm_browsers is not None:
            self._vm_browsers.check_screen(env)

    def vm_browsers(self) -> Any:
        """The machine side of "vm" browsers (openajar.environments.vmbrowser.VmBrowsers), made once. The
        local node installs its routers when it is built; any other node on its first "vm" browser."""
        if self._vm_browsers is None:
            from openajar.environments import desklink, vmbrowser

            self._vm_browsers = vmbrowser.VmBrowsers(self)
            desklink.install(self, self._vm_browsers)  # the desktop's terminal window, focus and DISPLAY
        return self._vm_browsers

    async def _browser_on_vm(self, env: Environment, lanes: Any) -> Environment:
        """Route the lane to the machine and make sure the machine has the browser. A lane manager that
        cannot be routed (the dev node's simulated lanes) keeps the browser on the node, and says so."""
        from openajar.environments import vmbrowser

        if not vmbrowser.install(lanes, self.vm_browsers()):
            log.warning("environment %s: this node's lanes cannot run a browser on the machine; it runs on the "
                        "node instead", env.id)

            def on_node(e: Environment) -> None:
                e.browser = "node"

            return self.store.update(env.id, on_node)
        try:
            await self._vm_browsers.ensure_installed(env)
        except OpenajarError as e:
            raise EnvironmentFailed(e.message) from None
        return env

    async def _clone(self, env: Environment) -> None:
        from openajar.git.wire import parse_remote

        remote = parse_remote(env.repo or "")
        name = remote.repo.rsplit("/", 1)[-1]
        dest = f"{machine.WORK}/{name}"
        git = self._get("git")
        declared = git is not None and git.remotes.find(remote, [env.terminal_lane, WORKSPACE]) is not None
        url = f"https://{remote.host}/{remote.repo}.git"
        q = shlex.quote
        checkout = f" && git -C {q(dest)} checkout -q {q(env.ref)}" if env.ref else ""
        # Through the gateway the pack crosses this computer's upload, so the clone is blobless: every
        # commit and tree, and only the files a checkout needs; git fetches any other file when a command
        # needs it, through the gateway too (a 241 MiB repository: about 57 MiB). Straight from the host
        # (public): all of it, at the cloud's speed.
        blobless = declared and env.id not in self._fetch_direct
        how = "--filter=blob:none " if blobless else ""
        from openajar.environments.identity import committer, git_identity_cmd

        who = git_identity_cmd(await committer(self), env.agent)  # the owner's GitHub account, else the agent
        cmd = (f"mkdir -p {machine.WORK} && if [ ! -d {q(dest)}/.git ]; then "
               f"git clone -q {how}{q(url)} {q(dest)}{checkout}; fi && cd {q(dest)} && "
               f"{who} && git rev-parse --abbrev-ref HEAD")
        # a declared repository goes through the gateway by the VM's own git config (_git_setup)
        res = await self.envs().exec(env.terminal_lane, cmd, timeout_s=CLONE_TIMEOUT_S)
        if res.exit_code != 0:
            from openajar.environments.repos import clone_failure

            raise clone_failure(remote.slug, (res.stderr or "") + "\n" + (res.stdout or ""), via_gateway=declared)
        branch = (res.stdout.strip().splitlines() or [None])[-1]

        def cloned(e: Environment) -> None:
            e.workdir, e.via_gateway, e.branch = dest, declared, branch

        self.store.update(env.id, cloned)
