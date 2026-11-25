"""Every MCP tool through an in-memory MCP client against the fakes."""

import base64
import json

import pytest
from fakes import PNG_1X1, FakeInteraction, FakeLaneManager
from mcp import Client

from openajar.core.config import NodeConfig, save_proxies
from openajar.core.errors import (
    EngineError,
    InvalidRequest,
    LaneBusy,
    LaneNotFound,
    LaneQuarantined,
    RefNotFound,
)
from openajar.core.models import ActionKind, ControlState, LaneSpec, LaneState, ProxyConfig
from openajar.core.runtime import Runtime
from openajar.environments.routing import kind_of
from openajar.mcp.registry import build_mcp
from openajar.mcp.server import build_server
from openajar.node.tools import INSTRUCTIONS, tool_specs

PASSWORD = "pw-Zx81-secret"
USERNAME = "customer-acme-zone-isp"
TOOLS = {
    "whoami", "browsers_list", "browser_open", "browser_close", "browser_view", "browser_claim", "browser_release", "browser_queue",
    "browser_navigate", "browser_snapshot", "browser_act", "browser_scroll", "browser_screenshot", "browser_wait",
    "browser_challenge",
    "howto",
}

EM_DASH = "\u2014"


@pytest.fixture
def config(tmp_path):
    cfg = NodeConfig(home=tmp_path, name="test-node", mcp_token="tok", max_warm_lanes=2)
    save_proxies(cfg, {"lt1": ProxyConfig(label="lt1", host="5.6.7.8", port=12323,
                                          username=USERNAME, password=PASSWORD)})
    return cfg


@pytest.fixture
async def manager(config):
    m = FakeLaneManager(config)
    await m.create(LaneSpec(id="personal/reddit", proxy_label="lt1"))
    await m.create(LaneSpec(id="biz-a/instagram"))
    return m


@pytest.fixture
def interaction():
    return FakeInteraction()


@pytest.fixture(params=["auto", "legacy"])
def connect(request, manager, interaction, config):
    """`async with connect() as client:`. A yield fixture would enter and exit the client's task
    group in different tasks. "legacy" runs the initialize handshake over JSON-RPC framing, the
    way Claude Code talks today; "auto" is the SDK's direct in-process path."""
    runtime = Runtime(config=config, lanes=manager, interaction=interaction)
    return lambda: Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS), mode=request.param)


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def data_of(result) -> dict:
    assert not result.is_error, text_of(result)
    return result.structured_content or json.loads(text_of(result))


async def test_tool_surface(connect):
    async with connect() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == TOOLS
        for name in TOOLS - {"whoami", "browsers_list", "howto"}:
            schema = tools[name].input_schema
            # a lane tool takes `on`; the browser and terminal ones may leave it out for the chat's environment
            assert "on" in schema["properties"], name
            assert "on" in schema.get("required", []) or kind_of(name) is not None, name
        act = tools["browser_act"].input_schema
        assert set(act["required"]) == {"ref", "action"}  # `on`: the chat's environment when left out
        assert "text" in act["properties"]


async def test_descriptions_are_written_for_agents(connect):
    async with connect() as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}
        assert "ref" in tools["browser_snapshot"] and "browser_act" in tools["browser_snapshot"]
        assert "not enough" in tools["browser_screenshot"]
        assert "take over" in tools["browser_view"] and "CAPTCHA" in tools["browser_view"]
        assert "browser_view" in INSTRUCTIONS and "browser_snapshot" in INSTRUCTIONS
        for text in [INSTRUCTIONS, *tools.values()]:
            assert EM_DASH not in text


async def test_whoami(connect):
    async with connect() as client:
        data = data_of(await client.call_tool("whoami", {}))
        assert data["node"] == "test-node"
        assert data["browsers"]["total"] == 2 and data["browsers"]["cold"] == 2
        assert data["limits"]["max_warm_browsers"] == 2


async def test_lanes_list(connect):
    async with connect() as client:
        lanes = data_of(await client.call_tool("browsers_list", {}))["browsers"]
        assert [lane["id"] for lane in lanes] == ["biz-a/instagram", "personal/reddit"]
        reddit = lanes[1]
        assert reddit == {"id": "personal/reddit", "kind": "browser", "engine": "camoufox", "state": "cold",
                          "proxy": "lt1", "device": None, "last_used": None}


async def test_lane_open_close_and_view(connect, manager, monkeypatch):
    monkeypatch.setenv("OPENAJAR_SSH_HOST", "mynode")
    async with connect() as client:
        cold = data_of(await client.call_tool("browser_view", {"on": "personal/reddit"}))
        assert cold["viewer"] is None and "browser_open" in cold["note"]

        opened = data_of(await client.call_tool("browser_open", {"on": "personal/reddit"}))
        assert opened["state"] == "warm"
        assert opened["proxy"] == {"label": "lt1", "host": "5.6.7.8", "port": 12323}
        port = opened["viewer"]["viewer_port"]
        assert opened["viewer"]["url"] == f"http://localhost:{port}/vnc.html?autoconnect=1&resize=scale"

        view = data_of(await client.call_tool("browser_view", {"on": "personal/reddit"}))
        assert view["viewer"]["tunnel"].startswith(f"ssh -N -L {port}:localhost:{port} mynode")
        assert "take over" in view["take_over"]

        closed = data_of(await client.call_tool("browser_close", {"on": "personal/reddit"}))
        assert closed["state"] == "cold"
        assert manager.calls[:2] == [("open", "personal/reddit"), ("close", "personal/reddit")]


async def test_navigate_snapshot_act_screenshot(connect, manager, interaction):
    async with connect() as client:
        nav = data_of(await client.call_tool("browser_navigate", {"on": "personal/reddit", "url": "reddit.com/r/x"}))
        assert nav == {"on": "personal/reddit", "url": "https://reddit.com/r/x",
                       "title": "Fake page at https://reddit.com/r/x", "status": 200, "signal": "ok"}

        snap = await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
        text = text_of(snap)
        assert not snap.is_error
        assert text.startswith("browser: personal/reddit\nurl: https://reddit.com/r/x\n")
        assert "[ref=e2]" in text and "refs: 2" in text

        act = data_of(await client.call_tool("browser_act", {"on": "personal/reddit", "ref": "[ref=e2]",
                                                             "action": "click"}))
        assert act["ok"] is True and act["changed"] is True and act["ref"] == "e2" and act["signal"] == "ok"
        assert interaction.acts == [("e2", ActionKind.CLICK, None)]

        shot = await client.call_tool("browser_screenshot", {"on": "personal/reddit"})
        assert not shot.is_error
        [image] = shot.content
        assert image.type == "image" and image.mime_type == "image/png"
        assert base64.b64decode(image.data) == PNG_1X1

        # navigate, snapshot, the click's form read (for an approval card), the click, the screenshot
        assert [c for c in manager.calls if c[0] == "using"] == [("using", "personal/reddit")] * 5
        assert manager.lanes["personal/reddit"].state == LaneState.WARM
        assert manager.lanes["personal/reddit"].last_used is not None


async def test_act_with_text(connect, interaction):
    async with connect() as client:
        args = {"on": "personal/reddit", "ref": "e1", "action": "fill", "text": "hello"}
        assert data_of(await client.call_tool("browser_act", args))["ok"]
        assert interaction.acts == [("e1", ActionKind.FILL, "hello")]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"on": "personal/reddit", "ref": "e1", "action": "fill"}, "invalid: action fill needs `text`"),
        ({"on": "personal/reddit", "ref": "e9", "action": "click"}, "ref_not_found: "),
        ({"on": "personal/reddit", "ref": " ", "action": "click"}, "invalid: ref is empty"),
        ({"on": "Personal/Reddit", "ref": "e1", "action": "click"}, "invalid: an id must look like"),
        ({"on": "nobody/here", "ref": "e1", "action": "click"}, "lane_not_found: "),
    ],
)
async def test_act_rejects(connect, args, expected):
    async with connect() as client:
        result = await client.call_tool("browser_act", args)
        assert result.is_error and text_of(result).startswith(expected), text_of(result)


async def test_act_rejects_unknown_action(connect):
    async with connect() as client:
        result = await client.call_tool("browser_act", {"on": "personal/reddit", "ref": "e1", "action": "explode"})
        assert result.is_error and "action" in text_of(result)


@pytest.mark.parametrize("url", ["file:///var/lib/agentos/secrets/proxies.json", "about:config",
                                 "javascript:alert(1)", "   ", "chrome://settings"])
async def test_navigate_only_allows_web_urls(connect, interaction, url):
    async with connect() as client:
        result = await client.call_tool("browser_navigate", {"on": "personal/reddit", "url": url})
        assert result.is_error and text_of(result).startswith("invalid: "), text_of(result)


@pytest.mark.parametrize(
    ("exc", "prefix"),
    [
        (LaneBusy("all 2 warm slots are active"), "busy: all 2 warm slots are active"),
        (LaneNotFound("no lane"), "lane_not_found: no lane"),
        (LaneQuarantined("exit IP mismatch"), "quarantined: exit IP mismatch"),
        (RefNotFound("gone"), "ref_not_found: gone"),
        (InvalidRequest("bad"), "invalid: bad"),
        (EngineError("browser died"), "engine_error: browser died"),
        (RuntimeError("Timeout 30000ms exceeded.\nCall log:\n  - waiting"), "engine_error: RuntimeError: Timeout"),
    ],
)
@pytest.mark.parametrize("tool", ["browser_open", "browser_navigate", "browser_snapshot", "browser_screenshot"])
async def test_error_mapping(connect, manager, tool, exc, prefix):
    async with connect() as client:
        manager.fail_with = exc
        args = {"on": "personal/reddit"} | ({"url": "https://example.com"} if tool == "browser_navigate" else {})
        result = await client.call_tool(tool, args)
        assert result.is_error
        assert text_of(result).startswith(prefix), text_of(result)
        assert "Call log" not in text_of(result)


async def test_interaction_errors_are_mapped(connect, interaction):
    async with connect() as client:
        interaction.fail_with = EngineError("page crashed")
        result = await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
        assert result.is_error and text_of(result).splitlines()[0] == "engine_error: page crashed"
        assert "fix:" in text_of(result)  # the error catalog's next step for the agent


async def test_credentials_never_appear(connect, manager, interaction):
    async with connect() as client:
        outputs = []

        async def call(name, args):
            result = await client.call_tool(name, args)
            outputs.append(text_of(result))
            outputs.append(json.dumps(result.structured_content or {}))
            return result

        on = {"on": "personal/reddit"}
        await call("whoami", {})
        await call("browsers_list", {})
        await call("browser_open", on)
        await call("browser_view", on)
        await call("browser_navigate", on | {"url": "https://example.com"})
        await call("browser_snapshot", on)
        await call("browser_act", on | {"ref": "e2", "action": "click"})
        await call("browser_screenshot", on)
        await call("browser_close", on)
        # An engine error that quotes the proxy URL must still not leak it.
        manager.fail_with = EngineError(f"proxy http://{USERNAME}:{PASSWORD}@5.6.7.8:12323 refused")
        await call("browser_open", on)
        interaction.fail_with = RuntimeError(f"auth failed for {USERNAME} with {PASSWORD}")
        await call("browser_snapshot", on)

        blob = "\n".join(outputs)
        assert PASSWORD not in blob
        assert USERNAME not in blob
        assert "engine_error: proxy http://***@5.6.7.8:12323 refused" in blob


async def test_unknown_lane(connect):
    async with connect() as client:
        result = await client.call_tool("browser_view", {"on": "nobody/here"})
        assert result.is_error and text_of(result).startswith("lane_not_found: ")


async def test_build_server_is_the_same_surface(manager, interaction, config):
    """The M1 entry point still works and serves the same tools as the registry path."""
    async with Client(build_server(manager, interaction, config, ssh_host="other-box")) as client:
        assert {t.name for t in (await client.list_tools()).tools} == TOOLS
        await client.call_tool("browser_open", {"on": "personal/reddit"})
        view = data_of(await client.call_tool("browser_view", {"on": "personal/reddit"}))
        assert view["viewer"]["tunnel"].endswith("other-box  then open " + view["viewer"]["url"])


async def test_browser_tools_pass_the_principal_kind(connect, manager):
    async with connect() as client:
        await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
        await client.call_tool("browser_act", {"on": "personal/reddit", "ref": "e1", "action": "click"})
    # the click also reads the form it would submit first (for an approval card), as the agent
    assert manager.principals == ["agent", "agent", "agent"]


@pytest.mark.parametrize(("state", "code"), [(ControlState.HUMAN, "human_in_control: "),
                                             (ControlState.PAUSED, "agent_paused: ")])
@pytest.mark.parametrize("tool", ["browser_snapshot", "browser_screenshot", "browser_close"])
async def test_a_human_in_control_stops_agent_calls(connect, manager, interaction, state, code, tool):
    manager.lanes["personal/reddit"].control.state = state
    async with connect() as client:
        result = await client.call_tool(tool, {"on": "personal/reddit"})
    assert result.is_error and text_of(result).startswith(code), text_of(result)
    assert ("close", "personal/reddit") not in manager.calls


async def test_invalid_arguments_start_with_the_code(connect):
    async with connect() as client:
        result = await client.call_tool("browser_act", {"on": "personal/reddit", "ref": "e1"})
        assert result.is_error and text_of(result) == (  # QA 2026-11-15 P2-4: a fix line, and where the arguments are
            'invalid: action: Field required; tools_more(toolset="browser_act") describes each one\n'
            "fix: Fix the arguments as the message says, then call again.")
        result = await client.call_tool("browser_navigate", {"on": "personal/reddit", "url": 7})
        assert result.is_error and text_of(result).startswith("invalid: url: ")
        assert "7" not in text_of(result).removeprefix("invalid: url: ")
