"""Toolsets: a small default tool list, more on demand, and the same checks on every path."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_policy_support import OWNER, SPECS, Clock, make_service

from openajar.core.config import NodeConfig
from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec, bind_request_token
from openajar.mcp.http import build_app
from openajar.mcp.providers import feature_providers, tool_providers
from openajar.mcp.registry import build_mcp
from openajar.mcp.toolsets import (
    CORE,
    NAMES,
    OTHER,
    TOOLSETS,
    Loaded,
    bind_connection,
    parse_toolsets,
    toolset_of,
)
from openajar.node import daemon
from openajar.node.tools import INSTRUCTIONS, RECIPES
from openajar.plugins.entries import PLUGINS
from openajar.policy import bind_request_token as bind_policy_token
from openajar.policy import visible_tools, wrap_tool

TOKEN = "toolsets-node-token-0123456789"

# The default surface's budget. Raise it only on purpose: every tool and every word in it is
# read by every connected agent on every turn.
CORE_MAX_TOOLS = 30  # the environment loop (28) and the two meta tools: docs/design/tool-surface-review.md
# approximate: characters of the tools/list JSON / 4. 6,600 since the agent text review (act one at
# a time, required arguments, call whoami and browsers_list once): on lanebench it cut calls per task
# by a fifth, which saves far more than these 100 tokens. 7,600 with the three environment tools and the
# optional `on` of the browser tools (an agent's own computer is the product: docs/design/environments.md).
# 8,400 with terminal_exec, terminal_job_start, terminal_job_read and terminal_job_cancel (about 830 tokens): a chat with an
# environment otherwise loads the terminals toolset, about 3,450 tokens on every later turn, so this is
# cheaper as soon as one chat in four opens an environment, and the agent finds its terminal without guessing
# 8,500 with environment_open's sizes, `bigger` and repo_not_connected (about 70 tokens after a trim): a real
# chat got a 2 CPU, 4 GB machine for a type check that needs 12 GB and did not know it could ask for more
# 9,150 with file_read, file_write and file_edit (about 510 tokens) and browser_screenshot's
# proof and width (about 100): coding is what an environment is for, and without file tools an agent writes
# code through shell quoting (heredocs, sed), which breaks on the first quote and costs a retry each time;
# loading them from a toolset would cost a tools_more in nearly every environment chat
# 9,250 after the 2026-11-15 node test fixes (about 70 tokens): environment_close says when the VM is really
# deleted and lists deleted branches, and environment_open's next line says which pushes are refused, so an
# agent tells the human the truth about the VM and does not try to rewrite a colleague's branch
# 9,350 for keeping environments (2026-11-15, about 80 tokens): environment_open's `keep` and
# environment_close's `final` and `saved` are the owner's three modes (single use, kept live, saved and
# resumed by title), which an agent must see in every environment chat to choose one
# 10,050 for environment_save, environment_upload, environment_download and environment_forget in core
# (2026-11-16, about 740 tokens, less about 60 cut from the environment tools' own descriptions): all three
# testers of 2026-11-15 needed one of them, none found them behind tools_call (they had no schema there),
# and one guessed the arguments blind. `on` now also takes an environment's title, which every
# terminal and browser tool says in a few words. 10,150 when builder I3 merged (2026-11-16): environment_open
# takes signins and a title found by repo, environment_close a note and forget, and environment() lists the
# open and saved ones, so an agent stops for the day, finishes and comes back in one call each
# 8,370 since 2026-11-19 (the tool surface review, the owner's decision): the default list is the
# environment loop, the account browser loop (15 tools) is the accounts toolset, the pull request,
# checkpoints, the port and browser_record are in it, and the shared browser tools lost the account
# rules. Measured 8,201 for 30 tools, plus about 2%.
# 7,100 since the review's phase 2 the same day (2026-11-19): three merges (browser_tabs selects,
# browser_dialog answers, approval_wait reads without waiting), a short `on` with no account browser in
# it, no "Effect:" lines (the annotations carry it), no "Errors:" lists (each error's own fix line says it)
# and no lists of returned fields the result shows anyway. `on` stays "The on environment_open gave":
# "From environment_open." saved 50 tokens and a live Haiku run then left `on` out three times and passed
# the open's id once. Measured 7,013 for 30 tools, plus about 2%.
# 7,175 for environment_port's ports and same_numbers (2026-11-22, about 25 tokens, its description cut to make
# room): a page whose code calls its API on another localhost port broke silently behind a one-port link
CORE_MAX_TOKENS = 7175

CORE_TOOLS = {
    "environment_open", "environment", "environment_save", "environment_close", "environment_upload",
    "environment_download", "terminal_exec", "terminal_job_start", "terminal_job_read", "terminal_job_cancel",
    "file_read", "file_write", "file_edit", "browser_navigate", "browser_snapshot", "browser_act", "browser_wait",
    "browser_screenshot", "browser_tabs", "browser_dialog", "browser_wait_handback", "browser_record",
    "environment_pull_request", "environment_checkpoint", "environment_rollback", "environment_port", "login",
    "approval_wait",
}
META = {"tools_more", "tools_call"}


class EveryService(Runtime):
    """A runtime that says every service is there, so every built-in tool is listed (as on a
    node with every feature installed). Listing only: nothing here is called."""

    def has(self, name: str) -> bool:
        return True


def every_spec() -> tuple[Runtime, list[ToolSpec]]:
    """Every built-in tool, the shipped workers' tools (browser_task) included."""
    runtime = EveryService(config=NodeConfig(home=Path(tempfile.mkdtemp(prefix="openajar-toolsets-"))))
    worker_modules = [m for plugin in PLUGINS for m in plugin.tools]
    providers = [*tool_providers(), *feature_providers(worker_modules)]
    return runtime, [s for p in providers for s in p(runtime) if isinstance(s, ToolSpec)]


def node_server(toolsets=None):
    runtime, specs = every_spec()
    return build_mcp(runtime, [lambda _rt: specs], toolsets=toolsets)


def tokens(tools) -> int:
    return sum(len(json.dumps(t.model_dump(mode="json", by_alias=True, exclude_none=True))) for t in tools) // 4


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


# ---- the catalog ----

def test_every_built_in_tool_has_a_toolset_and_every_named_tool_exists():
    _, specs = every_spec()
    homeless = sorted(s.name for s in specs if toolset_of(s) == OTHER)
    assert homeless == [], f"put these in a toolset (openajar.mcp.toolsets.TOOLSETS): {homeless}"
    names = {s.name for s in specs}
    stale = sorted(t for ts in TOOLSETS for t in ts.tools if t not in names)
    assert stale == [], f"toolsets name tools that do not exist: {stale}"
    named = [t for ts in TOOLSETS for t in ts.tools]
    assert len(named) == len(set(named)), "a tool is named by two toolsets"
    assert {ts.name for ts in TOOLSETS if ts.name == CORE} == {CORE} and len(set(NAMES)) == len(NAMES)
    assert all("—" not in ts.summary for ts in TOOLSETS)


def test_the_server_instructions_name_every_toolset():
    missing = [name for name in NAMES if name != CORE and name not in INSTRUCTIONS]
    assert missing == [], f"say what these toolsets hold in openajar.node.tools.INSTRUCTIONS: {missing}"


def test_recipes_say_how_to_reach_tools_outside_core():
    """A recipe that names a tool outside core says which toolset holds it, or how to run it."""
    _, specs = every_spec()
    names = {s.name for s in specs}
    for topic, recipe in RECIPES.items():
        named = {w for w in re.findall(r"\b([a-z]+_[a-z_]+)\(", recipe["steps"]) if w in names}
        outside = sorted(named - CORE_TOOLS - META)
        if outside:
            steps = recipe["steps"]
            assert "tools_more" in steps or "toolset" in steps or "tools_call" in steps, (topic, outside)


async def test_core_descriptions_say_where_a_tool_outside_core_is():
    """A fresh agent sees only core: a core tool that names another tool says which toolset holds it."""
    _, specs = every_spec()
    home = {spec.name: toolset_of(spec) for spec in specs}
    for tool in await node_server(["core"]).list_tools():
        props = (tool.input_schema or {}).get("properties", {})
        text = " ".join([tool.description or "", *(str(p.get("description", "")) for p in props.values())])
        for name in set(re.findall(r"\b[a-z]+_[a-z_]+\b", text)) & set(home):
            if home[name] != CORE:
                assert f"{home[name]} toolset" in text, f"{tool.name} names {name}: say it is in the {home[name]} toolset"


def test_dynamic_tools_land_by_tag():
    async def op(ctx, *, on: str) -> dict:
        """A connector operation."""
        return {}

    assert toolset_of(ToolSpec(name="instagram_post_photo", handler=op, tags=("connectors", "connector"))) == "connectors"
    assert toolset_of(ToolSpec(name="flow_biz_a_post", handler=op, tags=("flows", "flow"))) == "flows"
    assert toolset_of(ToolSpec(name="mystery", handler=op)) == OTHER


def test_parse_toolsets():
    assert parse_toolsets(None) == {CORE}
    assert parse_toolsets("browser, flows") == {CORE, "browser", "flows"}
    assert parse_toolsets(["FILES"]) == {CORE, "browser"}  # the browser toolset's name before 2026-11-19
    assert parse_toolsets("accounts,full") is None and parse_toolsets(["all"]) is None
    assert parse_toolsets("accounts,nonsense") == {CORE, "accounts"}


def test_node_config_checks_the_toolset_names(tmp_path):
    assert NodeConfig().mcp_toolsets == ["core"]
    assert NodeConfig(mcp_toolsets=["core", "accounts"]).mcp_toolsets == ["core", "accounts"]
    NodeConfig(mcp_toolsets=["core", "files"])  # the old name of the browser toolset still loads
    NodeConfig(mcp_toolsets=["full"])
    with pytest.raises(ValueError, match="mcp_toolsets may list only"):
        NodeConfig(mcp_toolsets=["core", "filez"])


def test_meta_tool_names_are_reserved():
    async def h(ctx) -> dict:
        """x"""
        return {}

    with pytest.raises(ValueError, match="reserved"):
        build_mcp(Runtime(), [lambda _rt: [ToolSpec(name="tools_call", handler=h, lane_arg=None)]])


# ---- the default surface ----

async def test_the_default_list_is_the_core_loop_and_the_meta_tools():
    server = node_server(NodeConfig().mcp_toolsets)
    names = {t.name for t in await server.list_tools()}
    assert names == CORE_TOOLS | META


async def test_the_default_surface_stays_within_its_budget():
    """The guard: fails when the default list grows past its tool count or token budget."""
    full = await node_server().list_tools()
    core = await node_server(["core"]).list_tools()
    assert len(core) <= CORE_MAX_TOOLS, f"{len(core)} tools by default (budget {CORE_MAX_TOOLS})"
    assert tokens(core) <= CORE_MAX_TOKENS, f"about {tokens(core)} tokens by default (budget {CORE_MAX_TOKENS})"
    assert tokens(core) * 3 < tokens(full)  # the point of it all


async def test_full_is_every_tool_without_meta_tools():
    _, specs = every_spec()
    for server in (node_server(), node_server(["full"]), node_server("core,all")):
        names = {t.name for t in await server.list_tools()}
        assert names == {s.name for s in specs}
    server = node_server(["core"])
    with bind_connection(session=None, query_string=b"toolsets=full"):
        assert {t.name for t in await server.list_tools()} == {s.name for s in specs}


async def test_the_url_asks_for_its_own_toolsets():
    server = node_server(["core"])
    with bind_connection(session=None, query_string=b"toolsets=files,flows"):
        names = {t.name for t in await server.list_tools()}
    assert {"browser_files", "browser_scroll", "flow_run", "flows_list"} <= names
    assert CORE_TOOLS | META <= names and "brain_run" not in names
    server = node_server()  # full by default: the URL still narrows it
    with bind_connection(session=None, query_string=b"toolsets=core"):
        assert {t.name for t in await server.list_tools()} == CORE_TOOLS | META


# ---- loading more ----

async def test_tools_more_lists_toolsets_and_loads_one_into_the_session():
    server = node_server(["core"])
    with bind_request_token("t1"), bind_connection(session="s1"):
        overview = (await server.call_tool("tools_more", {})).structured_content
        rows = {row["toolset"]: row for row in overview["toolsets"]}
        assert set(rows) == set(NAMES) and rows["core"]["loaded"] and not rows["browser"]["loaded"]
        assert "browser_files" in rows["browser"]["tools"] and "browsers_list" in rows["accounts"]["tools"]

        result = await server.call_tool("tools_more", {"toolset": "browser"})
        data = result.structured_content
        assert data["toolset"] == "browser" and "list_changed" in data["next"]
        put = next(t for t in data["tools"] if t["name"] == "browser_files")
        assert put["description"] and "on" in put["input_schema"]["properties"]
        names = {t.name for t in await server.list_tools()}
        assert {"browser_files", "browser_scroll"} <= names and "brain_run" not in names

        # a tool's name loads its toolset
        assert (await server.call_tool("tools_more", {"toolset": "brain_run"})).structured_content["toolset"] == "brains"
        assert "brain_run" in {t.name for t in await server.list_tools()}
        again = (await server.call_tool("tools_more", {"toolset": "browser"})).structured_content
        assert again["next"] == "These tools are already in your list."

    # another session, or the same session id with another token, still has the default
    with bind_request_token("t1"), bind_connection(session="s2"):
        assert {t.name for t in await server.list_tools()} == CORE_TOOLS | META
    with bind_request_token("t2"), bind_connection(session="s1"):
        assert {t.name for t in await server.list_tools()} == CORE_TOOLS | META


async def test_without_a_session_tools_more_says_to_use_tools_call():
    server = node_server(["core"])
    data = (await server.call_tool("tools_more", {"toolset": "files"})).structured_content
    assert data["next"] == "Call them with tools_call(name, arguments)."
    assert "browser_files" not in {t.name for t in await server.list_tools()}


async def test_tools_more_refuses_unknown_names():
    server = node_server(["core"])
    result = await server.call_tool("tools_more", {"toolset": "nope"})
    assert result.is_error and text_of(result).startswith("invalid: toolset: no toolset or tool of yours named 'nope'")
    assert (await server.call_tool("tools_more", {"toolset": 5})).is_error


def test_loaded_sessions_are_bounded():
    loaded = Loaded(limit=2)
    assert not loaded.add("t", None, "files") and loaded.get("t", None) == frozenset()
    for session in ("a", "b", "c"):
        assert loaded.add("t", session, "files")
    assert loaded.get("t", "a") == frozenset() and loaded.get("t", "c") == {"files"}


# ---- the same checks on every path ----

@contextmanager
def as_caller(token):
    """Bind the token as openajar.mcp.http does on the node: for the registry and for the policy."""
    with bind_request_token(token), bind_policy_token(token):
        yield


@pytest.fixture
def policed(tmp_path):
    """The real policy on four small tools, none of them in a named toolset (so all in `other`)."""
    svc, rt = make_service(tmp_path, clock=Clock())
    server = build_mcp(rt, [lambda _rt: SPECS], wrap_tool, visible=lambda tok, specs: visible_tools(tok, specs, rt),
                       toolsets=["core"])
    yield svc, server
    svc.close()


async def test_discovery_shows_only_what_the_grant_may_use(policed):
    svc, server = policed
    _, read_only = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.READ)
    with bind_request_token(read_only):
        assert {t.name for t in await server.list_tools()} == META
        overview = (await server.call_tool("tools_more", {})).structured_content
        assert overview["toolsets"] == [{"toolset": "other", "summary": "tools no other toolset holds",
                                         "loaded": False, "tools": ["lane_read", "node_info"]}]
        hidden = await server.call_tool("tools_more", {"toolset": "lane_post"})
        assert hidden.is_error and "lane_post" in text_of(hidden)
    with bind_request_token(OWNER):
        rows = (await server.call_tool("tools_more", {})).structured_content["toolsets"]
        assert {row["toolset"]: set(row["tools"]) for row in rows} == {
            "core": {"terminal_exec"}, "other": {"lane_read", "lane_post", "node_info"}}
    with bind_request_token("not-a-token"):
        assert (await server.call_tool("tools_more", {})).structured_content["toolsets"] == []


async def test_tools_call_runs_the_same_checks_as_a_direct_call(policed):
    svc, server = policed
    _, read_only = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.READ)
    _, drafter = svc.issue(subject={"id": "agent-2"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)

    async def both(name, args):
        direct = await server.call_tool(name, args)
        routed = await server.call_tool("tools_call", {"name": name, "arguments": args})
        return direct, routed

    with as_caller(read_only):
        direct, routed = await both("lane_read", {"on": "biz-a/insta"})
        assert not routed.is_error and routed.structured_content == direct.structured_content == {
            "on": "biz-a/insta", "who": "agent-1"}
        for name, args in [("lane_post", {"on": "biz-a/insta", "caption": "hi"}),  # above the grant: hidden
                           ("lane_read", {"on": "biz-b/insta"}),  # out of scope
                           ("lane_read", {}),  # bad arguments
                           ("nope", {})]:
            direct, routed = await both(name, args)
            assert direct.is_error and routed.is_error and text_of(direct) == text_of(routed), name
    with as_caller(drafter):  # sees lane_post, whose handler asks for commit: above the ceiling
        direct, routed = await both("lane_post", {"on": "biz-a/insta", "caption": "hi"})
        assert direct.is_error and text_of(direct) == text_of(routed)
    with as_caller(None):
        direct, routed = await both("node_info", {})
        assert direct.is_error and routed.is_error

    traced = [t.tool for t in svc.traces.query(limit=500)[0]]
    assert "tools_call" not in traced and "lane_read" in traced  # the trail names the real tool


async def test_tools_call_refuses_bad_shapes_and_itself(policed):
    _, server = policed
    with as_caller(OWNER):
        for args in ({}, {"name": ""}, {"name": "lane_read", "arguments": "on=x"},
                     {"name": "tools_call", "arguments": {"name": "lane_read"}}, {"name": "tools_more"}):
            result = await server.call_tool("tools_call", args)
            assert result.is_error and text_of(result).startswith("invalid:"), args


# ---- over HTTP, as Claude Code connects ----

async def test_a_real_session_loads_a_toolset_and_hears_list_changed(tmp_path):
    config = NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())
    lanes = FakeLaneManager(config)
    runtime = Runtime(config=config, lanes=lanes, interaction=FakeInteraction())
    mcp = build_mcp(runtime, tool_providers(), toolsets=config.mcp_toolsets)
    server = daemon._Uvicorn.server(build_app(runtime, mcp), config)
    task = asyncio.create_task(server.serve())
    heard: list[str] = []

    async def on_message(message):
        method = getattr(getattr(message, "root", message), "method", None)
        if isinstance(method, str):
            heard.append(method)

    try:
        while not server.started:
            await asyncio.sleep(0.02)
        url = f"http://localhost:{daemon.bound_port(server)}/mcp"
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(url, http_client=http), mode="legacy", message_handler=on_message) as client,
        ):
            assert client.server_capabilities.tools.list_changed
            before = {t.name for t in (await client.list_tools()).tools}
            assert "browser_files" not in before and {"browser_snapshot", "tools_more", "tools_call"} <= before
            loaded = await client.call_tool("tools_more", {"toolset": "browser"})
            assert not loaded.is_error and "list_changed" in loaded.structured_content["next"]
            after = {t.name for t in (await client.list_tools()).tools}
            assert after - before == {"browser_file_get", "browser_files", "browser_file_delete", "browser_scroll"}
            routed = await client.call_tool("tools_call", {"name": "browsers_list", "arguments": {}})
            assert not routed.is_error
        assert "notifications/tools/list_changed" in heard

        async with (  # the URL can ask for everything, as before toolsets
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(url + "?toolsets=full", http_client=http), mode="legacy") as client,
        ):
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"browser_file_put", "browser_control", "browser_tabs"} <= names and not names & META
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
