"""Commands in a VM lane: every exec, shell and service is one `ssh` from the node to the VM.

The VM holds nothing of openajar: no node token, no vault, no agentosd (an environment's VM holds
one gateway token for its repository, openajar.environments). A workload's environment goes on the
ssh command line, which `ps` on the node shows: never put a secret there. The node keeps the VM's private
key (one per VM) and the VM's host key, which openajar made itself and handed to the VM through
cloud-init, so the very first connection is already pinned (StrictHostKeyChecking=yes against a
known_hosts file of one line, under a HostKeyAlias that does not depend on the IP).
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
from pathlib import Path

from openajar.env import oom
from openajar.env.models import EnvLane, RemoteHost, RemoteVia
from openajar.env.sandbox import Command, LanePlan, Workload

SSH = "ssh"
CONNECT_TIMEOUT_S = 10
JOB_STATUS_TIMEOUT_S = 20.0


def via_command(via: RemoteVia) -> str:
    """The ProxyCommand of a machine reached through another SSH host: an ssh to that host, as strict as
    ssh_base (its own key, its pinned host key, no config file, no agent, no password), that only carries
    the connection (`-W`). ssh runs it with /bin/sh, so every word is quoted; a `%` of a path is doubled,
    since ssh expands `%h` and `%p` in it."""
    argv = [
        SSH, "-F", "/dev/null",
        "-i", via.key_path,
        "-p", str(via.port),
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={via.known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"HostKeyAlias={via.host_alias}",
        "-o", "UpdateHostKeys=no",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "LogLevel=ERROR",
    ]
    target = shlex.quote(f"{via.user}@{via.host}")
    return f"{shlex.join(argv)} -W %h:%p {target}".replace("%", "%%").replace("%%h:%%p", "%h:%p")


def ssh_base(remote: RemoteHost, *, tty: bool = False, quiet: bool = False) -> list[str]:
    """The ssh argv up to (not including) the remote command. No config file, no agent, no
    password, no forwarding of anything. `quiet`: ssh says nothing of its own on stderr (a job's
    follower, whose stderr is the job's: a word of ssh's there would shift the offsets). A machine
    reached through another SSH host (`remote.via`) gets that hop as its ProxyCommand."""
    hop = ["-o", f"ProxyCommand={via_command(remote.via)}"] if remote.via is not None else []
    return [
        SSH, "-F", "/dev/null",
        "-i", remote.key_path,
        "-p", str(remote.port),
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={remote.known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"HostKeyAlias={remote.host_alias}",
        "-o", "UpdateHostKeys=no",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", f"LogLevel={'QUIET' if quiet else 'ERROR'}",
        *hop,
        "-tt" if tty else "-T",
        f"{remote.user}@{remote.host}",
    ]


# Where a workload on the VM writes its process group id, so the node can end every process it started
# (terminal_job_cancel left a dev server's vite running: killing the local ssh never reached the VM's processes).
# `kill -s SIG -- -PGID`, not `kill -SIG -- -PGID`: Ubuntu's /bin/sh is dash, whose kill refuses the latter
# ("Illegal number: -") and signals nothing, so the group lived on (checked in ubuntu:24.04, 2026-11-16).
PGID_DIR = "/tmp/agentos-wl"
_WID_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def pgid_file(wid: str) -> str:
    return f"{PGID_DIR}/{wid}.pgid"


def remote_command(wl: Workload) -> str:
    """One string for the remote shell: the working directory, the workload's environment and
    its argv, each quoted. sshd runs it with the user's login shell. A workload without a terminal
    runs in a process group of its own (setsid -w, which still waits for it and passes its exit
    status), whose id it writes to pgid_file(wid) for stop_argv, and what it leaves in the background
    ends with it; one with a terminal ends with its terminal."""
    cwd = wl.cwd or "~"
    cd = "cd" if cwd == "~" else f"cd {shlex.quote(cwd)}"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(wl.env.items()) if _env_name_ok(k))
    run = shlex.join(wl.argv)
    if env:
        run = f"env {env} {run}"
    if wl.pty or not wl.wid or set(wl.wid) - _WID_OK:
        return f"{cd} && exec {run}"
    f = pgid_file(wl.wid)
    # after the command, what it left in the background ends too (the shell ignores the TERM it sends)
    record = keep = ""
    if wl.oom_record:  # the start record (openajar.env.oom) stays after a failure or an oom kill, for the node
        s = oom.start_file(PGID_DIR, wl.wid)
        record, keep = f"{oom.record(s)}; ", oom.keep(s)
    group = shlex.quote(f'echo $$ > {f}; {record}"$@"; rc=$?; trap "" TERM; kill -s TERM -- -$$ 2>/dev/null; '
                        f'rm -f {f}; {keep}exit $rc')
    return f"mkdir -p {PGID_DIR} && {cd} && exec setsid -w sh -c {group} sh {run}"


# A job (terminal_job_start) on a VM does not live in the node's ssh connection (field report 2026-11-16: the
# service restarted for an upgrade, its ssh sessions logged out, and every dev server on the VM died with
# them). It runs detached: a session of its own (setsid), its stdout and stderr in files beside its pgid
# file, so a restart, an upgrade or a dropped connection of the node leaves it running. The node follows
# those files from the offsets it already has (`follow_script`, one ssh it may drop and open again) and asks
# how the job stands after each follower ends (`status_script`); once it has read the end, `forget_script`
# removes the files. setsid and not a transient systemd unit (systemd-run): the docker provider's machines
# have no systemd, and a job keeps the environment of the ssh login it was started from (PATH, HOME) and
# its session scope, which the kernel names when it kills for memory (openajar.env.oom reads that cgroup).
# The node ends a job (cancel, timeout, close) as before, by its pgid file (stop_script).
JOB_SUFFIXES = (".out", ".err", ".rc", ".timeout", ".pgid", ".start")
JOB_WATCHDOG_S = 60       # past its timeout, the job's own shell ends it when no node did (the node was gone)
JOB_STALE_MINUTES = 2880  # a job's files nobody forgot (the node never came back): removed after two days
FOLLOW_WAIT_S = 10        # a follower waits this long for a job just started to write its pgid file


def job_file(wid: str, suffix: str) -> str:
    return f"{PGID_DIR}/{wid}{suffix}"


def detach_ok(wl: Workload) -> bool:
    return bool(wl.wid) and not wl.pty and not set(wl.wid) - _WID_OK


def start_script(wl: Workload, timeout_s: float | None) -> str:
    """Start the job detached (unless its files are there already): a new session whose shell writes its
    pgid (and the start record, openajar.env.oom), runs the command with its output in the job's files,
    writes `<exit status> <epoch>` to the rc file, and ends what the command left behind. A watchdog in the
    same group ends the job JOB_WATCHDOG_S past its timeout, when no node did."""
    cwd = wl.cwd or "~"
    cd = "cd" if cwd == "~" else f"cd {shlex.quote(cwd)}"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(wl.env.items()) if _env_name_ok(k))
    run = shlex.join(wl.argv)
    if env:
        run = f"env {env} {run}"
    p, rc, to = job_file(wl.wid, ".pgid"), job_file(wl.wid, ".rc"), job_file(wl.wid, ".timeout")
    out, err = shlex.quote(job_file(wl.wid, ".out")), shlex.quote(job_file(wl.wid, ".err"))
    record = keep = ""
    if wl.oom_record:
        s = oom.start_file(PGID_DIR, wl.wid)
        record, keep = f"{oom.record(s)}; ", oom.keep(s)
    watchdog = ""
    if timeout_s:
        secs = int(timeout_s) + JOB_WATCHDOG_S
        watchdog = (f'( sleep {secs}; trap "" TERM; : > {to}; kill -s TERM -- -$$ 2>/dev/null; sleep 5; '
                    f'kill -s KILL -- -$$ 2>/dev/null ) & ')
    group = shlex.quote(f'echo $$ > {p}; {record}{watchdog}"$@"; rc=$?; echo "$rc $(date +%s)" > {rc}.n; '
                        f'mv -f {rc}.n {rc}; trap "" TERM; kill -s TERM -- -$$ 2>/dev/null; rm -f {p}; '
                        f'{keep}exit $rc')
    return (f"mkdir -p {PGID_DIR} && {cd} && {{ [ -e {out} ] || {{ : > {err}; : > {out}; "
            f"setsid sh -c {group} sh {run} < /dev/null >> {out} 2>> {err} & }}; }}")


_ALIVE = ('a=0; [ ! -e "$w.rc" ] && p=$(cat "$w.pgid" 2>/dev/null) && [ -n "$p" ] && '
          'kill -s 0 -- "-$p" 2>/dev/null && a=1')


def follow_script(wid: str, out_offset: int, err_offset: int) -> str:
    """Print the job's stdout (to stdout) and stderr (to stderr) from the given offsets as they grow, and
    end once the job has ended and all of it is sent, or when the ssh that runs it is gone. The job runs
    while its rc file is not written and a process of its group lives."""
    d, w = shlex.quote(PGID_DIR), shlex.quote(wid)
    o, e = max(0, int(out_offset)), max(0, int(err_offset))
    return f"""cd {d} 2>/dev/null || exit 0
w={w}; o={o}; e={e}; i=0; pp=$PPID; k=0
while [ ! -e "$w.pgid" ] && [ ! -e "$w.rc" ] && [ -e "$w.out" ] && [ $i -lt {FOLLOW_WAIT_S * 10} ]; do sleep 0.1; i=$((i+1)); done
while :; do
  {_ALIVE}
  n=$(wc -c < "$w.out" 2>/dev/null); n=$((n+0)); m=$(wc -c < "$w.err" 2>/dev/null); m=$((m+0)); new=0
  if [ $n -gt $o ]; then tail -c +$((o+1)) "$w.out" | head -c $((n-o)) || exit 0; o=$n; new=1; fi
  if [ $m -gt $e ]; then tail -c +$((e+1)) "$w.err" | head -c $((m-e)) >&2 || exit 0; e=$m; new=1; fi
  [ $a = 1 ] || [ $new = 1 ] || exit 0
  kill -s 0 $pp 2>/dev/null || exit 0
  if [ $new = 1 ]; then k=0; else k=$((k+1)); fi
  if [ $k -lt 5 ]; then sleep 0.2; elif [ $k -lt 20 ]; then sleep 0.5; else sleep 1; fi
done
"""


def status_script(wid: str) -> str:
    """How the job stands: `G` when its files are gone (it never started, or the machine restarted),
    else `A 1` while it runs (`A 0` once not), `R <exit status> <epoch>` once it exited, `T` when its
    own watchdog ended it, and `S <stdout bytes> <stderr bytes>`."""
    d, w = shlex.quote(PGID_DIR), shlex.quote(wid)
    return f"""cd {d} 2>/dev/null || {{ echo G; exit 0; }}
w={w}
[ -e "$w.out" ] || {{ echo G; exit 0; }}
{_ALIVE}
echo "A $a"
[ -e "$w.rc" ] && echo "R $(cat "$w.rc")"
[ -e "$w.timeout" ] && echo T
echo "S $(wc -c < "$w.out") $(wc -c < "$w.err" 2>/dev/null || echo 0)"
true
"""


def forget_script(wid: str) -> str:
    """Remove a job's files once the node has read its end, and any job's files left past
    JOB_STALE_MINUTES (a node that never came back)."""
    d = shlex.quote(PGID_DIR)
    files = " ".join(shlex.quote(wid + s) for s in JOB_SUFFIXES)
    return (f"cd {d} 2>/dev/null || exit 0; rm -f {files} {shlex.quote(wid + '.rc.n')}; "
            f"find . -maxdepth 1 -type f -name 'j-*' -mmin +{JOB_STALE_MINUTES} -delete 2>/dev/null; true\n")


def parse_status(text: str) -> dict | None:
    """status_script's lines as {gone, alive, rc, ended_at (epoch), timed_out, out, err}; None when
    the output is not one (the machine did not answer)."""
    st: dict = {"gone": False, "alive": False, "rc": None, "ended_at": None, "timed_out": False, "out": 0, "err": 0}
    seen = False
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "G":
            return {**st, "gone": True}
        if parts[0] == "A" and len(parts) > 1:
            st["alive"], seen = parts[1] == "1", True
        elif parts[0] == "R" and len(parts) > 1:
            with contextlib.suppress(ValueError):
                st["rc"] = int(parts[1])
                st["ended_at"] = float(parts[2]) if len(parts) > 2 else None
        elif parts[0] == "T":
            st["timed_out"] = True
        elif parts[0] == "S" and len(parts) > 2:
            with contextlib.suppress(ValueError):
                st["out"], st["err"] = int(parts[1]), int(parts[2])
    return st if seen else None


def stop_script(wid: str, sig: str) -> str:
    """Signal every process of the workload's group on the VM; the file goes once it is killed."""
    f = shlex.quote(pgid_file(wid))
    tail = f"; rm -f {f}" if sig == "KILL" else ""
    return f'p=$(cat {f} 2>/dev/null) && [ -n "$p" ] && kill -s {sig} -- "-$p" 2>/dev/null{tail}; true'


def _env_name_ok(name: str) -> bool:
    return bool(name) and (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name)


class SshLauncher:
    """The launcher of a VM lane (the env manager picks it for lanes with `remote`)."""

    detaches_jobs = True  # a job runs detached on the VM and outlives the node's ssh (openajar.env.jobs)

    def transport(self, lane: EnvLane, script: str, *, tty: bool = False, quiet: bool = False) -> list[str]:
        """The argv that runs `script` on the lane's VM (one ssh)."""
        assert lane.remote is not None, "SshLauncher serves VM lanes only"
        return ssh_base(lane.remote, tty=tty, quiet=quiet) + [script]

    async def run_script(self, lane: EnvLane, script: str, *, timeout_s: float) -> tuple[int, str]:
        assert lane.remote is not None, "SshLauncher serves VM lanes only"
        return await run(lane.remote, script, timeout_s=timeout_s)

    def command(self, plan: LanePlan | None, lane: EnvLane, wl: Workload, *, cgroup: str | None,
                workdir: Path) -> Command:
        from openajar.env.sandbox import cgroup_wrap
        quiet = wl.detach is not None and detach_ok(wl)
        if quiet:  # a job: start it detached (once), then follow its files
            out_off, err_off = wl.detach or (0, 0)
            script = follow_script(wl.wid, out_off, err_off)
            if wl.detach_start:
                script = f"{start_script(wl, wl.detach_timeout_s)} && {{\n{script}}}"
        else:
            script = remote_command(wl)
        argv = (cgroup_wrap(cgroup) if cgroup else []) + self.transport(lane, script, tty=wl.pty, quiet=quiet)
        # the local ssh client gets a plain environment: nothing of agentosd's leaks to it
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(workdir), "LANG": "C.UTF-8"}
        workdir.mkdir(parents=True, exist_ok=True)
        return Command(argv, env, str(workdir))

    def stop_argv(self, lane: EnvLane, wid: str, sig: str) -> list[str] | None:
        """The ssh command that sends `sig` (TERM or KILL) to a workload's process group on the VM
        (openajar.env.lanes calls it before it kills the local ssh). None for an id it never wrote."""
        if lane.remote is None or not wid or set(wid) - _WID_OK:
            return None
        return self.transport(lane, stop_script(wid, sig))

    async def job_status(self, lane: EnvLane, wid: str) -> dict | None:
        """How a detached job stands on the VM (status_script); None when the VM did not answer."""
        if lane.remote is None or not wid or set(wid) - _WID_OK:
            return None
        code, out = await self.run_script(lane, status_script(wid), timeout_s=JOB_STATUS_TIMEOUT_S)
        return parse_status(out) if code == 0 else None

    async def job_forget(self, lane: EnvLane, wid: str) -> None:
        if lane.remote is None or not wid or set(wid) - _WID_OK:
            return
        await self.run_script(lane, forget_script(wid), timeout_s=JOB_STATUS_TIMEOUT_S)

    async def oom_kills(self, lane: EnvLane, wid: str, *, elapsed_s: float, final: bool = False) -> list[dict] | None:
        """The processes the VM's kernel killed for memory since the workload started (openajar.env.oom);
        None when the VM could not be asked. `final` removes the workload's start record."""
        if lane.remote is None or not wid or set(wid) - _WID_OK:
            return None
        code, out = await self.run_script(lane, oom.probe_script(PGID_DIR, wid, final=final),
                                          timeout_s=oom.PROBE_TIMEOUT_S)
        if code != 0:
            return None
        return oom.parse(out, elapsed_s=elapsed_s)


async def probe(remote: RemoteHost, *, timeout_s: float = 20.0) -> tuple[bool, str]:
    """Run `true` on the VM. (True, "") when SSH answers with the pinned host key."""
    argv = ssh_base(remote) + ["true"]
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return False, f"cannot run ssh: {e}"
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "ssh timed out"
    if proc.returncode == 0:
        return True, ""
    return False, (err or b"").decode(errors="replace").strip()[-300:] or f"ssh exited {proc.returncode}"


async def run(remote: RemoteHost, script: str, *, timeout_s: float = 900.0) -> tuple[int, str]:
    """Run a shell script on the VM (image builds). Returns (exit code, the last 4000 characters
    of its output)."""
    argv = ssh_base(remote) + ["bash -s"]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out"
    except BaseException:  # the caller gave up (a probe beside a read): no ssh is left behind
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return proc.returncode or 0, (out or b"").decode(errors="replace")[-4000:]


async def push_tree(remote: RemoteHost, src: Path, dest: str, *, timeout_s: float = 600.0) -> None:
    """Copy the directory `src` (on the node) to `dest` (an absolute path on the VM) as one tar
    stream over SSH. Refuses a dest that exists and is not empty."""
    import os

    q = shlex.quote(dest)
    script = (f"if [ -e {q} ] && [ -n \"$(ls -A {q} 2>/dev/null)\" ]; then echo '{dest} exists and is not empty' >&2; "
              f"exit 17; fi; mkdir -p {q} && tar -x --no-same-owner -f - -C {q}")
    r, w = os.pipe()
    try:
        tar = await asyncio.create_subprocess_exec("tar", "-C", str(src), "-cf", "-", ".", stdout=w,
                                                   stderr=asyncio.subprocess.PIPE,
                                                   env={"PATH": "/usr/bin:/bin", "COPYFILE_DISABLE": "1"})
    finally:
        os.close(w)
    try:
        sh = await asyncio.create_subprocess_exec(*ssh_base(remote), script, stdin=r,
                                                  stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except BaseException:
        tar.kill()
        await tar.wait()
        raise
    finally:
        os.close(r)
    try:
        (_, terr), (_, serr) = await asyncio.wait_for(asyncio.gather(tar.communicate(), sh.communicate()),
                                                      timeout=timeout_s)
    except TimeoutError:
        for p in (tar, sh):
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            await p.wait()
        from openajar.vms.models import VmError
        raise VmError(f"copying to the VM took longer than {int(timeout_s)} s") from None
    from openajar.vms.models import VmError
    if sh.returncode == 17:
        raise VmError(f"{dest} already exists on the VM and is not empty; pick another dest")
    if sh.returncode != 0:
        raise VmError(f"copying to the VM failed: {(serr or b'').decode(errors='replace').strip()[-300:] or sh.returncode}")
    if tar.returncode != 0:
        raise VmError(f"packing the checkout failed: {(terr or b'').decode(errors='replace').strip()[-300:]}")
