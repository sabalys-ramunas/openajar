"""Field report 2026-11-21 (run 2): a wait on a port hid a crashed child, reading the end of a log took
offset arithmetic, dev servers' colour codes filled the output, and terminal_exec ran dash, which fails
on bash syntax (`${v:0:15}`: "Bad substitution").

A crashed child is played by a job that prints an error and sleeps on (as turbo did after its API child
died on env validation); the port wait by a script that only waits (lane_jobs.port_wait_script)."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess

import pytest
from test_env_jobs import Ctx, tools
from test_vm_oom import KERNEL

from openajar.core.runtime import Runtime
from openajar.env import execs, lane_jobs, oom
from openajar.env.host import FakeHost
from openajar.env.jobs import Job
from openajar.env.lanes import LaneRuntime
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher


@pytest.fixture
async def env(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0, max_timeout_s=30))
    mgr.create("dev/run", preset="sandbox", overrides={"filesystem": "overlay"})
    yield Runtime(envs=mgr)
    await mgr.shutdown()


@pytest.fixture
def no_port(monkeypatch):
    monkeypatch.setattr(lane_jobs, "port_wait_script", lambda port, secs: f"sleep {secs}; echo waited")


async def until_printed(t, ctx, job, text, stream="stderr"):
    for _ in range(100):
        r = await t["terminal_job_read"](ctx, job=job, wait_s=0.1, max_bytes=1 << 20)
        if text in r[stream]:
            return r
    raise AssertionError(f"{text!r} never came")


# ------------------------------------------------------------------ the port wait that runs out


async def test_a_port_wait_that_runs_out_gives_the_end_of_a_running_jobs_output(env, no_port):
    t, ctx = tools(env), Ctx(env)
    cmd = "seq 1 5000; echo 'Error: API_URL_WH_OVERRIDE is not a valid URL' >&2; echo 'api#dev: exit 1'; sleep 20"
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd=cmd)
    await until_printed(t, ctx, job["id"], "not a valid URL")
    r = await t["terminal_job_read"](ctx, job=job["id"], until_port=3000, wait_s=1, max_bytes=400)
    assert r["listening"] is False and r["state"] == "running"
    out = r["stdout"]
    assert out.startswith("1\n2\n") and out.endswith("5000\napi#dev: exit 1\n")   # the start and the end
    assert "bytes skipped here" in out and "Error: API_URL_WH_OVERRIDE" in r["stderr"]
    assert r["next_stdout_offset"] == r["stdout_bytes"]          # the next read goes on from the end
    assert "There was more stdout since your offset than max_bytes" in r["note"]
    assert re.search(r"Port 3000 did not listen within [12] s, and the job still runs", r["note"])
    assert "may have crashed" in r["note"] and "terminal_job_cancel" in r["note"]
    # the marker's offset reads what was skipped, and the next read neither skips nor repeats
    skip = int(out.split("stdout_offset=")[1].split("]")[0])
    gap = await t["terminal_job_read"](ctx, job=job["id"], stdout_offset=skip, wait_s=0, max_bytes=1 << 20)
    whole = "".join(f"{i}\n" for i in range(1, 5001)) + "api#dev: exit 1\n"
    head, tail = out.split("[openajar")[0], out.split("]\n", 1)[1]
    assert skip == len(head) and head + gap["stdout"] == whole and whole.endswith(tail)
    nxt = await t["terminal_job_read"](ctx, job=job["id"], stdout_offset=r["next_stdout_offset"],
                                       stderr_offset=r["next_stderr_offset"], wait_s=0)
    assert nxt["stdout"] == "" and nxt["stderr"] == "" and nxt["note"] is None
    for text in (r["note"], out):
        assert "—" not in text
    await t["terminal_job_cancel"](ctx, job=job["id"])


async def test_a_quiet_job_says_it_printed_nothing_and_how_to_read_its_last_lines(env, no_port, monkeypatch):
    monkeypatch.setattr("openajar.env.tools.QUIET_S", 1)
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="echo 'Error: env validation failed' >&2; sleep 20")
    seen = await until_printed(t, ctx, job["id"], "env validation failed")
    r = await t["terminal_job_read"](ctx, job=job["id"], until_port=3000, wait_s=1,
                                     stderr_offset=seen["next_stderr_offset"])
    assert r["stderr"] == "" and r["state"] == "running"
    assert "printing nothing meanwhile" in r["note"] and "stderr_offset=-4000" in r["note"]
    last = await t["terminal_job_read"](ctx, job=job["id"], stderr_offset=-4000, wait_s=0)
    assert last["stderr"] == "Error: env validation failed\n"
    await t["terminal_job_cancel"](ctx, job=job["id"])


async def test_a_port_wait_ends_early_and_says_so_when_the_kernel_killed_a_child(env, no_port, monkeypatch):
    """Run 1: turbo's API child was OOM-killed and the job read `running`. The kernel's kill ends the
    port wait at once, and the note says what was killed although the job still runs."""
    kills = oom.parse(KERNEL, elapsed_s=999)[:1]

    async def fake(self, wid, *, elapsed_s, final=False):
        await asyncio.sleep(0.2)
        return kills

    monkeypatch.setattr(Job, "on_vm", property(lambda self: self.rt is not None))
    monkeypatch.setattr(LaneRuntime, "oom_kills", fake)
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="echo compiling; sleep 20")
    loop = asyncio.get_running_loop()
    began = loop.time()
    r = await t["terminal_job_read"](ctx, job=job["id"], until_port=3000, wait_s=60)
    assert loop.time() - began < 10
    assert r["state"] == "running" and r["listening"] is False and r["oom"][0]["pid"] == 4242
    assert r["note"].startswith("The kernel killed pid 4242 (node, 4.0 GB) of this job")
    assert "the kernel killed one of its processes for memory meanwhile" in r["note"]
    await t["terminal_job_cancel"](ctx, job=job["id"])


# ------------------------------------------------------------------ reading the tail, colour codes


async def test_a_negative_offset_reads_the_last_bytes_from_a_lines_start(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="seq 1 5000; echo oops >&2")
    done = await t["terminal_job_read"](ctx, job=job["id"], until_exit=True, wait_s=10, max_bytes=100)
    assert done["state"] == "exited"
    r = await t["terminal_job_read"](ctx, job=job["id"], stdout_offset=-12, stderr_offset=-100, wait_s=0)
    assert r["stdout"] == "4999\n5000\n" and r["stderr"] == "oops\n"   # -12 starts mid "4998": the next line
    assert r["next_stdout_offset"] == done["stdout_bytes"]
    capped = await t["terminal_job_read"](ctx, job=job["id"], stdout_offset=-10**9, max_bytes=8, wait_s=0)
    assert capped["stdout"] == "5000\n"                             # at most max_bytes, still the end
    small = await t["terminal_job_read"](ctx, job=job["id"], stderr_offset=-4000, wait_s=0)
    assert small["stderr"] == "oops\n" and small["next_stderr_offset"] == 5


async def test_colour_codes_are_left_out_of_text_and_kept_in_base64(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run",
                                        cmd=r"printf '\033[32m  VITE ready\033[0m \033]8;;http://x\033\\link\033]8;;\033\\\n'")
    r = await t["terminal_job_read"](ctx, job=job["id"], until_exit=True, wait_s=10)
    assert r["stdout"] == "  VITE ready link\n"
    raw = await t["terminal_job_read"](ctx, job=job["id"], base64_output=True, wait_s=0)
    assert "\x1b" in base64.b64decode(raw["stdout"]).decode()


def test_a_colour_code_cut_at_the_end_of_a_read_waits_for_the_next():
    from openajar.env.tools import _whole

    assert _whole(b"ok \x1b[3") == b"ok "
    assert _whole(b"ok \x1b[32mgreen") == b"ok \x1b[32mgreen"
    assert _whole("é".encode()[:1]) == b""
    assert execs.strip_ansi("a\x1b[1;31mb\x1b[0m\x1b[") == "ab"


# ------------------------------------------------------------------ bash


@pytest.mark.skipif(not os.access(execs.BASH, os.X_OK), reason="no bash on this machine")
async def test_terminal_exec_and_jobs_run_bash(env):
    t, ctx = tools(env), Ctx(env)
    r = await t["terminal_exec"](ctx, on="dev/run", cmd='v=abcdefghijklmnopq; echo "${v:0:5}"; [[ a == a ]] && echo yes')
    assert r["exit_code"] == 0 and r["stdout"] == "abcde\nyes\n"
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="a=(x y z); echo ${a[1]}; exit 4")
    r = await t["terminal_job_read"](ctx, job=job["id"], until_exit=True, wait_s=10)
    assert r["stdout"] == "y\n" and r["exit_code"] == 4
    # the cwd check, the exit status and stdin go through as before
    r = await t["terminal_exec"](ctx, on="dev/run", cmd="cat; exit 3", stdin="in\n")
    assert r["stdout"] == "in\n" and r["exit_code"] == 3


def test_without_bash_the_command_runs_under_sh(monkeypatch):
    monkeypatch.setattr(execs, "BASH", "/nonexistent/bash")
    line = execs.in_bash("echo \"it's $0\"; echo $c")
    out = subprocess.run(["/bin/sh", "-c", line], capture_output=True, text=True, check=False)
    assert out.returncode == 0 and out.stdout == "it's /bin/sh\n\n"   # `c` is the wrapper's, never exported
