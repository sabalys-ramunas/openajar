"""`openajar mcp` (openajar.client.mcp_command): bridge to the local node when it runs, start it when it is
stopped, and when openajar was never set up, still answer initialize and say what to run."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openajar.client import bridge
from openajar.client import mcp_command as command
from openajar.client.state import Connection
from openajar.local import home as local_home

PORT = 7999


class FakeTokens:
    def __init__(self, token: str | None):
        self.token = token

    def get(self, name: str) -> str | None:
        return self.token


class FakeClient:
    def __init__(self, conn: Connection | None, token: str | None = "t" * 32):
        self.conn, self.token = conn, token

    def get(self, name: str) -> Connection:
        from openajar.client.errors import NotConnected

        if self.conn is None or name != self.conn.name:
            raise NotConnected(f"no connection named {name}")
        return self.conn

    def tokens(self, kind: str) -> FakeTokens:
        return FakeTokens(self.token)


def local_conn() -> Connection:
    return Connection(name="local", ssh_host="localhost", remote_port=PORT, local_port=PORT, node_name="local",
                      token_store="file", service="none", credential="agent_grant")


@pytest.fixture
def bridged(monkeypatch):
    calls: list = []
    monkeypatch.setattr(bridge, "serve_connection", lambda client, conn, stdin=None: calls.append(conn))
    return calls


def messages(*items: dict) -> io.BytesIO:
    return io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in items))


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def test_a_running_local_node_is_bridged_and_nothing_is_started(tmp_path, bridged):
    home = tmp_path / "openajar"
    local_home.ensure(home, PORT)
    started, logged = [], []
    deps = command.Deps(home=home, client=FakeClient(local_conn()), answers=lambda port: port == PORT,
                        start=lambda *a: started.append(a), log=logged.append)
    assert command.run(deps=deps, stdin=io.BytesIO()) == 0
    assert [c.name for c in bridged] == ["local"] and started == [] and logged == []


def test_a_stopped_local_node_is_started_on_its_own_port_then_bridged(tmp_path, bridged):
    home = tmp_path / "openajar"
    local_home.ensure(home, PORT)
    started, logged = [], []

    def start(where: Path, port: int) -> None:
        started.append((where, port))

    deps = command.Deps(home=home, client=FakeClient(local_conn()), answers=lambda port: False, start=start,
                        log=logged.append)
    assert command.run(deps=deps, stdin=io.BytesIO()) == 0
    assert started == [(home, PORT)] and [c.name for c in bridged] == ["local"]
    assert any("starting it" in line for line in logged)


def test_a_local_node_that_does_not_start_is_explained_not_bridged(tmp_path, bridged):
    home = tmp_path / "openajar"
    local_home.ensure(home, PORT)
    out, logged = io.BytesIO(), []
    deps = command.Deps(home=home, client=FakeClient(local_conn()), answers=lambda port: False,
                        start=lambda *a: "port 7999 is already in use.", log=logged.append)
    assert command.run(deps=deps, stdin=messages(INIT), out=out) == 0
    answer = json.loads(out.getvalue())
    assert bridged == [] and "did not start: port 7999 is already in use" in answer["result"]["instructions"]
    assert "openajar local start" in logged[-1]


def test_a_home_without_access_for_the_agents_says_run_local_start(tmp_path, bridged):
    home = tmp_path / "openajar"
    local_home.ensure(home, PORT)
    for client in (FakeClient(None), FakeClient(local_conn(), token=None)):
        out = io.BytesIO()
        deps = command.Deps(home=home, client=client, answers=lambda port: True, log=lambda _: None)
        command.run(deps=deps, stdin=messages(INIT), out=out)
        assert "Run `openajar local start`" in json.loads(out.getvalue())["result"]["instructions"]
    assert bridged == []


def test_never_set_up_it_answers_and_says_what_to_install(tmp_path, bridged):
    out, logged = io.BytesIO(), []
    deps = command.Deps(home=tmp_path / "nothing-here", log=logged.append)
    stdin = messages(INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": command.HELP_TOOL, "arguments": {}}},
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "environment_open"}},
                     {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                     {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
                     {"jsonrpc": "2.0", "id": 7, "method": "completion/complete"})
    assert command.run(deps=deps, stdin=stdin, out=out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6, 7]  # the notification got no answer
    init = replies[0]["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "openajar"
    for text in (init["instructions"], replies[2]["result"]["content"][0]["text"], logged[0]):
        assert command.INSTALL_LINE in text and "openajar setup" in text and "Docker" in text
    assert [t["name"] for t in replies[1]["result"]["tools"]] == [command.HELP_TOOL]
    assert replies[2]["result"]["isError"] is False and replies[3]["result"]["isError"] is True
    assert replies[4]["result"] == {} and replies[5]["result"] == {"resources": []}
    assert replies[6]["error"]["code"] == -32601
    assert bridged == [] and not (tmp_path / "nothing-here").exists()  # it wrote nothing
    assert "\u2014" not in out.getvalue().decode() + "".join(logged)


def test_the_command_on_a_fresh_computer_answers_on_stdout_and_explains_on_stderr(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAJAR_", "XDG_"))}
    env["HOME"] = str(tmp_path)
    stdin = b"".join(json.dumps(m).encode() + b"\n" for m in (INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    proc = subprocess.run([sys.executable, "-m", "openajar.cli", "mcp"], input=stdin, capture_output=True,
                          env=env, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]  # only MCP messages on stdout
    assert [r["id"] for r in replies] == [1, 2]
    assert b"openajar mcp: openajar is installed but not set up" in proc.stderr
    assert list(tmp_path.iterdir()) == []


def test_mcp_bridge_still_bridges_a_named_connection(monkeypatch):
    from openajar.client import ops

    calls = []
    monkeypatch.setattr(ops, "Client", lambda out=None: FakeClient(local_conn()))
    monkeypatch.setattr(bridge, "serve_connection", lambda client, conn, stdin=None: calls.append(conn.name))
    monkeypatch.setattr(bridge, "quit_on_signals", lambda: None)
    assert bridge.run("local") == 0 and calls == ["local"]
    assert bridge.run("other") == 1


def test_the_npm_launcher_tells_the_same_install_line():
    launcher = Path(__file__).resolve().parents[2] / "integrations/npm/bin/openajar.js"
    assert command.INSTALL_LINE in launcher.read_text()
