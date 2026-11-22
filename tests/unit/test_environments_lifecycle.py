"""An environment's life cycle as the 2026-11-15 test on the node found it (environments
P2-6, P2-10, P2-11 and the CLI's F15): a deleted branch shows as a delete,
limit errors say what is true, the last calls carry exit codes and refusals, the close says whether
the machine is gone, a closed environment's lanes go, and the branches it created outlive a restart.
The fixture is test_environments.py's (a fake cloud, fake lanes, no SSH)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)
from typer.testing import CliRunner

from openajar.core import catalog
from openajar.core.models import utcnow
from openajar.env.models import ExecResult
from openajar.environments import manager as manager_mod
from openajar.environments.cli import app as env_cli
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.policy.wrap import ToolCallError

REPO = "example.test/me/shop"
ZERO = "0" * 40
A, B = "a" * 40, "b" * 40


def pushed(node, env_id: str, *refs: tuple[str, str, str]) -> None:  # noqa: F811
    """A push from the environment's VM as the git gateway records it: (ref, old, new) each."""
    env = node["mgr"].get(env_id)
    ctx = node["policy"].context_for_grant(node["grant"].id, tool="git_push")
    rows = [{"ref": ref, "kind": "create" if old == ZERO else "delete" if new == ZERO else "update",
             "old": f"sha1:{old}", "new": f"sha1:{new}"} for ref, old, new in refs]
    ctx.trace("git_push", lane=env.terminal_lane, remote=REPO, refs=rows, result="pushed")


# ------------------------------------------------------------------ P2-6: deletes are deletes


async def test_a_deleted_branch_shows_as_a_delete_and_not_as_pushed(node):  # noqa: F811
    out = await opened(node, repo=REPO)
    pushed(node, out["id"], ("refs/heads/agent/x", ZERO, A))
    pushed(node, out["id"], ("refs/heads/agent/x", A, B), ("refs/heads/agent/old", ZERO, A))
    pushed(node, out["id"], ("refs/heads/agent/old", A, ZERO))
    st = await node["call"]("environment")
    # what is on the remote now: each branch once, at its last commit; the deleted one apart
    assert [(p["branch"], p["commit"]) for p in st["git"]["pushed"]] == [("agent/x", B[:12])]
    assert [p["branch"] for p in st["git"]["deleted"]] == ["agent/old"]
    assert "000000000000" not in str(st["git"])
    closed = await node["call"]("environment_close")
    assert [(p["branch"], p["commit"]) for p in closed["pushed"]] == [("agent/x", B[:12])]
    assert [p["branch"] for p in closed["deleted"]] == ["agent/old"]
    # the record kept at close says the same, without the traces
    env = node["mgr"].get(out["id"])
    assert {(p.branch, p.deleted) for p in env.pushed} == {("agent/x", False), ("agent/old", True)}


async def test_a_branch_pushed_again_after_its_delete_is_pushed(node):  # noqa: F811
    out = await opened(node, repo=REPO)
    pushed(node, out["id"], ("refs/heads/agent/x", ZERO, A))
    pushed(node, out["id"], ("refs/heads/agent/x", A, ZERO))
    pushed(node, out["id"], ("refs/heads/agent/x", ZERO, B))
    st = await node["call"]("environment")
    assert [(p["branch"], p["commit"]) for p in st["git"]["pushed"]] == [("agent/x", B[:12])]
    assert st["git"]["deleted"] == []


# ------------------------------------------------------------------ P1-1: the branches it created


async def test_the_branches_an_environment_created_survive_a_restart(node, tmp_path):  # noqa: F811
    out = await opened(node, repo=REPO)
    mgr = node["mgr"]
    lane = f"env/{out['id']}"
    mgr.note_branches(lane, created=["agent/x", "agent/y"], deleted=[])
    mgr.note_heads(lane, {"agent/x": "a" * 40, "agent/y": "b" * 40, "feature": "c" * 40})  # feature: not its own
    mgr.note_branches(lane, created=[], deleted=["agent/y"])
    again = EnvironmentManager(mgr.home, runtime=node["rt"], settings=EnvSettings())  # the node restarted
    assert again.branches_created(lane) == ["agent/x"]
    assert again.branch_heads(lane) == {"agent/x": "a" * 40}  # the commit it last pushed, kept; deleted: gone
    assert again.branches_created(f"{lane}-browser") == [] and again.branches_created("env/enosuch") == []
    await node["call"]("environment_close")
    assert mgr.branches_created(lane) == []  # a closed one creates and owns nothing


# ------------------------------------------------------------------ P2-10: limit and size errors


async def test_an_unknown_size_is_invalid_before_anything_is_rented(node):  # noqa: F811
    with pytest.raises(ToolCallError, match="invalid") as err:
        await node["call"]("environment_open", title=TITLE, size="xxlarge")
    words = str(err.value)
    assert "small, medium, large or xlarge" in words and "server type" in words and "Nothing was rented" in words
    assert "vm_error" not in words and "openajar vm" not in words
    assert node["vms"].store.recent() == [] and node["mgr"].store.recent() == []


async def test_a_known_server_type_still_opens(node):  # noqa: F811
    out = await opened(node, size="cx23")
    assert out["state"] == "ready" and out["machine"]["server_type"] == "cx23"


async def test_a_size_past_the_limit_names_the_setting_that_would_allow_it(node):  # noqa: F811
    node["mgr"].settings.max_size = "small"
    with pytest.raises(ToolCallError, match="environment_limit") as err:
        await node["call"]("environment_open", title=TITLE, size="large")
    assert "--max-size large" in str(err.value) and "Ask for small or less" in str(err.value)
    with pytest.raises(ToolCallError, match="environment_limit") as err:
        await node["call"]("environment_open", title=TITLE, size="cpx42")  # 8 CPU, 16 GB: large
    assert "--max-size large" in str(err.value) and "--max-size xlarge" not in str(err.value)
    assert node["vms"].store.recent() == []


def test_a_server_type_bigger_than_any_size_says_none_may_be_bigger(node):  # noqa: F811
    mgr = node["mgr"]
    assert mgr.settings.max_size == "xlarge"
    with pytest.raises(Exception, match="no environment may be bigger than xlarge") as err:
        mgr._check_planned({"server_type": "ccx63", "specs": {"cores": 48, "memory_gb": 192}})
    assert "--max-size" not in str(err.value)


def test_a_vm_error_while_planning_says_nothing_was_rented():
    assert "The message says when nothing was rented" in catalog.entry("vm_error").agent


# ------------------------------------------------------------------ P2-11: the last calls


async def test_a_command_that_failed_shows_its_exit_code(node, monkeypatch):  # noqa: F811
    await opened(node, repo=REPO)
    real = node["envs"].exec

    async def exec_(lane_id, cmd, **kw):
        if "\nexit 7" in cmd:  # in the repository's folder, under bash
            return ExecResult(exit_code=7, stdout="", stderr="", duration_s=0.01)
        return await real(lane_id, cmd, **kw)

    monkeypatch.setattr(node["envs"], "exec", exec_)
    res = await node["call"]("terminal_exec", cmd="exit 7")
    assert res["exit_code"] == 7
    st = await node["call"]("environment")
    assert st["calls"][0]["tool"] == "terminal_exec" and st["calls"][0]["exit_code"] == 7


async def test_a_refused_close_is_one_of_the_last_calls(node):  # noqa: F811
    await opened(node, repo=REPO)
    node["machine"].git = "repo=/work/shop\nbranch=f\nahead=1\ndirty_count=0\nunpushed=abc1234 wip\n"
    with pytest.raises(ToolCallError, match="unsaved_work"):
        await node["call"]("environment_close")
    st = await node["call"]("environment")
    [call] = [c for c in st["calls"] if c["tool"] == "environment_close"]
    assert call["ok"] is False and call["error"] == "unsaved_work"


# ------------------------------------------------------------------ the close: the machine, the lanes


async def test_the_close_says_the_machine_is_deleted_once_the_provider_has_none(node):  # noqa: F811
    out = await opened(node, repo=REPO)
    rec = node["vms"].store.get(node["mgr"].get(out["id"]).vm_id)
    closed = await node["call"]("environment_close")
    assert closed["machine"] == "deleted" and rec.provider_id in node["prov"].deleted
    # its lanes are gone too, not left as cold lanes (F7)
    with pytest.raises(Exception, match="no browser|not found|unknown"):
        node["lanes"].get(f"env/{out['id']}-browser")
    assert not node["envs"].has(f"env/{out['id']}")
    # closed again: the same answer from the record
    assert (await node["call"]("environment_close", on=out["id"]))["machine"] == "deleted"


async def test_the_close_does_not_say_deleted_while_the_provider_still_has_it(node, monkeypatch):  # noqa: F811
    await opened(node, repo=REPO)
    monkeypatch.setattr(manager_mod, "GONE_WAIT_S", 0)

    async def still_there(server_id):
        return SimpleNamespace(id=server_id)

    monkeypatch.setattr(node["prov"], "get_server", still_there)
    closed = await node["call"]("environment_close")
    assert closed["machine"] == "deleting"


async def test_the_close_says_deleting_when_the_delete_failed(node):  # noqa: F811
    await opened(node, repo=REPO)
    node["prov"].fail_delete = 1
    closed = await node["call"]("environment_close")
    assert closed["machine"] == "deleting"


async def test_the_reaper_removes_the_lanes_a_closed_environment_left(node, monkeypatch):  # noqa: F811
    out = await opened(node, repo=REPO)
    browser = f"env/{out['id']}-browser"
    lanes = node["lanes"]
    real_delete = lanes.delete
    refused = {"n": 0}

    async def busy_delete(lane_id, *, by="owner"):
        refused["n"] += 1
        raise RuntimeError("a browser still holds the profile")

    monkeypatch.setattr(manager_mod, "LANE_DELETE_WAIT_S", 0)
    monkeypatch.setattr(lanes, "delete", busy_delete)
    await node["call"]("environment_close")
    assert refused["n"] == manager_mod.LANE_DELETE_TRIES and lanes.get(browser)  # it stayed
    # the lane list leaves it out meanwhile
    from openajar.api.routes_core import _closed_environment_lane

    api = SimpleNamespace(runtime=node["rt"])
    assert _closed_environment_lane(api, browser) and not _closed_environment_lane(api, "personal/reddit")
    monkeypatch.setattr(lanes, "delete", real_delete)
    report = await node["mgr"].reap_once()
    assert report["swept"] == [out["id"]]
    with pytest.raises(Exception):  # noqa: B017 - the fake's own not-found error
        lanes.get(browser)
    assert (await node["mgr"].reap_once())["swept"] == []  # nothing left: nothing to do
    # a day later it is no longer looked at
    assert await node["mgr"].sweep_lanes(utcnow() + timedelta(days=2)) == []


# ------------------------------------------------------------------ F15: openajar environments list


async def test_the_list_has_a_header_and_the_whole_repo(node, monkeypatch):  # noqa: F811
    long_repo = "example.test/someone-else/openajar-with-a-long-name"
    await opened(node, repo=long_repo)
    runner = CliRunner()
    home = str(node["mgr"].home)
    res = runner.invoke(env_cli, ["--home", home, "list"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert lines[0].split() == ["ID", "STATE", "TITLE", "REPOSITORY", "MACHINE", "COST", "TIME", "LEFT"]
    assert long_repo in lines[1]


async def test_an_empty_list_says_how_to_see_closed_ones(node):  # noqa: F811
    await opened(node, repo=REPO)
    await node["call"]("environment_close")
    runner = CliRunner()
    home = str(node["mgr"].home)
    res = runner.invoke(env_cli, ["--home", home, "list"])
    assert res.exit_code == 0 and "No open environments" in res.output and "list --all" in res.output
    res = runner.invoke(env_cli, ["--home", home, "list", "--all"])
    assert res.exit_code == 0 and res.output.splitlines()[0].startswith("ID")
