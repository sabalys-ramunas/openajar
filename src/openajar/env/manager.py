"""EnvManager: terminal and service lanes on one node (runtime service name "envs").

Owns the env lane registry (its own SQLite file), warm lane runtimes, PTY sessions,
service supervisors, installs and base images, plus the lifecycle around them: an idle
reaper, an orphan sweep on start (mounts, cgroups and network namespaces a crash left
behind), and stale-overlay cleanup. Browser lanes stay with the M1 lane manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import secrets
import shlex
import shutil
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core.errors import (
    InvalidRequest,
    LaneBusy,
    LaneExists,
    LaneNotFound,
    LaneQuarantined,
    MachineGone,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.models import IsolationProfile, LaneKind, LaneState, Preset, utcnow
from openajar.env import execs, exposure, images, presets, programs
from openajar.env import pty as ptys
from openajar.env import seccomp as sc
from openajar.env import sync as syncs
from openajar.env.host import Host, HostError, LinuxHost, NullHost
from openajar.env.jobs import JobTable
from openajar.env.lane_jobs import Jobs
from openajar.env.lane_sync import Sync
from openajar.env.lanes import LaneRuntime, NoSandbox, SandboxError
from openajar.env.models import (
    EnvLane,
    EnvLaneInfo,
    ExecResult,
    ImageInfo,
    ImageTemplate,
    Limits,
    Policy,
    RemoteHost,
    ServiceConfig,
    ServiceStatus,
)
from openajar.env.netproxy import EgressPolicy, Forwarder, Upstream
from openajar.env.paths import EnvPaths, EnvSettings, load_settings
from openajar.env.sandbox import BwrapLauncher, DirectLauncher, check_home, plan_lane
from openajar.env.services import RotatingLog, Supervisor, expiry
from openajar.env.store import EnvStore

log = logging.getLogger("openajar.env")

ENV_KINDS = (LaneKind.TERMINAL, LaneKind.SERVICE)


class RuntimeOwned(ServiceUnavailable):
    """Another process (agentosd) holds this home's env lock. Same code as before; `openajar env` prints
    its own fix line for it, since the catalog's one is about a feature not set up (finding 30)."""

    fix = ("openajar env list, create, remove and doctor go through agentosd; for exec, shell, install, reset "
           "and images, use the MCP tools or the panel, or stop agentosd first (sudo systemctl stop agentosd)")


NOT_ROOT = ("sandboxed lanes need agentosd to run as root, and this node runs it as {user} (a hardened "
            "node). A sandbox needs root for its mounts, disk caps, network namespace and limits")


def isolation_reason() -> str | None:
    """Why this machine cannot run isolated (sandboxed) lanes, or None when it can.

    Not unprivileged bubblewrap (QA 2026-11-10, finding 31): bwrap alone would give namespaces, but a
    lane's sandbox is more than that, and the rest is done outside bwrap as root: the overlay mount of
    its base image and kept files, its disk cap (a loop-mounted image), the network namespace its only
    route out (the egress forwarder) lives in, and the cgroup that caps and kills its programs. Doing
    those without root needs a user-namespace rework of each (and Ubuntu 24.04 and later restrict
    unprivileged user namespaces by default), so a hardened node runs workbench lanes only."""
    if not sys.platform.startswith("linux"):
        return "isolated lanes need a Linux node; this machine can run workbench (env0) lanes only"
    if os.geteuid() != 0:
        from openajar.env.exposure import node_user
        return NOT_ROOT.format(user=node_user())
    if not shutil.which("bwrap"):
        return "isolated lanes need bubblewrap: apt-get install bubblewrap"
    return None


CHOICE = ("The choice is the owner's: a node that can run sandboxes (Linux with bubblewrap and agentosd as "
          "root; a hardened node goes back with openajar migrate-service-user --rollback), or a workbench lane "
          "the owner makes with openajar env create WS/NAME --preset workbench " + exposure.FLAG + ", which has "
          "no sandbox and can read the vault and the node token.")


def default_mechanisms(paths: EnvPaths, settings: EnvSettings) -> tuple[Host, Any]:
    reason = isolation_reason()
    if reason is not None:
        return NullHost(reason), DirectLauncher()
    return LinuxHost(paths, settings), BwrapLauncher(sc.compile_filter() if sc.supported() else None)


class EnvManager(Jobs, Sync):
    """The env service: terminal and service lanes. Its jobs (Jobs) and file sync (Sync) are mixins in
    lane_jobs.py and lane_sync.py."""

    def __init__(self, home: Path, *, runtime: Any = None, host: Host | None = None, launcher: Any = None,
                 settings: EnvSettings | None = None, use_lock: bool = True):
        self.paths = EnvPaths(Path(home))
        self.settings = settings or load_settings(self.paths.home)
        self.runtime = runtime
        if host is None:
            host, launcher = default_mechanisms(self.paths, self.settings)
        self.host: Host = host
        self.launcher = launcher or DirectLauncher()
        # a VM lane's launcher: one ssh to the VM per workload. openajar.vms sets it (it builds on env),
        # and so does the env CLI; without it a VM lane does not warm
        self.remote_launcher: Any = None
        self.store = EnvStore(self.paths.db)
        self.shells: dict[str, ptys.ShellSession] = {}
        self.services: dict[str, Supervisor] = {}
        self.egress_log: deque[dict] = deque(maxlen=500)
        self.last_sweep: dict = {}
        self._rts: dict[str, LaneRuntime] = {}
        self._lane_locks: dict[str, asyncio.Lock] = {}
        self._install_locks: dict[str, asyncio.Lock] = {}
        self._warming: set[str] = set()
        self._started = False
        self._start_lock: asyncio.Lock | None = None
        self._reaper: asyncio.Task | None = None
        self._lockfd: int | None = None
        self._use_lock = use_lock
        self._home_error: str | None = None
        self.jobs = JobTable(self.paths, self.settings)
        self._reattach: asyncio.Task | None = None
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._stores: dict[str, syncs.ChunkStore] = {}
        # Hooks the environments service sets for a machine whose screen runs its desktop (openajar.
        # environments.desklink): `exec_mirror(lane_id, cmd, cwd)` gives a tap that shows a command and its
        # output in the desktop's terminal window, `screen_env(lane_id)` the DISPLAY a command's GUI uses.
        # Both are best effort: they never change a command, and a failing hook is as if it were not set.
        self.exec_mirror: Any = None
        self.screen_env: Any = None
        # `machine_lost(lane_id)`, awaited: a VM lane's ssh could not reach its machine. It asks the machine's
        # provider (openajar.environments.liveness) and returns why the machine is gone for good, or None
        # (it runs: a network blip, a busy machine). Then the call fails with machine_gone instead of
        # handing back ssh's exit 255, and a job on it is lost at once with that reason.
        self.machine_lost: Any = None
        self.jobs.lost_check = self.lost_reason

    # =============================================================== lifecycle
    async def start(self) -> None:
        """Take ownership of this home's env runtime, sweep orphans, restart wanted services."""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self._started:
                return
            if self._use_lock:
                self._acquire_lock()
            for d in (self.paths.run, self.paths.images, self.paths.empty, self.paths.volumes):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(self.paths.run, 0o700)
            if self.host.can_isolate:
                try:
                    check_home(self.paths, self.settings)
                except InvalidRequest as e:
                    self._home_error = e.message
            await asyncio.to_thread(self._sweep)
            if self.host.can_isolate:
                await asyncio.to_thread(self._build_mask)
            for lane in self.store.list_lanes():
                state, _ = self.store.get_state(lane.id)
                if state != LaneState.QUARANTINED:
                    await self.store.run(self.store.set_state, lane.id, LaneState.COLD)
            lanes = self.store.list_lanes()
            lost = await asyncio.to_thread(self.jobs.load, [lane.id for lane in lanes],
                                           {lane.id for lane in lanes if lane.remote is not None})
            if lost:
                log.warning("%d background job(s) were running when the node stopped; marked lost", lost)
            self._started = True
            if self.jobs.pending:  # jobs that ran on on their VMs while the node was down
                self._reattach = asyncio.create_task(self._reattach_jobs(), name="env-jobs-reattach")
            for lane in self.store.list_lanes():
                desired, exp = self.store.service_desired(lane.id)
                if lane.service and desired == "running":
                    if exp and exp <= utcnow():
                        await self.store.run(self.store.set_service_desired, lane.id, "stopped")
                        continue
                    self._supervisor(lane).start(exp)
            self._reaper = asyncio.create_task(self._reap_loop(), name="env-reaper")

    async def ensure_started(self) -> None:
        if not self._started:
            await self.start()

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        if self._reattach is not None:
            self._reattach.cancel()
            with contextlib.suppress(BaseException):
                await self._reattach
            self._reattach = None
        # jobs on VMs run on there: only the node's followers go, and the next start reattaches
        detached = await self.jobs.detach_all()
        if detached:
            log.info("%d background job(s) run on on their VMs; the next start follows them again", detached)
        for sup in list(self.services.values()):
            await sup.stop("node shutting down")
        for sid in list(self.shells):
            await self.shell_close(sid)
        for lane_id in list(self._rts):
            await self.cool(lane_id)
        if self._started:
            await asyncio.to_thread(self.host.cgroup_prune)
        self._started = False
        if self._lockfd is not None:
            fcntl.flock(self._lockfd, fcntl.LOCK_UN)
            os.close(self._lockfd)
            self._lockfd = None

    def _acquire_lock(self) -> None:
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeOwned(
                "agentosd (or another openajar process) is running terminal and service lanes for this "
                "OPENAJAR_HOME, so this command cannot run them here at the same time") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lockfd = fd

    def _sweep(self) -> None:
        mounts = self.host.mounts_under(self.paths.run)
        for m in mounts:
            self.host.unmount(Path(m))
        cgroups = self.host.cgroup_sweep()
        removed = 0
        if self.paths.run.is_dir():
            for d in self.paths.run.iterdir():
                if d.is_dir() and not self.host.mounts_under(d):
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
        for d in self.paths.images.glob(".build-*") if self.paths.images.is_dir() else []:
            if not self.host.mounts_under(d):
                shutil.rmtree(d, ignore_errors=True)
        self.last_sweep = {"mounts": len(mounts), "cgroups": cgroups, "run_dirs": removed, "at": utcnow().isoformat()}
        if mounts or cgroups:
            log.warning("env orphan sweep: %s", self.last_sweep)

    def _build_mask(self) -> None:
        self.host.build_mask(self.paths.mask, list(self.settings.extra_etc_masks))
        # skeleton for the lane-only /var and /root
        var = self.paths.mask / "var"
        for sub in ("log", "tmp", "cache/apt/archives/partial", "lib/misc", "spool", "mail", "backups", "local", "opt"):
            (var / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(var / "tmp", 0o1777)
        root = self.paths.mask / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        for f in (".bashrc", ".profile"):
            src = Path("/etc/skel") / f
            if src.is_file() and not (root / f).exists():
                shutil.copyfile(src, root / f)

    # =============================================================== lanes
    def _lock_for(self, lane_id: str) -> asyncio.Lock:
        return self._lane_locks.setdefault(lane_id, asyncio.Lock())

    def _node_config(self):
        if self.runtime is not None and self.runtime.has("config"):
            return self.runtime.config
        from openajar.core.config import load_config
        return load_config(self.paths.home)

    def _browser_lane_exists(self, lane_id: str) -> bool:
        if (self.paths.lane_dir(lane_id) / "profile").exists():
            return True
        lanes = self.runtime.get("lanes") if self.runtime is not None else None
        if lanes is None:
            return False
        try:
            lanes.get(lane_id)
            return True
        except LaneNotFound:
            return False
        except Exception:  # noqa: BLE001
            return False

    def create(self, lane_id: str, *, kind: str = "terminal", preset: str = "sandbox",
               overrides: dict[str, Any] | None = None, service: ServiceConfig | dict | None = None,
               exit_proxy: str | None = None, allowed_programs: list[str] | None = None,
               by_agent: bool = False, node_secrets_ok: bool = False) -> EnvLaneInfo:
        """`node_secrets_ok`: the owner said yes to a lane with no sandbox (a workbench lane), which runs
        as agentosd's own user and can read the vault and the node token (openajar.env.exposure)."""
        lane = self.check_create(lane_id, kind=kind, preset=preset, overrides=overrides, service=service,
                                 exit_proxy=exit_proxy, allowed_programs=allowed_programs, by_agent=by_agent,
                                 node_secrets_ok=node_secrets_ok)
        self.store.add_lane(lane)
        return self.info(lane_id)

    def check_create(self, lane_id: str, *, kind: str = "terminal", preset: str = "sandbox",
                     overrides: dict[str, Any] | None = None, service: ServiceConfig | dict | None = None,
                     exit_proxy: str | None = None, allowed_programs: list[str] | None = None,
                     by_agent: bool = False, node_secrets_ok: bool = False) -> EnvLane:
        """The lane `create` would add, or the error it would raise, with nothing stored: terminal_create asks
        a person only for a lane that can be made (QA-2 #13). An agent's isolated lane also needs a node
        that can isolate, so the approval is not spent on a lane whose first command fails."""
        try:
            kind_v = LaneKind(kind)
        except ValueError:
            raise InvalidRequest(f"no lane kind {kind!r}: terminal or service") from None
        if kind_v not in ENV_KINDS:
            raise InvalidRequest(f"{kind} lanes are not env lanes; browser lanes are created with `openajar lane create`")
        try:
            preset_v = Preset(preset)
        except ValueError:
            raise InvalidRequest(f"no preset {preset!r}: {', '.join(p.value for p in Preset)}") from None
        if preset_v == Preset.ACCOUNT:
            raise InvalidRequest("the account preset is for browser and android lanes (no shell)")
        if preset_v == Preset.AGENT:
            raise InvalidRequest("the agent preset is for agent lanes, which are created by openajar.agents")
        if by_agent and preset_v.value not in self.settings.agent_create_presets:
            raise PolicyDenied(f"agents may create only {', '.join(self.settings.agent_create_presets)} lanes; "
                               f"a {preset_v.value} lane is created by the owner with `openajar env create`")
        if self.store.has_lane(lane_id) or self._browser_lane_exists(lane_id):
            raise LaneExists(f"lane {lane_id} already exists")
        profile, limits, policy = presets.resolve(preset_v, kind=kind_v, overrides=overrides)
        if by_agent:
            self._check_agent_dials(profile.filesystem, limits)
        if profile.base_image:
            profile.base_image = self.resolve_image(profile.base_image).ref
            if profile.filesystem == "shared":
                raise InvalidRequest("a base image needs an isolated filesystem (overlay or ephemeral)")
        svc = ServiceConfig.model_validate(service) if isinstance(service, dict) else service
        if kind_v == LaneKind.SERVICE and svc is None:
            raise InvalidRequest("service lanes need a command (service.command)")
        if kind_v != LaneKind.SERVICE and svc is not None:
            raise InvalidRequest("only service lanes take a service command")
        if exit_proxy:
            if profile.network != "isolated":
                raise InvalidRequest("an exit proxy needs network=isolated")
            from openajar.core.config import load_proxies
            if exit_proxy not in load_proxies(self._node_config()):
                raise InvalidRequest(f"no proxy labelled {exit_proxy!r}; add it with `openajar proxy add`")
        if policy.signed_only and not allowed_programs:
            allowed_programs = []
        lane = EnvLane(id=lane_id, kind=kind_v, preset=preset_v, profile=profile, limits=limits, policy=policy,
                       service=svc, exit_proxy=exit_proxy, allowed_programs=list(allowed_programs or []))
        if lane.isolated and not getattr(self.host, "can_isolate", True):
            # refused for the owner too: a lane, or a service, that could never start (finding 31)
            raise NoSandbox(f"this node cannot run a {preset_v.value} lane, so nothing was created: "
                            f"{self._no_isolation()}. {CHOICE}")
        if exposure.reaches_node(lane) and not node_secrets_ok:
            raise InvalidRequest(exposure.create_refusal(lane_id, lane=lane))
        return lane

    def _no_isolation(self) -> str:
        return getattr(self.host, "reason", None) or "this node cannot run isolated lanes"

    def check_runnable(self, lane: EnvLane) -> None:
        """Refuse, before anyone is asked, a command in a sandboxed lane this node can never run."""
        if lane.isolated and not getattr(self.host, "can_isolate", True):
            raise NoSandbox(f"{lane.id} is a sandboxed ({lane.preset.value}) lane, and this node cannot run one: "
                            f"{self._no_isolation()}. Nothing ran. {CHOICE}")

    def _check_agent_dials(self, filesystem: str, limits: Limits) -> None:
        """An agent's lane keeps a private filesystem, and its size stays under the owner's caps (env.toml)."""
        if filesystem == "shared":
            raise PolicyDenied("an agent's lane never shares the node filesystem; use overlay (kept) or ephemeral")
        caps = (("memory_mb", limits.memory_mb, self.settings.agent_max_memory_mb, "agent_max_memory_mb"),
                ("cpus", limits.cpus, self.settings.agent_max_cpus, "agent_max_cpus"),
                ("disk_mb", limits.disk_mb, self.settings.agent_max_disk_mb, "agent_max_disk_mb"))
        for name, value, cap, setting in caps:
            if value is not None and value > cap:
                raise PolicyDenied(f"{name} {value:g} is over what this node lets an agent ask for ({cap:g}; "
                                   f"{setting} in env.toml on the node)")

    def create_remote(self, lane_id: str, remote: RemoteHost) -> EnvLaneInfo:
        """A VM lane (openajar.vms): a terminal lane whose commands run on `remote` over SSH. Only
        the VM manager makes these; agents ask for one with vm_create."""
        if self.store.has_lane(lane_id) or self._browser_lane_exists(lane_id):
            raise LaneExists(f"lane {lane_id} already exists")
        profile = IsolationProfile(filesystem="shared", network="shared", processes="shared")
        policy = Policy(shell=True, exec=True, install=True, seccomp=False, tier=0,
                        warning=None)
        lane = EnvLane(id=lane_id, kind=LaneKind.TERMINAL, preset=Preset.SANDBOX, profile=profile,
                       limits=Limits(), policy=policy, remote=remote)
        self.store.add_lane(lane)
        return self.info(lane_id)

    async def rehost(self, lane_id: str, remote: RemoteHost) -> None:
        """A VM lane's machine answers on another address or port now (openajar.vms started a stopped one
        again): the lane goes cold, and its next command reaches the machine there."""
        lane = self.store.get_lane(lane_id)
        if lane.remote is None:
            raise InvalidRequest(f"{lane_id} is not a VM lane")
        await self.cool(lane_id)
        await self.store.run(self.store.put_lane, lane.model_copy(update={"remote": remote}))

    def get(self, lane_id: str) -> EnvLane:
        return self.store.get_lane(lane_id)

    def has(self, lane_id: str) -> bool:
        return self.store.has_lane(lane_id)

    def state(self, lane_id: str) -> LaneState:
        st, _ = self.store.get_state(lane_id)
        if st == LaneState.QUARANTINED:
            return LaneState.QUARANTINED
        if lane_id in self._warming:
            return LaneState.WARMING
        rt = self._rts.get(lane_id)
        if rt is None or not rt.up_done:
            return LaneState.COLD
        sup = self.services.get(lane_id)
        if rt.busy() or (sup and sup.running) or any(s.lane_id == lane_id and s.running for s in self.shells.values()):
            return LaneState.ACTIVE
        return LaneState.WARM

    def info(self, lane_id: str) -> EnvLaneInfo:
        lane = self.store.get_lane(lane_id)
        _, reason = self.store.get_state(lane_id)
        rt = self._rts.get(lane_id)
        sup = self.services.get(lane_id)
        status = sup.status.model_copy() if sup else None
        if status is None and lane.service:
            desired, exp = self.store.service_desired(lane_id)
            status = ServiceStatus(lane=lane_id, desired=desired, state="stopped", expires_at=exp,
                                   log_path=str(self.paths.logs(lane_id) / "service.log"))
        if status is not None and status.state == "stopped" and not status.detail and lane.isolated \
                and not getattr(self.host, "can_isolate", True):
            status.detail = f"it cannot start on this node: {self._no_isolation()}"
        disk = self._disk(lane, rt)
        return EnvLaneInfo(
            lane=lane, state=self.state(lane_id), last_used=self.store.last_used(lane_id),
            quarantine_reason=reason, isolation=presets.describe(lane), disk=disk,
            shells=sum(1 for s in self.shells.values() if s.lane_id == lane_id),
            execs=sum(1 for w in (rt.workloads.values() if rt else []) if w.kind == "exec"),
            service=status, egress=rt.forwarder.stats.model_copy() if rt and rt.forwarder else None)

    def list(self) -> list[EnvLaneInfo]:
        return [self.info(lane.id) for lane in self.store.list_lanes()]

    def _disk(self, lane: EnvLane, rt: LaneRuntime | None) -> dict | None:
        cap = lane.limits.disk_mb if lane.profile.filesystem != "shared" else None
        if rt and rt.plan and rt.plan.disk:
            usage = self.host.disk_usage(rt.plan.disk.target)
            if usage:
                return {"used_mb": round(usage[0] / 1048576, 1), "cap_mb": round(usage[1] / 1048576, 1)}
        img = self.paths.upper_image(lane.id)
        if img.exists():
            return {"used_mb": round(img.stat().st_blocks * 512 / 1048576, 1), "cap_mb": cap}
        return {"used_mb": None, "cap_mb": cap} if cap else None

    def resolve_image(self, ref: str) -> ImageInfo:
        name, ver = images.parse_ref(ref)
        ver = ver or self.store.latest_version(name)
        info = self.store.get_image(name, ver) if ver else None
        if info is None:
            raise NotFound(f"base image {ref} is not built on this node; build it with `openajar env images build {name}`")
        return info

    async def warm(self, lane_id: str, *, use: bool = True) -> LaneRuntime:
        """The lane's runtime, up. `use=False`: the node's own command (a check of the machine), which is no
        use of the lane: its last use is left as it was (the idle rule reads it)."""
        await self.ensure_started()
        lane = self.store.get_lane(lane_id)
        st, reason = self.store.get_state(lane_id)
        if st == LaneState.QUARANTINED:
            raise LaneQuarantined(f"lane {lane_id} is quarantined: {reason}. Reset it with `openajar env reset {lane_id}`")
        async with self._lock_for(lane_id):
            rt = self._rts.get(lane_id)
            if rt is not None and rt.up_done:
                if use:
                    await self.store.run(self.store.touch, lane_id)  # done before the reaper can read it
                return rt
            self.check_runnable(lane)
            if lane.isolated and self._home_error:
                raise SandboxError(self._home_error)
            plan = None
            if lane.isolated:
                layers, env = [], {}
                if lane.profile.base_image:
                    info = self.resolve_image(lane.profile.base_image)
                    layers = [self.paths.image_dir(info.name, info.version) / "layer"]
                    env = dict(info.env)
                plan = plan_lane(lane, self.paths, self.settings, image_layers=layers, image_env=env)
            launcher = self.launcher
            if lane.remote is not None:  # a VM lane: every workload is one ssh to the VM
                if self.remote_launcher is None:
                    raise SandboxError(f"{lane_id} is a VM lane, and this process has no VMs (openajar.vms)")
                launcher = self.remote_launcher
            rt = LaneRuntime(lane, plan, paths=self.paths, settings=self.settings, host=self.host,
                             launcher=launcher, upstream=self._upstream(lane), on_egress=self.egress_log.append)
            self._warming.add(lane_id)
            try:
                await rt.up()
            except HostError as e:
                if "damaged" in e.message:
                    await self.store.run(self.quarantine, lane_id, e.message)
                raise SandboxError(f"could not warm {lane_id}: {e.message}") from e
            finally:
                self._warming.discard(lane_id)
            self._rts[lane_id] = rt
            await self.store.run(self._mark_warm, lane_id)
            return rt

    async def cool(self, lane_id: str) -> None:
        """Send a lane to cold: stop its service and shells, kill workloads, unmount."""
        sup = self.services.get(lane_id)
        if sup and sup.running:
            await sup.stop("lane going cold")
        for sid in [s.id for s in self.shells.values() if s.lane_id == lane_id]:
            await self.shell_close(sid)
        self.jobs.mark_stopping(lane_id, "the lane was stopped while it ran (cold, reset or removed)")
        async with self._lock_for(lane_id):
            rt = self._rts.pop(lane_id, None)
            if rt is not None:
                await rt.down()
            await self.jobs.wait_lane(lane_id)
            if self.store.has_lane(lane_id):
                st, _ = self.store.get_state(lane_id)
                if st != LaneState.QUARANTINED:
                    await self.store.run(self.store.set_state, lane_id, LaneState.COLD)

    async def remove(self, lane_id: str, *, vm_ok: bool = False) -> None:
        lane = self.store.get_lane(lane_id)
        if lane.remote is not None and not vm_ok:
            raise InvalidRequest(f"{lane_id} is a VM lane; delete it with vm_destroy (or `openajar vm destroy`), "
                                 "which deletes the machine too")
        await self.ensure_started()
        await self.cool(lane_id)
        self.services.pop(lane_id, None)
        self.jobs.forget_lane(lane_id)
        self._stores.pop(lane_id, None)
        d = self.paths.lane_dir(lane_id)
        if d.exists() and not (d / "profile").exists():
            shutil.rmtree(d, ignore_errors=True)
        await self.store.run(self.store.remove_lane, lane_id)

    def quarantine(self, lane_id: str, reason: str) -> None:
        """Blocking: on the event loop call it through `await self.store.run(self.quarantine, ...)`."""
        self.store.set_state(lane_id, LaneState.QUARANTINED, reason)

    def _mark_warm(self, lane_id: str) -> None:
        """WARM and touched, in one call on the store's worker thread (blocking)."""
        self.store.set_state(lane_id, LaneState.WARM)
        self.store.touch(lane_id)

    async def reset(self, lane_id: str) -> EnvLaneInfo:
        self.store.get_lane(lane_id)
        await self.cool(lane_id)
        await self.store.run(self.store.set_state, lane_id, LaneState.COLD, None)
        return self.info(lane_id)

    def _upstream(self, lane: EnvLane) -> Upstream | None:
        if not lane.exit_proxy:
            return None
        from openajar.core.config import load_proxies
        p = load_proxies(self._node_config()).get(lane.exit_proxy)
        if p is None:
            raise SandboxError(f"lane {lane.id} needs proxy {lane.exit_proxy!r}, which is not configured")
        return Upstream(p.host, p.port, p.username, p.password)

    # =============================================================== exec
    def command_argv(self, lane: EnvLane, cmd: str | list[str]) -> list[str]:
        if isinstance(cmd, list):
            argv = [str(c) for c in cmd]
        elif lane.policy.signed_only:
            try:
                argv = shlex.split(cmd)
            except ValueError as e:
                raise InvalidRequest(f"cannot parse the command: {e}") from e
        else:
            argv = ["/bin/sh", "-c", cmd]
        if not argv or not argv[0]:
            raise InvalidRequest("empty command")
        if lane.policy.signed_only:
            prog = argv[0]
            if not program_allowed(prog, lane.allowed_programs):
                allowed = ", ".join(lane.allowed_programs) or "none"
                raise PolicyDenied(f"locked lane {lane.id} runs allowlisted programs only ({allowed}); "
                                   f"{prog!r} is not one of them")
        return argv

    async def git_env(self, rt: LaneRuntime, grant_id: str | None) -> dict[str, str]:
        """GIT_CONFIG_* that point a command's git at the node's git gateway (openajar.git), for a
        command `grant_id` starts: {} without a grant, a declared remote or the git service."""
        git = self.runtime.get("git") if self.runtime is not None else None
        if git is None or not grant_id:
            return {}
        try:
            return await git.lane_env(rt, grant_id)
        except Exception:  # the command still runs; its git just reaches no declared remote
            log.exception("could not set up git for a command in %s", rt.lane.id)
            return {}

    def mirror_tap(self, lane_id: str, cmd: str, cwd: str | None) -> Any:
        """The exec_mirror hook's tap for one command, or None (no hook, or the hook failed)."""
        hook = self.exec_mirror
        if hook is None:
            return None
        try:
            return hook(lane_id, cmd, cwd)
        except Exception:  # best effort: the command runs as if nothing watched it
            log.debug("the exec mirror failed for %s", lane_id, exc_info=True)
            return None

    def _screen_env(self, lane_id: str) -> dict[str, str]:
        hook = self.screen_env
        if hook is None:
            return {}
        try:
            return dict(hook(lane_id) or {})
        except Exception:
            log.debug("the screen env hook failed for %s", lane_id, exc_info=True)
            return {}

    async def command_env(self, rt: LaneRuntime, grant_id: str | None) -> dict[str, str]:
        """What a command, a job or a shell started on the lane gets set: its screen (screen_env) and git's
        gateway (git_env), git's last."""
        return {**self._screen_env(rt.lane.id), **(await self.git_env(rt, grant_id))}

    async def exec(self, lane_id: str, cmd: str | list[str], *, timeout_s: float | None = None,
                   stdin: str | bytes | None = None, grant_id: str | None = None,
                   max_bytes: int | None = None, oom_check: bool = False, tap: Any = None,
                   stop: asyncio.Event | None = None, use: bool = True) -> ExecResult:
        """`max_bytes`: stdout and stderr together, at most (execs.fit); without it each stream is kept
        to the node's max_output_bytes. `oom_check`: see execs.run. `tap`: sees the output as it is read
        (mirror_tap); it changes nothing of the result. `stop`: set, it ends the command early (execs.run).
        `use=False`: the node's own command, which never counts as use of the lane (warm)."""
        lane = self.store.get_lane(lane_id)
        if not lane.policy.exec:
            raise PolicyDenied(f"commands are off in {lane.preset.value} lanes")
        argv = self.command_argv(lane, cmd)
        timeout = min(float(timeout_s or self.settings.default_timeout_s), self.settings.max_timeout_s)
        data = stdin.encode() if isinstance(stdin, str) else stdin
        rt = await self.warm(lane_id, use=use)
        env = await self.command_env(rt, grant_id)
        async with rt.exec_sem:
            res = await execs.run(rt, argv, timeout_s=timeout, stdin=data,
                                  max_bytes=max(self.settings.max_output_bytes, max_bytes or 0),
                                  env=env or None, budget=max_bytes, oom_check=oom_check, tap=tap, stop=stop)
        if use:
            await self.store.run(self.store.touch, lane_id)
        if lane.remote is not None and execs.ssh_lost(res.exit_code, res.stderr):
            why = await self.lost_reason(lane_id)
            if why:
                raise MachineGone(f"Nothing ran: {why}")
        return res

    async def lost_reason(self, lane_id: str) -> str | None:
        """The machine_lost hook's answer for a lane whose ssh failed: why its machine is gone, or None (no
        hook, the hook failed, or the machine still runs)."""
        hook = self.machine_lost
        if hook is None:
            return None
        try:
            return await hook(lane_id)
        except Exception:  # best effort: without an answer the call goes on as before
            log.debug("the machine_lost hook failed for %s", lane_id, exc_info=True)
            return None


    # =============================================================== shells
    async def shell_open(self, lane_id: str, *, cols: int = 120, rows: int = 32, term: str = "xterm-256color",
                         argv: list[str] | None = None, grant_id: str | None = None) -> ptys.ShellSession:
        lane = self.store.get_lane(lane_id)
        if not lane.policy.shell:
            raise PolicyDenied(f"interactive shells are off in {lane.preset.value} lanes")
        if sum(1 for s in self.shells.values() if s.lane_id == lane_id) >= self.settings.max_shells_per_lane:
            raise LaneBusy(f"lane {lane_id} already has {self.settings.max_shells_per_lane} shells; close one")
        if len(self.shells) >= self.settings.max_shells:
            raise LaneBusy("this node has too many open shells; close one")
        rt = await self.warm(lane_id)
        env = await self.command_env(rt, grant_id)
        s = await ptys.open_session(rt, cols=cols, rows=rows, term=term,
                                    buffer_bytes=self.settings.shell_buffer_bytes, argv=argv, env=env or None)
        self.shells[s.id] = s
        await self.store.run(self.store.touch, lane_id)
        return s

    def shell(self, sid: str) -> ptys.ShellSession:
        s = self.shells.get(sid)
        if s is None:
            raise NotFound(f"no shell session {sid} (it may have been closed after being idle)")
        return s

    async def shell_close(self, sid: str) -> None:
        s = self.shells.pop(sid, None)
        if s is not None:
            await s.close()

    # =============================================================== services
    def _supervisor(self, lane: EnvLane) -> Supervisor:
        sup = self.services.get(lane.id)
        if sup is None:
            assert lane.service is not None
            logfile = RotatingLog(self.paths.logs(lane.id) / "service.log", lane.service.log_max_bytes,
                                  lane.service.log_backups)
            sup = Supervisor(lane.id, lane.service, logfile, get_runtime=lambda: self.warm(lane.id),
                             run_check=self._check)
            self.services[lane.id] = sup
        return sup

    async def _check(self, rt: LaneRuntime, argv: list[str], timeout_s: float) -> bool:
        res = await execs.run(rt, argv, timeout_s=timeout_s, max_bytes=4096, kind="health")
        return res.exit_code == 0

    def _service_lane(self, lane_id: str) -> EnvLane:
        lane = self.store.get_lane(lane_id)
        if lane.kind != LaneKind.SERVICE or lane.service is None:
            raise InvalidRequest(f"{lane_id} is not a service lane")
        return lane

    async def service_start(self, lane_id: str, *, ttl_s: float | None = None) -> ServiceStatus:
        lane = self._service_lane(lane_id)
        self.check_runnable(lane)
        await self.ensure_started()
        sup = self._supervisor(lane)
        if not sup.running:
            used = sum((self.store.get_lane(i).limits.memory_mb or 0) for i, s in self.services.items()
                       if s.running and i != lane_id)
            need = lane.limits.memory_mb or 0
            if used + need > self.settings.service_budget_mb:
                raise LaneBusy(f"starting {lane_id} would exceed the node's service memory budget "
                               f"({used} + {need} > {self.settings.service_budget_mb} MB)")
        exp = expiry(ttl_s)
        saved = self.store.submit(self.store.set_service_desired, lane_id, "running", exp)
        sup.start(exp)  # in the same tick as the budget check: two starts cannot both pass it
        await saved
        await asyncio.sleep(0.05)
        return sup.status.model_copy()

    async def service_stop(self, lane_id: str) -> ServiceStatus:
        self._service_lane(lane_id)
        await self.store.run(self.store.set_service_desired, lane_id, "stopped")
        sup = self.services.get(lane_id)
        if sup is None:
            return self.service_status(lane_id)
        await sup.stop()
        return sup.status.model_copy()

    def service_status(self, lane_id: str) -> ServiceStatus:
        lane = self._service_lane(lane_id)
        info = self.info(lane.id)
        assert info.service is not None
        return info.service

    def service_logs(self, lane_id: str, lines: int = 100) -> str:
        lane = self._service_lane(lane_id)
        assert lane.service is not None
        logfile = self.services[lane_id].log if lane_id in self.services else RotatingLog(
            self.paths.logs(lane_id) / "service.log", lane.service.log_max_bytes, lane.service.log_backups)
        return logfile.tail(max(1, min(int(lines), 5000)))

    # =============================================================== installs
    async def install(self, lane_id: str, packages: list[str], source: str = "apt") -> dict:
        lane = self.store.get_lane(lane_id)
        pkgs = programs.validate(source, packages)
        if not lane.policy.install:
            raise PolicyDenied(f"lane {lane_id} runs a pinned program set (programs: base); "
                               "bake programs into its base image instead")
        if lane.policy.signed_only:
            if source != "apt":
                raise PolicyDenied("locked lanes install from the signed apt sources only")
            bad = [p for p in pkgs if programs.package_name("apt", p) not in self.settings.locked_apt_allowlist]
            if bad:
                raise PolicyDenied(f"not on the locked-lane apt allowlist: {', '.join(bad)}")
        hosts = programs.source_hosts(source, self.settings)
        argv, env = programs.install_argv(source, pkgs, self.settings)
        rt = await self.warm(lane_id)
        lock = self._install_locks.setdefault(lane_id, asyncio.Lock())
        async with lock:
            fwd = None
            if rt.netns is not None:
                fwd = Forwarder(f"{lane_id}#install", EgressPolicy(hosts, "deny"), on_event=self.egress_log.append)
                port = await rt.listen(fwd, self.settings.install_proxy_port)
                proxy = f"http://127.0.0.1:{port}"
                env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy})
            try:
                async with rt.exec_sem:
                    res = await execs.run(rt, argv, timeout_s=self.settings.install_timeout_s, env=env,
                                          install=lane.isolated, max_bytes=self.settings.max_output_bytes,
                                          kind="install")
                ok = res.exit_code == 0
                installed: dict[str, str] = {}
                if ok:
                    m = await execs.run(rt, programs.manifest_argv(source, pkgs), timeout_s=60, max_bytes=1 << 20)
                    installed = programs.parse_manifest(source, m.stdout, pkgs)
            finally:
                if fwd:
                    await fwd.close()

        def save() -> dict:  # one call on the worker: two installs never lose each other's entries
            self.store.record_install(lane_id, source, pkgs, ok, res.stderr[-1000:])
            manifest = self.store.manifest(lane_id)
            if ok:
                manifest.setdefault(source, {}).update(installed)
                self.store.set_manifest(lane_id, manifest)
            return manifest

        manifest = await self.store.run(save)
        import hashlib
        mhash = "sha256:" + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        return {"ok": ok, "lane": lane_id, "source": source, "packages": pkgs, "installed": installed,
                "exit_code": res.exit_code, "duration_s": res.duration_s, "manifest_hash": mhash,
                "output_tail": (res.stdout[-2000:] if ok else (res.stderr or res.stdout)[-3000:])}

    # =============================================================== images
    def templates(self) -> dict[str, ImageTemplate]:
        return images.load_templates(self.paths)

    def images(self) -> list[ImageInfo]:
        return self.store.list_images()

    async def build_image(self, name: str) -> ImageInfo:
        await self.ensure_started()
        t = self.templates().get(name)
        if t is None:
            raise NotFound(f"no image template {name!r}; templates: {', '.join(sorted(self.templates()))}")
        if t.placeholder:
            raise InvalidRequest(f"{name} is a placeholder template with nothing to build yet")
        if not self.host.can_isolate:
            raise SandboxError(getattr(self.host, "reason", "this node cannot build images"))
        if self._home_error:
            raise SandboxError(self._home_error)
        version = self.store.latest_version(name) + 1
        build = self.paths.images / f".build-{name}@{version}-{secrets.token_hex(3)}"
        layer, work = build / "layer", build / "work"
        layer.mkdir(parents=True)
        work.mkdir(parents=True)
        sources = [s for s in ("apt", "pip", "npm") if getattr(t, s)]
        hosts = sorted({h for s in sources for h in programs.source_hosts(s, self.settings)})
        profile, limits, policy = presets.resolve(Preset.SANDBOX, overrides={
            "network": "isolated" if hosts else "off", "filesystem": "overlay", "egress_allowlist": hosts})
        limits = Limits(memory_mb=2048, cpus=2.0, pids=4096)
        policy = Policy(shell=False, exec=True, install=True, egress_default="deny", tier=0)
        lane = EnvLane(id=f"build/{name}-{version}", preset=Preset.SANDBOX, profile=profile, limits=limits,
                       policy=policy)
        plan = plan_lane(lane, self.paths, self.settings, upper_root=layer, work_root=work)
        rt = LaneRuntime(lane, plan, paths=self.paths, settings=self.settings, host=self.host,
                         launcher=self.launcher, on_egress=self.egress_log.append)
        manifest: dict[str, dict[str, str]] = {}
        try:
            await rt.up()
            for source in sources:
                pkgs = programs.validate(source, getattr(t, source))
                argv, env = programs.install_argv(source, pkgs, self.settings)
                res = await execs.run(rt, argv, timeout_s=self.settings.install_timeout_s, env=env, install=True,
                                      max_bytes=self.settings.max_output_bytes, kind="install")
                if res.exit_code != 0:
                    raise SandboxError(f"building {name}@{version}: {source} install failed "
                                       f"({res.exit_code}): {(res.stderr or res.stdout)[-1500:]}")
                m = await execs.run(rt, programs.manifest_argv(source, pkgs), timeout_s=60, max_bytes=1 << 20)
                manifest[source] = programs.parse_manifest(source, m.stdout, pkgs)
            for path, content in sorted(t.files.items()):
                point, rel = images.point_for(path)
                target = plan.run_dir / "mnt" / point.flat / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        finally:
            await rt.down()
        try:
            await asyncio.to_thread(_clean_layer, layer)
            content_hash, size = await asyncio.to_thread(images.tree_hash, layer)
            info = ImageInfo(name=name, version=version, ref=f"{name}@{version}",
                             template_hash=images.template_hash(t), content_hash=content_hash,
                             base_fingerprint=images.base_fingerprint(), built_at=utcnow(), size_bytes=size,
                             env=dict(t.env), manifest=manifest)
            final = self.paths.image_dir(name, version)
            final.mkdir(parents=True)
            layer.rename(final / "layer")
            (final / "meta.json").write_text(info.model_dump_json(indent=2))
            await self.store.run(self.store.add_image, info)
            return info
        finally:
            shutil.rmtree(build, ignore_errors=True)

    # =============================================================== reaper
    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.reaper_interval_s)
            try:
                await self.reap_once()
            except Exception:
                log.exception("env reaper failed")

    async def reap_once(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        closed, cooled, expired = [], [], []
        for s in list(self.shells.values()):
            idle = (now - s.last_activity).total_seconds()
            if s.running and not s.subscribers and idle > self.settings.shell_idle_s or not s.running and s.exited_at and (now - s.exited_at).total_seconds() > self.settings.shell_exited_keep_s:
                closed.append(s.id)
        for sid in closed:
            await self.shell_close(sid)
        for lane_id, sup in list(self.services.items()):
            if sup.running and sup.expired():
                await self.store.run(self.store.set_service_desired, lane_id, "stopped")
                await sup.stop("grant time ran out")
                expired.append(lane_id)
        for lane_id in list(self._rts):
            if self.state(lane_id) != LaneState.WARM:
                continue
            last = self.store.last_used(lane_id)
            if last is None or (now - last).total_seconds() > self.settings.lane_idle_s:
                await self.cool(lane_id)
                cooled.append(lane_id)
        return {"shells_closed": closed, "lanes_cooled": cooled, "services_expired": expired}

    def node_report(self) -> dict:
        return {"host": self.host.report(), "isolation": self.host.can_isolate and not self._home_error,
                "home_error": self._home_error, "last_sweep": self.last_sweep, "warm": sorted(self._rts),
                "shells": len(self.shells), "seccomp": sc.supported()}


BIN_DIRS = ("/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin", "/bin", "/sbin")


def program_allowed(prog: str, allowed: list[str]) -> bool:
    """Locked lanes: `python3` allows `python3` and `/usr/bin/python3`; `/opt/x/run` allows only that path."""
    for entry in allowed:
        if prog == entry:
            return True
        if "/" not in entry and "/" in prog and os.path.dirname(prog) in BIN_DIRS and os.path.basename(prog) == entry:
            return True
    return False


def _clean_layer(layer: Path) -> None:
    """Drop download caches from a freshly built layer (regular files only, never whiteouts)."""
    for rel in ("var/cache/apt", "var-lib-apt/lists", "root/.cache", "root/.npm", "var/log"):
        d = layer / rel
        if not d.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(d):
            for f in files:
                p = Path(dirpath) / f
                if p.is_file() and not p.is_symlink():
                    p.unlink()
