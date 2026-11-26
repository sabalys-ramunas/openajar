"""openajar local: the home it makes, the composition it serves (over real HTTP, on a free port), the
grant and the clients it registers, and the service it installs. Nothing here touches this
computer's real setup: HOME is a temporary folder, launchd and systemd are fakes (no command ever
runs), port probes are fakes, and the one real server binds a port the OS picks."""

from __future__ import annotations

import asyncio
import json
import logging
import plistlib
import socket
import stat
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from typer.testing import CliRunner

from openajar.client.choices import Tools
from openajar.client.ops import Client
from openajar.client.state import Paths
from openajar.client.tokens import FileStore
from openajar.local import home as local_home
from openajar.local import service, wake
from openajar.local.home import LocalError
from openajar.local.node import LOCAL_SERVICES, build_runtime, run
from openajar.local.paths import AVOIDED_PORTS, DEFAULT_PORT, NAME, default_home, log_path
from openajar.local.register import register_clients
from openajar.policy.service import PolicyService

NEVER = {7400, 7450, 5392}  # in use on the computers this runs on: never probed, bound or killed


class Ports:
    """A fake port probe: `taken` are busy. Records every port it was asked about."""

    def __init__(self, *taken: int):
        self.taken = set(taken)
        self.asked: list[int] = []

    def __call__(self, port: int) -> bool:
        self.asked.append(port)
        assert port not in NEVER
        return port in self.taken


@pytest.fixture(autouse=True)
def _logging_put_back():
    """`openajar local serve` sets up logging for its whole process, with a handler on the stream it was given.
    Run in a CliRunner, that stream closes with the test: put the root logger back, so a later test's output
    (test_ops_cli, test_wsexport_cli: JSON) gets no log lines and no "I/O operation on closed file"."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """A temporary HOME: the client's files, Claude Code's, Codex's and the LaunchAgents go there."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # set, not deleted, so it is put back after the test: the local node sets it to its own home
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "not-this-one"))
    return home


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ------------------------------------------------------------------ the home and its port


def test_the_default_home_and_port(user_home):
    assert default_home() == user_home / ".openajar"
    assert DEFAULT_PORT == 7410 and {7400, 7450} <= AVOIDED_PORTS


def test_the_first_start_takes_7410_and_remembers_it(tmp_path):
    home, ports = tmp_path / "openajar", Ports()
    cfg = local_home.ensure(home, local_home.choose_port(home, None, busy=ports))
    assert cfg.mcp_port == 7410 and cfg.mcp_host == "127.0.0.1" and cfg.name == NAME and len(cfg.mcp_token) > 30
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "secrets" / "vault.key").stat().st_mode) == 0o600
    again = local_home.ensure(home, local_home.choose_port(home, None, busy=ports))
    assert again.mcp_port == 7410 and again.mcp_token == cfg.mcp_token  # same address, same token


def test_a_taken_port_moves_to_the_next_free_one_and_that_one_is_remembered(tmp_path):
    home = tmp_path / "openajar"
    assert local_home.ensure(home, local_home.choose_port(home, None, busy=Ports(7410, 7411))).mcp_port == 7412
    assert local_home.choose_port(home, None, busy=Ports(7410)) == 7412  # remembered, though 7410 is free again
    assert local_home.choose_port(home, None, busy=Ports(7412)) == 7413  # the remembered one is taken now


def test_the_ports_of_connect_and_dev_up_are_never_taken_or_probed(tmp_path):
    ports = Ports(*range(7395, 7400))
    assert local_home.free_port(7395, busy=ports) == 7401
    assert 7400 not in ports.asked
    with pytest.raises(LocalError, match="kept for openajar connect"):
        local_home.choose_port(tmp_path, 7400, busy=ports)
    with pytest.raises(LocalError, match="kept for openajar dev up"):
        local_home.choose_port(tmp_path, 7450, busy=ports)
    with pytest.raises(LocalError, match="already in use"):
        local_home.choose_port(tmp_path, 7420, busy=Ports(7420))


# ------------------------------------------------------------------ the composition


def test_the_runtime_has_the_environment_parts_and_the_browser_routed_to_the_vm(tmp_path):
    from openajar.environments.vmbrowser import RoutedDisplays, RoutedEngine
    from openajar.local.lanes import LocalLanes, NoLocalDisplays, NoLocalEngine

    cfg = local_home.ensure(tmp_path / "openajar", 7419)
    runtime = build_runtime(cfg)
    built = [name for name, _ in runtime.get("feature_services")]
    assert built == [*LOCAL_SERVICES, "openajar.local.wake"]
    for name in ("policy", "traces", "freeze", "vault", "git", "envs", "vms", "environments", "interaction"):
        assert runtime.get(name) is not None, name
    for name in ("runner", "brains", "profiles", "keepalive", "studio_browser", "workspaces"):
        assert runtime.get(name) is None, name
    lanes = runtime.lanes
    assert isinstance(lanes, LocalLanes) and lanes.list() == [] and callable(lanes.take_control)
    # the routers are in front from the start, and behind them nothing that starts a browser here
    assert isinstance(lanes.engine, RoutedEngine) and isinstance(lanes.engine.inner, NoLocalEngine)
    assert isinstance(lanes.displays, RoutedDisplays) and isinstance(lanes.displays.inner, NoLocalDisplays)
    assert lanes.engine.vm is runtime.environments.vm_browsers()  # the one the environments use
    assert runtime.environments.settings.browser == "vm"  # the default here
    assert runtime.vms.follow_ip is True  # a laptop: SSH from wherever it is now
    assert runtime.vms.home == runtime.environments.home == cfg.home


def test_browser_node_in_environments_toml_is_refused_with_what_to_do(tmp_path):
    cfg = local_home.ensure(tmp_path / "openajar", 7419)
    (cfg.home / "environments.toml").write_text('browser = "node"\n')
    with pytest.raises(LocalError, match="cannot run an environment's browser itself") as err:
        build_runtime(cfg)
    assert 'browser = "vm"' in err.value.hint and "—" not in err.value.message + err.value.hint
    (cfg.home / "environments.toml").write_text('browser = "vm"\n')
    assert build_runtime(cfg).environments.settings.browser == "vm"


@pytest.mark.parametrize(("name", "text"), [("vms.toml", 'provider = "docker"\nssh_form = ["10.0.0.0/8"]\n'),
                                          ("environments.toml", 'browser_langauge = "en-GB"\n'),
                                          ("vms.toml", "provider = \"docker\n")])
def test_a_settings_file_that_does_not_read_stops_the_start_and_names_the_file(tmp_path, name, text):
    """S2-01: 0.7.0 served on without that service, and the other service's reaper then deleted machines with
    no save. A local node is its environments: it does not start without them, and says which file to fix."""
    cfg = local_home.ensure(tmp_path / "openajar", 7419)
    (cfg.home / name).write_text(text)
    with pytest.raises(LocalError, match=name) as err:
        build_runtime(cfg)
    words = err.value.message + (err.value.hint or "")
    assert str(cfg.home / name) in err.value.hint and "\u2014" not in words, words
    if name == "vms.toml":  # R1 #9: moved aside, a Docker node's vms.toml falls back to Hetzner and loses its machines
        assert "aside" not in err.value.hint and "keep its provider line" in err.value.hint, err.value.hint
    (cfg.home / name).unlink()
    assert build_runtime(cfg).get("vms") is not None


def test_serve_does_not_start_on_a_settings_file_it_cannot_read(tmp_path, user_home):
    from openajar.cli import app

    root = tmp_path / "openajar"
    local_home.ensure(root, free_port())
    (root / "vms.toml").write_text('ssh_form = ["10.0.0.0/8"]\n')
    out = CliRunner().invoke(app, ["local", "serve", "--home", str(root)])
    assert out.exit_code == 1 and "vms.toml" in out.output and "ssh_form" in out.output, out.output


def test_environments_setup_says_the_browser_runs_on_the_vm_on_a_laptop(tmp_path):
    from openajar.environments.cli import _local_browser

    cfg = local_home.ensure(tmp_path / "openajar", 7419)
    assert _local_browser(cfg.home, "node") == (None, "vm")  # nothing in environments.toml: the VM
    (cfg.home / "environments.toml").write_text('browser = "node"\n')
    refused, where = _local_browser(cfg.home, "node")
    assert refused[0] == "missing" and 'browser = "vm"' in refused[2] and where == "vm"
    server = tmp_path / "server"
    server.mkdir()
    assert _local_browser(server, "node") == (None, "node")  # a server node's home: as it says


def test_serve_says_why_it_did_not_start(tmp_path, user_home):
    from openajar.cli import app

    root = tmp_path / "openajar"
    local_home.ensure(root, free_port())
    (root / "environments.toml").write_text('browser = "node"\n')
    out = CliRunner().invoke(app, ["local", "serve", "--home", str(root)])
    assert out.exit_code == 1 and "cannot run an environment's browser itself" in out.output


def test_serve_refreshes_only_the_openajar_skills_it_installed(tmp_path, user_home):
    """An update restarts the service, and the skills were copied only by `openajar setup agents` and
    `openajar connect` (a day old on 2026-11-16): serve brings openajar's own up to date, keeps one someone
    else wrote under its name, never touches another skill, and installs none where there were none."""
    from openajar.cli import app
    from openajar.client import skills

    root = tmp_path / "openajar"
    local_home.ensure(root, free_port())
    (root / "environments.toml").write_text('browser = "node"\n')  # it stops right after the refresh
    dest = skills.skills_dir(user_home)
    old = skills.source("openajar-environment").replace("environment_open", "environment_start")
    (dest / "openajar-environment").mkdir(parents=True)
    (dest / "openajar-environment" / "SKILL.md").write_text(old)
    (dest / "openajar-studio").mkdir()
    (dest / "openajar-studio" / "SKILL.md").write_text("my own studio notes\nmine\n")  # the owner's, not ours
    (dest / "other").mkdir()
    (dest / "other" / "SKILL.md").write_text("---\nname: other\n---\nnot ours\n")
    local_home.mark_registered(root, True)  # as `openajar local start` and `openajar setup agents` mark it
    CliRunner().invoke(app, ["local", "serve", "--home", str(root)])
    assert (dest / "openajar-environment" / "SKILL.md").read_text() == skills.source("openajar-environment")
    assert (dest / "openajar-lanes" / "SKILL.md").read_text() == skills.source("openajar-lanes")  # one it lacked
    assert (dest / "openajar-studio" / "SKILL.md").read_text().endswith("mine\n")
    assert (dest / "other" / "SKILL.md").read_text().endswith("not ours\n")


@pytest.mark.parametrize("how", ["another home", "told not to", "--no-skills"])
def test_serve_of_a_home_that_did_not_register_never_writes_into_claude_codes_folder(tmp_path, user_home, how):
    """A second node (a test's, a builder's, with --home), one started with --no-register, or serve
    --no-skills: the person's skills stay as they are, whatever is outdated in them."""
    from openajar.cli import app
    from openajar.client import skills

    root = tmp_path / "openajar" if how == "another home" else user_home / ".openajar"
    local_home.ensure(root, free_port())
    (root / "environments.toml").write_text('browser = "node"\n')
    if how == "told not to":
        local_home.mark_registered(root, False)
    dest = skills.skills_dir(user_home)
    old = skills.source("openajar-environment").replace("environment_open", "environment_start")
    (dest / "openajar-environment").mkdir(parents=True)
    (dest / "openajar-environment" / "SKILL.md").write_text(old)
    CliRunner().invoke(app, ["local", "serve", "--home", str(root), *(["--no-skills"] if how == "--no-skills" else [])])
    assert (dest / "openajar-environment" / "SKILL.md").read_text() == old
    assert not (dest / "openajar-lanes").exists()


def test_the_default_home_from_before_the_mark_still_refreshes(tmp_path, user_home):
    from openajar.cli import app
    from openajar.client import skills

    root = user_home / ".openajar"  # ~/.openajar, which an older `openajar local start` registered
    local_home.ensure(root, free_port())
    (root / "environments.toml").write_text('browser = "node"\n')
    dest = skills.skills_dir(user_home)
    (dest / "openajar-environment").mkdir(parents=True)
    (dest / "openajar-environment" / "SKILL.md").write_text(skills.source("openajar-environment") + "old\n")
    CliRunner().invoke(app, ["local", "serve", "--home", str(root)])
    assert (dest / "openajar-environment" / "SKILL.md").read_text() == skills.source("openajar-environment")


def test_another_home_has_a_service_of_its_own_and_never_touches_the_persons(tmp_path, user_home):
    assert service.names_for(user_home / ".openajar") == (service.LABEL, service.UNIT)
    label, unit = service.names_for(tmp_path / "scratch")
    assert label.startswith("dev.openajar.local.") and label != service.LABEL and unit != service.UNIT
    assert service.names_for(tmp_path / "other") != (label, unit)
    fake = FakeLaunchctl()
    deps = service.Deps(manager=service.manager(user_home=user_home, run=fake, platform="macos",
                                                home=tmp_path / "scratch"),
                        busy=Ports(), answers=lambda port: fake.loaded, sleep=lambda s: None, wait_s=1)
    service.start(tmp_path / "scratch", deps=deps)
    service.stop(tmp_path / "scratch", deps=deps)
    agents = user_home / "Library" / "LaunchAgents"
    assert not (agents / "dev.openajar.local.plist").exists()
    assert not any(c[-1].endswith("/dev.openajar.local") for c in fake.calls)  # never the person's own job
    assert any(label in " ".join(c) for c in fake.calls)
    systemd = service.manager(user_home=user_home, run=fake, platform="linux", home=tmp_path / "scratch")
    assert systemd.unit_path().name == unit


def test_start_marks_whether_its_home_registered_the_clients(tmp_path, user_home, monkeypatch):
    from openajar.cli import app

    home = tmp_path / "openajar"
    monkeypatch.setattr(service, "start", lambda root, port=None: service.ServiceState(
        True, 1, 7411, "http://127.0.0.1:7411/mcp", log_path(root), "Running."))
    CliRunner().invoke(app, ["local", "start", "--home", str(home), "--no-register"])
    assert not local_home.registered(home)
    registered = []
    monkeypatch.setattr("openajar.local.register.register_clients",
                        lambda root, port, **kw: registered.append(kw) or [])
    CliRunner().invoke(app, ["local", "start", "--home", str(home)])
    assert registered and local_home.registered(home)


def test_no_skills_are_installed_where_none_were(tmp_path):
    from openajar.client import skills

    assert skills.refresh(tmp_path) is None and not skills.skills_dir(tmp_path).exists()
    skills.install(tmp_path)
    assert skills.refresh(tmp_path) is None  # all current: nothing written


# The base install (scripts/install.sh, the `local` extra) on a Mac: no Camoufox, no browser binary, no Xvfb.
# Both packages are made unimportable, PATH has no Xvfb, and the platform is darwin. On darwin, urllib's
# proxy lookup imports _scproxy, which only a real macOS Python has: off a Mac (CI's Linux) a stub answers
# "no proxies", as a Mac with none set would.
MAC_WITHOUT_A_BROWSER = """\
import sys, tempfile, os, types, importlib.util
sys.platform = "darwin"
if importlib.util.find_spec("_scproxy") is None:
    scproxy = types.ModuleType("_scproxy")
    scproxy._get_proxy_settings = lambda: {}
    scproxy._get_proxies = lambda: {}
    sys.modules["_scproxy"] = scproxy
sys.modules["camoufox"] = None  # an import of it fails, as on an install without the node extra
os.environ["PATH"] = tempfile.mkdtemp()  # no Xvfb, no x11vnc, no browser on PATH
import asyncio, shutil, subprocess
from pathlib import Path
ran = []
real_popen = subprocess.Popen.__init__
def popen(self, args, *a, **kw):
    ran.append(args)
    return real_popen(self, args, *a, **kw)
subprocess.Popen.__init__ = popen
from openajar.local import home
from openajar.local.node import run
cfg = home.ensure(Path(tempfile.mkdtemp()) / "a", int(sys.argv[1]))
async def main():
    stop, started = asyncio.Event(), asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(cfg, stop=stop, on_started=started.set_result))
    await asyncio.wait_for(started, 60)
    stop.set()
    await asyncio.wait_for(task, 30)
asyncio.run(main())
print(sorted({m.split(".")[0] for m in sys.modules if sys.modules[m] is not None} & {"camoufox", "playwright"}))
print(shutil.which("Xvfb"), [a for a in ran if any(w in str(a) for w in ("Xvfb", "x11vnc", "camoufox", "firefox"))])
"""


def test_it_starts_on_a_mac_with_no_browser_stack_and_imports_none(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("OPENAJAR_HOME",)}
    env["HOME"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", MAC_WITHOUT_A_BROWSER, str(free_port())], capture_output=True,
                         text=True, timeout=180, check=False, env=env)
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout.split("\n")[:2] == ["[]", "None []"], out.stdout + out.stderr[-2000:]


async def test_it_serves_mcp_and_the_web_app_and_the_registered_grant_works(tmp_path, user_home):
    home = tmp_path / "openajar"
    cfg = local_home.ensure(home, free_port())
    stop, started = asyncio.Event(), asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(cfg, stop=stop, on_started=started.set_result))
    try:
        port = await asyncio.wait_for(started, 30)
        client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
        lines = register_clients(home, port, client=client)
        assert any(line.startswith("Agent grant gr_") and "issued" in line for line in lines), lines
        token = FileStore(Paths(user_home)).get(NAME)
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            McpClient(streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http), mode="legacy") as mcp,
        ):
            tools = {tool.name for tool in (await mcp.list_tools()).tools}
            assert {"environment_open", "environment", "environment_close", "terminal_exec", "file_read"} <= tools
            # the environment's browser (on its VM), its proof and pull request, its sign-in with the owner's bound
            # logins, and nothing built on account browsers (those are in the accounts toolset)
            assert {"browser_navigate", "browser_snapshot", "browser_act", "browser_wait", "browser_screenshot",
                    "browser_record", "environment_pull_request", "login"} <= tools
            assert not tools & {"flow_run", "lane_create", "profile_use", "browsers_list", "browser_view"}
            who = await mcp.call_tool("environment", {})
            assert not who.is_error and json.loads(who.content[0].text)["environment"] is None
            up = await mcp.call_tool("tools_call", {"name": "environment_upload", "arguments": {}})
            assert "no tool named" not in up.content[0].text, up.content[0].text  # a laptop's checkout goes up too
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}") as web:
            assert (await web.get("/")).status_code == 200
            assert (await web.get("/api/status")).status_code == 401
            assert (await web.post("/mcp", json={})).status_code == 401  # no token, no MCP
            # the live view of an environment's browser: the node's viewer, behind its signed link
            assert (await web.get("/view/env/e1234567890-browser")).status_code in (401, 403)
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
    assert wake.read_last(home)["reason"] == "started"  # the catch-up ran at the start


# ------------------------------------------------------------------ the grant and the clients


def fake_claude(tmp_path: Path) -> tuple[str, Path]:
    calls = tmp_path / "claude-calls.txt"
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
    exe.chmod(0o755)
    return str(exe), calls


def test_register_issues_the_connect_grant_stores_its_token_and_registers_openajar_local(tmp_path, user_home):
    home = tmp_path / "openajar"
    local_home.ensure(home, 7410)
    exe, calls = fake_claude(tmp_path)
    (user_home / ".codex").mkdir()
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(claude=exe), out=lambda _: None)
    lines = register_clients(home, 7410, client=client)
    text = "\n".join(lines)
    assert "Claude Code: registered openajar-local (user scope)" in text
    assert "Codex: registered openajar-local" in text and "Skills: installed" in text and "—" not in text
    token_file = Paths(user_home).tokens_dir / NAME
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    helper = Paths(user_home).headers_helper
    assert helper.exists() and str(Paths(user_home).tokens_dir) in helper.read_text()
    added = calls.read_text()
    assert "add-json --scope user openajar-local" in added and "http://127.0.0.1:7410/mcp" in added
    assert token_file.read_text().strip() not in added  # the token never goes to Claude Code
    codex = (user_home / ".codex" / "config.toml").read_text()
    assert "openajar-local" in codex and token_file.read_text().strip() not in codex

    policy = PolicyService(home, node_config=local_home.read(home))
    try:
        _, grant = policy.resolve(token_file.read_text().strip())
        record = policy.grant_record(grant.id)
        assert sorted(record["scope"]["specials"]) == ["exec", "memory", "secret", "vm"]
        assert record["scope"]["ceiling"] == "commit"
        conn = client.records.get(NAME)
        assert conn.agent_grant_id == grant.id and conn.url == "http://127.0.0.1:7410/mcp"
        assert conn.claude_name == "openajar-local" and conn.service == "none"
    finally:
        policy.close()


def test_register_again_keeps_a_working_grant_and_replaces_a_revoked_one(tmp_path, user_home):
    home = tmp_path / "openajar"
    local_home.ensure(home, 7410)
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
    register_clients(home, 7410, client=client, codex=False)
    first = client.records.get(NAME).agent_grant_id
    lines = register_clients(home, 7411, client=client, codex=False)  # the port moved: the same grant
    assert lines[0] == f"Agent grant {first} kept"
    assert client.records.get(NAME).url == "http://127.0.0.1:7411/mcp"
    policy = PolicyService(home, node_config=local_home.read(home))
    policy.revoke(first, by="owner")
    policy.close()
    lines = register_clients(home, 7411, client=client, codex=False)
    second = client.records.get(NAME).agent_grant_id
    assert second != first and "issued" in lines[0]


def test_register_again_replaces_a_grant_from_before_secret_joined_the_environments_special(tmp_path, user_home):
    """A 0.3.0 grant has vm and exec on env/* but not secret (0.4.0 added it, for login): registering again,
    as an upgrade does, issues one with today's specials instead of keeping it, so no connect is needed."""
    from openajar.api.bridge import ttl_seconds
    from openajar.client.choices import DEFAULT_AGENT_TTL, DEFAULT_CEILING
    from openajar.client.state import Connection

    home = tmp_path / "openajar"
    local_home.ensure(home, 7410)
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
    policy = PolicyService(home, node_config=local_home.read(home))
    lanes = {"lanes": ["env/*"]}
    old, token = policy.issue(subject={"kind": "agent", "id": "claude-code@mac"}, workspaces=["*"], ceiling=DEFAULT_CEILING,
                              specials={"vm": lanes, "exec": lanes, "memory": {}},
                              ttl_s=ttl_seconds(DEFAULT_AGENT_TTL), by="owner", note="0.3.0")
    policy.close()
    kind = client.store_kind("auto")
    client.tokens(kind).put(NAME, token)
    client.records.save(Connection(name=NAME, ssh_host="localhost", remote_home=str(home), remote_port=7410,
                                  local_port=7410, node_name=NAME, token_store=kind, service="none",
                                  credential="agent_grant", agent_grant_id=old.id,
                                  agent_ceiling=DEFAULT_CEILING, agent_specials=["environments", "memory_read"]))
    lines = register_clients(home, 7410, client=client, codex=False)
    new = client.records.get(NAME).agent_grant_id
    assert new != old.id and "issued" in lines[0] and any(f"revoked the grant it replaces, {old.id}" in x for x in lines)
    policy = PolicyService(home, node_config=local_home.read(home))
    try:
        assert "secret" in policy.grant_record(new)["scope"]["specials"]
    finally:
        policy.close()


def test_register_before_the_home_exists_says_to_start_first(tmp_path, user_home):
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
    with pytest.raises(LocalError, match="no local node"):
        register_clients(tmp_path / "none", 7410, client=client)


# ------------------------------------------------------------------ the service


class FakeLaunchctl:
    """launchctl as a fake Run: bootstrap loads the label, bootout unloads it, print reports it."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.loaded = False
        self.pid = 4242

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        verb = cmd[1]
        if verb == "kickstart":  # -k: the running node ends and a new one starts
            if not self.loaded:
                return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
            self.pid += 1
        elif verb == "bootstrap":
            self.loaded = True
        elif verb == "bootout":
            ok, self.loaded = self.loaded, False
            return subprocess.CompletedProcess(cmd, 0 if ok else 3, "", "")
        elif verb == "print":
            if not self.loaded or not cmd[-1].endswith(service.LABEL):  # one job, the new one
                return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
            return subprocess.CompletedProcess(cmd, 0, f"\tstate = running\n\tpid = {self.pid}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def launchd_deps(user_home: Path, *, taken: tuple[int, ...] = ()) -> tuple[service.Deps, FakeLaunchctl]:
    fake = FakeLaunchctl()
    mgr = service.Launchd(Paths(user_home), fake, uid=501)
    return service.Deps(manager=mgr, busy=Ports(*taken), answers=lambda port: fake.loaded, sleep=lambda s: None,
                        wait_s=1), fake


def test_start_installs_and_loads_a_launch_agent_that_runs_serve(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, fake = launchd_deps(user_home)
    state = service.start(home, deps=deps)
    assert state.running and state.pid == 4242 and state.port == 7410
    assert state.url == "http://127.0.0.1:7410/mcp" and state.log_path == log_path(home)
    assert state.note.startswith("Running on port 7410.")
    plist_path = user_home / "Library" / "LaunchAgents" / "dev.openajar.local.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "dev.openajar.local" and plist["KeepAlive"] and plist["RunAtLoad"]
    assert plist["ProgramArguments"][-4:] == ["local", "serve", "--home", str(home)]
    assert plist["ProgramArguments"][0] == sys.executable
    assert plist["EnvironmentVariables"]["OPENAJAR_HOME"] == str(home)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(log_path(home))
    assert plist["ProcessType"] == "Interactive"  # Background throttles every git and ssh it starts
    assert ["launchctl", "bootstrap", "gui/501", str(plist_path)] in fake.calls
    assert local_home.read(home).mcp_port == 7410


def test_start_when_running_leaves_it_and_stop_keeps_it_stopped_past_a_login(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, fake = launchd_deps(user_home)
    service.start(home, deps=deps)
    calls = len(fake.calls)
    again = service.start(home, deps=deps)
    assert again.running and again.note.startswith("Already running on port 7410.")
    assert not any(c[1] in ("bootstrap", "bootout") for c in fake.calls[calls:])
    stopped = service.stop(home, deps=deps)
    assert not stopped.running and stopped.note.startswith("Stopped. It stays stopped")
    assert not (user_home / "Library" / "LaunchAgents" / "dev.openajar.local.plist").exists()
    assert service.status(home, deps=deps).note.startswith("Not running.")


def test_start_moves_a_node_running_as_a_background_job_to_an_interactive_one(tmp_path, user_home):
    """A plist from before 2026-11-16 ran the node as a Background job, and launchd throttled it and
    every git and ssh it started (a 13 MB clone: 212 s instead of 2 s). The next start loads it again
    from a new plist on the same port; serve says so in the log until then."""
    home = tmp_path / "openajar"
    deps, fake = launchd_deps(user_home)
    service.start(home, deps=deps)
    plist_path = user_home / "Library" / "LaunchAgents" / "dev.openajar.local.plist"
    old = plistlib.loads(plist_path.read_bytes())
    plist_path.write_bytes(plistlib.dumps({**old, "ProcessType": "Background"}))
    assert service.throttled_note(deps=deps) == service.THROTTLED_WORDS
    calls = len(fake.calls)
    again = service.start(home, deps=deps)
    assert again.running and again.port == 7410
    assert ["launchctl", "bootstrap", "gui/501", str(plist_path)] in fake.calls[calls:]
    assert plistlib.loads(plist_path.read_bytes())["ProcessType"] == "Interactive"
    assert service.throttled_note(deps=deps) is None
    assert "\u2014" not in service.THROTTLED_WORDS


def test_a_restart_waits_for_the_old_node_to_let_go_of_its_port(tmp_path, user_home):
    """Seen on the owner's Mac (2026-11-16): the reload from a Background plist booted the old node
    out, then found port 7410 still taken by it while it shut down, said "already in use" and left
    no node running. The restart waits for the port first."""
    home = tmp_path / "openajar"
    deps, fake = launchd_deps(user_home)
    service.start(home, deps=deps)
    plist_path = user_home / "Library" / "LaunchAgents" / "dev.openajar.local.plist"
    plist_path.write_bytes(plistlib.dumps({**plistlib.loads(plist_path.read_bytes()), "ProcessType": "Background"}))
    holding = {"left": 0}
    real_bootout = fake.__call__

    def launchctl(cmd):
        if cmd[1] == "bootout":
            holding["left"] = 3  # the old node answers three more port probes before it exits
        return real_bootout(cmd)

    def busy(port):
        if port == 7410 and holding["left"] > 0:
            holding["left"] -= 1
            return True
        return False

    deps.manager.run = launchctl
    deps.busy = busy
    again = service.start(home, deps=deps)
    assert again.running and again.port == 7410, again.note
    assert plistlib.loads(plist_path.read_bytes())["ProcessType"] == "Interactive"


def test_restart_starts_the_running_node_again_from_the_same_plist_and_says_so_in_one_line(tmp_path, user_home):
    """Upgrading the owner's Mac from 0.5.0 to 0.5.1 with the install line replaced the tool, but the running
    service kept the old code until it was stopped and started by hand. The installer now calls restart: the
    job starts again from the plist as it is (same home, port and environment), and it says so in one line."""
    from openajar import __version__

    home = tmp_path / "openajar"
    deps, fake = launchd_deps(user_home)
    service.start(home, deps=deps, port=7415)
    plist_path = user_home / "Library" / "LaunchAgents" / "dev.openajar.local.plist"
    before, calls = plist_path.read_bytes(), len(fake.calls)
    state = service.restart(home, deps=deps)
    assert state is not None and state.running and state.port == 7415 and state.pid == 4243
    assert ["launchctl", "kickstart", "-k", "gui/501/dev.openajar.local"] in fake.calls[calls:]
    assert not any(c[1] in ("bootstrap", "bootout") for c in fake.calls[calls:])
    assert plist_path.read_bytes() == before
    assert state.note == (f"Restarted the local service on port 7415, so it runs openajar {__version__} "
                          "(same home, same port).")


def test_restart_says_what_it_interrupted_on_open_environments(tmp_path, user_home, monkeypatch):
    home = tmp_path / "openajar"
    deps, _ = launchd_deps(user_home)
    service.start(home, deps=deps)
    monkeypatch.setattr(service, "open_environments", lambda h: [("e1", "Shop page"), ("e2", "Docs fix")])
    note = service.restart(home, deps=deps).note
    head, more = note.split("\n")
    assert head.startswith("Restarted the local service on port 7410")
    assert more.startswith("2 environments stay open and their jobs keep running.")
    assert "opens again on the next browser call" in more and "upload or download link" in more
    assert "\u2014" not in note


def test_restart_of_a_service_that_is_not_running_does_nothing(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, fake = launchd_deps(user_home)
    assert service.restart(home, deps=deps) is None  # never set up
    local_home.ensure(home, 7410)
    assert service.restart(home, deps=deps) is None  # set up, not loaded
    assert not any(c[1] in ("kickstart", "bootstrap") for c in fake.calls)


def test_restart_of_a_node_that_does_not_come_back_says_where_to_look(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, _ = launchd_deps(user_home)
    service.start(home, deps=deps)
    deps.answers = lambda port: False
    state = service.restart(home, deps=deps)
    assert not state.running and "did not answer on port 7410" in state.note and "openajar local logs" in state.note


def test_restart_of_a_background_job_writes_its_plist_again(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, _ = launchd_deps(user_home)
    service.start(home, deps=deps)
    plist_path = user_home / "Library" / "LaunchAgents" / "dev.openajar.local.plist"
    plist_path.write_bytes(plistlib.dumps({**plistlib.loads(plist_path.read_bytes()), "ProcessType": "Background"}))
    state = service.restart(home, deps=deps)
    assert state.running and state.port == 7410 and state.note.startswith("Restarted the local service on port 7410")
    assert plistlib.loads(plist_path.read_bytes())["ProcessType"] == "Interactive"


def test_restart_on_linux_restarts_the_user_unit(tmp_path, user_home):
    calls: list[list[str]] = []
    pid = {"now": 777}

    def run(cmd):
        calls.append(cmd)
        if "restart" in cmd:
            pid["now"] += 1
        out = "active\n" if "is-active" in cmd else f"{pid['now']}\n" if "show" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    home = tmp_path / "openajar"
    mgr = service.Systemd(Paths(user_home), run)
    deps = service.Deps(manager=mgr, busy=Ports(), answers=lambda port: True, sleep=lambda s: None, wait_s=1)
    service.start(home, deps=deps)
    unit = mgr.unit_path().read_bytes()
    calls.clear()
    state = service.restart(home, deps=deps)
    assert state.running and state.pid == 779 and state.note.startswith("Restarted the local service on port 7410")
    assert ["systemctl", "--user", "restart", "openajar-local.service"] in calls
    assert not any("enable" in c or "daemon-reload" in c for c in calls) and mgr.unit_path().read_bytes() == unit


def test_the_restart_command_is_quiet_with_if_running_and_says_why_without(tmp_path, monkeypatch):
    from openajar.cli import app

    asked = []
    monkeypatch.setattr(service, "restart", lambda home: asked.append(home))
    quiet = CliRunner().invoke(app, ["local", "restart", "--if-running", "--home", str(tmp_path / "a")])
    assert quiet.exit_code == 0 and quiet.output == "" and asked == [tmp_path / "a"]
    loud = CliRunner().invoke(app, ["local", "restart", "--home", str(tmp_path / "a")])
    assert loud.exit_code == 1 and "not running, so there is nothing to restart" in loud.output

    done = service.ServiceState(True, 5, 7410, "http://127.0.0.1:7410/mcp", tmp_path / "log", "Restarted it.")
    monkeypatch.setattr(service, "restart", lambda home: done)
    out = CliRunner().invoke(app, ["local", "restart", "--if-running", "--home", str(tmp_path / "a")])
    assert out.exit_code == 0 and out.output == "Restarted it.\n"


def test_start_on_a_taken_port_picks_the_next_and_status_says_what_the_catch_up_did(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, _ = launchd_deps(user_home, taken=(7410,))
    assert service.start(home, deps=deps).port == 7411
    wake_record = {"at": "2026-11-15T10:00:00+00:00", "reason": "woke", "slept_s": 5400,
                   "closed": [{"id": "eabc", "title": "Shop page", "reason": "time limit"}], "vms_deleted": [],
                   "errors": []}
    wake.catch_up_path(home).write_text(json.dumps(wake_record))
    note = service.status(home, deps=deps).note
    assert "Running on port 7411." in note and "after 1 h 30 min asleep" in note
    assert "closed 1 environment at once: eabc (Shop page: time limit)." in note and "—" not in note


def test_status_before_anything_was_set_up(tmp_path, user_home):
    deps, fake = launchd_deps(user_home)
    state = service.status(tmp_path / "none", deps=deps)
    assert not state.running and state.port is None and "openajar local start" in state.note
    assert fake.calls == []


def test_a_service_that_does_not_answer_says_so(tmp_path, user_home):
    home = tmp_path / "openajar"
    deps, _ = launchd_deps(user_home)
    deps.answers = lambda port: False
    state = service.start(home, deps=deps)
    assert not state.running and "did not answer on port 7410" in state.note


def test_the_systemd_unit_runs_serve_with_the_home_and_logs_to_its_file(tmp_path, user_home):
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        out = "active\n" if "is-active" in cmd else "777\n" if "show" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    home = tmp_path / "openajar"
    mgr = service.Systemd(Paths(user_home), run)
    deps = service.Deps(manager=mgr, busy=Ports(), answers=lambda port: True, sleep=lambda s: None, wait_s=1)
    state = service.start(home, deps=deps)
    assert state.running and state.pid == 777
    unit = (user_home / ".config" / "systemd" / "user" / "openajar-local.service").read_text()
    assert f"local serve --home {home}" in unit and f"Environment=OPENAJAR_HOME={home}" in unit
    assert f"StandardOutput=append:{log_path(home)}" in unit and "Restart=always" in unit
    assert ["systemctl", "--user", "enable", "openajar-local.service"] in calls
    service.stop(home, deps=deps)
    assert calls[-3][:4] == ["systemctl", "--user", "disable", "--now"]


def test_the_manager_is_launchd_on_a_mac_and_systemd_on_linux(user_home):
    assert isinstance(service.manager(platform="macos", user_home=user_home), service.Launchd)
    assert isinstance(service.manager(platform="linux", user_home=user_home), service.Systemd)


def test_the_cli_lists_its_commands():
    from openajar.cli import app

    out = CliRunner().invoke(app, ["local", "--help"])
    assert out.exit_code == 0
    for name in ("serve", "start", "stop", "restart", "status", "logs"):
        assert name in out.output


def test_logs_before_a_start_say_there_is_none_yet(tmp_path):
    from openajar.cli import app

    out = CliRunner().invoke(app, ["local", "logs", "--home", str(tmp_path / "none")])
    assert out.exit_code == 0 and "No log yet" in out.output


def test_the_other_commands_find_a_laptops_home_when_there_is_no_server_node(tmp_path, monkeypatch):
    """`openajar environments list` and the rest use core.config.home_from_env: with no OPENAJAR_HOME and no
    server node in /var/lib/agentos, a laptop's ~/.openajar (openajar local) is the home; a server wins."""
    from openajar.core import config
    from openajar.local import paths

    monkeypatch.delenv("OPENAJAR_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    server = tmp_path / "var-lib-openajar"
    monkeypatch.setattr(config, "DEFAULT_HOME", server)
    assert config.local_home() == paths.default_home()
    assert config.home_from_env() == server                  # nothing set up anywhere: the server default
    (paths.default_home()).mkdir(parents=True)
    (paths.default_home() / "node.toml").write_text('name = "local"\n')
    assert config.home_from_env() == paths.default_home()    # a laptop running openajar local
    server.mkdir()
    (server / "node.toml").write_text('name = "node-1"\n')
    assert config.home_from_env() == server                  # a server node wins
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "given"))
    assert config.home_from_env() == tmp_path / "given"      # OPENAJAR_HOME always wins


def test_a_start_right_after_a_stop_waits_for_launchd_to_finish_the_stop(user_home):
    """launchd finishes a bootout in the background: a bootstrap right after `openajar local stop` failed with
    "5: Input/output error" on a real Mac (2026-11-15). The load is tried again, waiting longer each time."""
    calls, waits = [], []

    def run(cmd):
        calls.append(cmd[1])
        failing = sum(1 for c in calls if c == "bootstrap") <= 3  # launchd still busy for three tries
        code = 5 if cmd[1] == "bootstrap" and failing else (113 if cmd[1] == "print" else 0)
        return subprocess.CompletedProcess(cmd, code, "", "Bootstrap failed: 5: Input/output error" if code == 5 else "")

    mgr = service.Launchd(Paths(user_home), run, uid=501, sleep=waits.append)
    mgr.install(user_home / "openajar")
    assert calls.count("bootstrap") == 4 and waits == [0.5, 1.0, 1.5]


def test_a_load_that_never_works_says_where_to_look(user_home):
    def run(cmd):
        code = 5 if cmd[1] == "bootstrap" else (113 if cmd[1] == "print" else 0)
        return subprocess.CompletedProcess(cmd, code, "", "Bootstrap failed: 5: Input/output error")

    mgr = service.Launchd(Paths(user_home), run, uid=501, sleep=lambda s: None)
    with pytest.raises(service.LocalError, match="could not load"):
        mgr.install(user_home / "openajar")
