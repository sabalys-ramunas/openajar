"""Separate human and agent credentials, end to end against the fakes (test_client_fakes.py).

`openajar connect` gives the agents their own grant and the owner a device credential; the
node token is used through the tunnel during connect and never stored. Then:
- the dashboard and `openajar approvals` on the Mac list and deny what the agents asked, but the owner's
  saved credential approves nothing: approving is a person's, in the panel (docs/THREAT-MODEL.md);
- the agents' grant never decides its own asks, and the owner's credential never calls MCP;
- an old connection that holds the node token is warned about and upgraded.

HOME is a temp dir and PATH holds only the fakes, so nothing here touches the real Keychain,
LaunchAgents or Claude Code configuration.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import shutil
import socket

import pytest
from test_client_fakes import FakeSystem, NodeServer
from typer.testing import CliRunner

from openajar.client import credentials, ops
from openajar.client.cli import app
from openajar.client.mcp_http import Failure, McpError, McpSession

TOKEN = "Tk_" + "q1W2e3R4t5" * 4  # the node's own token
EM_DASH = "—"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def system(tmp_path, home, monkeypatch):
    fake = FakeSystem(tmp_path / "fake")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for var in ("OPENAJAR_HOME", "OPENAJAR_URL", "OPENAJAR_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    for key, value in fake.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ops, "current_platform", lambda: "macos")
    monkeypatch.setattr(ops, "DEFAULT_LOCAL_PORT", free_port())
    monkeypatch.setattr(ops, "VERIFY_SECONDS", 8.0)
    monkeypatch.setattr(credentials, "computer_name", lambda: "testmac")  # the grant subject and device label
    for tool in ("ssh", "security", "launchctl", "systemctl", "claude"):
        assert shutil.which(tool) == str(fake.bin / tool), "a real system tool is reachable"
    yield fake
    fake.cleanup()
    assert TOKEN not in fake.everything_visible()


@pytest.fixture
def node(tmp_path, system):
    server = NodeServer(tmp_path / "node-home", TOKEN).start()
    system.add_node("mynode", server.home)
    yield server
    server.stop()


def run(*args: str):
    result = CliRunner().invoke(app, list(args))
    assert EM_DASH not in result.output
    return result


def record(home) -> dict:
    return json.loads((home / ".config/openajar/connections/mynode.json").read_text())


def items(system: FakeSystem) -> tuple[str | None, str | None]:
    kc = system.keychain()
    return kc.get("openajar/mynode"), kc.get("agentos-owner/mynode")


def connected(system, home) -> tuple[str, str]:
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    agent, device = items(system)
    assert agent and device and TOKEN not in (agent, device)
    assert TOKEN not in json.dumps(system.keychain())
    return agent, device


def asked_by(node: NodeServer, agent: str, summary: str):
    """What a routed call of the agent leaves: a pending approval in its grant family."""
    policy = node.runtime.policy
    principal, grant = policy.resolve(agent)
    return policy.approvals.create_action(
        effect="commit", tool="browser_act", lane="biz-a/instagram", domain="instagram.com", summary=summary,
        content_hash="h-" + summary.replace(" ", "-"), preview=None, requester=principal, grant_id=grant.id,
        grant_path=grant.path, reason="commit needs the owner")


def post(port: int, path: str, *, headers: dict, body: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("localhost", port, timeout=10)
    try:
        conn.request("POST", path, body=json.dumps(body or {}), headers={"Content-Type": "application/json",
                                                                         "Accept": "application/json", **headers})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    return resp.status, (json.loads(raw) if raw else {})


# ---- connect ----


def test_connect_issues_a_grant_and_stores_no_node_token(system, node, home):
    agent, device = connected(system, home)
    rec = record(home)
    assert rec["credential"] == "agent_grant" and rec["agent_subject"] == "claude-code@testmac"
    assert TOKEN not in json.dumps(rec) and agent not in json.dumps(rec) and device not in json.dumps(rec)
    # the agents' grant is a real, non-admin grant of this node, revocable on its own
    _, grant = node.runtime.policy.resolve(agent)
    assert grant.id == rec["agent_grant_id"] and not grant.admin and not grant.human
    assert grant.subject.kind == "agent" and grant.subject.id == "claude-code@testmac"
    assert grant.ceiling.value == "commit" and grant.root_id == grant.id
    # nothing on this computer holds the node token: not the store, the record, files, or a command line
    for path in (home / ".config/openajar").rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text()
    assert TOKEN not in system.security_stdin()


def test_options_set_the_grant_and_a_change_replaces_it(system, node, home):
    result = run("connect", "mynode", "--ceiling", "draft", "--client", "codex", "--special", "exec",
                 "--agent-ttl", "30d")
    assert result.exit_code == 0, result.output
    first = record(home)
    assert (first["agent_subject"], first["agent_ceiling"], first["agent_specials"]) == ("codex@testmac", "draft",
                                                                                        ["exec"])
    agent, _ = items(system)
    _, grant = node.runtime.policy.resolve(agent)
    assert grant.ceiling.value == "draft" and grant.blocks[0].specials.exec is not None

    # the same again (no options: what the connection had) keeps it
    again = run("connect", "mynode")
    assert again.exit_code == 0 and "kept" in again.output and record(home)["agent_grant_id"] == first["agent_grant_id"]

    # a new ceiling issues a new grant and revokes the old one
    raised = run("connect", "mynode", "--ceiling", "commit")
    assert raised.exit_code == 0, raised.output
    assert f"revoked the previous agent grant {first['agent_grant_id']}" in raised.output
    assert node.runtime.policy.grant_record(first["agent_grant_id"])["state"] == "revoked"
    assert record(home)["agent_specials"] == ["exec"]  # what it had, unless asked otherwise

    bad = run("connect", "mynode", "--ceiling", "everything")
    assert bad.exit_code == 1 and "--ceiling must be one of" in bad.output


def test_disconnect_revokes_the_grant_on_the_node(system, node, home):
    from openajar.policy.errors import Denied

    agent, _ = connected(system, home)
    gid = record(home)["agent_grant_id"]
    result = run("disconnect", "mynode")
    assert result.exit_code == 0, result.output
    assert f"agent grant {gid} (revoked on the node)" in result.output
    assert node.runtime.policy.grant_record(gid)["state"] == "revoked"
    with pytest.raises(Denied):
        node.runtime.policy.resolve(agent)
    assert system.keychain() == {}


def test_disconnect_with_the_node_down_says_how_to_revoke_there(system, node, home):
    connected(system, home)
    gid = record(home)["agent_grant_id"]
    node.stop()
    result = run("disconnect", "mynode")
    assert result.exit_code == 0, result.output
    assert f"could not revoke agent grant {gid} on the node" in result.output
    assert f"ssh mynode openajar grants revoke {gid}" in result.output
    assert system.keychain() == {} and "connection record" in result.output


# ---- the owner on the Mac ----


def test_dash_cannot_approve_with_the_owner_credential_but_it_denies(system, node, home):
    from openajar.tui.cli import owner_credential
    from openajar.tui.client import ApiClient, ApiError
    from openajar.tui.connect import resolve

    agent, device = connected(system, home)
    ap = asked_by(node, agent, "post the reel")
    other = asked_by(node, agent, "delete the draft")

    # what `openajar dash` resolves on the Mac: the tunnel and the owner's device credential
    conn = resolve(env={}, owner=owner_credential)
    assert conn.session == device and not conn.token and conn.url == f"http://127.0.0.1:{record(home)['local_port']}"
    assert device not in repr(conn) and device not in conn.describe() and "owner credential" in conn.describe()

    async def decide() -> tuple[list, dict, ApiError, dict]:
        client = ApiClient(conn.url, conn.token, session=conn.session)
        try:
            status = await client.status()
            pending = await client.approvals()
            with pytest.raises(ApiError) as refused:  # a saved credential on this computer is no person
                await client.approve(ap.id)
            return pending, status, refused.value, await client.deny(other.id, note="not now")
        finally:
            await client.aclose()

    pending, status, refused, denied = asyncio.run(decide())
    assert status["principal"] == {"kind": "human", "id": "owner", "owner": True}
    assert {a["id"] for a in pending} == {ap.id, other.id}
    assert refused.status == 403 and refused.code == "presence_required"
    assert node.runtime.policy.approvals.get(ap.id).state == "pending"
    assert denied["state"] == "denied"  # deny still works from the Mac
    assert node.runtime.policy.approvals.get(other.id).state == "denied"


def test_approvals_from_the_mac_with_the_cli(system, node, home):
    from openajar.policy.cli import approvals_app

    agent, _ = connected(system, home)
    first = asked_by(node, agent, "send the invoice")
    second = asked_by(node, agent, "delete the draft")
    runner = CliRunner()

    listed = runner.invoke(approvals_app, ["list", "--node", "mynode"])
    assert listed.exit_code == 0, listed.output
    assert first.id in listed.output and second.id in listed.output and "commit on biz-a/instagram" in listed.output

    approved = runner.invoke(approvals_app, ["approve", first.id])  # the only connection, no node.toml here
    assert approved.exit_code == 1, approved.output
    assert "needs you in person" in approved.output and "deny still works here" in approved.output
    assert f"http://localhost:{record(home)['local_port']}/needs-you" in approved.output  # the panel, through the tunnel
    assert node.runtime.policy.approvals.get(first.id).state == "pending"
    denied = runner.invoke(approvals_app, ["deny", second.id, "--node", "mynode", "--note", "not now"])
    assert denied.exit_code == 0 and f"{second.id} denied" in denied.output
    assert node.runtime.policy.approvals.get(second.id).note == "not now"


def test_the_agent_grant_cannot_approve_its_own_request(system, node, home):
    from openajar.policy.errors import Denied

    agent, _ = connected(system, home)
    ap = asked_by(node, agent, "pay the supplier")
    port = record(home)["local_port"]

    status, body = post(port, f"/api/approvals/{ap.id}/approve", headers={"Authorization": f"Bearer {agent}"})
    assert status == 403 and body["error"]["code"] == "presence_required"
    status, body = post(port, "/api/approvals/batch", headers={"Authorization": f"Bearer {agent}"},
                        body={"ids": [ap.id], "decision": "approve"})
    assert status == 403
    # in the policy itself: an agent's grant is never a decider
    _, grant = node.runtime.policy.resolve(agent)
    with pytest.raises(Denied) as refused:
        node.runtime.policy.decide(ap.id, "approve", decider=grant, in_person=False)
    assert refused.value.reason in ("not_approver", "self_approval")
    # nor can it make itself an owner credential: only the node token may
    status, _ = post(port, "/api/session/device", headers={"Authorization": f"Bearer {agent}"},
                     body={"label": "sneaky"})
    assert status == 403
    assert node.runtime.policy.approvals.get(ap.id).state == "pending"


def test_the_owner_credential_is_refused_on_mcp(system, node, home):
    _, device = connected(system, home)
    port = record(home)["local_port"]
    with pytest.raises(McpError) as refused:
        McpSession(port, device).initialize()
    assert refused.value.failure is Failure.UNAUTHORIZED
    # as the session cookie too: /mcp reads only a bearer token
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    status, _ = post(port, "/mcp", headers={"Cookie": f"agentos_session={device}",
                                            "Accept": "application/json, text/event-stream"}, body=init)
    assert status == 401
    # and the panel's bearer path refuses it as a token
    status, _ = post(port, "/api/lanes/biz-a/instagram/open", headers={"Authorization": f"Bearer {device}"})
    assert status == 401


def test_the_owner_credential_is_never_in_claude_codes_reach(system, node, home):
    agent, device = connected(system, home)
    helper = (home / ".config/openajar/bin/openajar-mcp-headers").read_text()
    assert "agentos-owner" not in helper and "/owner/" not in helper
    entry = json.dumps(system.claude_servers())
    assert device not in entry and agent not in entry and "agentos-owner" not in entry


# ---- migration ----


def make_old_connection(system: FakeSystem, home) -> None:
    """What a client before the split left: the record without credential fields, the node token
    in the agents' Keychain item, and no owner credential."""
    connected(system, home)
    rec = record(home)
    old = {k: rec[k] for k in ("name", "ssh_host", "remote_home", "remote_port", "local_port", "node_name",
                               "token_store", "service", "claude_registered", "updated_at")}
    (home / ".config/openajar/connections/mynode.json").write_text(json.dumps(old))
    system._save("keychain.json", {"openajar/mynode": TOKEN})


def test_doctor_warns_while_a_connection_holds_the_node_token_and_upgrade_fixes_it(system, node, home,
                                                                                   tmp_path, monkeypatch):
    from openajar.ops import cli as ops_cli

    make_old_connection(system, home)
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "not-a-node"))  # this computer has no node.toml
    doctor = CliRunner().invoke(ops_cli.app, ["doctor"])
    assert "This computer is not a node; checking its connections" in doctor.output
    assert "WARN" in doctor.output and "node's own token" in doctor.output
    assert "openajar connect --upgrade" in doctor.output and "openajar node rotate-token" in doctor.output
    assert TOKEN not in doctor.output and EM_DASH not in doctor.output
    nodes = run("nodes")
    assert "node token (run openajar connect --upgrade)" in nodes.output and "warning:" in nodes.output
    monkeypatch.delenv("OPENAJAR_HOME")

    # it keeps working as it did, on the node token
    assert run("status", "mynode").exit_code == 0

    upgraded = run("connect", "--upgrade")
    assert upgraded.exit_code == 0, upgraded.output
    assert "moved from the node token to its own grant" in upgraded.output
    agent, device = items(system)
    assert agent and agent.startswith("agt2_") and device and TOKEN not in json.dumps(system.keychain())
    assert record(home)["credential"] == "agent_grant"
    assert run("status", "mynode").exit_code == 0
    assert run("connect", "--upgrade").output.startswith("Every connection already has its own grant")

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "not-a-node"))
    doctor = CliRunner().invoke(ops_cli.app, ["doctor", "--json"])
    checks = json.loads(doctor.output)["checks"]
    assert [(c["id"], c["status"]) for c in checks] == [("connections", "pass"), ("claude_profiles", "pass")]
    assert checks[0]["data"]["grant"] == record(home)["agent_grant_id"]


def test_an_old_connection_upgrades_by_connecting_again(system, node, home):
    make_old_connection(system, home)
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    assert record(home)["credential"] == "agent_grant" and TOKEN not in json.dumps(system.keychain())


def test_a_node_that_cannot_issue_grants_keeps_the_node_token_with_a_warning(system, tmp_path, home):
    server = NodeServer(tmp_path / "old-node", TOKEN, policy=False).start()  # no policy service: no grant routes
    system.add_node("mynode", server.home)
    try:
        result = run("connect", "mynode")
        assert result.exit_code == 0, result.output
        assert "cannot issue agent grants yet" in result.output and "openajar connect --upgrade" in result.output
        assert system.keychain() == {"openajar/mynode": TOKEN}  # no owner credential left behind either
        assert record(home)["credential"] == "node_token"
    finally:
        server.stop()


def test_open_uses_the_owner_credential(system, tmp_path, home):
    server = NodeServer(tmp_path / "node-home", TOKEN, control=True).start()
    system.add_node("mynode", server.home)
    try:
        connected(system, home)
        port = record(home)["local_port"]
        woke = run("open", "personal/reddit", "--wake", "--print")
        assert woke.exit_code == 0, woke.output
        assert f"http://localhost:{port}/view/personal/reddit" in woke.output
        traced = [e for e in server.runtime.policy.traces.query(kind="panel", limit=50)[0]
                  if e.tool == "panel.lane.view_link"]
        assert traced and traced[0].principal == "owner"  # the owner's credential, not the agents' grant
    finally:
        server.stop()


def test_a_controller_keeps_its_agent_token(system, tmp_path, home):
    from openajar.core.config import NodeConfig, save_config

    stub_home = tmp_path / "controller"
    server = NodeServer(tmp_path / "ctl-node", "aga_" + "c" * 40).start()
    try:
        stub_home.mkdir()
        save_config(NodeConfig(home=stub_home, name="controller", mcp_port=server.port, mcp_token=server.token))
        (stub_home / "node.toml").write_text('kind = "controller"\n' + (stub_home / "node.toml").read_text())
        system.add_node("ctl", stub_home)
        result = run("connect", "ctl")
        assert result.exit_code == 0, result.output
        assert "this is a controller" in result.output
        assert system.keychain() == {"openajar/ctl": server.token}
        rec = json.loads((home / ".config/openajar/connections/ctl.json").read_text())
        assert rec["credential"] == "controller"
        assert "controller agent token" in run("nodes").output
    finally:
        server.stop()


def test_a_failed_connect_leaves_no_grant_or_owner_credential_on_the_node(system, node, home, monkeypatch):
    real = credentials.probe  # the agent grant's check, in the credential steps

    def refuse_the_grant(port, token, **kw):
        if token != TOKEN:
            raise McpError(Failure.UNAUTHORIZED, "the node refused the token (401)")
        return real(port, token, **kw)

    monkeypatch.setattr(credentials, "probe", refuse_the_grant)
    result = run("connect", "mynode")
    assert result.exit_code == 1 and "did not accept the agent grant" in result.output
    grants = node.runtime.policy.list_grants(include_inactive=True)
    assert len(grants) == 1 and grants[0]["state"] == "revoked"
    from openajar.api.auth import PanelStore
    _, device = items(system)
    assert device is None or PanelStore.for_home(node.home / "run").get_session(device) is None
