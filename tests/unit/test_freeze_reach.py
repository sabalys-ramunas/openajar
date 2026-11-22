"""A workspace freeze holds a kept wide grant off everything in that workspace, not only calls that
name a lane or a top-level `workspace` (review of the 2026-11-10 fix wave, finding 4). A grant on
biz-a and biz-b, kept by a freeze of biz-a, could save and read workflows there, propose leads to
its lists, and read its jobs. These started as the reviewer's proofs; they
now assert the freeze stops each, that a listing leaves the frozen rows out and says so, and that a
person in person is not stopped. The last tests walk the tool registry, so a new tool that reaches
a workspace another way must say how the freeze reaches it."""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest
from workflow_support import LIST, make_node, workflow

from openajar.core.config import NodeConfig
from openajar.core.errors import Frozen
from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec, covers_workspace, pattern_reaches, pattern_within, require_lanes
from openajar.freeze.service import FreezeService
from openajar.mcp.providers import feature_providers, tool_providers
from openajar.plugins.entries import PLUGINS
from openajar.policy.wrap import ToolCallError, bind_request_token, wrap_tool
from openajar.runner.workflows import tools as wf_tools


@pytest.fixture
def frozen(tmp_path):
    n = make_node(tmp_path)
    fz = FreezeService(n.h.runtime, home=tmp_path / "fz")
    n.h.runtime.register("freeze", fz)
    # a kept wide grant: biz-a AND biz-b (the sweep keeps it during a biz-a freeze)
    _g, tok = n.svc.issue(subject={"id": "wide"}, workspaces=["biz-a", "biz-b"], ceiling=Effect.COMMIT)
    specs = {s.name: wrap_tool(s, n.h.runtime) for s in wf_tools.tool_specs(n.h.runtime)}
    return n, fz, tok, specs


def freeze(fz):
    fz.mark("workspace", "biz-a", reason="stop", by="owner")


def is_frozen(e: pytest.ExceptionInfo) -> bool:
    return isinstance(e.value, ToolCallError) and str(e.value).startswith("frozen:")


async def test_control_a_call_naming_the_workspace_is_refused(frozen):
    _n, fz, tok, specs = frozen
    freeze(fz)
    with bind_request_token(tok), pytest.raises(ToolCallError) as e:
        await specs["workflow_catalog"](workspace="biz-a")
    assert is_frozen(e)


async def test_a_kept_wide_grant_cannot_save_a_workflow_in_the_frozen_workspace(frozen):
    n, fz, tok, specs = frozen
    freeze(fz)
    assert fz.frozen_for(None, "biz-a") is not None
    with bind_request_token(tok), pytest.raises(ToolCallError) as e:
        await specs["workflow_save"](definition=workflow(name="saved during freeze"))
    assert is_frozen(e) and "biz-a is frozen" in str(e.value)
    assert not [w for w in n.wf.list(allowed=lambda ws: True) if w.get("name") == "saved during freeze"]


async def test_the_frozen_workspaces_workflows_are_left_out_and_unreadable(frozen):
    _n, fz, tok, specs = frozen
    with bind_request_token(tok):
        a = await specs["workflow_save"](definition=workflow(name="in biz-a"))
        b = await specs["workflow_save"](definition={**workflow(name="in biz-b"), "workspace": "biz-b"})
    freeze(fz)
    with bind_request_token(tok):
        listed = await specs["workflow_list"]()
    ids = {w["id"] for w in listed["workflows"]}
    assert b["id"] in ids and a["id"] not in ids
    assert "workspace biz-a is frozen" in listed["frozen"] and "—" not in listed["frozen"]
    for tool, args in (("workflow_get", {"id": a["id"]}), ("workflow_runs", {"id": a["id"]}),
                       ("workflow_run", {"id": a["id"]}), ("workflow_propose", {"id": a["id"]})):
        with bind_request_token(tok), pytest.raises(ToolCallError) as e:
            await specs[tool](**args)
        assert is_frozen(e), tool
    with bind_request_token(tok):  # the other workspace works as before, with no note
        got = await specs["workflow_get"](id=b["id"])
    assert got["id"] == b["id"] and "frozen" not in got


async def test_a_kept_wide_grant_cannot_propose_leads_to_a_frozen_workspace_list(frozen):
    _n, fz, tok, specs = frozen
    freeze(fz)
    with bind_request_token(tok), pytest.raises(ToolCallError) as e:
        await specs["leads_propose"](list=LIST, leads=[{"email": "x@acme.example",
                                                         "source": "https://www.linkedin.com/in/x"}])
    assert is_frozen(e)


async def test_control_before_the_freeze_the_same_calls_work(frozen):
    _n, _fz, tok, specs = frozen
    with bind_request_token(tok):
        out = await specs["leads_propose"](list=LIST, leads=[{"email": "x@acme.example",
                                                               "source": "https://www.linkedin.com/in/x"}])
    assert "frozen" not in out


def test_the_scope_helpers_hold_an_agent_off_a_frozen_workspace_and_a_person_not(frozen):
    """The one place: every helper a tool uses to reach a workspace asks the freeze. A listing of jobs
    (terminal_jobs) filters with
    can_access_lane, so it leaves a frozen lane's rows out."""
    n, fz, tok, _specs = frozen
    freeze(fz)
    p, g = n.svc.resolve(tok)
    agent = n.svc.context(g, principal=p, tool="terminal_jobs")
    assert not agent.can_access_lane("biz-a/social") and agent.can_access_lane("biz-b/social")
    assert not covers_workspace(agent, "biz-a") and covers_workspace(agent, "biz-b")
    assert not pattern_within(agent, "biz-a/*") and pattern_within(agent, "biz-b/*")
    assert not pattern_reaches(agent, "biz-a/*") and pattern_reaches(agent, "biz-b/*")
    with pytest.raises(Frozen):
        require_lanes(agent, ["biz-a/social"], Effect.READ)
    assert agent.frozen_hits
    person = n.svc.context(g, principal=p, tool="terminal_jobs", in_person=True)
    assert person.can_access_lane("biz-a/social") and covers_workspace(person, "biz-a")
    require_lanes(person, ["biz-a/social"], Effect.READ)
    assert person.frozen_hits == []


async def test_an_unfreeze_gives_the_workspace_back(frozen):
    n, fz, tok, _specs = frozen
    freeze(fz)
    await fz.unfreeze("workspace", "biz-a", by="owner")
    p, g = n.svc.resolve(tok)
    assert covers_workspace(n.svc.context(g, principal=p, tool="workflow_list"), "biz-a")


# ---- the registry: every tool that reaches a workspace without a lane id or a top-level workspace ----

# How the freeze reaches each tool whose lane or workspace argument is missing or optional. One of:
#   id      it resolves an id (job, session, entry, run, task, approval, item) to its lane or workspace
#           and authorizes that lane (the freeze gate in authorize) or calls refuse_frozen/covers_workspace
#   filter  it lists through ctx.can_access_lane, covers_workspace or pattern_reaches: frozen rows are
#           left out, and a dict result says so (`frozen`)
#   check   it checks what it will act on with covers_workspace, require_lanes, pattern_within or
#           can_access_lane before it acts: a frozen one fails with `frozen`
#   none    it reaches no workspace's data (the node's own catalogs, the caller's own grant, a question)
FREEZE_REACH = {
    "agenda_cancel": "id", "agenda_get": "id", "agenda_propose": "id", "agenda_update": "id",
    "agenda_draft": "check", "agenda_list": "filter",
    "approval_confirm": "id", "approval_wait": "id",  # without an id it lists (frozen_off)
    "ask_first_on": "id",
    "brain_cancel": "id", "brain_resume": "id", "brain_status": "id", "brain_tail": "id",
    "brain_tasks": "filter", "brain_use": "check", "brains_list": "none",
    "connectors_list": "filter",
    # openajar.environments.tools: the chat's environment or an id of the grant family -> its lanes
    # (refuse_frozen); a new one authorizes env/<id>; the listing leaves frozen ones out (frozen_off)
    "environment": "id", "environment_close": "id", "environment_open": "check", "environment_save": "id", "environment_port": "id", "environment_pull_request": "id",
    "environment_forget": "check", "environment_checkpoint": "id", "environment_rollback": "id",  # checkpoint_tools: _find
    "environment_app_restart": "id",
    "terminals_list": "filter", "events_list": "filter", "triggers_list": "filter", "trigger_propose": "check",
    "flow_get": "id", "flow_run": "id", "flow_save": "check", "flows_list": "filter",
    "grant_info": "filter", "howto": "none", "human_ask": "none", "image_build": "none", "images_list": "none",
    "browser_check": "filter", "terminal_job_cancel": "id", "terminal_job_read": "id", "terminal_jobs": "filter",
    "browsers_list": "filter", "leads_propose": "check", "mail_mailboxes": "filter",
    "memory_forget": "check", "memory_get": "check",  # openajar.memory: a frozen workspace reads as not found
    "plugins_list": "filter",
    # openajar.profiles: _check_reach / _sees go through covers_workspace and can_access_lane
    "profile_get": "check", "profile_list": "filter", "profile_save": "check", "profile_sync_status": "check",
    "profile_update": "check", "profile_use": "check", "profile_propose": "check",
    "profile_learn_brand": "check",
    "run_cancel": "id", "run_status": "id",
    # openajar.environments.screen: `on` (filled by routing) names the environment, desktop_of calls refuse_frozen
    # on its lanes and every call authorizes lane_id=on
    "screen_act": "id", "screen_open": "id", "screen_snapshot": "id",
    "shell_close": "id", "shell_read": "id", "shell_write": "id",
    "site_api_propose": "check",
    # openajar.studio.tools: a URL, not a lane; a named or matched profile through covers_workspace and
    # can_access_lane (_sees), runs and captures are studio's own store
    "finding_get": "none", "studio_status": "none", "system_check": "check", "ui_baseline": "check",
    "ui_check": "check", "ui_diff": "check", "ui_image": "none", "site_api_try": "id", "site_apis_list": "filter",
    "traces_query": "filter", "vault_list": "filter", "vms_list": "filter", "whoami": "none",
    "workflow_catalog": "filter", "workflow_discard": "id", "workflow_get": "id", "workflow_list": "filter", "workflow_propose": "id",
    "workflow_retry": "id", "workflow_run": "id", "workflow_runs": "id", "workflow_save": "check",
    "workspace_apply": "check", "workspace_list": "filter", "workspace_plan": "check", "workspace_queue": "filter",
}

# What a module must use for its tools to be reached by the freeze (the freeze-aware helpers).
FREEZE_AWARE = ("can_access_lane", "covers_workspace", "pattern_reaches", "pattern_within", "require_lanes",
                "require_lane_patterns", "refuse_frozen", "frozen_off", "lane_id=", "rules.caller")


@pytest.fixture(scope="module")
def specs() -> list[ToolSpec]:
    runtime = Runtime(config=NodeConfig(home=Path(tempfile.mkdtemp(prefix="openajar-freeze-reach-"))))
    providers = [*tool_providers(), *feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))]
    return [spec for provider in providers for spec in provider(runtime) if isinstance(spec, ToolSpec)]


def _reaches_without_naming(spec: ToolSpec) -> bool:
    """No required lane argument and no required top-level workspace: the wrapper's freeze check sees
    no scope, so the tool must resolve it itself."""
    sig = inspect.signature(spec.handler).parameters
    lane = spec.lane_arg is not None and sig[spec.lane_arg].default is inspect.Parameter.empty
    ws = "workspace" in sig and sig["workspace"].default is inspect.Parameter.empty
    return not lane and not ws


def test_every_tool_that_reaches_a_workspace_another_way_says_how_the_freeze_reaches_it(specs):
    need = {s.name for s in specs if _reaches_without_naming(s)}
    unlisted = sorted(need - set(FREEZE_REACH))
    assert not unlisted, (
        f"these tools take no lane id or top-level workspace: {unlisted}. Make each reach what it acts on or "
        "returns through the freeze-aware helpers (ctx.can_access_lane, covers_workspace, require_lanes, "
        "pattern_within, refuse_frozen, frozen_off) and add it to FREEZE_REACH with how")
    stale = sorted(set(FREEZE_REACH) - need)
    assert not stale, f"stale FREEZE_REACH entries (gone, or they take a lane or workspace now): {stale}"


def test_a_tool_that_reaches_a_workspace_uses_a_freeze_aware_helper(specs):
    lacking = []
    for s in specs:
        how = FREEZE_REACH.get(s.name)
        if how in (None, "none"):
            continue
        source = inspect.getsource(inspect.getmodule(s.handler))
        if not any(marker in source for marker in FREEZE_AWARE):
            lacking.append(s.name)
    assert not lacking, f"no freeze-aware helper in the module of: {lacking}"
