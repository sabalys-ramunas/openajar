"""Coming back to an environment: the saved services started again and checked listening, the
environment found by its repository alone, and what a pause costs in words.

- `start_services`: on a resume (and after a snapshot) each service a save holds (a terminal_job_start
  command that ran, with the port it served) is started again, and the node waits up to
  SERVICE_WAIT_S in all for those ports to listen. `resumed.services` says for each one whether it
  runs and listens, and gives a ready `restart` call for one that did not come back.
- `listening`: a save also records what really listens on the machine (scripts.LISTENING), not only
  the jobs the node knows: a node that restarted while a dev server ran has lost its job (round 3,
  T2: `resumed.services: []`). Each port's process chain gives the command that was started: the
  child of openajar's own workload shell (a terminal_job_start), else the topmost process in /work.
  Only what listened at save time is recorded, so a resume starts nothing that was not serving.
- `by_repo`: environment_open(repo=X) with no title. The newest open or saved environment of the
  family for that repository, when there is exactly one; several are a choice the agent makes (each
  title is listed with the call that takes it); none: a new one titled after the repository.
- `pause_words`: what a stopped environment costs while it waits, and how long a resume takes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import posixpath
import re
import shlex
from typing import Any

from openajar.core.errors import InvalidRequest, OpenajarError

log = logging.getLogger("openajar.environments")

SERVICE_WAIT_S = 45.0     # all the saved services' ports together, at most, on a resume
POLL_S = 1.0


class EnvironmentChoice(InvalidRequest):
    """environment_open(repo=X) with no title found several environments for X."""
    code = "environment_choice"


def restart_call(cmd: str, cwd: str | None) -> str:
    """The terminal_job_start call that starts a service again, ready to send."""
    args = f"cmd={json.dumps(cmd)}" + (f", cwd={json.dumps(cwd)}" if cwd else "")
    return f"terminal_job_start({args})"


async def start_services(mgr: Any, env: Any, services: list[dict], *, wait_s: float = 0.0) -> list[dict]:
    """Start each saved service; with `wait_s`, wait (all together, at most that long) for the ports
    they serve to listen. One entry per service: cmd, cwd, port, running, listening (None when it names
    no port), job_id, and for one that did not come back `restart` and `why`."""
    envs = mgr.envs()
    out: list[dict] = []
    for s in services:
        row: dict = {"cmd": s["cmd"], "cwd": s.get("cwd"), "port": s.get("port"), "running": False}
        try:
            job = await envs.job_start(env.terminal_lane, s["cmd"], cwd=s.get("cwd"), by="openajar (resume)",
                                       grant_id=env.grant_id)
            row["running"] = job.state == "running"
            row["job_id"] = job.id
            if not row["running"]:
                row["why"] = f"it {job.state} at once"
        except Exception as e:  # noqa: BLE001 - the resume says it did not start, and how to start it
            row["why"] = str(getattr(e, "message", e))[:200] or type(e).__name__
            log.warning("environment %s: service %r did not start: %s", env.id, s["cmd"], row["why"])
        out.append(row)
    waiting = {r["port"] for r in out if r["running"] and r.get("port")}
    listening: set[int] = set()
    if waiting and wait_s > 0:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while True:
            with contextlib.suppress(OpenajarError, Exception):
                listening = {p["port"] for p in await mgr.ports(env) if p.get("port")}
            if waiting <= listening or loop.time() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        _still_running(envs, env.terminal_lane, out)
    for r in out:
        if r.get("port") and r["running"] and wait_s > 0:
            r["listening"] = r["port"] in listening
            if not r["listening"]:
                r["why"] = (f"it runs, but port {r['port']} did not listen within {int(wait_s)} s; "
                            "terminal_job_read(id) shows its output")
        if not r["running"]:
            r["restart"] = restart_call(r["cmd"], r.get("cwd"))
    return out


def _still_running(envs: Any, lane: str, rows: list[dict]) -> None:
    """A service that exited while the ports were awaited does not run (a crash at start)."""
    with contextlib.suppress(Exception):
        jobs = {j.id: j for j in envs.job_list(lane)}
        for r in rows:
            j = jobs.get(r.get("job_id") or "")
            if r["running"] and j is not None and j.state != "running":
                r["running"], r["why"] = False, f"it {j.state} while starting; terminal_job_read(\"{j.id}\") shows why"


WRAPPER_MARK = "/tmp/agentos-wl/"   # openajar.vms.ssh's workload shell names its pgid file under it
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ash", "fish"})
STOPS = frozenset({"sshd", "sshd-session", "systemd", "init", "tmux", "screen", "setsid", "su", "sudo", "login"})
CD_RE = re.compile(r"^cd '([^']+)' && (.+)$", re.DOTALL)   # how terminal_job_start runs a job with a cwd
MAX_LISTENING = 8


def listening(lines: list[str], work: str = "/work") -> list[dict]:
    """scripts.LISTENING's `proc=` values -> [{cmd, cwd, port}], one per command (its lowest port),
    for a listener whose folder is under `work`."""
    chains: dict[int, list[tuple[int, str, list[str]]]] = {}
    for line in lines:
        parts = line.split(" ")
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            cwd = base64.b64decode(parts[2]).decode(errors="replace").strip()
            argv = [a.decode(errors="replace") for a in base64.b64decode(parts[3]).split(b"\0") if a]
        except (binascii.Error, ValueError):
            continue
        if argv:
            chains.setdefault(int(parts[0]), []).append((int(parts[1]), cwd, argv))
    out: list[dict] = []
    for port in sorted(chains):
        got = _started(sorted(chains[port]), work)
        if got is not None and not any(r["cmd"] == got[0] and r["cwd"] == got[1] for r in out):
            out.append({"cmd": got[0], "cwd": got[1], "port": port})
    return out[:MAX_LISTENING]


def _under(path: str, work: str) -> bool:
    return path == work or path.startswith(work.rstrip("/") + "/")


def _started(chain: list[tuple[int, str, list[str]]], work: str) -> tuple[str, str | None] | None:
    """The command that was started, from a listener's chain (listener first): the child of openajar's
    workload shell, else the topmost process still in `work` below a login shell, sshd or init."""
    if not chain or chain[0][0] != 0 or not _under(chain[0][1], work):
        return None
    wrapper = next((i for i, (_, _, argv) in enumerate(chain) if any(WRAPPER_MARK in a for a in argv)), None)
    if wrapper == 0:
        return None
    top = wrapper - 1 if wrapper is not None else 0
    if wrapper is None:
        for i in range(1, len(chain)):
            if not _under(chain[i][1], work) or _stops(chain[i][2]):
                break
            top = i
    _, cwd, argv = chain[top]
    name = posixpath.basename(argv[0]).lstrip("-")
    if name in SHELLS and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
        m = CD_RE.match(argv[2])
        if m:
            return m.group(2), m.group(1)
        cmd = argv[2]
    elif len(argv) == 1 and " " in argv[0]:   # a process that set its own title ("npm run dev")
        cmd = argv[0]
    else:
        cmd = shlex.join(argv)
    return cmd, cwd if _under(cwd, work) else None


def _stops(argv: list[str]) -> bool:
    name = posixpath.basename(argv[0]).lstrip("-").split(":")[0]
    if name in SHELLS:
        return not any(a in ("-c", "-lc") for a in argv[1:3])   # an interactive or login shell
    return name in STOPS


def with_listening(services: list[Any], found: list[dict], make: Any) -> list[Any]:
    """The node's job services and what listened on the machine, each command once: a listener whose
    port or command a job has is that job's (a job without a port gets the listener's); `make(**kw)`
    builds a new entry (saves.Service)."""
    out = [s.model_copy() for s in services]
    for f in found:
        same = next((s for s in out if s.port == f["port"] or (s.cmd == f["cmd"] and (s.cwd or None) == f["cwd"])),
                    None)
        if same is None:
            out.append(make(cmd=f["cmd"], cwd=f["cwd"], port=f["port"]))
        elif same.port is None:
            same.port = f["port"]
    return out


def lost_jobs(services: list[dict]) -> list[str]:
    """`running_jobs_lost`: the saved services, in words, when none of them came back."""
    if not services or any(s.get("running") for s in services):
        return []
    return [s["cmd"] + (f" (port {s['port']})" if s.get("port") else "") for s in services]


# ---------------------------------------------------------------- the repository alone
def _repo_title(repo: str) -> str:
    name = repo.rstrip("/").rsplit("/", 1)[-1] or repo
    return name[:80]


def by_repo(mgr: Any, family: str | None, repo: str, *, saves_of: str, reaches: Any = None) -> str:
    """The title environment_open(repo=X) with no title uses: the one open or saved environment of
    this family for `repo` (newest first), or the repository's name when there is none. Raises
    EnvironmentChoice, listing them, when there are several. `family` None: the owner (every open
    one); `saves_of`: whose saves; `reaches(env)`: the caller may use that open one."""
    found: dict[str, dict] = {}
    for e in mgr.open_for_family(family):
        if e.repo == repo and e.state in ("starting", "ready") and (reaches is None or reaches(e)):
            found.setdefault(e.title.lower(), {"title": e.title, "state": "open",
                                               "at": e.last_activity().isoformat()})
    for m in mgr.saves().all(saves_of):
        if (m.repo or None) == repo:
            found.setdefault(m.title_key, {"title": m.title, "state": "saved", "at": m.saved_at.isoformat()})
    rows = sorted(found.values(), key=lambda r: r["at"], reverse=True)
    if len(rows) == 1:
        return rows[0]["title"]
    if not rows:
        return _repo_title(repo)
    listed = "; ".join(f"\"{r['title']}\" ({r['state']}, {r['at'][:16].replace('T', ' ')} UTC)" for r in rows[:8])
    raise EnvironmentChoice(
        f"{len(rows)} environments work on {repo}: {listed}. Pick one by its title: "
        f"environment_open(title=\"{rows[0]['title']}\") takes the newest; a new title opens a new one")


# ---------------------------------------------------------------- what a pause costs
def resume_time(source: str, provider: str = "") -> str:
    if provider == "own" and source != "snapshot":  # a machine handed over whole: its saves hold the files
        return "in a minute or two (the files go back onto the machine)"
    if provider in ("docker", "own", "morph"):  # a container, or a Morph snapshot's start
        return "in seconds"
    return "in about a minute" if source == "snapshot" else "in a few minutes (a fresh machine, then the files)"


def pause_words(cost_eur_month: float | None, *, snapshot: bool, provider: str = "") -> str:
    """"saved: about EUR 0.05 a month, resumes in about a minute"."""
    if not cost_eur_month:
        from openajar.vms.providers import FREE

        # nothing rented (docker, the owner's own machine): the save's image is in their own Docker
        cost = "costs nothing while it waits" if not snapshot or provider in FREE else "costs under a cent a month"
    else:
        cost = f"about EUR {cost_eur_month:.2f} a month" if cost_eur_month >= 0.01 else "under a cent a month"
    return f"saved: {cost}, resumes {resume_time('snapshot' if snapshot else 'files', provider)}"


def closed_words(mgr: Any, env: Any) -> str:
    """What a call with the handle of an environment that has closed hears. Saved: when, and how to come
    back (environment_open with its title, never on the agent's behalf), or that it is open again (the
    owner resumed it, or another chat did). Not saved: closed, and how to open a new one."""
    from openajar.environments.models import LIVE_STATES
    from openajar.environments.picking import _when, is_let_go, same_title

    title = env.title
    why = f" ({env.closed_reason})" if env.closed_reason else ""
    saved = None
    with contextlib.suppress(Exception):
        saved = mgr.saves().latest(env.family, title)
    again = next((e for e in mgr.open_for_family(env.family) if same_title(e.title, title) and e.state in LIVE_STATES),
                 None)
    if saved is None and again is None:
        return (f"environment {env.id} (\"{title}\") is closed{why} and has no save, so this on reaches nothing and "
                "nothing ran. environment_open(title=...) opens a new one")
    when = _when(saved.saved_at) if saved is not None else None
    head = (f"environment {env.id} (\"{title}\") was saved{' at ' + when if when else ''} and its machine "
            f"closed{why}, so this on no longer reaches a machine and nothing ran. ")
    if again is not None:
        take = ("no chat holds it: environment_open(title=\"{t}\") takes it" if is_let_go(again) else
                "another chat holds it: environment_open(title=\"{t} 2\") opens your own").format(t=title)
        return head + f"It is open again as {again.id}; {take}"
    return head + (f"environment_open(title=\"{title}\") resumes it on a new machine with a new on (it is never "
                   "resumed for you)")
