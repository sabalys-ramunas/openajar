"""Every tool says which argument names its lane, or why it has none.

The policy precheck (openajar/policy/wrap.py) authorizes the lane a call targets from
`spec.lane_arg` alone. A tool with `lane_arg=None` gets no lane check before its handler runs, so
it must either take no lane at all or call `ctx.authorize(..., lane_id=...)` itself. Nothing in
the registry enforces that, so these tests do: a new tool with a lane-like argument must declare
`lane_arg`, and a new tool without one must be listed below with how it stays inside the grant.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from openajar.core.config import NodeConfig
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec
from openajar.mcp.providers import feature_providers, tool_providers
from openajar.plugins.entries import PLUGINS
from openajar.policy.manifest import handler_params

# Argument names that hold a lane id (or a surface or mailbox id that is authorized as one).
LANE_PARAMS = frozenset({"on", "lane", "lanes", "lane_id", "surface", "mailbox"})

# Tools with a lane-like argument and lane_arg=None: each authorizes its lane in the handler.
SELF_AUTHORIZED_LANE_PARAM = {
    # `lanes` is the list a brain may drive, not one lane to precheck: the handler authorizes
    # BRAIN for the workspace, and minting the child token refuses any lane outside the parent's
    # grant (brains/service.py mint_child -> policy/service.py lane_allowed).
    "brain_run": "lanes",
    # `on` is an optional filter; the handler calls ctx.authorize(READ, lane_id=on).
    "vault_list": "on",
    # `on` is optional and defaults to the API's first surface; the handler authorizes the
    # resolved surface with ctx.authorize(effect, lane_id=target.surface.id).
    "site_api_try": "on",
    # `on` is an optional filter; the handler calls ctx.authorize(READ, lane_id=on) and filters the
    # list with ctx.can_access_lane.
    "terminal_jobs": "on",
    # `on` beside a job id (agents pass it on every call): routing makes an environment's handle its terminal
    # lane (the lease is checked there); the handler authorizes the job's lane and refuses another `on`
    "terminal_job_read": "on", "terminal_job_cancel": "on",
    # `lane` only narrows a list already filtered by flow_seen (ctx.can_access_lane); it acts on nothing.
    "flows_list": "lane",
    # `on` names one of the caller's environments by its handle (or a session's own id or env/<id>), found
    # only in its grant family and only when the caller holds it (EnvironmentManager.reach); refuse_frozen
    # on its lanes (openajar.environments.tools._find)
    "environment": "on", "environment_close": "on", "environment_save": "on", "environment_port": "on",
    "environment_pull_request": "on", "environment_checkpoint": "on", "environment_rollback": "on",
    "environment_app_restart": "on",
    # `on` is an environment's handle: openajar.environments.routing.studio_proxy reaches it (the lease) and
    # points the review browser at its tunnel; the check acts on no lane (a review browser is no lane)
    "ui_check": "on", "ui_diff": "on", "system_check": "on", "ui_baseline": "on",
}

# Tools with lane_arg=None whose lane comes from another argument: the handler resolves it and
# authorizes or checks it itself.
SELF_AUTHORIZED_INDIRECT = {
    "shell_write": "session -> lane; ctx.authorize(EXEC, lane_id=s.lane_id)",
    "shell_read": "session -> lane; ctx.authorize(READ, lane_id=s.lane_id)",
    "shell_close": "session -> lane; ctx.authorize(EXEC, lane_id=s.lane_id)",
    "workspace_plan": "every planned lane checked with ctx.can_access_lane (_check_grant)",
    "git_remotes": "workspace -> pattern_reaches(ws/*), then lane-scoped remotes filtered by ctx.can_access_lane",
    "workspace_apply": "every planned lane checked with ctx.can_access_lane, then COMMIT is approved",
    "trigger_propose": "target and grant lanes checked with ctx.can_access_lane; DRAFT on the first",
    "flow_save": "definition.lane; ctx.authorize(DRAFT, lane_id=lane) unless it is a pattern",
    "flow_get": "the saved flow's lane (seen_flow); ctx.authorize(READ, lane_id=lane) unless it is a pattern",
    "workflow_save": "each step names its account; the run authorizes every step's lane",
    "workflow_run": "each step authorizes its own account (runner/workflows/engine.py)",
    "workflow_retry": "re-runs steps that authorize their own account",
    "workflow_propose": "a draft for a person to save; no lane is touched",
    "workflow_discard": "removes the caller's own never-run draft; no lane is touched",
    "mail_mailboxes": "mailbox ids filtered with ctx.can_access_lane",
    "connectors_list": "results filtered with ctx.can_access_lane",
    "events_list": "events filtered per lane with ctx.can_access_lane",
    "agenda_get": "entry id -> its lane (or workspace); _entry checks it, ctx.authorize(READ, lane_id=acts_on)",
    "agenda_update": "entry id -> its lane; ctx.authorize(DRAFT, lane_id=acts_on)",
    "agenda_propose": "entry id -> its lane or workspace; _entry checks it with ctx.can_access_lane",
    "agenda_cancel": "entry id -> its lane; ctx.authorize(DRAFT, lane_id=acts_on)",
    "agenda_draft": "action.on -> lane; ctx.authorize(DRAFT, lane_id=probe.acts_on)",
    "vms_list": "VM records filtered with ctx.can_access_lane",
    "profile_list": "profiles filtered with covers_workspace or ctx.can_access_lane on their accounts (_sees)",
    "profile_save": "a draft; needs a grant over the whole workspace (_check_reach), its accounts must be in it",
    "profile_update": "a draft; needs a grant over the whole workspace (_check_reach), its accounts must be in it",
    "profile_propose": "a draft; needs a grant over the whole workspace (_check_reach)",
    "profile_learn_brand": "a draft; needs a grant over the whole workspace (_check_reach); opens a url, no lane",
    "profile_use": "reads the approved profile; _check_reach: covers_workspace or ctx.can_access_lane on its accounts",
    # openajar.environments.tools: the lane is the one the node picks (env/<id>), or the chat's environment's
    "environment_open": "plans env/<id>; ctx.precheck(VM, lane_id=env/<id>), ctx.authorize(READ, lane_id=env/<id>)",
    "environment": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_close": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_save": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_checkpoint": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_rollback": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_port": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_pull_request": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    "environment_app_restart": "the picked environment, or `on` in the grant family; refuse_frozen on its lanes",
    # saves of the caller's grant family by title (Saving.forget), no lane: the machine is gone
    "environment_forget": "saves of the grant family only (mgr.forget(family, title)); refuse_frozen on their lanes",
}

# Tools with lane_arg=None that touch no lane: approvals, brains, memory, runs, catalogs and
# the caller's own grant. Workspace-scoped ones check the workspace in the handler.
NO_LANE = frozenset({
    "approval_confirm", "approval_wait",
    "brain_cancel", "brain_resume", "brain_status", "brain_tail", "brain_tasks", "brain_use", "brains_list",
    "terminals_list", "grant_info", "howto", "human_ask", "image_build", "images_list",
    "browsers_list", "leads_propose", "memory_context", "memory_forget", "memory_get", "memory_search",
    "memory_write", "plugins_list", "run_cancel", "run_status", "send_suppress", "site_api_propose",
    "site_apis_list", "triggers_list", "whoami", "workflow_catalog", "workflow_get", "workflow_list",
    "workflow_runs", "workspace_list", "workspace_queue",
    # studio: a URL in the review browser, never a lane (openajar.studio.tools); a profile is read only
    # when the grant reaches its workspace
    "finding_get", "studio_status", "ui_image",
})


@pytest.fixture(scope="module")
def specs() -> list[ToolSpec]:
    """Every tool this build serves, plugins included (the list `openajar tools` prints)."""
    runtime = Runtime(config=NodeConfig(home=Path(tempfile.mkdtemp(prefix="openajar-lane-args-"))))
    providers = [*tool_providers(), *feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))]
    return [spec for provider in providers for spec in provider(runtime) if isinstance(spec, ToolSpec)]


def params(spec: ToolSpec) -> list[str]:
    return [p.name for p in handler_params(spec)]


def test_the_registry_is_not_empty(specs):
    assert len(specs) > 50


def test_a_declared_lane_arg_is_one_of_the_tools_arguments(specs):
    wrong = {s.name: s.lane_arg for s in specs if s.lane_arg is not None and s.lane_arg not in params(s)}
    assert not wrong, f"lane_arg names no argument of the tool, so the precheck never sees a lane: {wrong}"


def test_a_lane_like_argument_is_the_lane_arg_or_listed(specs):
    missing = {}
    for s in specs:
        lane_like = sorted(LANE_PARAMS & set(params(s)))
        if s.lane_arg is None and lane_like and SELF_AUTHORIZED_LANE_PARAM.get(s.name) not in lane_like:
            missing[s.name] = lane_like
    assert not missing, (
        "these tools take a lane but set lane_arg=None, so policy/wrap.py prechecks no lane: "
        f"{missing}. Set lane_arg, or authorize the lane in the handler and add the tool to "
        "SELF_AUTHORIZED_LANE_PARAM with how it does that.")


def test_every_tool_without_a_lane_arg_is_accounted_for(specs):
    known = set(SELF_AUTHORIZED_LANE_PARAM) | set(SELF_AUTHORIZED_INDIRECT) | NO_LANE
    unlisted = sorted(s.name for s in specs if s.lane_arg is None and s.name not in known)
    assert not unlisted, (
        f"new tools with lane_arg=None: {unlisted}. If the tool acts on a lane, set lane_arg (or "
        "authorize the lane in the handler and add it to SELF_AUTHORIZED_INDIRECT); if it touches "
        "no lane, add it to NO_LANE.")


def test_the_lists_name_only_tools_that_exist_without_a_lane_arg(specs):
    without = {s.name for s in specs if s.lane_arg is None}
    listed = set(SELF_AUTHORIZED_LANE_PARAM) | set(SELF_AUTHORIZED_INDIRECT) | NO_LANE
    assert not listed - without, f"stale entries (gone, or they declare lane_arg now): {sorted(listed - without)}"
    assert not (NO_LANE & set(SELF_AUTHORIZED_INDIRECT)) and not (NO_LANE & set(SELF_AUTHORIZED_LANE_PARAM))
