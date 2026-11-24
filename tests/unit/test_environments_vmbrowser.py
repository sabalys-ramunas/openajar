"""An environment's browser on its own machine (environments.toml browser = "vm"): the lane manager's
engine and displays send that one lane to the machine, every other lane stays on the node, the
machine's screen and browser are started and stopped over the node's SSH connection, the tunnel
carries the live view and the browser server, and the machine's stack is pinned to the node's.
No SSH, no browser and no X here: the machine and Playwright are fakes. tests/unit/
test_environments_vmbrowser_docker.py runs it on a real docker machine."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from test_environments import node  # noqa: F401 - the fixture
from test_lanes import FakeEngine, make

from openajar.core.models import LaneSpec
from openajar.env.models import ExecResult, RemoteHost
from openajar.environments import tunnels, vmbrowser
from openajar.environments.models import Environment, EnvSettings
from openajar.environments.tunnels import Tunnel, tunnel_argv
from openajar.node.display import read_xauthority

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "openajar" / "environments"
REMOTE = RemoteHost(vm_id="vm_1", host="127.0.0.1", port=2222, key_path="/k/id", known_hosts="/k/kh",
                    host_alias="openajar-vm_1", provider="docker", label="docker")


# ------------------------------------------------------------------ the machine's stack


def _locked(name: str) -> str:
    lock = (ROOT / "uv.lock").read_text()
    found = re.search(rf'\[\[package\]\]\nname = "{name}"\nversion = "([^"]+)"', lock)
    assert found, name
    return found.group(1)


def test_the_machines_browser_is_pinned_to_the_nodes_versions_with_hashes():
    text = (PKG / "browser-requirements.txt").read_text()
    for name in ("camoufox", "playwright"):  # Playwright's client and server must be the same version
        assert f"\n{name}=={_locked(name)} \\" in text, name
    reqs = re.split(r"\n(?=[a-z0-9])", text.split("\n", 6)[-1].strip())
    assert reqs and all("--hash=sha256:" in r for r in reqs if "==" in r)
    script = (PKG / "browser.sh").read_text()
    assert "--require-hashes" in script and "camoufox fetch" in script and "install-deps firefox" in script


def test_the_install_script_writes_the_requirements_and_the_script_then_runs_it():
    s = vmbrowser.install_script()
    req = (PKG / "browser-requirements.txt").read_text().rstrip()
    assert f"<<'AGENTOS_BROWSER_REQUIREMENTS'\n{req}\nAGENTOS_BROWSER_REQUIREMENTS\n" in s
    assert "OPENAJAR_BROWSER_SCRIPT" not in (PKG / "browser.sh").read_text()  # the heredoc's end mark
    assert s.rstrip().endswith(f"sh {vmbrowser.SCRIPT_ON_VM}")


def test_the_dev_snapshot_gets_the_browser_before_its_cleanup():
    from openajar.vms.manager import DEFAULT_IMAGE_SCRIPT, IMAGE_CLEANUP, image_script

    full = image_script(vmbrowser.install_script())
    assert full.startswith(DEFAULT_IMAGE_SCRIPT) and full.endswith(IMAGE_CLEANUP)
    assert full.index("browser.sh") < full.index("cloud-init clean")
    assert image_script() == DEFAULT_IMAGE_SCRIPT + IMAGE_CLEANUP  # other images: as before


def test_the_docker_image_and_its_build_carry_the_browser():
    docker = (ROOT / "deploy" / "environments" / "Dockerfile").read_text()
    assert "COPY browser.sh browser-requirements.txt /usr/local/lib/agentos-env/" in docker
    browser = docker.index("sh /usr/local/lib/agentos-env/browser.sh")
    assert docker.index("setup.sh\n") < browser
    # the token for Camoufox's releases API comes as a build secret, never a layer or an image env
    assert "RUN --mount=type=secret,id=github_token,required=false" in docker[:browser]
    assert "ENV GITHUB_TOKEN" not in docker and "ARG GITHUB_TOKEN" not in docker
    cli = (PKG / "cli.py").read_text()
    assert '"browser.sh", "browser-requirements.txt"' in cli


def _ctl_namespace() -> dict:
    ns: dict = {"__name__": "not-the-machine"}
    exec(compile(vmbrowser.ctl_script(), "vmbrowser_ctl", "exec"), ns)  # noqa: S102 - our own file
    return ns


def test_the_control_script_does_nothing_on_import_and_matches_the_tunnel():
    ns = _ctl_namespace()  # main() runs only as __openajar_vm__, never on an import
    assert ns["VNC_SOCK"] == tunnels.VM_VNC_SOCKET and ns["VENV_PY"] == vmbrowser.VENV_PY
    # no TCP listener on the machine's screen or its live view, as on the node (openajar.node.display)
    script = vmbrowser.ctl_script()
    assert '"-nolisten", "tcp"' in script and '"-rfbport", "0"' in script and '"-rfbportv6", "-1"' in script
    assert 'host="127.0.0.1"' in script


def test_the_machines_screen_takes_only_clients_with_its_cookie(tmp_path):
    ns = _ctl_namespace()
    path = tmp_path / "xauth"
    ns["_write_xauthority"](str(path))
    name, cookie = read_xauthority(path)
    assert name == b"MIT-MAGIC-COOKIE-1" and len(cookie) == 16
    assert path.read_bytes()[:2] == b"\xff\xff"  # FamilyWild: the machine's own name does not matter
    assert (path.stat().st_mode & 0o777) == 0o600


def test_the_boot_line_carries_no_request():
    cmd = vmbrowser.ctl_command()
    assert cmd == f"{vmbrowser.VENV_PY} -I -c '{vmbrowser.BOOT}'" and "json.load(sys.stdin)" in cmd
    assert "ws_path" not in cmd and "options" not in cmd  # the request, the browser's path included, is on stdin


# ------------------------------------------------------------------ the tunnel


def test_the_tunnel_forwards_the_live_view_the_browser_and_the_desktop_only_for_a_vm_browser():
    plain = Tunnel(REMOTE, gateway_port=None)
    assert plain._forwards() == () and plain.browser_port is None and plain.vnc_path is None
    assert plain.desk_path is None
    vm = Tunnel(REMOTE, gateway_port=4000, vm_browser=True, browser_port=45001)
    vm.dir = "/tmp/agentos-env-x"
    forwards = vm._forwards()
    argv = tunnel_argv(REMOTE, socks_path="/tmp/agentos-env-x/socks", gateway_port=4000, forwards=forwards)
    assert "/tmp/agentos-env-x/vnc:/run/agentos-browser/vnc.sock" in argv
    assert f"/tmp/agentos-env-x/browser:127.0.0.1:{tunnels.VM_BROWSER_PORT}" in argv
    assert "/tmp/agentos-env-x/desk:/run/agentos-browser/desk.sock" in argv  # the desktop's helper
    assert argv.count("-L") == 3 and "StreamLocalBindMask=0177" in argv
    assert vm.vnc_path == "/tmp/agentos-env-x/vnc" and vm.browser_port == 45001
    assert vm.desk_path == "/tmp/agentos-env-x/desk"
    assert "-L" not in tunnel_argv(REMOTE, socks_path="/s", gateway_port=None)


def test_the_browser_starts_as_a_desktop_window_with_the_nodes_prefs_and_no_proxy():
    from openajar.node.camoufox.prefs import CACHE_PREFS, WHEEL_PREFS

    opts = vmbrowser.browser_options(LaneSpec(id="env/e1234567890-browser"), identity_os=["linux"])
    assert opts["screen"] == [1920, 1080] and opts["window"] == [1920, 1080] and opts["headless"] is False
    assert "proxy" not in opts and opts["os"] == ["linux"] and opts["geoip"] is False
    assert all(opts["firefox_user_prefs"][k] == v for k, v in {**CACHE_PREFS, **WHEEL_PREFS}.items())
    assert "network.proxy.autoconfig_url" not in opts["firefox_user_prefs"]
    json.dumps(opts)  # it travels to the machine as JSON


# ------------------------------------------------------------------ routing, with fakes


class FakeMachine:
    """The environment's machine as the control script answers, and what it was asked."""

    def __init__(self, *, installed: bool = True):
        self.installed = installed
        self.asked: list[dict] = []
        self.ran: list[str] = []

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None, oom_check=False):
        self.ran.append(cmd)
        if cmd == vmbrowser.READY_CHECK:
            return ExecResult(exit_code=0 if self.installed else 1, stdout="", stderr="", duration_s=0.01)
        if cmd == "sh -s":
            self.installed = True
            return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
        assert cmd == vmbrowser.ctl_command(), cmd
        req = json.loads(stdin)
        assert req.pop("script") == vmbrowser.ctl_script()
        self.asked.append(req)
        return ExecResult(exit_code=0, stdout=json.dumps({"ok": True}) + "\n", stderr="", duration_s=0.01)


class FakePage:
    def __init__(self, context):
        self.context, self.url, self.closed = context, "about:blank", False

    def on(self, *_a):
        pass

    def is_closed(self):
        return self.closed


class FakeContext:
    def __init__(self):
        self.pages: list = []
        self.closed = False

    def on(self, *_a):
        pass

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.connected = True
        self.context = FakeContext()

    def on(self, *_a):
        pass

    def is_connected(self):
        return self.connected

    async def new_context(self, **kw):
        assert kw == {"no_viewport": True}
        return self.context


class FakePlaywright:
    def __init__(self):
        self.connected_to: list[str] = []
        self.browser = FakeBrowser()
        self.firefox = self
        self.stopped = False

    async def connect(self, url, *, timeout):
        self.connected_to.append(url)
        self.browser = FakeBrowser()  # each launch is a browser of its own
        return self.browser

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True
        self.browser.connected = False


class FakeTunnel:
    def __init__(self, folder: str):
        self.alive, self.vm_browser, self.browser_port = True, True, 45999
        self.vnc_path = os.path.join(folder, "vnc")
        Path(self.vnc_path).write_text("")  # ssh's -L socket, as a file the health check sees


def _env(eid: str, browser: str = "vm") -> Environment:
    from openajar.core.models import utcnow

    now = utcnow()
    return Environment(id=eid, title="t", grant_id="g", binding="session:x", created_at=now, expires_at=now,
                       browser=browser, state="ready")


class FakeService:
    """What VmBrowsers reads of the environments service."""

    def __init__(self, envs_by_id: dict, tunnel, machine: FakeMachine):
        self.by_id, self.tunnel, self.machine = envs_by_id, tunnel, machine
        self.store = SimpleNamespace(get=lambda eid: self.by_id[eid])

    def env_of_lane(self, lane_id):
        name = lane_id.split("/", 1)[1]
        env = self.by_id.get(name.removesuffix("-browser"))
        return (env, "browser" if name.endswith("-browser") else "terminal") if env else None

    def tunnel_of(self, env_id):
        return self.tunnel

    def envs(self):
        return self.machine


@pytest.fixture
def routed(tmp_path, monkeypatch):
    import playwright.async_api

    pw = FakePlaywright()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: pw)
    machine = FakeMachine()
    svc = FakeService({"e1234567890": _env("e1234567890"), "e0987654321": _env("e0987654321", "node")},
                      FakeTunnel(str(tmp_path)), machine)
    mgr, engine, displays, _clock = make(tmp_path / "home", self_check=False)
    vm = vmbrowser.VmBrowsers(svc)
    assert vmbrowser.install(mgr, vm) and vmbrowser.install(mgr, vm)  # once; a second call changes nothing
    assert vm.engine is engine and mgr.engine.inner is engine and mgr.displays.inner is displays
    return SimpleNamespace(mgr=mgr, engine=engine, displays=displays, vm=vm, svc=svc, machine=machine, pw=pw)


async def _create(mgr, lane_id):
    await mgr.create(LaneSpec(id=lane_id))


async def test_a_vm_environments_browser_lane_opens_on_its_machine(routed):
    mgr, machine = routed.mgr, routed.machine
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        info = await mgr.open("env/e1234567890-browser")
        assert info.state.value == "warm" and routed.engine.launches == []  # not the node's engine
        assert [a["action"] for a in machine.asked] == ["display", "browser"]
        display, browser = machine.asked
        assert (display["width"], display["height"]) == (1920, 1080)
        assert browser["port"] == tunnels.VM_BROWSER_PORT and browser["ws_path"].startswith("/")
        assert "proxy" not in browser["options"]
        assert routed.pw.connected_to == [f"ws://127.0.0.1:45999{browser['ws_path']}"]
        assert info.viewer.vnc_socket == routed.svc.tunnel.vnc_path  # the live view: the forwarded socket
        session = mgr.live_session("env/e1234567890-browser")
        assert session.alive and session.pids == [] and session.guard is not None
        assert len(routed.pw.browser.context.pages) == 1  # a window shows at once

        await mgr.close("env/e1234567890-browser")
        assert [a["action"] for a in machine.asked[2:]] == ["stop", "stop"]
        assert machine.asked[2]["what"] == ["browser"] and machine.asked[3]["what"] == ["browser", "display"]
        assert routed.pw.stopped and routed.pw.browser.context.closed
    finally:
        await mgr.shutdown()


async def test_a_vm_browser_opens_with_the_saved_sign_ins_and_keeps_them_when_it_closes(routed, monkeypatch):
    """A fresh context each launch, so the sign-ins (openajar.environments.signins) go in as its
    storage_state, and the context's own are read back when the lane closes, for its next launch."""
    from openajar.environments import signins

    state = {"cookies": [{"name": "sid", "value": "v1", "domain": "shop.test", "path": "/"}], "origins": []}
    later = {"cookies": [{"name": "sid", "value": "v2", "domain": "shop.test", "path": "/"}], "origins": []}
    opened_with: list[dict] = []

    async def new_context(self, **kw):
        opened_with.append(kw)
        self.context.storage_state = lambda: _value(later)
        return self.context

    monkeypatch.setattr(FakeBrowser, "new_context", new_context)
    mgr = routed.mgr
    signins.remember(routed.svc, "e1234567890", state)
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        await mgr.open("env/e1234567890-browser")
        assert opened_with == [{"no_viewport": True, "storage_state": state}]
        await mgr.close("env/e1234567890-browser")
        assert signins.pending(routed.svc, "e1234567890") == later  # the lane's next launch opens with these
        routed.svc.by_id["e1234567890"].keep_signins = False
        await mgr.open("env/e1234567890-browser")
        assert opened_with[-1] == {"no_viewport": True}  # opted out: a fresh one
    finally:
        await mgr.shutdown()


async def _value(v):
    return v


async def test_a_cold_browser_lane_shows_its_environments_screen(routed):
    mgr, vm = routed.mgr, routed.vm
    await _create(mgr, "env/e1234567890-browser")
    await _create(mgr, "env/e0987654321-browser")
    assert mgr.view("env/e1234567890-browser") is None and mgr.get("env/e1234567890-browser").viewer is None
    vm._screens["e1234567890"] = (1920, 1080)  # the desktop runs on the machine (VmBrowsers.screen_soon)
    info = mgr.get("env/e1234567890-browser")
    assert info.state.value == "cold" and info.viewer is not None
    assert mgr.view("env/e1234567890-browser").vnc_socket == routed.svc.tunnel.vnc_path
    assert mgr.view("env/e0987654321-browser") is None  # a browser on the node has no such screen


async def test_every_other_lane_stays_on_the_node(routed):
    mgr = routed.mgr
    await mgr.start()
    try:
        for lane in ("personal/x", "env/e0987654321-browser", "env/e1234567890"):
            await _create(mgr, lane)
            await mgr.open(lane)
        assert [lid for lid, *_ in routed.engine.launches] == ["personal/x", "env/e0987654321-browser",
                                                                "env/e1234567890"]
        assert routed.machine.asked == [] and set(routed.displays.slots) == {
            "personal/x", "env/e0987654321-browser", "env/e1234567890"}
    finally:
        await mgr.shutdown()


async def test_a_dropped_connection_restarts_the_browser_on_its_machine(routed):
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        await mgr.open("env/e1234567890-browser")
        routed.pw.browser.connected = False  # the tunnel dropped: Playwright is disconnected
        await mgr.open("env/e1234567890-browser")
        actions = [a["action"] for a in routed.machine.asked]
        assert actions.count("browser") == 2 and actions.count("display") == 2
    finally:
        await mgr.shutdown()


async def test_a_machine_that_fails_to_start_the_browser_leaves_the_lane_cold(routed):
    async def broken(lane_id, cmd, *, timeout_s=None, stdin=None, **_kw):
        req = json.loads(stdin)
        ok = req["action"] != "browser"
        out = {"ok": ok} if ok else {"ok": False, "error": "browser exited (code 1): no display"}
        return ExecResult(exit_code=0, stdout=json.dumps(out), stderr="", duration_s=0.01)

    routed.machine.exec = broken
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        with pytest.raises(Exception, match="no display"):
            await mgr.open("env/e1234567890-browser")
        assert mgr.get("env/e1234567890-browser").state.value == "cold"
        assert routed.vm.get("env/e1234567890-browser") is None  # its screen was released
    finally:
        await mgr.shutdown()


async def test_the_live_views_copy_reads_the_machines_clipboard(routed):
    async def answers(lane_id, cmd, *, timeout_s=None, stdin=None, **_kw):
        req = json.loads(stdin)
        routed.machine.asked.append(req)
        out = {"ok": True, "text": "copied on the vm"} if req["action"] == "clipboard" else {"ok": True}
        return ExecResult(exit_code=0, stdout=json.dumps(out), stderr="", duration_s=0.01)

    routed.machine.exec = answers
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        info = await mgr.open("env/e1234567890-browser")
        assert await mgr.displays.read_clipboard("env/e1234567890-browser", info.viewer) == "copied on the vm"
        [req] = [r for r in routed.machine.asked if r["action"] == "clipboard"]
        assert req["limit"] == 1024 * 1024 and "no owner" in req["nothing"]
    finally:
        await mgr.shutdown()


async def test_a_clipboard_too_large_on_the_machine_is_the_same_error_as_on_the_node(routed):
    from openajar.node.lane_clipboard import ClipboardTooLarge

    async def answers(lane_id, cmd, *, timeout_s=None, stdin=None, **_kw):
        req = json.loads(stdin)
        out = {"ok": False, "kind": "too_large", "error": "big"} if req["action"] == "clipboard" else {"ok": True}
        return ExecResult(exit_code=0, stdout=json.dumps(out), stderr="", duration_s=0.01)

    routed.machine.exec = answers
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        info = await mgr.open("env/e1234567890-browser")
        with pytest.raises(ClipboardTooLarge):
            await mgr.displays.read_clipboard("env/e1234567890-browser", info.viewer)
    finally:
        await mgr.shutdown()


async def test_no_tunnel_no_browser(routed):
    routed.svc.tunnel = None
    mgr = routed.mgr
    await mgr.start()
    try:
        await _create(mgr, "env/e1234567890-browser")
        with pytest.raises(Exception, match="connection to its machine is down"):
            await mgr.open("env/e1234567890-browser")
        assert routed.machine.asked == []
    finally:
        await mgr.shutdown()


def test_a_lane_manager_without_an_engine_keeps_its_own_browser():
    assert vmbrowser.install(SimpleNamespace(), vmbrowser.VmBrowsers(None)) is False
    assert vmbrowser.install(None, vmbrowser.VmBrowsers(None)) is False


def test_the_router_passes_settings_through_to_the_nodes_engine(tmp_path):
    engine = FakeEngine()
    front = vmbrowser.RoutedEngine(engine, vmbrowser.VmBrowsers(None))
    front.allow_private_network = True  # what the stealth check sets on its engine
    assert engine.allow_private_network is True and front.exit_ip == engine.exit_ip


# ------------------------------------------------------------------ the environments service


def test_the_setting_defaults_to_the_node():
    assert EnvSettings().browser == "node" and EnvSettings(browser="vm").browser == "vm"
    with pytest.raises(ValueError):
        EnvSettings(browser="laptop")


class VmTunnel:
    """bringup's Tunnel, as a fake that records what it was made with."""

    made: ClassVar[list[dict]] = []
    stopped: ClassVar[list[str]] = []

    def __init__(self, remote, *, gateway_port, socks_port=None, vm_browser=False, browser_port=None):
        VmTunnel.made.append({"vm_browser": vm_browser, "browser_port": browser_port})
        self.socks_port, self.vm_browser = socks_port or 41080, vm_browser
        self.browser_port = (browser_port or 45999) if vm_browser else None
        self.vnc_path = "/tmp/agentos-env-fake/vnc" if vm_browser else None
        self.alive = False

    async def start(self):
        self.alive = True

    async def stop(self):
        VmTunnel.stopped.append("tunnel")
        self.alive = False


@pytest.fixture
def vm_node(node, monkeypatch):  # noqa: F811 - test_environments' fixture
    from openajar.environments import bringup

    VmTunnel.made, VmTunnel.stopped = [], []
    monkeypatch.setattr(bringup, "Tunnel", VmTunnel)
    lanes = node["lanes"]
    lanes.engine, lanes.displays = FakeEngine(), SimpleNamespace(get=lambda _l: None)  # a lane manager that routes
    node["mgr"].settings = EnvSettings(browser="vm", desktop=False)  # the desktop: test_environments_desktop.py
    return node


async def test_open_with_the_browser_on_the_machine(vm_node):
    from test_environments import opened

    out = await opened(vm_node)
    mgr, lanes = vm_node["mgr"], vm_node["lanes"]
    env = mgr.get(out["id"])
    assert out["state"] == "ready" and env.browser == "vm"
    assert VmTunnel.made == [{"vm_browser": True, "browser_port": None}]
    assert isinstance(lanes.engine, vmbrowser.RoutedEngine) and isinstance(lanes.displays, vmbrowser.RoutedDisplays)
    assert mgr._vm_browsers.handles(env.browser_lane) and not mgr._vm_browsers.handles(env.terminal_lane)
    ran = [cmd for cmd, _g in vm_node["machine"].ran]
    assert vmbrowser.READY_CHECK in ran and "sh -s" not in ran  # the image has the browser already
    # the lane is the node's, as always (its tools, its guard, its live view link)
    assert lanes.get(env.browser_lane).spec.proxy_label == f"env-{env.id}" and "browser" in out["links"]


async def test_a_machine_without_the_browser_gets_it_while_it_comes_up(vm_node):
    from test_environments import opened

    machine = vm_node["machine"]
    plain = machine.exec

    async def exec_(lane_id, cmd, *, stdin=None, **kw):
        if cmd == vmbrowser.READY_CHECK:
            done = any(c == "sh -s" and s == vmbrowser.install_script() for c, s in installs)
            return ExecResult(exit_code=0 if done else 1, stdout="", stderr="", duration_s=0.01)
        if cmd == "sh -s" and stdin == vmbrowser.install_script():
            installs.append((cmd, stdin))
            return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
        return await plain(lane_id, cmd, stdin=stdin, **kw)

    installs: list = []
    machine.exec = exec_
    vm_node["envs"].exec = exec_
    out = await opened(vm_node)
    assert out["state"] == "ready" and len(installs) == 1


async def test_a_failed_install_fails_the_environment_with_its_reason(vm_node):
    from test_environments import opened

    async def exec_(lane_id, cmd, *, stdin=None, **kw):
        if cmd == vmbrowser.READY_CHECK:
            return ExecResult(exit_code=1, stdout="", stderr="", duration_s=0.01)
        if cmd == "sh -s" and stdin == vmbrowser.install_script():
            return ExecResult(exit_code=100, stdout="", stderr="E: Unable to locate package xvfb", duration_s=0.01)
        return await plain(lane_id, cmd, stdin=stdin, **kw)

    plain = vm_node["machine"].exec
    vm_node["envs"].exec = exec_
    with pytest.raises(Exception, match="environment_failed: environment e\\w+ did not start: .*Unable to locate "
                                        "package xvfb"):
        await opened(vm_node)
    env = vm_node["mgr"].store.recent(1)[0]
    assert env.state == "failed" and "Unable to locate package xvfb" in (env.error or "")


async def test_a_lane_manager_that_cannot_route_keeps_the_browser_on_the_node(node, monkeypatch):  # noqa: F811
    from test_environments import opened

    from openajar.environments import bringup

    monkeypatch.setattr(bringup, "Tunnel", VmTunnel)
    node["mgr"].settings = EnvSettings(browser="vm")  # the dev node's simulated lanes have no engine
    out = await opened(node)
    assert out["state"] == "ready" and node["mgr"].get(out["id"]).browser == "node"


async def test_closing_stops_the_browser_on_the_machine_before_its_tunnel(vm_node):
    from test_environments import opened

    out = await opened(vm_node)
    lanes = vm_node["lanes"]
    order = VmTunnel.stopped
    close = lanes.close

    async def closing(lane_id, **kw):
        order.append(f"lane {lane_id}")
        return await close(lane_id, **kw)

    lanes.close = closing
    await vm_node["call"]("environment_close", discard=True)
    assert order[:2] == [f"lane env/{out['id']}-browser", "tunnel"]


async def test_the_node_setting_is_unchanged(node, monkeypatch):  # noqa: F811
    """browser = "node" (the default): the tunnel is made exactly as before, with no browser forwards."""
    from test_environments import opened

    from openajar.environments import bringup

    made = []

    class Plain:
        def __init__(self, remote, *, gateway_port, socks_port=None):
            made.append(gateway_port)
            self.socks_port, self.alive = 41080, False

        async def start(self):
            self.alive = True

        async def stop(self):
            self.alive = False

    monkeypatch.setattr(bringup, "Tunnel", Plain)
    out = await opened(node)
    assert out["state"] == "ready" and made and node["mgr"].get(out["id"]).browser == "node"
    assert node["mgr"]._vm_browsers is None


def test_setup_says_where_the_browser_runs():
    from openajar.environments.cli import _browser_line

    assert _browser_line("node", "docker", "img", "docker")[0] == "ok"
    status, _what, words = _browser_line("vm", "hetzner", "img", "docker")
    assert status == "ok" and "dev snapshot" in words
    status, _what, words = _browser_line("vm", "docker", "img", "/nonexistent/docker")
    assert status == "missing" and "openajar environments setup --pull" in words
    assert "—" not in words


def test_a_pid_file_from_before_a_save_never_names_a_process_started_after_it(tmp_path, monkeypatch):
    """A docker save keeps /run: on the resumed machine xvfb.pid may hold the number of a process that
    started later (a restored job). _running says it is not ours, so _stop never signals it."""
    import os
    import time

    ns = _ctl_namespace()
    ns["DIR"] = str(tmp_path)
    (tmp_path / "xvfb.pid").write_text(str(os.getpid()))
    old = time.time() - 3600
    os.utime(tmp_path / "xvfb.pid", (old, old))
    ns["_started_at"] = lambda pid: time.time() - 60  # the process with that number began after the file
    assert ns["_running"]("xvfb") is None
    killed = []
    monkeypatch.setattr(ns["os"], "killpg", lambda pid, sig: killed.append(pid))
    ns["_stop"]("xvfb")
    assert killed == [] and not (tmp_path / "xvfb.pid").exists()
    (tmp_path / "xvfb.pid").write_text(str(os.getpid()))
    ns["_started_at"] = lambda pid: time.time() - 7200  # started before the file was written: ours
    assert ns["_running"]("xvfb") == os.getpid()
    ns["_started_at"] = lambda pid: None  # no /proc (not Linux): trusted as before
    assert ns["_running"]("xvfb") == os.getpid()
