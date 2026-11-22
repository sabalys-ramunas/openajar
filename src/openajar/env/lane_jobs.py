"""Background jobs in a lane: start one, read its output as it runs, cancel it, list them. A mixin
of EnvManager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from openajar.core.errors import (
    InvalidRequest,
    LaneBusy,
    PolicyDenied,
)
from openajar.env import execs
from openajar.env import sync as syncs
from openajar.env.jobs import OOM_EVERY_S, Job, _read_at, new_job_id
from openajar.env.models import (
    EnvLane,
    JobInfo,
)

log = logging.getLogger("openajar.env")
REATTACH_LAUNCHER_TRIES = 20  # half seconds a restarted node waits for its ssh launcher before it reattaches
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")  # a job's environment variable names

# terminal_job_read(until_port=): one command in the job's lane that returns once something listens on the
# port there (a LISTEN socket, state 0A, in /proc/net/tcp or tcp6: no ss or netstat needed, and a
# server on 0.0.0.0, 127.0.0.1 or ::1 counts), or at its own deadline, so it never outlives the wait.
PORT_WAIT = """p=$(printf '%04X' {port}); end=$(($(date +%s) + {secs}))
while :; do
  if grep -qE "^ *[0-9]+: [0-9A-F]+:$p [0-9A-F]+:[0-9A-F]+ 0A " /proc/net/tcp /proc/net/tcp6 2>/dev/null; then
    echo listening; exit 0
  fi
  [ "$(date +%s)" -ge "$end" ] && {{ echo waited; exit 0; }}
  sleep 0.5
done
"""


def port_wait_script(port: int, secs: int) -> str:
    return PORT_WAIT.format(port=int(port), secs=max(1, int(secs)))


class Jobs:
    """EnvManager's job commands (they read self.jobs, self.store and self.warm)."""

    # =============================================================== jobs
    def _job_argv(self, lane: EnvLane, cmd: str, cwd: str | None) -> list[str]:
        if not lane.policy.exec:
            raise PolicyDenied(f"commands are off in {lane.preset.value} terminals")
        if not isinstance(cmd, str) or not cmd.strip():
            raise InvalidRequest("empty command")
        if lane.policy.signed_only:
            if cwd is not None:
                raise InvalidRequest(f"locked terminal {lane.id} runs allowlisted programs only, without a cwd")
            return self.command_argv(lane, cmd)
        # every job is an agent's or a person's command (environment.toml's start, a resumed job): under bash
        # when the machine has it (execs.in_bash)
        if cwd is not None:
            cwd = syncs.check_cwd(cwd, remote=lane.remote is not None)
            # plain names (check_cwd), so it is safe as one shell word
            return self.command_argv(lane, execs.in_bash(f"cd '{cwd}' && {cmd}"))
        return self.command_argv(lane, execs.in_bash(cmd))

    def check_job(self, lane_id: str, cmd: str, *, cwd: str | None = None, env: dict[str, str] | None = None,
                  timeout_s: float | None = None) -> float:
        """What job_start would refuse, before anyone is asked to approve it. Returns the timeout."""
        lane = self.store.get_lane(lane_id)
        self._job_argv(lane, cmd, cwd)
        for k, v in (env or {}).items():
            if not isinstance(k, str) or not _ENV_NAME_RE.match(k) or not isinstance(v, str) or "\x00" in v \
                    or len(v) > 4096:
                raise InvalidRequest(f"env: {str(k)[:64]!r} is not a variable name with a short text value")
        if len(env or {}) > 32:
            raise InvalidRequest("env takes at most 32 variables")
        self.jobs.check_room(lane_id)
        return min(float(timeout_s or self.settings.max_timeout_s), self.settings.max_timeout_s)

    async def job_start(self, lane_id: str, cmd: str, *, cwd: str | None = None, env: dict[str, str] | None = None,
                        timeout_s: float | None = None, sync: dict | None = None, by: str | None = None,
                        grant_id: str | None = None, source: dict | None = None) -> JobInfo:
        """Start `cmd` in the background (after applying `sync`, the output of sync_prepare). Its output
        is logged on the node; read it with job_read, from any offset, while it runs and after."""
        timeout = self.check_job(lane_id, cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        lane = self.store.get_lane(lane_id)
        argv = self._job_argv(lane, cmd, cwd)
        applied = None
        if sync is not None:
            async with self._sync_lock(lane_id):
                busy = [j for j in self.jobs.running_in(lane_id) if (j.info.sync or {}).get("dest") == sync["dest"]]
                if busy:
                    raise LaneBusy(f"job {busy[0].info.id} still runs in {sync['dest']} of {lane_id}; a sync now "
                                   "would change its files under it. Wait for it, or cancel it with terminal_job_cancel")
                applied = await self.sync_apply(lane_id, sync)
                self.jobs.check_room(lane_id)
                rt = await self.warm(lane_id)
                info = JobInfo(id=new_job_id(), lane=lane_id, cmd=cmd, cwd=cwd, timeout_s=timeout, sync=applied,
                               by=by, source=source)
                job_env = {**(await self.command_env(rt, grant_id)), **(env or {})}
                job = await self.jobs.start(rt, argv, info=info, env=job_env or None, on_end=self._job_ended)
        else:
            rt = await self.warm(lane_id)
            self.jobs.check_room(lane_id)
            info = JobInfo(id=new_job_id(), lane=lane_id, cmd=cmd, cwd=cwd, timeout_s=timeout, by=by, source=source)
            job_env = {**(await self.command_env(rt, grant_id)), **(env or {})}
            job = await self.jobs.start(rt, argv, info=info, env=job_env or None, on_end=self._job_ended)
        await self.store.run(self.store.touch, lane_id)
        await asyncio.to_thread(self.jobs.prune, lane_id)
        return job.info.model_copy()

    async def _reattach_jobs(self) -> None:
        """After a restart: follow again the jobs that ran on on their VMs (openajar.env.jobs). The VM
        manager hands this manager its ssh launcher just after the start, so that is waited for a moment."""
        for _ in range(REATTACH_LAUNCHER_TRIES):
            if self.remote_launcher is not None:
                break
            await asyncio.sleep(0.5)
        for job in list(self.jobs.pending):
            if not job.running or not job.pending:
                continue
            lane_id = job.info.lane
            try:
                rt = await self.warm(lane_id)
            except Exception as e:  # noqa: BLE001 (the lane is gone, or this process has no VMs)
                self.jobs.give_up(job, f"the node restarted while it ran and could not reach {lane_id} again: "
                                       f"{getattr(e, 'message', e)}")
                continue
            if not rt.detaches_jobs:
                self.jobs.give_up(job, "the node restarted while it ran, and this process cannot follow jobs on VMs")
                continue
            self.jobs.attach(rt, job, on_end=self._job_ended)
            log.info("job %s on %s: following it again after the restart", job.info.id, lane_id)

    async def _job_ended(self, job: Job) -> None:
        if self.store.has_lane(job.info.lane):
            await self.store.run(self.store.touch, job.info.lane)  # the idle timer starts when the job ends

    def job(self, job_id: str) -> Job:
        return self.jobs.get(job_id)

    async def job_read(self, job_id: str, *, stdout_offset: int = 0, stderr_offset: int = 0, wait_s: float = 0,
                       max_bytes: int = 65536, until_exit: bool = False) -> tuple[JobInfo, bytes, bytes]:
        job = self.jobs.get(job_id)
        out, err = await self.jobs.read(job, stdout_offset, stderr_offset, wait_s=wait_s, max_bytes=max_bytes,
                                        until_exit=until_exit)
        info = job.info.model_copy()
        if job.running:  # what the command wrote so far (the meta has it once it ended)
            info.stdout_bytes = job.out.total if job.out else info.stdout_bytes
            info.stderr_bytes = job.err.total if job.err else info.stderr_bytes
        return info, out, err

    async def job_log(self, job_id: str, stream: str, offset: int, n: int) -> bytes:
        """`n` bytes of a job's stdout or stderr log from `offset`, as kept on the node, without waiting."""
        return await asyncio.to_thread(_read_at, self.jobs.get(job_id).log_path(stream), max(0, offset), n)

    async def job_wait_port(self, job_id: str, port: int, wait_s: float) -> bool:
        """Wait up to `wait_s` for `port` to listen in the job's lane (port_wait_script, one command
        there), for the job to end, or for the kernel to kill one of its processes for memory.
        True when the port listens."""
        job = self.jobs.get(job_id)
        if not job.running:
            return False
        secs = max(1, int(wait_s))
        kills = len(job.info.oom or [])
        watcher = asyncio.create_task(self.exec(job.info.lane, port_wait_script(port, secs), timeout_s=secs + 15))

        async def over() -> None:
            async with job.changed:
                await job.changed.wait_for(lambda: not job.running or len(job.info.oom or []) > kills)

        ended = asyncio.create_task(over())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + secs + 5
        try:
            while not (watcher.done() or ended.done()) and (left := deadline - loop.time()) > 0:
                self.jobs.probe_oom(job)
                await asyncio.wait({watcher, ended}, timeout=min(left, OOM_EVERY_S),
                                   return_when=asyncio.FIRST_COMPLETED)
            if watcher.done():
                return "listening" in (watcher.result().stdout or "")
            return False
        finally:
            for t in (watcher, ended):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(BaseException):
                        await t

    async def job_cancel(self, job_id: str) -> JobInfo:
        job = self.jobs.get(job_id)
        await self.jobs.cancel(job)
        return job.info.model_copy()

    def job_list(self, lane_id: str | None = None) -> list[JobInfo]:
        return [i.model_copy() for i in self.jobs.infos(lane_id)]
