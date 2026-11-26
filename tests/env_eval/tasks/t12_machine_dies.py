"""A machine that dies under the agent: the harness removes its container in the middle of a build (`docker
rm -f`, as a lost host would; one that only stopped is started again with its disk, so it is no dead machine). The agent has to notice, take a new machine and run the build
again there (merged from the evals/ suite of 2026-11-20, `machine-dies`). The agent leaves the new
machine open; the checker reads the build's output on it, and the harness removes it."""

from __future__ import annotations

import asyncio
import hashlib
import time

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

N = 71
BUILD_ID = hashlib.sha256(f"release-{N}".encode()).hexdigest()[:12]
KILL_AFTER_S = 12          # into the build: well inside its 80 s
BUILT = "cat $(find /work -path '*/dist/build-id.txt' 2>/dev/null | head -1) 2>/dev/null"


def _building(ctx: Ctx) -> str | None:
    """The environment whose machine runs the build now, if one does."""
    for env in ctx.environments():
        code, _ = ctx.node.exec_in(env.id, "pgrep -f 'build.sh' >/dev/null")
        if code == 0:
            return env.id
    return None


async def kill_mid_build(ctx: Ctx) -> None:
    """The harness's step beside the session: once the build runs, wait a little, then kill that machine. Once."""
    while (env_id := await asyncio.to_thread(_building, ctx)) is None:
        await asyncio.sleep(1)
    await asyncio.sleep(KILL_AFTER_S)
    killed = await asyncio.to_thread(ctx.node.kill, env_id)
    if killed:
        ctx.notes["killed"] = {"environment": env_id, "container": killed, "at": time.time()}


async def check(ctx: Ctx) -> list[Check]:
    killed = ctx.notes.get("killed") or {}
    envs = ctx.environments()
    after = [e for e in envs if e.id != killed.get("environment")]
    code, out = ctx.on_machine(BUILT) if after else (-1, "")
    return [
        ("the harness killed the machine in the middle of the build", bool(killed)),
        ("the agent took another machine after that", bool(killed) and bool(after)),
        ("the build finished on the machine left open", code == 0 and out.strip() == BUILD_ID),
        ("the answer has the build id", BUILD_ID in ctx.answer()),
    ]


async def _build(a: Agent, workdir: str) -> dict:
    job = await a.on_call("terminal_job_start", cmd=f"sh build.sh {N}", cwd=workdir)
    return await a.on_call("terminal_job_read", job=job["id"], until_exit=True, wait_s=120)


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    """Reads the job to its end; when it ended without its build id (state lost, its note: the machine stopped),
    opens the same title again, as the note says: the node ends the dead environment and starts a new machine on
    that title (`replaced`), with no close and no second title. Then it builds again there."""
    env = await a.open(title="Release build", repo=ctx.repo.slug, keep="none")
    first = await _build(a, env["workdir"])
    if "BUILD_ID=" not in str(first.get("stdout", "")):
        env = await a.open(title="Release build", repo=ctx.repo.slug, keep="none")
        await _build(a, env["workdir"])
    return done(f"The first machine died during the build, so I ran it again on a new one: build id {BUILD_ID}. "
                "The new machine is open.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Reads the job again after the machine died, then reports the failure instead of taking a new machine."""
    env = await a.open(title="Release build", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd=f"sh build.sh {N}", cwd=env["workdir"])
    for _ in range(2):
        ok, _ = await a.raw("terminal_job_read", {"on": a.on, "job": job["id"], "until_exit": True, "wait_s": 120})
        if ok:
            break
    return done("The build failed: the machine stopped answering.", status="blocked")


TASK = Task(
    id="t12_machine_dies",
    title="the machine dies in the middle of a build: notice, take a new one, build again",
    repo="slowbuild",
    sessions=[Session(f"In openajar, run the release build of {{repo}} (`sh build.sh {N}`, a bit over a minute) and "
                      "tell me the build id it prints at the end. If anything goes wrong with the machine, sort it "
                      "out and get me the build id anyway. Leave the machine open: I want to look at it.",
                      during=kill_mid_build)],
    check=check, perfect=perfect, careless=careless,
    new_environments=2,
    timeout_s=720,
)
