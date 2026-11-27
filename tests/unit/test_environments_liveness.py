"""A machine that stops for good while its environment is open (openajar.environments.liveness; evals t12): the
node asks the provider, twice, and only then says it is gone; every call that needs the machine fails with
machine_gone and one next step; environment() shows it; environment_close ends it without a save; an open with
the same title starts a new machine instead of handing back the dead one; a job on it is lost with the true
reason; and none of these checks is the agent's use of the machine. The fixture is test_environments.py's (a
fake cloud, fake lanes, no SSH)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from openajar.core import catalog
from openajar.core.errors import MachineGone
from openajar.environments import liveness, routing
from openajar.policy.wrap import ToolCallError

REPO = "example.test/me/shop"


def _server(node, env_id):  # noqa: F811
    env = node["mgr"].get(env_id)
    return node["prov"].servers[node["vms"].store.get(env.vm_id).provider_id]


@pytest.fixture
def quick(node):  # noqa: F811
    node["mgr"].gone_recheck_s = 0.0
    return node


async def _killed(node, out):  # noqa: F811
    """The machine is gone for good (docker says dead: its container is past starting again), and the node finds
    out. A machine that only stopped is started again (test_a_stopped_machine_is_started_again...)."""
    _server(node, out["id"]).status = "dead"
    assert await node["mgr"].machine_gone(node["mgr"].get(out["id"]), cause="test")
    return node["mgr"].get(out["id"])


# ------------------------------------------------------------------ gone, or only not answering


async def test_the_provider_decides_and_a_running_machine_is_never_called_gone(quick):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    env = mgr.get(out["id"])
    asked = []
    real = quick["prov"].get_server

    async def get_server(sid):
        asked.append(sid)
        return await real(sid)

    quick["prov"].get_server = get_server
    assert await mgr.machine_gone(env) is None  # ssh failed once, the machine runs: a blip, nothing said
    assert await mgr.machine_gone(env) is None and len(asked) == 1  # not asked again for ALIVE_AGAIN_S
    assert mgr.get(env.id).gone_at is None and mgr.get(env.id).shown_state(mgr._clock()) != "gone"


async def test_a_provider_that_does_not_answer_or_changes_its_mind_says_nothing(quick, monkeypatch):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    env = mgr.get(out["id"])

    async def broken(sid):
        raise TimeoutError("api down")

    monkeypatch.setattr(quick["prov"], "get_server", broken)
    assert await mgr.machine_gone(env) is None
    monkeypatch.undo()
    mgr._alive_seen().clear()
    server = _server(quick, env.id)
    answers = iter(["dead", "running"])  # it came back by the second look
    real = quick["prov"].get_server

    async def flaky(sid):
        s = await real(sid)
        s.status = next(answers)
        return s

    monkeypatch.setattr(quick["prov"], "get_server", flaky)
    assert await mgr.machine_gone(env) is None and server is not None and mgr.get(env.id).gone_at is None


@pytest.mark.parametrize(("status", "words"), [("dead", "says its server is dead"),
                                               ("deleting", "says its server is deleting"),
                                               (None, "no longer has its server")])
async def test_a_stopped_or_deleted_machine_is_gone_with_why(quick, status, words):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    if status is None:
        quick["prov"].servers.pop(quick["vms"].store.get(mgr.get(out["id"]).vm_id).provider_id)
    else:
        _server(quick, out["id"]).status = status
    why = await mgr.machine_gone(mgr.get(out["id"]))
    assert words in why
    env = mgr.get(out["id"])
    assert env.gone_at is not None and env.gone_why == why and env.shown_state(mgr._clock()) == "gone"
    assert out["id"] not in mgr._tunnels  # nothing to reach: its tunnel is stopped


async def test_the_checks_are_never_the_agents_use_of_the_machine(quick):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    env = mgr.get(out["id"])
    before = (env.last_call_at, quick["envs"].store.last_used(env.terminal_lane), len(quick["machine"].ran))
    await mgr.machine_gone(env)
    _server(quick, env.id).status = "dead"
    mgr._alive_seen().clear()
    await mgr.machine_gone(mgr.get(env.id))
    after = mgr.get(env.id)
    assert (after.last_call_at, quick["envs"].store.last_used(env.terminal_lane), len(quick["machine"].ran)) == before


# ------------------------------------------------------------------ what the agent hears


async def test_every_call_that_needs_the_machine_says_machine_gone_with_one_next_step(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    for tool, args in (("terminal_exec", {"cmd": "sh build.sh"}),
                       ("terminal_job_start", {"cmd": "sh build.sh"}), ("environment_save", {}),
                       ("environment_port", {"port": 3000})):
        with pytest.raises(ToolCallError, match="^machine_gone") as err:
            await quick["call"](tool, on=out["on"], **args)
        words = str(err.value)
        assert f'environment_open(title="{TITLE}")' in words and "no save" in words and "lost" in words, words
        assert words.startswith("machine_gone: Nothing ran: environment ") and "stopped at" in words
        assert "is dead" in words and "\u2014" not in words
    assert "same title" in catalog.entry("machine_gone").agent


async def test_environment_shows_the_machine_gone_and_asks_nothing_of_it(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    ran = len(quick["machine"].ran)
    st = await quick["call"]("environment", on=out["on"])
    assert st["state"] == "gone" and st["machine_gone"]["why"].endswith("is dead")
    assert st["next"] == st["machine_gone"]["next"] and "environment_open" in st["next"]
    assert len(quick["machine"].ran) == ran  # no git, no ports: nothing on the dead machine


async def test_the_list_says_so_too_with_when_and_why(quick):
    """The web app's list (EnvSummary) carries machine_gone, so its cards say why without reading each one."""
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    listed = quick["mgr"].summary(env)
    assert listed["state"] == "gone" and listed["machine_gone"]["why"].endswith("is dead")
    assert listed["machine_gone"]["at"]
    assert "machine_gone" not in quick["mgr"].summary(quick["mgr"].get((await opened(quick, title="Another", repo=REPO))["id"]))


async def test_environment_finds_it_out_itself_when_git_does_not_answer(quick, monkeypatch):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    _server(quick, out["id"]).status = "dead"

    async def no_answer(env):
        return None

    monkeypatch.setattr(mgr, "git_repos", no_answer)
    st = await quick["call"]("environment", on=out["on"])
    assert st["state"] == "gone" and "machine_gone" in st


async def test_closing_it_needs_no_discard_and_says_what_is_left(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    closed = await quick["call"]("environment_close", on=out["on"])
    assert closed["state"] == "closed" and closed["saves"] == {"made": None, "left": []}
    assert "had stopped" in closed["next"] and f'environment_open(title="{TITLE}")' in closed["next"]
    assert quick["mgr"].get(out["id"]).closed_reason.startswith("its machine stopped")


async def test_the_same_title_opens_a_new_machine_instead_of_the_dead_one(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    again = await opened(quick, repo=REPO)
    assert again["id"] != out["id"] and again["opened"] == "new" and again["state"] == "ready"
    assert again["replaced"]["id"] == out["id"] and again["replaced"]["why"].endswith("is dead")
    assert again["next"].startswith("New machine: the one this title had stopped")
    assert quick["mgr"].get(out["id"]).state == "closed"


async def test_an_open_notices_a_dead_machine_nobody_called_since(quick):
    """The harness killed it and the agent's next call is the open itself: still never an attach."""
    out = await opened(quick, repo=REPO)
    _server(quick, out["id"]).status = "dead"
    again = await opened(quick, repo=REPO)
    assert again["opened"] == "new" and again["replaced"]["id"] == out["id"]


async def test_the_words_name_the_save_to_resume_when_there_is_one(quick, monkeypatch):
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    from datetime import UTC, datetime

    save = SimpleNamespace(id="s1234567890", saved_at=datetime(2026, 11, 21, 9, 30, tzinfo=UTC))
    monkeypatch.setattr(quick["mgr"], "saves", lambda: SimpleNamespace(latest=lambda family, title: save))
    words = quick["mgr"].gone_words(env)
    assert "from its save of 2026-11-21T09:30:00Z (s1234567890)" in words and "after that save is lost" in words
    assert len(liveness.ran_nothing(words)) < 500  # the wrapper's limit on an error's message


async def test_a_job_read_still_reads_and_calls_elsewhere_still_route(quick):
    """A job's output is on the node: terminal_job_read is not refused (route leaves ON_ONLY tools alone)."""
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    kwargs = {"on": out["on"], "job": "job-0123456789"}
    assert routing.route(quick["rt"], "terminal_job_read", kwargs, session="chat-1", grant_id=quick["grant"].id,
                         family=quick["grant"].root_id).id == env.id


# ------------------------------------------------------------------ the reaper


async def test_the_reaper_finds_a_dead_machine_by_its_tunnel_and_ends_it_after_idle_minutes(quick):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    _server(quick, out["id"]).status = "dead"
    mgr._tunnels[out["id"]].alive = False  # ssh -N ended when the container died
    await mgr.reap_once()
    env = mgr.get(out["id"])
    assert env.gone_at is not None and env.open and out["id"] not in mgr._tunnels  # never started again
    later = env.gone_at + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later)
    assert out["id"] in got["gone"] and mgr.get(out["id"]).state == "closed"


def test_the_gone_statuses_are_the_providers_words_for_gone_and_stopped_is_not_gone():
    """S1-04: docker's exited and Hetzner's off are a machine that stopped with its disk whole, never gone."""
    assert {"dead", "removing", "deleting", "deleted"} == liveness.GONE_STATUSES
    assert {"exited", "off", "stopping"} <= liveness.STOPPED_STATUSES
    assert not liveness.GONE_STATUSES & liveness.STOPPED_STATUSES
    for words in ("running", "starting", "initializing", "paused"):
        assert words not in liveness.GONE_STATUSES | liveness.STOPPED_STATUSES


# ------------------------------------------------------------------ S1-04: stopped is not gone


@pytest.mark.parametrize("status", ["exited", "off"])
async def test_a_stopped_machine_is_started_again_with_its_disk_never_called_gone(quick, status):
    """A reboot, a Docker Desktop restart or an agent's poweroff leaves the machine stopped with its disk whole:
    0.7.0 called it gone and deleted it. It is started again, and nothing is deleted."""
    mgr, prov = quick["mgr"], quick["prov"]
    out = await opened(quick, repo=REPO)
    server = _server(quick, out["id"])
    server.status = status
    assert await mgr.machine_gone(mgr.get(out["id"]), cause="test") is None
    env = mgr.get(out["id"])
    assert env.gone_at is None and env.open and server.id in prov.servers and server.status == "running"
    assert prov.started == [server.id] and prov.deleted == []


async def test_a_stopped_machine_that_does_not_start_is_kept_and_the_owner_told_once(quick, monkeypatch):
    mgr, prov = quick["mgr"], quick["prov"]
    told: list[tuple[str, str]] = []

    async def notify(title, body):
        told.append((title, body))

    monkeypatch.setattr(mgr, "_notify", notify)
    out = await opened(quick, repo=REPO)
    server = _server(quick, out["id"])
    server.status, prov.fail_start = "exited", 99
    for minutes in (1, 2, 3):
        mgr._alive_seen().clear()
        mgr._tunnels[out["id"]].alive = False  # ssh -N ended when the machine stopped
        await mgr.reap_once(mgr._clock() + timedelta(minutes=minutes))
    env = mgr.get(out["id"])
    assert env.open and env.gone_at is None and server.id in prov.servers and prov.deleted == []
    stopped = [t for t in told if "stopped" in t[0]]
    assert len(stopped) == 1 and "could not be started" in stopped[0][1] and "nothing was deleted" in stopped[0][1]


async def test_a_machine_marked_gone_that_runs_again_is_not_gone_any_more(quick):
    """gone_at is never a one-way door: the reaper asks again, and a machine the provider says runs is back."""
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    _server(quick, out["id"]).status = "running"
    await mgr.reap_once()
    env = mgr.get(out["id"])
    assert env.gone_at is None and env.gone_why is None and env.open
    again = await opened(quick, repo=REPO)  # the same title: the same machine, not a new one
    assert again["id"] == out["id"] and "replaced" not in again


async def test_closing_a_machine_marked_gone_that_runs_again_is_an_ordinary_close(quick):
    """environment_close asks the provider again: a machine that runs is closed as any other (its work checked)."""
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    _server(quick, out["id"]).status = "running"
    closed = await quick["call"]("environment_close", on=out["on"])
    env = quick["mgr"].get(out["id"])
    assert closed["state"] == "closed" and "had stopped" not in closed.get("next", "")
    assert env.gone_at is None and not env.closed_reason.startswith("its machine stopped")


async def test_a_provider_that_does_not_answer_never_ends_a_machine(quick, monkeypatch):
    """Silence is never permission: past idle_minutes a machine marked gone is ended only once the provider says
    again that it is gone."""
    mgr, prov = quick["mgr"], quick["prov"]
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    real, answering = prov.get_server, [False]

    async def maybe(sid):
        if not answering[0]:
            raise TimeoutError("Cannot connect to the Docker daemon")
        return await real(sid)

    monkeypatch.setattr(prov, "get_server", maybe)
    later = env.gone_at + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later)
    assert out["id"] not in got["gone"] and mgr.get(out["id"]).open
    answering[0] = True
    got = await mgr.reap_once(later + timedelta(minutes=1))
    assert out["id"] in got["gone"] and mgr.get(out["id"]).state == "closed"


async def test_a_gone_machine_shows_what_is_not_known_as_unknown(quick):
    """U6-01: environment() of a gone machine said unsaved null and git ahead 0, dirty 0, as facts."""
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    st = await quick["call"]("environment", on=out["on"])
    assert st["git"]["ahead"] is None and st["git"]["dirty"] is None
    assert st["unsaved"]["unreachable"] is True and st["unsaved"]["dirty_files"] == []


# ------------------------------------------------------------------ S1-04: docker that does not answer


def _docker(tmp_path, *, stdout: str, stderr: str, code: int):
    """The docker CLI as it answers with no daemon (V1: `docker inspect` prints [] and exits 1)."""
    from openajar.vms.docker import DockerProvider

    bin_ = tmp_path / "docker"
    bin_.write_text(f"#!/bin/sh\nprintf '%s' '{stdout}'\nprintf '%s\\n' '{stderr}' >&2\nexit {code}\n")
    bin_.chmod(0o755)
    return DockerProvider(docker=str(bin_))


async def test_docker_that_does_not_answer_is_an_error_never_no_such_container(tmp_path):
    from openajar.vms.models import VmError

    down = _docker(tmp_path, stdout="[]", code=1,
                   stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?")
    for call in (down.get_server("abc123"), down.delete_server("abc123"), down.get_image("agentos-env:save-s1"),
                 down.delete_image("agentos-env:save-s1")):
        with pytest.raises(VmError, match="Cannot connect"):
            await call


async def test_docker_no_such_container_is_none(tmp_path):
    gone = _docker(tmp_path, stdout="[]", code=1, stderr="Error: No such object: abc123")
    assert await gone.get_server("abc123") is None
    await gone.delete_server("abc123")  # nothing to delete: done
    assert await gone.get_image("agentos-env:save-s1") is None
    await gone.delete_image("agentos-env:save-s1")


async def test_a_machine_that_stops_again_right_after_each_start_is_gone_after_three(quick, monkeypatch):
    """R1 #6: a container that dies seconds after `docker start` read "runs" each time and was started on every
    check, forever, and the agent never heard machine_gone. After 3 starts in 30 minutes it is not started again:
    it is marked gone with why, the owner is told, and the agent's next call says machine_gone."""
    mgr, prov = quick["mgr"], quick["prov"]
    told: list[tuple[str, str]] = []

    async def notify(title, body):
        told.append((title, body))

    monkeypatch.setattr(mgr, "_notify", notify)
    out = await opened(quick, repo=REPO)
    server = _server(quick, out["id"])
    for _ in range(3):  # it stops again right after each start
        server.status = "exited"
        mgr._alive_seen().clear()
        assert await mgr.machine_gone(mgr.get(out["id"]), cause="test") is None
    assert prov.started == [server.id] * 3
    server.status = "exited"
    mgr._alive_seen().clear()
    why = await mgr.machine_gone(mgr.get(out["id"]), cause="test")
    assert why and "after each of its last 3 starts" in why and "does not start it again" in why, why
    assert prov.started == [server.id] * 3 and mgr.get(out["id"]).gone_at is not None
    assert any("does not start it again" in body for _, body in told), told
    with pytest.raises(MachineGone, match="does not start it again"):  # what the agent's next call gets
        mgr.refuse_gone(mgr.get(out["id"]))
