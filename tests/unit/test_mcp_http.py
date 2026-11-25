"""The node's HTTP app: trusted-Host check on every route, mounts, bearer and token binding."""

import asyncio
from contextlib import contextmanager

import httpx2
import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient, WebSocketDenialResponse

from openajar.core.config import NodeConfig
from openajar.core.models import Effect, LaneSpec
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec, current_request_token
from openajar.mcp.http import BearerAuth, TrustedHosts, build_app, parse_host
from openajar.mcp.registry import OwnerContext, build_mcp
from openajar.node import daemon
from openajar.node.tools import tool_specs

TOKEN = "node-token-value"
GRANT = "grant-token-value"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class ConfigWithHosts(NodeConfig):
    """node.toml with the extra_allowed_hosts setting (proposed for core.config)."""
    extra_allowed_hosts: list[str] = Field(default_factory=list)


def node_runtime(config, lanes, interaction) -> Runtime:
    return Runtime(config=config, lanes=lanes, interaction=interaction)


def sub_app(name: str) -> Starlette:
    async def where(request):
        return JSONResponse({"app": name, "path": request.url.path})

    async def socket(websocket):
        await websocket.accept()
        await websocket.send_text(f"{name} {websocket.url.path}")
        await websocket.close()

    return Starlette(routes=[Route("/{path:path}", where), WebSocketRoute("/{path:path}", socket)])


def app_for(config, **kwargs):
    runtime = node_runtime(config, FakeLaneManager(config), FakeInteraction())
    return build_app(runtime, build_mcp(runtime, [tool_specs]), **kwargs)


@pytest.fixture
def config(tmp_path):
    return ConfigWithHosts(home=tmp_path, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())


@pytest.fixture
def mounted(config):
    return app_for(config, api_app=sub_app("api"), viewer_app=sub_app("viewer"))


# ---- trusted hosts ----

BAD_HOSTS = ["evil.example", "evil.example:7400", "localhost.evil.example", "127.0.0.1.nip.io:7400",
             "[::2]:7400", "10.0.0.5:7400", "localhost:notaport", "", "local host"]
GOOD_HOSTS = ["localhost", "localhost:7400", "LOCALHOST:7400", "127.0.0.1", "127.0.0.1:1234", "[::1]",
              "[::1]:7400"]
PATHS = ["/mcp", "/api/lanes", "/view/personal/reddit", "/viewer-ws/x", "/", "/anything"]


@pytest.mark.parametrize("host", BAD_HOSTS)
@pytest.mark.parametrize("path", PATHS)
def test_wrong_host_gets_421_on_every_route(mounted, host, path):
    with TestClient(mounted) as client:
        headers = {"Host": host, "Authorization": f"Bearer {TOKEN}"} | MCP_HEADERS
        response = client.post(path, json=INIT, headers=headers)
        assert response.status_code == 421, response.text
        assert response.json()["error"]["code"] == "misdirected" and "\u2014" not in response.text


@pytest.mark.parametrize("path", ["/viewer-ws/personal/reddit", "/api/lanes/a/b/vnc", "/mcp"])
def test_wrong_host_websocket_is_refused(mounted, path):
    client = TestClient(mounted)
    with pytest.raises(WebSocketDenialResponse) as refused, client.websocket_connect(f"ws://evil.example{path}"):
        pass
    assert refused.value.status_code == 421 and refused.value.json()["error"]["code"] == "misdirected"


async def test_wrong_host_websocket_without_denial_support_is_closed():
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/viewer-ws/x", "headers": [(b"host", b"evil.example")]}
    await TrustedHosts(app=None, allowed=["localhost"])(scope, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize("host", GOOD_HOSTS)
def test_local_hosts_pass(mounted, host):
    with TestClient(mounted) as client:
        assert client.get("/api/lanes", headers={"Host": host}).json() == {"app": "api", "path": "/api/lanes"}
        assert client.get("/mcp", headers={"Host": host}).status_code == 401
        ok = client.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Host": host, "Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200, ok.text


def test_extra_allowed_hosts(tmp_path):
    config = ConfigWithHosts(home=tmp_path, mcp_token=TOKEN, extra_allowed_hosts=["mynode.tail.ts.net", "box:8080"])
    with TestClient(app_for(config)) as client:
        auth = MCP_HEADERS | {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/mcp", json=INIT, headers=auth | {"Host": "mynode.tail.ts.net:7400"}).status_code == 200
        assert client.post("/mcp", json=INIT, headers=auth | {"Host": "box:8080"}).status_code == 200
        assert client.post("/mcp", json=INIT, headers=auth | {"Host": "box:8081"}).status_code == 421
        assert client.post("/mcp", json=INIT, headers=auth | {"Host": "other.tail.ts.net"}).status_code == 421


def test_plain_node_config_allows_only_local_hosts(tmp_path):
    with TestClient(app_for(NodeConfig(home=tmp_path, mcp_token=TOKEN))) as client:
        assert client.get("/mcp", headers={"Host": "localhost:7400"}).status_code == 401
        assert client.get("/mcp", headers={"Host": "mynode:7400"}).status_code == 421


def test_parse_host():
    assert parse_host("LocalHost:7400") == ("localhost", 7400)
    assert parse_host("[::1]") == ("[::1]", None)
    assert parse_host("::1") is None and parse_host("a b") is None and parse_host("") is None
    with pytest.raises(ValueError, match="not a host"):
        TrustedHosts(app=None, allowed=["bad host"])


# ---- mounts ----

def test_mounts_see_the_full_path_and_bring_their_own_auth(mounted):
    with TestClient(mounted, base_url="http://localhost:7400") as client:
        assert client.get("/api").json() == {"app": "api", "path": "/api"}
        assert client.get("/api/approvals/1").json() == {"app": "api", "path": "/api/approvals/1"}
        assert client.get("/view/personal/reddit").json() == {"app": "viewer", "path": "/view/personal/reddit"}
        with client.websocket_connect("ws://localhost:7400/viewer-ws/personal/reddit") as ws:
            assert ws.receive_text() == "viewer /viewer-ws/personal/reddit"
        with client.websocket_connect("ws://127.0.0.1:7400/api/lanes/a/b/vnc") as ws:
            assert ws.receive_text() == "api /api/lanes/a/b/vnc"
        # Only real prefixes are mounted; everything else needs the bearer token.
        for path in ("/apix", "/viewer", "/", "/mcp"):
            assert client.get(path).status_code == 401, path


def test_nothing_is_mounted_unless_given(config):
    with TestClient(app_for(config), base_url="http://localhost:7400") as client:
        for path in ("/api/lanes", "/view/a/b"):
            assert client.get(path).status_code == 401, path


# ---- bearer and token binding ----

def token_echo():
    async def app(scope, receive, send):
        await PlainTextResponse(str(current_request_token()))(scope, receive, send)

    return app


def test_bearer_binds_the_token_for_the_request():
    bound: list[tuple] = []

    @contextmanager
    def bind(token, *, session=None):
        bound.append((token, session))
        yield

    auth = BearerAuth(token_echo(), TOKEN, accept=lambda token: token == GRANT, bind=bind)
    client = TestClient(auth)  # no lifespan: token_echo is a bare HTTP app
    assert client.get("/", headers={"Authorization": f"Bearer {TOKEN}"}).text == TOKEN
    response = client.get("/", headers={"Authorization": f"bearer {GRANT}", "Mcp-Session-Id": "s-1"})
    assert response.text == GRANT
    assert client.get("/", headers={"Authorization": "Bearer other"}).status_code == 401
    assert bound == [(TOKEN, None), (GRANT, "s-1")]
    assert current_request_token() is None


def test_binder_without_session_and_a_failing_check():
    bound: list[str] = []

    @contextmanager
    def bind(token):
        bound.append(token)
        yield

    def accept(token):
        raise RuntimeError("store locked")

    client = TestClient(BearerAuth(token_echo(), TOKEN, accept=accept, bind=bind))
    assert client.get("/", headers={"Authorization": f"Bearer {GRANT}"}).status_code == 401
    assert client.get("/", headers={"Authorization": f"Bearer {TOKEN}"}).text == TOKEN
    assert bound == [TOKEN]


# ---- a real session through uvicorn ----

async def ping(ctx, *, word: str = "ping") -> dict:
    """Echo a word."""
    return {"word": word, "principal": ctx.principal.kind}


async def test_real_http_sessions_per_token(config):
    """Two agents with different tokens through uvicorn: each call sees its own token, and the
    listing is filtered per token."""
    manager = FakeLaneManager(config)
    await manager.create(LaneSpec(id="personal/reddit"))
    runtime = node_runtime(config, manager, FakeInteraction())
    seen: list[tuple[str, str | None]] = []

    def wrap(spec, rt):
        async def run(**arguments):
            seen.append((spec.name, current_request_token()))
            return await spec.handler(OwnerContext(runtime=rt, tool=spec.name), **arguments)
        return run

    def visible(token, specs):
        return specs if token == TOKEN else [s for s in specs if s.effect is Effect.READ]

    def extra(rt):
        return [ToolSpec(name="ping", handler=ping)]

    mcp = build_mcp(runtime, [tool_specs, extra], wrap, visible=visible)
    app = build_app(runtime, mcp, accept_token=lambda token: token == GRANT)
    server = daemon._Uvicorn.server(app, config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        url = f"http://localhost:{daemon.bound_port(server)}/mcp"
        names = {}
        for token in (TOKEN, GRANT):
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
                Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
            ):
                names[token] = {t.name for t in (await client.list_tools()).tools}
                result = await client.call_tool("ping", {"word": token[:5]})
                assert result.structured_content == {"word": token[:5], "principal": "agent"}
                snap = await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
                assert not snap.is_error and "[ref=e1]" in snap.content[0].text
                act = await client.call_tool("browser_act", {"on": "personal/reddit", "ref": "e1", "action": "click"})
                assert act.is_error == (token == GRANT), act.content[0].text
        assert "browser_act" in names[TOKEN] and "browser_act" not in names[GRANT]
        assert names[TOKEN] - names[GRANT] == {"browser_act", "browser_challenge"}  # the draft tools
        assert seen == [("ping", TOKEN), ("browser_snapshot", TOKEN), ("browser_act", TOKEN),
                        ("ping", GRANT), ("browser_snapshot", GRANT)]

        async with httpx2.AsyncClient() as anonymous:
            assert (await anonymous.post(url, json=INIT, headers=MCP_HEADERS)).status_code == 401
            bad = await anonymous.post(url, json=INIT, headers=MCP_HEADERS | {
                "Authorization": f"Bearer {TOKEN}", "Host": "evil.example"})
            assert bad.status_code == 421
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


def test_build_app_needs_a_config():
    with pytest.raises(Exception, match="config is not configured"):
        build_app(Runtime(), build_mcp(Runtime(), []))


# ---- the panel at the root, and lifespan for every mounted app ----

def lifespan_app(name: str, events: list[str], *, fail: bool = False) -> Starlette:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        events.append(f"start {name}")
        if fail:
            raise RuntimeError("no")
        yield
        events.append(f"stop {name}")

    async def where(request):
        return JSONResponse({"app": name, "path": request.url.path})

    return Starlette(routes=[Route("/{path:path}", where)], lifespan=lifespan)


def test_root_app_serves_everything_else_without_shadowing_mcp(config):
    events: list[str] = []
    panel = lifespan_app("panel", events)
    with TestClient(app_for(config, api_app=panel, root_app=panel), base_url="http://localhost:7400") as client:
        assert client.get("/").json() == {"app": "panel", "path": "/"}
        assert client.get("/needs-you/ap_1").json()["app"] == "panel"
        assert client.get("/api/status").json() == {"app": "panel", "path": "/api/status"}
        assert client.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        ok = client.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200, ok.text
        # MCP clients look for auth metadata here; it stays behind the bearer check, never the page shell
        assert client.get("/.well-known/oauth-protected-resource").status_code == 401
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 421
        assert events == ["start panel"]
    assert events == ["start panel", "stop panel"]


def test_lifespan_reaches_every_mounted_app_in_order(config):
    events: list[str] = []
    app = app_for(config, api_app=lifespan_app("api", events), viewer_app=lifespan_app("viewer", events))
    with TestClient(app, base_url="http://localhost:7400") as client:
        ok = client.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200  # the MCP session manager started too
    assert events == ["start api", "start viewer", "stop viewer", "stop api"]


def test_a_failed_mounted_startup_stops_the_others(config):
    events: list[str] = []
    app = app_for(config, api_app=lifespan_app("api", events), viewer_app=lifespan_app("viewer", events, fail=True))
    with pytest.raises(Exception), TestClient(app, base_url="http://localhost:7400"):  # noqa: B017
        pass
    assert events == ["start api", "start viewer", "stop api"]


def test_an_app_without_lifespan_is_served_without_one(config):
    async def bare(scope, receive, send):
        assert scope["type"] == "http"  # raises on lifespan, like many plain ASGI apps
        await JSONResponse({"bare": True})(scope, receive, send)

    with TestClient(app_for(config, viewer_app=bare), base_url="http://localhost:7400") as client:
        assert client.get("/view/x").json() == {"bare": True}
