"""`openajar connect | disconnect | nodes | status` end to end, against fakes only.

HOME is a temp dir and PATH holds only the fakes from test_client_fakes.py, so nothing
here can touch the real Keychain, launchd, systemd or Claude Code configuration. The
node is a real agentosd (FakeLaneManager and the real policy service) on loopback; the
fake ssh forwards to it. TOKEN is the node's own token: connect reads it, uses it through
the tunnel to get the agents' grant and the owner's device credential, and never stores it.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import urllib.request

import pytest
from test_client_fakes import FakeSystem, NodeServer, wait_until
from typer.testing import CliRunner

import openajar
from openajar.client import credentials, ops
from openajar.client import install as install_mod
from openajar.client.cli import app
from openajar.client.mcp_http import McpSession
from openajar.client.ports import port_in_use

TOKEN = "Tk_" + "a1B2c3D4e5" * 4
NEW_TOKEN = "Nw-" + "z9Y8x7W6v5" * 4
EM_DASH = "\u2014"
LABEL = "dev.agentos.tunnel.mynode"


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
    # Whatever the test did, no fake ever saw the token on its command line (what `ps` shows).
    visible = fake.everything_visible()
    assert TOKEN not in visible and NEW_TOKEN not in visible


@pytest.fixture
def node(tmp_path, system):
    server = NodeServer(tmp_path / "node-home", TOKEN).start()
    system.add_node("mynode", server.home)
    yield server
    server.stop()


def run(*args: str):
    result = CliRunner().invoke(app, list(args))
    if result.exception and not isinstance(result.exception, SystemExit):
        import traceback
        traceback.print_exception(result.exception)
    assert EM_DASH not in result.output
    return result


def stored(system: FakeSystem, name: str = "mynode") -> tuple[str, str]:
    """The connection's two items in the fake Keychain: the agents' grant token, the owner's device credential."""
    items = system.keychain()
    assert set(items) == {f"openajar/{name}", f"agentos-owner/{name}"}, items
    agent, device = items[f"openajar/{name}"], items[f"agentos-owner/{name}"]
    assert agent.startswith("agt2_") and TOKEN not in (agent, device)
    return agent, device


def port_of(home) -> int:
    record = json.loads((home / ".config/openajar/connections/mynode.json").read_text())
    return record["local_port"]


def plist(home) -> dict:
    return plistlib.loads((home / f"Library/LaunchAgents/{LABEL}.plist").read_bytes())


def helper_output(system: FakeSystem, home, name: str = "mynode") -> str:
    helper = home / ".config/openajar/bin/openajar-mcp-headers"
    proc = subprocess.run(["/bin/sh", "-c", f"'{helper}' {name}"], capture_output=True, text=True,
                          env={**system.env(), "HOME": str(home)}, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# ---- connect ----


def test_connect_sets_up_tunnel_keychain_and_claude(system, node, home):
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    port = port_of(home)
    uid = os.getuid()
    plist_path = home / f"Library/LaunchAgents/{LABEL}.plist"
    log = str(home / "Library/Logs/openajar/tunnel-mynode.log")

    # The node was read over SSH with the script on stdin; the remote command line is just `sh -s`.
    assert system.argv("ssh")[0] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-T", "mynode", "sh", "-s"]
    # The agents' grant and the owner's credential went to two Keychain items through `security -i`
    # on stdin, never as an argument; the node token was never stored.
    assert ["-i"] in system.argv("security")
    agent, device = stored(system)
    assert f"add-generic-password -U -s openajar -a mynode -w {agent}" in system.security_stdin()
    assert f"add-generic-password -U -s agentos-owner -a mynode -w {device}" in system.security_stdin()
    assert TOKEN not in system.security_stdin()
    record = json.loads((home / ".config/openajar/connections/mynode.json").read_text())
    assert record["credential"] == "agent_grant" and record["agent_subject"] == "claude-code@testmac"
    # the node knows how this computer reaches it for that grant, for browser_view's link and tunnel
    routes = json.loads((node.home / "run" / "connect-routes.json").read_text())  # the daemon's home
    assert {k: routes[record["agent_grant_id"]][k] for k in ("ssh_host", "local_port")} == {
        "ssh_host": "mynode", "local_port": port}
    # memory_read by default: the node's instructions ask the agent for memory_context first
    assert record["agent_ceiling"] == "commit" and record["agent_specials"] == ["environments", "memory_read"]
    grant = node.runtime.policy.grant_record(record["agent_grant_id"])
    assert {k: v for k, v in grant["subject"].items() if v} == {"kind": "agent", "id": "claude-code@testmac",
                                                               "owner": "owner"}
    assert grant["scope"]["blocks"][0]["workspaces"] == ["*"] and grant["scope"]["ceiling"] == "commit"
    assert grant["scope"]["admin"] is False and grant["scope"]["specials"] == ["exec", "memory", "secret", "vm"]
    _, g = node.runtime.policy.resolve(agent)
    assert g.caps("memory") and not any(c.write for c in g.caps("memory"))  # reads memory, never writes it
    # environments: its own cloud computer, commands and the owner's bound logins there only, never on an account lane
    assert [c.lanes for c in g.caps("vm")] == [["env/*"]] and [c.lanes for c in g.caps("exec")] == [["env/*"]]
    assert [c.lanes for c in g.caps("secret")] == [["env/*"]]

    assert plist(home) == {
        "Label": LABEL,
        "ProgramArguments": [
            str(system.bin / "ssh"), "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-L", f"{port}:localhost:{node.port}", "mynode",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    assert system.argv("launchctl")[-1] == ["bootstrap", f"gui/{uid}", str(plist_path)]
    assert LABEL in system.launchd_jobs()

    helper = home / ".config/openajar/bin/openajar-mcp-headers"
    assert system.argv("claude") == [
        ["mcp", "remove", "--scope", "user", "openajar-mynode"],
        ["mcp", "add-json", "--scope", "user", "openajar-mynode",
         json.dumps({"type": "http", "url": f"http://localhost:{port}/mcp", "headersHelper": f"{helper} mynode"})],
    ]  # an entry under the name before the rename is looked for in ~/.claude.json, without running claude
    assert {c["cwd"] for c in system.calls("claude")} == {str(home)}
    assert system.claude_servers()["user:openajar-mynode"]["headersHelper"] == f"{helper} mynode"

    # What Claude Code does: run the helper, then talk MCP through the tunnel with its header.
    header = json.loads(helper_output(system, home))
    assert header == {"Authorization": f"Bearer {agent}"}
    with McpSession(port, header["Authorization"].removeprefix("Bearer ")) as session:
        assert session.call("whoami")["node"] == "node-test"

    out = result.output
    assert "Connected mynode. Undo all of it with: openajar connect --remove mynode" in out
    assert "macOS Keychain (service openajar, account mynode)" in out
    assert "macOS Keychain (service agentos-owner, account mynode)" in out
    assert f"Agent grant {record['agent_grant_id']} issued for claude-code@testmac: every lane, up to commit" in out
    assert f"localhost:{port} -> mynode:{node.port}" in out
    assert "Claude Code: registered openajar-mynode (user scope)" in out
    # Codex is not on this computer (no `codex`, no ~/.codex), so its config is not created
    assert "Codex: not installed" in out and not (home / ".codex").exists()
    assert "Claude Code skills: installed (openajar-lanes, openajar-offload in ~/.claude/skills)" in out \
        or "Claude Code skills: installed" in out
    for skill in ("openajar-lanes", "openajar-offload"):
        assert f"name: {skill}" in (home / ".claude/skills" / skill / "SKILL.md").read_text()
    assert "T3 Code: its Claude and Codex threads see this server too. Start a new thread." in out
    assert 'Try it: start a new Claude Code session and ask "list my openajar lanes".' in out

    # The node token is nowhere on this computer, and the credentials are only in the (fake) Keychain:
    # not in output, files, logs or any process's argv.
    record_path = home / ".config/openajar/connections/mynode.json"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    for text in (out, plist_path.read_text(), helper.read_text(), record_path.read_text(),
                 json.dumps(system.claude_servers()), system.everything_visible()):
        for secret in (TOKEN, agent, device):
            assert secret not in text
    for path in (home / "Library/Logs/openajar").iterdir():
        assert TOKEN not in path.read_text()
    ps = subprocess.run(["/bin/ps", "-axww", "-o", "args="], capture_output=True, text=True, check=True).stdout
    assert TOKEN not in ps and agent not in ps and device not in ps


def test_connect_again_changes_nothing(system, node, home):
    assert run("connect", "mynode").exit_code == 0
    first = stored(system)
    first_plist = (home / f"Library/LaunchAgents/{LABEL}.plist").read_bytes()
    first_port = port_of(home)
    adds = len([a for a in system.argv("security") if a == ["-i"]])

    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    assert "Tunnel already running" in result.output
    assert "Token already in macOS Keychain" in result.output
    assert "Owner credential already in macOS Keychain" in result.output and "kept" in result.output
    assert (home / f"Library/LaunchAgents/{LABEL}.plist").read_bytes() == first_plist
    assert port_of(home) == first_port
    launchctl = [a[0] for a in system.argv("launchctl")]
    assert launchctl.count("bootstrap") == 1 and "bootout" not in launchctl
    assert len([a for a in system.argv("security") if a == ["-i"]]) == adds  # no second write
    assert list(system.claude_servers()) == ["user:openajar-mynode"]
    assert stored(system) == first
    assert len(node.runtime.policy.list_grants()) == 1  # no second grant on the node


def test_the_old_claude_entry_goes_when_the_new_one_is_already_there(system, node, home):
    """An upgrade on a computer that already had openajar-mynode (an earlier run) and still has the old
    agentos-mynode: the old one goes too (`claude mcp get` does not show a helper, so its URL tells; legacy-name)."""
    assert run("connect", "mynode").exit_code == 0
    servers = system.claude_servers()
    url = servers["user:openajar-mynode"]["url"].replace("localhost", "127.0.0.1")
    servers["user:agentos-mynode"] = {"type": "http", "url": url,  # legacy-name
                                      "headersHelper": "~/.config/agentos/bin/agentos-mcp-headers mynode"}  # legacy-name
    system.set_claude_servers(servers)
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    assert list(system.claude_servers()) == ["user:openajar-mynode"]
    assert "removed agentos-mynode from ~/.claude.json, its name before the rename" in result.output  # legacy-name


# ---- every Claude Code profile (field run 2026-11-21: a session on CLAUDE_CONFIG_DIR=~/.claude_work had no tools) ----


@pytest.fixture
def profiles(tmp_path, home, monkeypatch):
    """Two more Claude Code profiles: ~/.claude_work (a sibling folder with a .claude.json) holding an entry from
    before the rename at a port where nothing listens any more, and the folder $CLAUDE_CONFIG_DIR names, outside
    HOME and not used yet. Returns (work, named)."""
    work, named = home / ".claude_work", tmp_path / "other-profile"
    work.mkdir()
    dead = free_port()
    with socket.socket() as live:  # something listens here: an agentos-* entry that answers is never touched
        live.bind(("127.0.0.1", 0))
        live.listen()
        (work / ".claude.json").write_text(json.dumps({"userID": "x", "mcpServers": {
            "agentos-agentvm": {"type": "http", "url": f"http://localhost:{dead}/mcp"},  # legacy-name: a dead port
            "agentos-mine": {"type": "http", "url": f"http://127.0.0.1:{live.getsockname()[1]}/mcp"},  # legacy-name
            "agentos-far": {"type": "http", "url": "https://example.test/mcp"},  # legacy-name: not on this computer
            "hermes": {"type": "stdio", "command": "hermes"}}}))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(named))
        yield work, named


def test_connect_registers_every_claude_profile_and_replaces_dead_old_entries(system, node, home, profiles):
    work, named = profiles
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    url = f"http://localhost:{port_of(home)}/mcp"
    for profile in (None, work, named):
        assert system.claude_servers(profile)["user:openajar-mynode"]["url"] == url, profile
    # claude ran with each profile's CLAUDE_CONFIG_DIR, and without one for the default profile
    assert {c.get("profile") for c in system.calls("claude")} == {None, str(work), str(named)}
    left = system.claude_servers(work)
    assert "user:agentos-agentvm" not in left  # legacy-name: on this computer, and nothing answers there
    assert {"user:agentos-mine", "user:agentos-far", "user:hermes"} <= set(left)  # legacy-name: the owner's stay
    assert json.loads((work / ".claude.json").read_text())["userID"] == "x"  # the rest of the file is untouched
    out = " ".join(result.output.split())
    assert f"In every Claude Code profile here: ~/.claude.json, {named}/.claude.json, ~/.claude_work/.claude.json" in out
    assert "removed agentos-agentvm from ~/.claude_work/.claude.json, an entry from before the rename" in out
    assert "restart them to see the tools" in out and "`openajar call <tool>`" in out
    for dest in (home / ".claude", work, named):  # the skills go where each profile reads them
        assert (dest / "skills/openajar-environment/SKILL.md").is_file(), dest

    adds = len([a for a in system.argv("claude") if a[1] == "add-json"])
    again = run("connect", "mynode")
    assert again.exit_code == 0, again.output
    assert "Claude Code: already registered openajar-mynode" in again.output
    assert len([a for a in system.argv("claude") if a[1] == "add-json"]) == adds  # each profile had it already

    gone = run("disconnect", "mynode")
    assert gone.exit_code == 0, gone.output
    for profile in (None, work, named):
        assert "user:openajar-mynode" not in system.claude_servers(profile), profile
    assert "Claude Code entry openajar-mynode from ~/.claude_work/.claude.json" in gone.output
    assert not (work / "skills/openajar-environment").exists() and not (named / "skills/openajar-lanes").exists()


def test_status_check_names_a_profile_whose_entry_does_not_answer(system, node, home, profiles, tmp_path,
                                                                    monkeypatch):
    from openajar.ops import cli as ops_cli

    work, named = profiles
    assert run("connect", "mynode").exit_code == 0
    servers = system.claude_servers(work)
    servers["user:openajar-mynode"]["url"] = f"http://127.0.0.1:{free_port()}/mcp"  # an address from an old setup
    servers["user:agentos-agentvm"] = {"type": "http", "url": f"http://localhost:{free_port()}/mcp"}  # legacy-name
    system.set_claude_servers(servers, work)
    system.set_claude_servers({}, named)  # this profile lost the entry

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "not-a-node"))
    doctor = CliRunner().invoke(ops_cli.app, ["doctor"])
    assert doctor.exit_code == 1, doctor.output
    out = " ".join(doctor.output.split())
    assert EM_DASH not in doctor.output
    assert "Claude Code ~/.claude_work/.claude.json" in out
    assert "openajar-mynode points at http://127.0.0.1:" in out and "an old address where nothing answers" in out
    assert "run `openajar connect mynode --name mynode` again (it writes the current address in every profile)" in out
    assert "agentos-agentvm points at" in out and "no openajar connection here uses it" in out  # legacy-name
    assert f"CLAUDE_CONFIG_DIR={work} claude mcp remove --scope user agentos-agentvm" in out  # legacy-name
    assert f"Claude Code {named}/.claude.json" in out and "sessions started with this profile do not see the tools" in out
    assert "openajar call <tool>" in out  # the way in until it is fixed

    monkeypatch.delenv("OPENAJAR_HOME")
    check = run("connect", "--check")
    assert check.exit_code == 1
    assert "FAIL Claude Code: ~/.claude_work/.claude.json: openajar-mynode points at" in check.output
    assert "openajar call <tool>" in check.output

    assert run("connect", "mynode").exit_code == 0  # connect fixes every profile
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "not-a-node"))
    doctor = CliRunner().invoke(ops_cli.app, ["doctor", "--json"])
    checks = {c["id"]: c for c in json.loads(doctor.output)["checks"]}
    assert checks["claude_profiles"]["status"] == "pass", checks


def test_toolsets_go_in_the_url_and_stay_until_changed(system, node, home):
    helper = home / ".config/openajar/bin/openajar-mcp-headers"

    def registered_url() -> str:
        return system.claude_servers()["user:openajar-mynode"]["url"]

    result = run("connect", "mynode", "--toolsets", "flows, files")  # files: the browser toolset's old name
    assert result.exit_code == 0, result.output
    port = port_of(home)
    assert registered_url() == f"http://localhost:{port}/mcp?toolsets=core,browser,flows"
    assert "Tools listed: core, browser, flows; agents load the rest with tools_more" in result.output

    # connecting again keeps the choice; full and default replace it
    assert run("connect", "mynode").exit_code == 0
    assert registered_url().endswith("?toolsets=core,browser,flows")
    assert run("connect", "mynode", "--toolsets", "full").exit_code == 0
    assert registered_url().endswith("/mcp?toolsets=full")
    result = run("connect", "mynode", "--toolsets", "default")
    assert result.exit_code == 0 and registered_url() == f"http://localhost:{port}/mcp"
    assert "Tools listed: the node's default" in result.output
    assert system.claude_servers()["user:openajar-mynode"]["headersHelper"] == f"{helper} mynode"

    bad = run("connect", "mynode", "--toolsets", "files,nope")
    assert bad.exit_code == 1 and "no toolset named nope" in bad.output and "accounts, browser, devtools" in bad.output
    assert registered_url() == f"http://localhost:{port}/mcp"  # a refused choice changes nothing


def test_default_port_taken_uses_the_next_free_one(system, node, home, monkeypatch):
    preferred = next(p for p in iter(free_port, None) if not port_in_use(p + 1))
    monkeypatch.setattr(ops, "DEFAULT_LOCAL_PORT", preferred)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", preferred))
        busy.listen()
        result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    assert port_of(home) == preferred + 1
    assert f"local port {preferred} is in use, so this tunnel uses {preferred + 1}" in result.output
    assert f"{preferred + 1}:localhost:{node.port}" in plist(home)["ProgramArguments"]


def test_explicit_port_in_use_fails_before_installing(system, node, home):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        result = run("connect", "mynode", "--local-port", str(taken))
    assert result.exit_code == 1
    assert f"local port {taken} is already in use" in result.output
    assert not (home / f"Library/LaunchAgents/{LABEL}.plist").exists()
    assert "bootstrap" not in [a[0] for a in system.argv("launchctl")]


def test_named_connection_and_custom_port(system, node, home):
    port = free_port()
    result = run("connect", "mynode", "--name", "home-box", "--local-port", str(port))
    assert result.exit_code == 0, result.output
    args = plistlib.loads((home / "Library/LaunchAgents/dev.agentos.tunnel.home-box.plist").read_bytes())
    assert f"{port}:localhost:{node.port}" in args["ProgramArguments"]
    stored(system, "home-box")
    assert "user:openajar-home-box" in system.claude_servers()


@pytest.mark.parametrize("name", ["Bad Name", "-x", "a" * 33, "dots.not.allowed"])
def test_rejects_bad_names(system, name):
    result = run("connect", "mynode", "--name", name)
    assert result.exit_code != 0
    assert system.calls("ssh") == []


# ---- what can go wrong ----


def nothing_installed(system: FakeSystem, home) -> bool:
    return (not (home / "Library/LaunchAgents").exists() and system.keychain() == {}
            and system.claude_servers() == {} and not (home / ".config/openajar/connections/mynode.json").exists())


def test_wrong_ssh_host(system, node, home):
    result = run("connect", "agentvn")
    assert result.exit_code == 1
    assert "SSH does not know the host 'agentvn'" in result.output
    assert "~/.ssh/config" in result.output
    assert nothing_installed(system, home)


def test_node_unreachable(system, node, home):
    system.set_node("mynode", down=True)
    result = run("connect", "mynode")
    assert result.exit_code == 1
    assert "cannot reach mynode" in result.output and "Operation timed out" in result.output
    assert nothing_installed(system, home)


def test_ssh_login_refused(system, node, home):
    system.set_node("mynode", refuse_login=True)
    result = run("connect", "mynode")
    assert result.exit_code == 1
    assert "mynode refused the SSH login" in result.output and "ssh-add" in result.output
    assert nothing_installed(system, home)


def test_node_without_node_toml(system, tmp_path, home):
    empty = tmp_path / "empty-node"
    empty.mkdir()
    system.add_node("bare", empty)
    system.set_node("bare", sudo=False)
    result = run("connect", "bare")
    assert result.exit_code == 1
    assert "bare has no openajar node, and connect cannot install one there" in result.output
    assert system.installs() == []


def test_node_toml_not_readable(system, node, home):
    toml = node.home / "node.toml"
    toml.chmod(0)
    try:
        result = run("connect", "mynode")
    finally:
        toml.chmod(0o600)
    assert result.exit_code == 1
    assert "is not readable as" in result.output


def test_keychain_denied_then_file_store(system, node, home):
    system.deny_keychain()
    result = run("connect", "mynode")
    assert result.exit_code == 1
    assert "the macOS Keychain refused to" in result.output
    assert "--token-store file" in result.output
    assert TOKEN not in result.output
    assert not (home / "Library/LaunchAgents").exists()

    result = run("connect", "mynode", "--token-store", "file")
    assert result.exit_code == 0, result.output
    token_file = home / ".config/openajar/tokens/mynode"
    owner_file = home / ".config/openajar/owner/mynode"
    agent = token_file.read_text().strip()
    assert agent.startswith("agt2_") and owner_file.read_text().strip() not in ("", TOKEN, agent)
    for path in (token_file, owner_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(helper_output(system, home)) == {"Authorization": f"Bearer {agent}"}


def test_claude_missing_prints_the_manual_command(system, node, home):
    system.remove_tool("claude")
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    port = port_of(home)
    assert "Claude Code: not registered (`claude` is not on PATH)" in result.output
    assert "claude mcp add-json --scope user openajar-mynode" in result.output
    assert f"http://localhost:{port}/mcp" in result.output
    assert TOKEN not in result.output
    record = json.loads((home / ".config/openajar/connections/mynode.json").read_text())
    assert record["claude_registered"] is False


def test_claude_refusing_the_entry_fails_with_the_manual_command(system, node, home):
    (system.state / "claude.fail").touch()
    result = run("connect", "mynode")
    assert result.exit_code == 1
    assert "not registered (claude mcp add-json failed: Error: Invalid configuration" in result.output
    assert "claude mcp add-json --scope user openajar-mynode" in result.output
    assert LABEL in system.launchd_jobs()  # the tunnel and token stay; connect again once fixed


def test_node_daemon_down_keeps_the_tunnel_and_says_why(system, node, home, monkeypatch):
    monkeypatch.setattr(ops, "VERIFY_SECONDS", 1.5)
    node.write_toml(port=free_port())  # nothing listens there on the "node"
    result = run("connect", "mynode")
    assert result.exit_code == 1
    assert "the tunnel is up, but nothing answers on port" in result.output
    assert "systemctl status agentosd" in result.output
    assert "open failed" in result.output  # the end of the tunnel log
    assert (home / f"Library/LaunchAgents/{LABEL}.plist").exists()
    assert system.claude_servers() == {}
    assert run("disconnect", "mynode").exit_code == 0
    assert not (home / f"Library/LaunchAgents/{LABEL}.plist").exists()


def test_rotating_the_node_token_leaves_connections_on_a_grant_working(system, node, tmp_path, home):
    assert run("connect", "mynode").exit_code == 0
    agent, device = stored(system)
    port = node.port
    node.stop()
    rotated = NodeServer(node.home, NEW_TOKEN, port=port).start()  # `openajar node rotate-token` and a restart
    try:
        result = run("status", "mynode")
        assert result.exit_code == 0, result.output  # the agents' grant does not depend on the node token
        assert NEW_TOKEN not in result.output

        result = run("connect", "mynode")
        assert result.exit_code == 0, result.output
        assert "Token already in" in result.output and "kept" in result.output
        assert stored(system) == (agent, device)
        assert NEW_TOKEN not in json.dumps(system.keychain())
        assert [a[0] for a in system.argv("launchctl")].count("bootstrap") == 1  # same tunnel kept
    finally:
        rotated.stop()


# ---- nodes, status, disconnect ----


def test_nodes_and_status(system, node, home):
    assert run("nodes").output.startswith("No connections")
    assert run("connect", "mynode").exit_code == 0
    port = port_of(home)

    result = run("nodes")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 2, result.stdout
    header, row = lines
    grant = json.loads((home / ".config/openajar/connections/mynode.json").read_text())["agent_grant_id"]
    assert header.split() == ["NAME", "SSH", "HOST", "URL", "NODE", "PORT", "TUNNEL", "MCP", "AGENTS", "USE"]
    assert row.split() == ["mynode", "mynode", f"http://localhost:{port}/mcp", str(node.port), "running",
                           "ok", "(node-test)", "grant", grant, "(claude-code@testmac,", "up", "to", "commit)"]
    assert "warning" not in result.output

    result = run("status")
    assert result.exit_code == 0, result.output
    assert f"mynode: node node-test (openajar {openajar.__version__}) via http://localhost:{port}/mcp" in result.output
    assert "lanes: 2 total, 2 cold" in result.output
    assert "personal/reddit" in result.output and "biz-a/instagram" in result.output
    assert TOKEN not in result.output


def test_nodes_reports_a_dead_tunnel(system, node, home):
    assert run("connect", "mynode").exit_code == 0
    pid = system.launchd_jobs()[LABEL]["pid"]
    os.kill(pid, 15)
    assert wait_until(lambda: not port_in_use(port_of(home)))
    result = run("nodes")
    assert "not running" in result.output or "stopped" in result.output
    assert "tunnel down" in result.output
    assert run("status", "mynode").exit_code == 1


def test_status_unknown_connection(system):
    result = run("status", "nope")
    assert result.exit_code == 1
    assert "no connection named nope" in result.output


def test_disconnect_removes_everything(system, node, home):
    from openajar.policy.errors import Denied

    assert run("connect", "mynode").exit_code == 0
    port = port_of(home)
    agent, device = stored(system)
    grant = json.loads((home / ".config/openajar/connections/mynode.json").read_text())["agent_grant_id"]
    node.runtime.policy.resolve(agent)  # live before
    result = run("disconnect", "mynode")
    assert result.exit_code == 0, result.output
    assert ["bootout", f"gui/{os.getuid()}/{LABEL}"] in system.argv("launchctl")
    assert not (home / f"Library/LaunchAgents/{LABEL}.plist").exists()
    assert ["delete-generic-password", "-s", "openajar", "-a", "mynode"] in system.argv("security")
    assert ["delete-generic-password", "-s", "agentos-owner", "-a", "mynode"] in system.argv("security")
    assert system.keychain() == {}
    assert system.claude_servers() == {}
    assert not (home / ".config/openajar/connections/mynode.json").exists()
    assert not (home / ".config/openajar/bin/openajar-mcp-headers").exists()
    assert wait_until(lambda: not port_in_use(port))
    assert not (home / ".claude/skills/openajar-lanes").exists()  # the last connection takes the skills along
    for item in ("tunnel LaunchAgent", "token from macOS Keychain", "Claude Code entry openajar-mynode",
                 "Claude Code skills openajar-environment, openajar-lanes, openajar-offload, openajar-studio",
                 "connection record", f"agent grant {grant} (revoked on the node)",
                 "owner device credential (ended on the node)", "owner credential from macOS Keychain"):
        assert item in result.output
    # the grant is revoked on the node, so a copy of it stops working, and so does the device credential
    with pytest.raises(Denied) as refused:
        node.runtime.policy.resolve(agent)
    assert refused.value.reason == "revoked"
    from openajar.api.auth import PanelStore
    assert PanelStore.for_home(node.home / "run").get_session(device) is None

    again = run("disconnect", "mynode")
    assert again.exit_code == 1
    assert "no connection named mynode" in again.output


# ---- Linux: systemd user unit and a 600 token file ----


@pytest.fixture
def linux(system, monkeypatch):
    monkeypatch.setattr(ops, "current_platform", lambda: "linux")
    return system


def test_linux_uses_systemd_and_a_token_file(linux, node, home):
    system = linux
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    port = port_of(home)
    unit = home / ".config/systemd/user/agentos-tunnel-mynode.service"
    text = unit.read_text()
    assert (f"ExecStart={system.bin / 'ssh'} -N -o BatchMode=yes -o ExitOnForwardFailure=yes "
            "-o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 -o ControlMaster=no "
            f"-o ControlPath=none -L {port}:localhost:{node.port} mynode") in text
    for line in ("Restart=always", "RestartSec=10", "RestartMaxDelaySec=300", "StartLimitIntervalSec=0",
                 "WantedBy=default.target"):
        assert line in text
    assert system.argv("systemctl") == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "agentos-tunnel-mynode.service"],
        ["--user", "restart", "agentos-tunnel-mynode.service"],
    ]
    assert system.calls("security") == [] and system.calls("launchctl") == []
    token_file = home / ".config/openajar/tokens/mynode"
    owner_file = home / ".config/openajar/owner/mynode"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600 and stat.S_IMODE(owner_file.stat().st_mode) == 0o600
    helper = (home / ".config/openajar/bin/openajar-mcp-headers").read_text()
    assert "security" not in helper and "owner" not in helper
    agent = token_file.read_text().strip()
    assert json.loads(helper_output(system, home)) == {"Authorization": f"Bearer {agent}"}
    assert "systemd user unit agentos-tunnel-mynode.service" in result.output
    assert TOKEN not in result.output + text

    assert "running" in run("nodes").output
    result = run("disconnect", "mynode")
    assert result.exit_code == 0, result.output
    assert ["--user", "disable", "--now", "agentos-tunnel-mynode.service"] in system.argv("systemctl")
    assert not unit.exists() and not token_file.exists() and not owner_file.exists()
    assert system.claude_servers() == {}


def test_linux_refuses_the_keychain(linux, node):
    result = run("connect", "mynode", "--token-store", "keychain")
    assert result.exit_code == 1
    assert "the Keychain store needs macOS" in result.output


def test_linux_without_systemd_says_what_to_do(linux, node):
    linux.remove_tool("systemctl")
    result = run("connect", "mynode")
    assert result.exit_code == 1
    assert "`systemctl` is not on PATH" in result.output
    assert "autossh" in result.output


# ---- openajar open ----

@pytest.fixture
def control_node(tmp_path, system):
    server = NodeServer(tmp_path / "node-home", TOKEN, control=True).start()
    system.add_node("mynode", server.home)
    yield server
    server.stop()


def test_open_prints_a_control_link_through_the_local_end_of_the_tunnel(system, control_node, home):
    assert run("connect", "mynode").exit_code == 0
    port = port_of(home)

    asleep = run("open", "personal/reddit", "--print")
    assert asleep.exit_code == 1
    assert "asleep" in asleep.output and "--wake" in asleep.output

    woke = run("open", "personal/reddit", "--wake", "--print")
    assert woke.exit_code == 0, woke.output
    url = next(line.strip() for line in woke.output.splitlines() if line.strip().startswith("http"))
    assert url.startswith(f"http://localhost:{port}/") and "valid until" in woke.output
    assert TOKEN not in woke.output
    with urllib.request.urlopen(url, timeout=10) as page:  # the viewer answers through the tunnel
        assert page.status == 200 and "text/html" in page.headers.get("content-type", "")

    missing = run("open", "personal/nope", "--print")
    assert missing.exit_code == 1 and "error:" in missing.output


def test_open_without_a_connection_says_how_to_add_one(system):
    result = run("open", "biz-a/instagram", "--print")
    assert result.exit_code == 1
    assert "openajar connect" in result.output


# ---- a host with no node: connect installs one ----


@pytest.fixture
def bare(tmp_path, system, monkeypatch):
    """A node server whose node.toml the fake installer copies onto the bare host root@box."""
    monkeypatch.setenv("OPENAJAR_GIT_URL", "https://git.example.test/openajar.git")
    server = NodeServer(tmp_path / "installed-node", TOKEN).start()
    system.add_bare_host("root@box", tmp_path / "box-home", server.home)
    yield server
    server.stop()


def test_connect_installs_a_node_on_a_bare_host_then_connects(system, bare, home):
    result = run("connect", "root@box", "--install")
    assert result.exit_code == 0, result.output
    [install] = system.installs()
    assert '/deploy/install-node.sh" --systemd --user agentos --hardened' in install["script"]
    assert "git clone --depth 1 --branch" in install["script"] and 'S="sudo -n"' in install["script"]
    out = result.output
    assert "root@box has no openajar node yet (ubuntu 24.04, x86_64). connect can install one there:" in out
    assert "Installed: agentosd runs on root@box as the agentos user, in the hardened unit" in out
    assert "Connected box." in out
    assert "Panel sign-in:" in out  # the fake cannot run the node's CLI, so it prints the command
    assert json.loads((home / ".config/openajar/connections/box.json").read_text())["credential"] == "agent_grant"

    # a second run finds the node and never installs again
    assert run("connect", "root@box", "--install").exit_code == 0
    assert len(system.installs()) == 1


def test_connect_asks_before_installing_and_installs_nothing_unasked(system, bare, home):
    result = run("connect", "root@box")
    assert result.exit_code == 1
    assert "pass --install to install one there" in result.output
    assert system.installs() == [] and not (home / ".config/openajar/connections").exists()


def test_dry_run_prints_every_remote_command_and_changes_nothing(system, bare, home):
    result = run("connect", "root@box", "--dry-run")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "-o BatchMode=yes -o ConnectTimeout=15 -T root@box sh -s <<'EOF'" in out
    assert '/deploy/install-node.sh" --systemd --user agentos --hardened' in out
    assert "git clone --depth 1 --branch" in out and "Nothing was changed." in out
    assert system.installs() == [] and not (home / ".config/openajar").exists()
    assert system.argv("ssh") == [["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-T", "root@box", "sh", "-s"]]


def test_dry_run_on_a_host_with_a_node_installs_nothing(system, node, home):
    result = run("connect", "mynode", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "has openajar already, so connect would install nothing there" in result.output


@pytest.mark.parametrize(("flags", "words"), [
    ({"os_release": "centos 9"}, "it runs centos 9"),
    ({"os_release": "ubuntu 20.04"}, "it runs ubuntu 20.04"),
    ({"kernel": "Darwin"}, "it runs Darwin, not Linux"),
    ({"arch": "armv7l"}, "it has a armv7l CPU"),
    ({"sudo": False}, "cannot run sudo without a password"),
    ({"systemd": False}, "does not run systemd"),
])
def test_install_refuses_hosts_it_cannot_serve(system, tmp_path, home, flags, words, monkeypatch):
    monkeypatch.setenv("OPENAJAR_GIT_URL", "https://git.example.test/openajar.git")
    system.add_bare_host("root@box", tmp_path / "box-home", tmp_path, **flags)
    result = run("connect", "root@box", "--install")
    assert result.exit_code == 1
    assert "root@box has no openajar node, and connect cannot install one there" in result.output
    assert words in result.output
    assert system.installs() == []


def test_a_failed_install_says_so_and_is_safe_to_repeat(system, tmp_path, home, monkeypatch):
    monkeypatch.setenv("OPENAJAR_GIT_URL", "https://git.example.test/openajar.git")
    system.add_bare_host("root@box", tmp_path / "box-home", tmp_path, install_fails=True)
    result = run("connect", "root@box", "--install")
    assert result.exit_code == 1
    assert "the node installer on root@box failed (exit 1)" in result.output
    assert "safe to repeat" in result.output


# ---- Codex: a stdio entry that runs the bridge, no token anywhere ----


CODEX_BEFORE = """\
# my Codex settings
model = "gpt-5"

[mcp_servers.other]  # keep me
command = "other-mcp"

# about the next table
[profiles.fast]
model = "gpt-5-mini"
"""


def test_codex_is_registered_with_the_bridge_and_check_runs_it(system, node, home):
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(CODEX_BEFORE)
    result = run("connect", "mynode")
    assert result.exit_code == 0, result.output
    text = (codex_dir / "config.toml").read_text()
    assert text.startswith(CODEX_BEFORE.rstrip("\n"))  # comments, order and other tables kept
    import tomllib
    entry = tomllib.loads(text)["mcp_servers"]["openajar-mynode"]
    assert entry["args"][-2:] == ["mcp-bridge", "mynode"] and entry["tool_timeout_sec"] == 600
    assert (codex_dir / "config.toml.agentos-backup").read_text() == CODEX_BEFORE
    agent, _ = stored(system)
    assert agent not in text and TOKEN not in text and "bearer" not in text.lower()
    assert "Codex: registered openajar-mynode in ~/.codex/config.toml" in result.output

    again = run("connect", "mynode")
    assert "Codex: already registered openajar-mynode" in again.output
    assert (codex_dir / "config.toml").read_text() == text

    check = run("connect", "--check")
    assert check.exit_code == 0, check.output
    for line in ("ok   Tunnel:", "ok   Grant:", "ok   Claude Code: openajar-mynode",
                 "ok   Codex: openajar-mynode: the bridge answers initialize", "ok   Skills:"):
        assert line in check.output
    assert 'Try it: start a new Claude Code session and ask "list my openajar lanes".' in check.output

    removed = run("connect", "--remove", "mynode")
    assert removed.exit_code == 0, removed.output
    assert "Codex entry openajar-mynode from ~/.codex/config.toml" in removed.output
    assert (codex_dir / "config.toml").read_text() == CODEX_BEFORE


def test_no_codex_and_no_skills_leave_them_alone(system, node, home):
    (home / ".codex").mkdir()
    result = run("connect", "mynode", "--no-codex", "--no-skills")
    assert result.exit_code == 0, result.output
    assert "Codex: skipped (--no-codex)" in result.output
    assert "Claude Code skills: skipped (--no-skills)" in result.output
    assert not (home / ".codex/config.toml").exists() and not (home / ".claude/skills").exists()


def test_check_says_what_is_wrong(system, node, home):
    assert run("connect", "mynode").exit_code == 0
    system.set_claude_servers({})  # Claude Code lost the entry
    check = run("connect", "--check")
    assert check.exit_code == 1
    assert "FAIL Claude Code: no openajar-mynode entry" in check.output
    assert "fix: run `openajar connect mynode --name mynode`" in check.output


def test_the_bridge_speaks_stdio_mcp_and_exits_when_stdin_closes(system, node, home):
    import sys

    assert run("connect", "mynode").exit_code == 0
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}},
    ]
    proc = subprocess.Popen([sys.executable, "-m", "openajar.client", "mcp-bridge", "mynode"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ})
    assert proc.stdin and proc.stdout
    for message in lines[:2]:
        proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()
    first = json.loads(proc.stdout.readline())
    assert first["id"] == 1 and "serverInfo" in first["result"]
    proc.stdin.write((json.dumps(lines[2]) + "\n").encode())
    proc.stdin.flush()
    second = json.loads(proc.stdout.readline())
    assert second["id"] == 2 and not second["result"].get("isError")
    proc.stdin.close()
    assert proc.wait(15) == 0  # a clean exit when the client closes stdin
    rest = proc.stdout.read()
    assert rest == b""  # nothing but protocol on stdout
    agent, _ = stored(system)
    assert agent not in proc.stderr.read().decode()


# ---- openajar up --here: this computer is the node ----


def test_up_here_connects_the_clients_straight_to_the_loopback_port(linux, node, home):
    (home / ".codex").mkdir()
    lines: list[str] = []
    result = ops.Client(out=lines.append).connect_local(port=node.port, token=TOKEN, node_name="node-test",
                                                        home=str(node.home))
    url = f"http://127.0.0.1:{node.port}/mcp"
    assert result.conn.url == url and result.conn.local and result.conn.credential == "agent_grant"
    assert linux.claude_servers()["user:openajar-local"]["url"] == url
    agent = (home / ".config/openajar/tokens/local").read_text().strip()
    assert agent.startswith("agt2_") and agent != TOKEN
    assert "[mcp_servers.openajar-local]" in (home / ".codex/config.toml").read_text()
    assert not (home / ".config/systemd/user/agentos-tunnel-local.service").exists()  # no tunnel

    check = run("connect", "--check", "local")
    assert check.exit_code == 0, check.output
    assert "--   Tunnel: none needed, the node is this computer" in check.output
    assert "ok   Codex: openajar-local: the bridge answers initialize" in check.output

    again = ops.Client(out=lines.append).connect_local(port=node.port, token=TOKEN, node_name="node-test",
                                                       home=str(node.home))
    assert again.conn.agent_grant_id == result.conn.agent_grant_id  # a working grant is kept

    removed = run("connect", "--remove", "local")
    assert removed.exit_code == 0, removed.output
    assert "(revoked on the node)" in removed.output and "Codex entry openajar-local" in removed.output


def test_a_mac_cannot_be_a_node_yet(tmp_path):
    import typer

    from openajar.controller.cli import here_wanted

    with pytest.raises(typer.Exit):
        here_wanted(True, "macos", tmp_path)
    assert here_wanted(None, "macos", tmp_path) is False
    assert here_wanted(None, "linux", tmp_path) in (True, False)  # follows what is installed
    assert here_wanted(False, "linux", tmp_path) is False
    assert here_wanted(True, "linux", tmp_path) is True


def test_dev_up_registers_the_clients_with_the_fake_node(system, home):
    (home / ".codex").mkdir()
    grant = "agt2_" + "d3v" * 10
    result = ops.Client(out=lambda _: None).connect_dev(port=7450, token=grant, subject="claude-code@dev")
    assert system.claude_servers()["user:openajar-dev"]["url"] == "http://127.0.0.1:7450/mcp"
    assert system.keychain() == {"openajar/dev": grant}
    text = (home / ".codex/config.toml").read_text()
    assert '"mcp-bridge", "dev"' in text and grant not in text
    assert {line.client: line.state for line in result.clients} == {
        "Claude Code": "registered", "Codex": "registered", "Skills": "installed"}
    again = ops.Client(out=lambda _: None).connect_dev(port=7450, token="agt2_" + "n3w" * 10, subject="claude-code@dev")
    assert {line.client: line.state for line in again.clients}["Codex"] == "already registered"
    assert system.keychain()["openajar/dev"] == "agt2_" + "n3w" * 10


def test_status_of_a_dev_node_that_is_not_running_says_to_start_it(system, home):
    ops.Client(out=lambda _: None).connect_dev(port=free_port(), token="agt2_" + "d3v" * 10, subject="claude-code@dev")
    result = run("status", "dev")
    assert result.exit_code == 1
    assert "the dev node is not running" in result.output and "openajar dev up" in result.output
    assert "openajar connect localhost" not in result.output and "tunnel down" not in result.output


def test_an_installed_package_installs_a_node_from_the_public_repository(monkeypatch):
    monkeypatch.delenv("OPENAJAR_GIT_URL", raising=False)
    monkeypatch.delenv("OPENAJAR_GIT_REF", raising=False)
    monkeypatch.setattr(install_mod, "local_checkout", lambda: None)  # an installed package, not a checkout
    source = install_mod.install_source()
    assert source.url == "https://github.com/sabalys-ramunas/openajar.git" and source.ref == "main"
    assert not source.sends


@pytest.fixture
def from_checkout(tmp_path, system, monkeypatch):
    """No OPENAJAR_GIT_URL, and this openajar runs from a checkout: connect sends it."""
    monkeypatch.delenv("OPENAJAR_GIT_URL", raising=False)
    checkout = install_mod.local_checkout()
    assert checkout is not None  # the tests run from one
    server = NodeServer(tmp_path / "installed-node", TOKEN).start()
    system.add_bare_host("ubuntu@box", tmp_path / "box-home", server.home)
    yield checkout
    server.stop()


def test_connect_sends_its_own_checkout_when_no_repository_is_set(system, from_checkout, home):
    result = run("connect", "ubuntu@box", "--install")
    assert result.exit_code == 0, result.output
    prepare, install = system.steps()
    assert prepare["step"] == "prepare" and "apt-get install -y -qq rsync" in prepare["script"]
    assert "git clone" not in install["script"]
    assert '"$src/deploy/install-node.sh" --systemd --user agentos --hardened' in install["script"]
    [sent] = system.argv("rsync")
    assert sent[-2:] == [f"{from_checkout}/", "ubuntu@box:/opt/agentos-src/"]
    assert "--rsync-path=sudo -n rsync" in sent and "--chmod=go-w" in sent and "--delete" in sent
    assert "--exclude=.git" in sent and "--exclude=/.venv/" in sent and "--exclude=/.agentos-node.env" in sent
    assert not any("dist" in a for a in sent)  # the laptop's web and panel builds go along
    assert f"send this checkout ({from_checkout}) to /opt/agentos-src" in result.output
    assert "Connected box." in result.output


def test_dry_run_from_a_checkout_shows_the_copy_and_sends_nothing(system, from_checkout, home):
    result = run("connect", "ubuntu@box", "--dry-run")
    assert result.exit_code == 0, result.output
    out = result.output
    assert f"would send this checkout ({from_checkout}) to the node" in out
    assert "openajar-prepare-source" in out and "'--rsync-path=sudo -n rsync'" in out
    assert "ubuntu@box:/opt/agentos-src/" in out and "Nothing was changed." in out
    assert system.steps() == [] and system.argv("rsync") == []
