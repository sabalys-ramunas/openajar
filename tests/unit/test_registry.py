"""The tool registry: ToolSpecs on the MCP SDK, one wrapper per call, errors with codes."""

from __future__ import annotations  # handler hints are strings here, as in most feature packages

import base64
import json
import logging
from typing import Annotated, Any

import pytest
from fakes import PNG_1X1, FakeInteraction, FakeLaneManager, LegacyLaneManager
from mcp import Client
from pydantic import BaseModel, Field

from openajar.core.config import NodeConfig, save_proxies
from openajar.core.errors import (
    AgentPaused,
    ApprovalPending,
    HumanInControl,
    InvalidRequest,
    LaneBusy,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.models import ActionKind, Effect, LaneSpec, Principal, ProxyConfig
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolImage, ToolSpec, bind_request_token, current_request_token
from openajar.mcp.registry import (
    LANE_ARG_DESCRIPTION,
    OwnerContext,
    allow_all_wrap,
    build_mcp,
    lean_schema,
    split_docstring,
    tool_params,
)
from openajar.node.tools import tool_specs as node_tool_specs
from openajar.runner.fakes import Harness, post_flow
from openajar.runner.tools import tool_specs as runner_tool_specs

PASSWORD = "pw-Zx81-secret"
USERNAME = "customer-acme-zone-isp"


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


async def listed(server) -> dict[str, Any]:
    async with Client(server) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


async def call(server, name: str, args: dict | None = None):
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


def provider(*specs: ToolSpec):
    return lambda runtime: list(specs)


def node_runtime(config, lanes, interaction) -> Runtime:
    return Runtime(config=config, lanes=lanes, interaction=interaction)


# ---- handlers used as tools ----

async def rich(
    ctx,
    *,
    on: str,
    count: int = 3,
    flag: bool | None = None,
    action: ActionKind = ActionKind.CLICK,
    labels: list[str] | None = None,
    note: Annotated[str, Field(description="A note from the field.")] = "",
) -> dict:
    """Do a rich thing on a lane, with a description that
    wraps over two lines.

    Args:
        count: How many times, at most
            ten.
        flag (bool): Whether to flag it.
        note: Ignored, the Field description wins.

    Returns the count.
    """
    return {"on": on, "count": count, "flag": flag, "action": action.value, "labels": labels, "note": note}


async def plain(ctx) -> dict:
    """A plain tool."""
    return {"principal": ctx.principal.kind}


async def echo(ctx, *, value: str) -> Any:
    """Echo a value."""
    return value


def runtime_with(**services: Any) -> Runtime:
    return Runtime(config=NodeConfig(), **services)


# ---- schema derivation ----

async def test_schema_from_keyword_only_parameters():
    tools = await listed(build_mcp(runtime_with(), [provider(ToolSpec(name="rich_tool", handler=rich))]))
    schema = tools["rich_tool"].input_schema
    props = schema["properties"]
    assert schema["required"] == ["on"]
    assert props["on"]["type"] == "string" and props["on"]["description"] == LANE_ARG_DESCRIPTION
    assert props["count"]["type"] == "integer" and props["count"]["default"] == 3
    assert props["count"]["description"] == "How many times, at most ten."
    # an optional argument with a null default is listed without its null branch (lean_schema)
    assert props["flag"]["type"] == "boolean" and "default" not in props["flag"] and "anyOf" not in props["flag"]
    assert props["flag"]["description"] == "Whether to flag it."
    assert props["action"]["default"] == "click"
    assert schema["$defs"]["ActionKind"]["enum"][:2] == ["click", "type"]
    assert props["labels"]["type"] == "array" and props["labels"]["items"] == {"type": "string"}
    assert not any("title" in p for p in props.values())  # pydantic's generated titles say nothing new
    assert props["note"]["description"] == "A note from the field."
    description = tools["rich_tool"].description
    assert description == "Do a rich thing on a lane, with a description that wraps over two lines.\n\nReturns the count."


async def test_arguments_are_validated_and_converted():
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="rich_tool", handler=rich))])
    result = await call(server, "rich_tool", {"on": "a/b", "count": "5", "action": "hover", "labels": ["x"]})
    assert not result.is_error
    assert result.structured_content == {"on": "a/b", "count": 5, "flag": None, "action": "hover", "labels": ["x"],
                                         "note": ""}
    result = await call(server, "rich_tool", {"on": "a/b", "count": "lots", "action": "explode"})
    assert result.is_error
    text = text_of(result)
    assert text.startswith("invalid: count: ") and "; action: " in text and "explode" not in text


def test_lean_schema_keeps_what_says_something():
    schema = {"title": "Args", "type": "object", "required": ["title"], "properties": {
        "title": {"title": "Title", "type": "string"},  # an argument named title stays
        "meta": {"title": "Meta", "type": "object", "default": {"title": "kept"}},
        "maybe": {"title": "Maybe", "anyOf": [{"type": "integer"}, {"type": "null"}], "default": None,
                  "description": "d"},
        "either": {"anyOf": [{"type": "string"}, {"type": "array"}, {"type": "null"}], "default": None},
        "nullable": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 3},
    }, "$defs": {"Kind": {"title": "Kind", "enum": ["a", "b"], "type": "string"}}}
    lean = lean_schema(schema)
    props = lean["properties"]
    assert "title" not in lean and lean["required"] == ["title"] and props["title"] == {"type": "string"}
    assert props["meta"] == {"type": "object", "default": {"title": "kept"}}
    assert props["maybe"] == {"type": "integer", "description": "d"}
    assert props["either"] == {"anyOf": [{"type": "string"}, {"type": "array"}]}
    assert props["nullable"] == schema["properties"]["nullable"]  # a default other than null: unchanged
    assert lean["$defs"] == {"Kind": {"enum": ["a", "b"], "type": "string"}}


async def test_a_leaned_optional_argument_still_takes_an_explicit_null():
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="rich_tool", handler=rich))])
    result = await call(server, "rich_tool", {"on": "personal/x", "flag": None, "labels": None})
    assert not result.is_error and result.structured_content["flag"] is None


def test_explicit_description_wins():
    spec = ToolSpec(name="plain_tool", handler=plain, description="Said otherwise.")
    server = build_mcp(runtime_with(), [provider(spec)])
    assert server._tool_manager.get_tool("plain_tool").description == "Said otherwise."


def test_docstring_args_section_parsing():
    description, args = split_docstring("""Top line.

    Args:
        a: First.
        b (int, optional): Second, which
            continues here.

    Raises:
        ValueError: sometimes.
    """)
    assert args == {"a": "First.", "b": "Second, which continues here."}
    assert description == "Top line.\n\nRaises:\nValueError: sometimes."
    assert split_docstring(None) == ("", {})
    assert split_docstring("- one\n- two")[0] == "- one\n- two"


async def _positional(ctx, on: str) -> dict:
    return {}


async def _unhinted(ctx, *, on) -> dict:
    return {}


async def _varargs(ctx, *args: str) -> dict:
    return {}


async def _no_ctx(*, on: str) -> dict:
    return {}


@pytest.mark.parametrize(("handler", "error"), [
    (_positional, "must be keyword-only"),
    (_unhinted, "needs a type hint"),
    (_varargs, "must be keyword-only"),
    (_no_ctx, "first parameter must be the ToolContext"),
])
def test_rejects_handlers_that_break_the_contract(handler, error):
    with pytest.raises(ValueError, match=error):
        tool_params(ToolSpec(name="bad_tool", handler=handler))


# ---- annotations ----

@pytest.mark.parametrize(("effect", "read_only", "destructive"), [
    (Effect.READ, True, None),
    (Effect.DRAFT, False, False),
    (Effect.COMMIT, False, True),
    (Effect.PAY, False, True),
    (Effect.IRREVERSIBLE, False, True),
    (Effect.SECRET, False, False),
    (Effect.EXEC, False, True),
    (Effect.INSTALL_OVERLAY, False, False),
    (Effect.INSTALL_BASE, False, True),
    (Effect.SERVICE, False, False),
    (Effect.BRAIN, False, False),
])
async def test_annotations_follow_the_effect(effect, read_only, destructive):
    specs = [ToolSpec(name="plain_tool", handler=plain, effect=effect),
             ToolSpec(name="web_tool", handler=echo, effect=effect, tags=("browser", "idempotent"))]
    tools = await listed(build_mcp(runtime_with(), [provider(*specs)]))
    hints = tools["plain_tool"].annotations
    assert hints.read_only_hint is read_only and hints.destructive_hint is destructive
    assert hints.open_world_hint is None and hints.idempotent_hint is None
    web = tools["web_tool"].annotations
    assert web.open_world_hint is True and web.idempotent_hint is True


async def test_node_tool_annotations():
    runtime = node_runtime(NodeConfig(), FakeLaneManager(), FakeInteraction())
    tools = await listed(build_mcp(runtime, [node_tool_specs]))
    assert tools["browser_act"].annotations.read_only_hint is False
    assert tools["browser_act"].annotations.destructive_hint is False
    assert tools["browser_act"].annotations.open_world_hint is True
    for name in ("whoami", "browsers_list", "browser_view", "browser_snapshot", "browser_screenshot"):
        assert tools[name].annotations.read_only_hint is True, name
    # READ for grants, but they change something: a client must not run them as read only
    for name in ("browser_open", "browser_close", "browser_claim", "browser_release", "browser_navigate", "browser_scroll"):
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is False, name


async def test_a_read_tool_that_changes_state_is_not_read_only():
    spec = ToolSpec(name="opens_things", handler=plain, effect=Effect.READ, tags=("changes_state",))
    hints = (await listed(build_mcp(runtime_with(), [provider(spec)])))["opens_things"].annotations
    assert hints.read_only_hint is False and hints.destructive_hint is False


# ---- composition ----

async def test_hidden_without_a_service():
    specs = [ToolSpec(name="vault_thing", handler=plain, hidden_without="vault"),
             ToolSpec(name="always_there", handler=plain)]
    assert set(await listed(build_mcp(runtime_with(), [provider(*specs)]))) == {"always_there"}
    with_vault = build_mcp(runtime_with(vault=object()), [provider(*specs)])
    assert set(await listed(with_vault)) == {"vault_thing", "always_there"}


async def test_node_tools_hide_without_lanes_or_interaction():
    assert await listed(build_mcp(runtime_with(), [node_tool_specs])) == {}
    lanes_only = await listed(build_mcp(runtime_with(lanes=FakeLaneManager()), [node_tool_specs]))
    assert set(lanes_only) == {"whoami", "browsers_list", "browser_open", "browser_close", "browser_view", "browser_claim",
                               "browser_release", "browser_queue", "howto"}


@pytest.mark.parametrize("hidden", [None, "vault"])
def test_duplicate_names_are_refused(hidden):
    one = ToolSpec(name="same_name", handler=plain)
    two = ToolSpec(name="same_name", handler=echo, hidden_without=hidden)
    with pytest.raises(ValueError, match="duplicate tool name 'same_name'"):
        build_mcp(runtime_with(), [provider(one), provider(two)])
    with pytest.raises(ValueError, match="duplicate"):
        build_mcp(runtime_with(), [provider(one, one)])


async def test_unknown_tool_is_not_found():
    result = await call(build_mcp(runtime_with(), [provider(ToolSpec(name="plain_tool", handler=plain))]), "nope")
    assert result.is_error and text_of(result).startswith("not_found: no tool named 'nope'")
    close = await call(build_mcp(runtime_with(), [provider(ToolSpec(name="plain_tool", handler=plain))]), "plain_tol")
    lines = text_of(close).splitlines()  # QA 2026-11-15 P2-4, P2-5: a close name, and a fix that works
    assert lines[0] == "not_found: no tool named 'plain_tol' (did you mean 'plain_tool'?)"
    assert lines[1].startswith("fix: Call tools_more()") and "tools/list" not in text_of(close)


# ---- unknown argument keys are refused, not dropped ----
# The MCP SDK ignores keys that are not parameters, so `acton` for `action` ran the tool with the default.

def recording(seen: list):
    """A wrap that records the arguments that reach it and never runs the handler."""
    def wrap(spec, runtime):
        async def run(**arguments):
            seen.append((spec.name, arguments))
            return {"recorded": spec.name}
        return run
    return wrap


async def test_a_misspelled_argument_is_refused_with_a_suggestion():
    seen: list = []
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="rich_tool", handler=rich))], wrap=recording(seen))
    result = await call(server, "rich_tool", {"on": "a/b", "acton": "type"})
    text = text_of(result)
    assert result.is_error and text.startswith("invalid:")
    assert "acton" in text and "action" in text  # names the key and suggests the parameter
    assert seen == []  # the handler never ran with the default


async def test_an_unknown_argument_with_no_close_match_is_named_without_a_suggestion():
    seen: list = []
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="rich_tool", handler=rich))], wrap=recording(seen))
    result = await call(server, "rich_tool", {"on": "a/b", "zzqx": 1})
    text = text_of(result)
    assert result.is_error and text.startswith("invalid:") and "zzqx" in text
    assert "did you mean" not in text.lower()
    assert "It takes: " in text and "action" in text.split("It takes: ")[1]  # the real ones, not a guess
    assert seen == []


async def test_tools_call_refuses_a_misspelled_argument_the_same_way():
    seen: list = []
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="rich_tool", handler=rich))], wrap=recording(seen))
    result = await call(server, "tools_call", {"name": "rich_tool", "arguments": {"on": "a/b", "acton": "type"}})
    text = text_of(result)
    assert result.is_error and text.startswith("invalid:")
    assert "acton" in text and "action" in text
    assert seen == []


async def test_a_typed_flow_tool_takes_its_declared_arguments_and_refuses_an_unknown_one(tmp_path):
    h = Harness(tmp_path)
    h.save(post_flow())
    flow_specs = [s for s in runner_tool_specs(h.runtime) if s.name == "flow_biz_a_social_post"]
    assert flow_specs, "the posting flow should be exposed as a typed tool"
    seen: list = []
    server = build_mcp(h.runtime, [lambda _rt: flow_specs], wrap=recording(seen))

    declared = {"caption": "hi", "image": "b.jpg", "wait_seconds": 0, "idempotency_key": "k-1"}
    accepted = await call(server, "flow_biz_a_social_post", declared)
    assert not accepted.is_error, text_of(accepted)
    assert seen == [("flow_biz_a_social_post", declared)]

    refused = await call(server, "flow_biz_a_social_post", {"caption": "hi", "image": "b.jpg", "captoin": "x"})
    text = text_of(refused)
    assert refused.is_error and text.startswith("invalid:")
    assert "captoin" in text and "caption" in text
    assert len(seen) == 1  # the refused call never reached the wrapper


# ---- the one wrapper ----

class RecordingContext:
    def __init__(self, runtime, spec):
        self.runtime = runtime
        self.principal = Principal(kind="agent", id="tester")
        self.spec = spec
        self.authorized: list[dict] = []
        self.traces: list[tuple[str, dict]] = []

    async def authorize(self, effect, **kwargs):
        self.authorized.append({"effect": effect, **kwargs})

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


class RecordingWrap:
    def __init__(self):
        self.built: list[str] = []
        self.calls: list[tuple[str, dict, str | None]] = []
        self.contexts: list[RecordingContext] = []

    def __call__(self, spec, runtime):
        self.built.append(spec.name)

        async def run(**arguments):
            self.calls.append((spec.name, arguments, current_request_token()))
            ctx = RecordingContext(runtime, spec)
            self.contexts.append(ctx)
            return await spec.handler(ctx, **arguments)

        return run


NODE_CALLS = [
    ("whoami", {}),
    ("browsers_list", {}),
    ("browser_open", {"on": "personal/reddit"}),
    ("browser_view", {"on": "personal/reddit"}),
    ("browser_claim", {"on": "personal/reddit", "purpose": "posting"}),
    ("browser_queue", {"on": "personal/reddit"}),
    ("browser_navigate", {"on": "personal/reddit", "url": "https://example.com/a"}),
    ("browser_snapshot", {"on": "personal/reddit"}),
    ("browser_act", {"on": "personal/reddit", "ref": "e2", "action": "type", "text": "hello"}),
    ("browser_scroll", {"on": "personal/reddit"}),
    ("browser_challenge", {"on": "personal/reddit"}),  # an account's browser, as any
    ("browser_screenshot", {"on": "personal/reddit"}),
    ("browser_wait", {"on": "personal/reddit", "for_text": "Posted", "wait_s": 1}),
    ("howto", {"topic": "waiting"}),
    ("browser_release", {"on": "personal/reddit"}),
    ("browser_close", {"on": "personal/reddit"}),
]


async def test_every_node_tool_goes_through_the_wrapper_and_authorizes(monkeypatch):
    async def wheel_down(page):  # the fake page has no window to wheel
        return None

    monkeypatch.setattr("openajar.node.human.wheel_down", wheel_down)
    manager = FakeLaneManager()
    await manager.create(LaneSpec(id="personal/reddit"))
    wrap = RecordingWrap()
    server = build_mcp(node_runtime(NodeConfig(), manager, FakeInteraction()), [node_tool_specs], wrap)
    assert sorted(wrap.built) == sorted(name for name, _ in NODE_CALLS)
    async with Client(server) as client:
        for name, args in NODE_CALLS:
            result = await client.call_tool(name, args)
            assert not result.is_error, (name, text_of(result))
    assert [name for name, _, _ in wrap.calls] == [name for name, _ in NODE_CALLS]
    by_tool = {ctx.spec.name: ctx.authorized for ctx in wrap.contexts}
    for name, args in NODE_CALLS:
        [auth] = by_tool[name]
        assert auth["effect"] is (Effect.DRAFT if name in ("browser_act", "browser_challenge") else Effect.READ), name
        assert auth.get("lane_id") == args.get("on"), name
        assert auth["summary"], name
    act = by_tool["browser_act"][0]
    assert act["summary"] == 'type e2 on personal/reddit with "hello"'
    assert act["content"] == {"ref": "e2", "action": "type", "text": "hello"}
    assert by_tool["browser_navigate"][0]["domain"] == "example.com"
    assert manager.principals == ["agent"] * 7


async def test_a_lane_manager_without_principal_kind_still_works():
    manager = LegacyLaneManager()
    await manager.create(LaneSpec(id="personal/reddit"))
    server = build_mcp(node_runtime(NodeConfig(), manager, FakeInteraction()), [node_tool_specs])
    result = await call(server, "browser_snapshot", {"on": "personal/reddit"})
    assert not result.is_error and manager.principals == ["legacy"]


async def test_allow_all_wrap_runs_as_an_owner_agent(caplog):
    spec = ToolSpec(name="plain_tool", handler=plain)
    assert await allow_all_wrap(spec, runtime_with())() == {"principal": "agent"}
    ctx = OwnerContext(runtime=runtime_with(), tool="x")
    assert ctx.principal.kind == "agent" and ctx.principal.owner == "owner"
    await ctx.authorize(Effect.PAY, lane_id="a/b", summary="pay")
    with caplog.at_level(logging.DEBUG, logger="openajar.mcp.registry"):
        ctx.trace("call", password="hunter2", api_key="k-123", token="t", text="x" * 500, on="a/b")
    logged = caplog.text
    assert "hunter2" not in logged and "k-123" not in logged and "'token': 't'" not in logged
    assert "'on': 'a/b'" in logged and "x" * 201 not in logged


# ---- errors and results ----

class CodedError(Exception):
    """Like openajar.policy's ToolCallError: not an OpenajarError, but it carries code and message."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def raising(exc: BaseException):
    async def handler(ctx) -> dict:
        """Fails."""
        raise exc

    return handler


@pytest.mark.parametrize(("exc", "text"), [
    (InvalidRequest("bad"), "invalid: bad"),
    (LaneBusy("all slots"), "busy: all slots"),
    (PolicyDenied("grant does not cover commit"), "denied: grant does not cover commit"),
    (ServiceUnavailable("vault is not configured on this node"), "unavailable: vault is not configured on this node"),
    (NotFound("no entry"), "not_found: no entry"),
    (HumanInControl("a human has it"), "human_in_control: a human has it"),
    (AgentPaused("stopped"), "agent_paused: stopped"),
    (CodedError("denied", "outside the grant"), "denied: outside the grant"),
    (RuntimeError("boom\nsecond line"), "engine_error: RuntimeError: boom"),
    (KeyError("k"), "engine_error: KeyError: 'k'"),
])
async def test_error_mapping(exc, text):
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="failing", handler=raising(exc)))])
    result = await call(server, "failing")
    first, *rest = text_of(result).split("\n")
    assert result.is_error and first == text
    assert rest and rest[0].startswith("fix: "), rest  # the error catalog's next step for the agent


async def test_long_errors_are_cut():
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="failing", handler=raising(InvalidRequest("x" * 2000))))])
    text = text_of(await call(server, "failing")).split("\n")[0]
    assert len(text) == 600 and text.endswith("...")


@pytest.mark.parametrize("summary", [None, "post 'hello' to r/test"])
async def test_approval_pending_is_a_structured_result(summary):
    exc = ApprovalPending("waiting for the owner to approve", approval_id="ap_123")
    if summary:
        exc.summary = summary
    server = build_mcp(runtime_with(), [provider(ToolSpec(name="poster", handler=raising(exc), effect=Effect.COMMIT))])
    result = await call(server, "poster")
    assert not result.is_error
    data = result.structured_content
    assert data["status"] == "approval_pending" and data["approval_id"] == "ap_123" and data["tool"] == "poster"
    assert data["summary"] == (summary or "waiting for the owner to approve")
    assert "approve" in data["next"] and "\u2014" not in data["next"]
    assert json.loads(text_of(result)) == data


async def test_a_wrapper_may_return_its_own_pending_result():
    pending = {"status": "approval_pending", "approval_id": "ap_9", "summary": "s", "check_with": "approval_wait"}

    def wrap(spec, runtime):
        async def run(**arguments):
            return pending
        return run

    result = await call(build_mcp(runtime_with(), [provider(ToolSpec(name="plain_tool", handler=plain))], wrap),
                        "plain_tool")
    assert not result.is_error and result.structured_content == pending


class Model(BaseModel):
    a: int
    b: ActionKind


@pytest.mark.parametrize(("value", "check"), [
    ("plain text", lambda r: text_of(r) == "plain text" and r.structured_content is None),
    ({"a": 1}, lambda r: r.structured_content == {"a": 1} and json.loads(text_of(r)) == {"a": 1}),
    (Model(a=1, b=ActionKind.FILL), lambda r: r.structured_content == {"a": 1, "b": "fill"}),
    ([1, 2], lambda r: json.loads(text_of(r)) == [1, 2] and r.structured_content is None),
    (ToolImage(PNG_1X1), lambda r: r.content[0].type == "image" and base64.b64decode(r.content[0].data) == PNG_1X1),
    ([{"type": "text", "text": "caption"}, ToolImage(b"jpg", "image/jpeg")],
     lambda r: [c.type for c in r.content] == ["text", "image"] and r.content[0].text == "caption"
     and r.content[1].mime_type == "image/jpeg"),
])
async def test_results(value, check):
    def wrap(spec, runtime):
        async def run(**arguments):
            return value
        return run

    result = await call(build_mcp(runtime_with(), [provider(ToolSpec(name="plain_tool", handler=plain))], wrap),
                        "plain_tool")
    assert not result.is_error and check(result), result


async def test_feature_results_and_errors_are_redacted(tmp_path):
    config = NodeConfig(home=tmp_path)
    save_proxies(config, {"lt1": ProxyConfig(label="lt1", host="5.6.7.8", port=1, username=USERNAME,
                                             password=PASSWORD)})
    specs = [ToolSpec(name="echo_tool", handler=echo),
             ToolSpec(name="failing", handler=raising(RuntimeError(f"proxy http://{USERNAME}:{PASSWORD}@h:1")))]
    server = build_mcp(Runtime(config=config), [provider(*specs)])
    echoed = await call(server, "echo_tool", {"value": f"user {USERNAME} pass {PASSWORD}"})
    failed = await call(server, "failing")
    assert text_of(echoed) == "user *** pass ***"
    assert text_of(failed).split("\n")[0] == "engine_error: RuntimeError: proxy http://***@h:1"


# ---- per-token visibility and the request token ----

def test_bind_request_token_nests_and_resets():
    assert current_request_token() is None
    with bind_request_token("a"):
        assert current_request_token() == "a"
        with bind_request_token("b"):
            assert current_request_token() == "b"
        assert current_request_token() == "a"
    assert current_request_token() is None


async def test_visible_filter_narrows_listing_and_calls():
    seen: list[str | None] = []

    def visible(token, specs):
        seen.append(token)
        return [s for s in specs if token == "full" or s.effect is Effect.READ]

    specs = [ToolSpec(name="reader", handler=plain), ToolSpec(name="writer", handler=plain, effect=Effect.COMMIT)]
    server = build_mcp(runtime_with(), [provider(*specs)], visible=visible)
    with bind_request_token("full"):
        assert {t.name for t in await server.list_tools()} == {"reader", "writer"}
    with bind_request_token("narrow"):
        assert {t.name for t in await server.list_tools()} == {"reader"}
        refused = await server.call_tool("writer", {})
        assert refused.is_error and refused.content[0].text.startswith("not_found: no tool named 'writer'")
        assert refused.content[0].text.splitlines()[-1].startswith("fix: Do not look for another way")
        assert not (await server.call_tool("reader", {})).is_error
    assert {t.name for t in await server.list_tools()} == {"reader"}  # no token: what the filter allows
    assert seen[:2] == ["full", "narrow"] and seen[-1] is None


async def test_a_failing_filter_shows_nothing():
    def visible(token, specs):
        raise RuntimeError("policy store is locked")

    server = build_mcp(runtime_with(), [provider(ToolSpec(name="reader", handler=plain))], visible=visible)
    assert await server.list_tools() == []
    assert (await server.call_tool("reader", {})).is_error


def test_a_calls_log_lines_never_carry_an_environments_handle():
    """An environment's `on` is a secret its chat holds: the log context names its lane instead."""
    from openajar.environments.picking import new_handle
    from openajar.mcp.registry import log_lane

    handle = new_handle("e7k2abc")
    assert log_lane(handle) == "env/e7k2abc"
    assert log_lane("personal/reddit") == "personal/reddit"
    assert log_lane("env/e7k2abc") == "env/e7k2abc"
    assert log_lane(None) is None and log_lane(3) is None
