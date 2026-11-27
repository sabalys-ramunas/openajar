"""An environment's browser on its own machine (environments.toml browser = "vm").

By default an environment's browser runs on the node (Camoufox on Xvfb) and reaches the machine
through a SOCKS tunnel. A node with no browser stack of its own (openajar on a Mac) runs it on the
machine instead, and drives it from here:

- The machine has the stack already (browser.sh, in the docker image and the `dev` snapshot), or gets
  it when the environment comes up.
- The browser lane `env/<id>-browser` stays a lane of the node's lane manager, so every browser tool,
  the egress guard, take over and proofs work as on any lane. `install()` puts a router in front of
  the lane manager's engine and displays: lanes of a "vm" environment go to `VmBrowsers`, every
  other lane to the node's own engine and displays, unchanged.
- The screen: Xvfb and x11vnc on the machine (vmbrowser_ctl.py), started over the SSH connection the
  node holds. The live view is the node's own relay (openajar.node.vnc_relay) on a Unix socket that
  the environment's tunnel forwards to the machine's x11vnc socket, so the viewer, its signed links
  and take over are the same code as for a display on the node.
- The browser: Camoufox's browser server on the machine, on 127.0.0.1 only, at a random path; the
  node connects Playwright to it through the tunnel (openajar.environments.tunnels). It is a new
  browser process each time the lane opens (a browser server cannot serve a persistent profile), but the
  same device: the fingerprint its first launch drew is read back, kept on the node and given to every
  later launch, and a save carries it to the resumed environment (openajar.environments.vmpin). The
  sign-ins made in it (cookies, local storage) are read when the lane closes and when it is saved, and
  its next context opens with them (openajar.environments.signins). It holds no account of the owner's.
- On the machine, localhost is the machine: never through a proxy. The rest of the browser's traffic
  goes out through the owner's proxy when the environment has one (openajar.environments.browserproxy).
  Closing the lane stops the browser, releasing the display stops the screen, and closing the
  environment does both before its tunnel goes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shlex
import time
from importlib import resources
from pathlib import Path
from typing import Any

from openajar.core.aio import run_to_end
from openajar.core.errors import EngineError
from openajar.core.models import LaneSpec, ProxyConfig, ViewerInfo
from openajar.environments.vmdownloads import DOWNLOADS_ON_VM, MachineDownloads
from openajar.node.camoufox.pin import CANVAS_PREF
from openajar.node.camoufox.prefs import CACHE_PREFS, LINUX_HOST_PREFS, STEADY_CHROME_PREFS, WHEEL_PREFS
from openajar.node.camoufox.session import CamoufoxSession

log = logging.getLogger("openajar.environments")

BROWSER_DIR = "/opt/agentos-browser"
VENV_PY = f"{BROWSER_DIR}/venv/bin/python"
READY = f"{BROWSER_DIR}/ready"
# One command at bring-up: the ready marker (the versions browser.sh installed) and the build Camoufox launches.
READY_CHECK = f"cat {READY} && {{ echo; cat {BROWSER_DIR}/cache/camoufox/config.json 2>/dev/null; true; }}"
DESKTOP_READY = "/opt/agentos-desktop/ready"
DESKTOP_VERSION = 1     # desktop.sh VERSION: a machine whose ready marker says less installs the desktop again
DESKTOP_SCRIPT_ON_VM = "/usr/local/lib/agentos-env/desktop.sh"
DESKTOP_INSTALL_TIMEOUT_S = 420.0  # apt, from the archive: about a minute on a cloud machine
DESKTOP_INSTALL_WAIT_S = 60.0      # a screen that starts while it installs waits this long for it, then goes bare
REQUIREMENTS_ON_VM = "/usr/local/lib/agentos-env/browser-requirements.txt"
SCRIPT_ON_VM = "/usr/local/lib/agentos-env/browser.sh"
# The machine runs the control script it is sent, with the request: nothing of it is on a command line.
BOOT = ('import json,sys; d=json.load(sys.stdin); '
        'exec(compile(d.pop("script"),"vmbrowser_ctl","exec"),{"__name__":"__openajar_vm__","REQ":d})')
CTL_TIMEOUT_S = 150.0
INSTALL_TIMEOUT_S = 1500.0
CONNECT_TIMEOUT_MS = 30_000
READBACK_BYTES = 4 << 20  # the browser's answer carries its config (fonts, voices, WebGL): well over 64 KB at times
# A launch is four steps, each with its own limit, so a step that hangs fails with its name and the time
# each step took, well inside the lane's wall (openajar.node.deadlines.WALL_S, 210 s). Field report
# 2026-11-16: browser_navigate failed twice with only "could not get the browser ... within 210 s" while
# the machine's log showed its browser server up in 1.4 s; the hang was between the node and the machine.
LAUNCH_STEPS = ("start", "connect", "context", "page")
# "start": the browser (up to 90 s on the machine), then its keyboard helper and the fingerprint readback
STEP_LIMIT_S = {"start": 110.0, "connect": CONNECT_TIMEOUT_MS / 1000 + 5, "context": 30.0, "page": 30.0}
STEP_DOING = {"start": "starting the browser server on the machine", "connect": "connecting to it through the tunnel",
              "context": "opening its browser context", "page": "opening its first page"}
STEP_DONE = {"start": "browser server started", "connect": "connected through the tunnel",
             "context": "context opened", "page": "first page opened"}
UNANSWERED_S = 120.0  # a machine that did not answer a stop this recently is not asked again while it closes
LAUNCH_HINT_KEEP_S = 60.0  # a stopped launch's steps, kept for the stopped call's error
# the processes of the machine's browser (Camoufox is Firefox; its server is Python), for a kill during a launch
BROWSER_PROCS = ("camoufox", "firefox", "web content", "isolated web co", "webextensions", "rdd process",
                 "socket process", "utility process", "privileged cont", "python", "xvfb", "x11vnc")
DESKTOP = (1920, 1080)   # an environment's browser shows pages at a desktop width, as on the node
# With the desktop session (environments.toml desktop = true, openajar/environments/desktop) the browser is a
# window on it, not the whole screen: an even margin of wallpaper beside it, its own title bar (its tabs) at the
# top, the top bar (deskart.py TOP_H, 54 px) above it and the dock's band (deskart.py BAND_H, 90 px) below it. Camoufox centres the window it reports on the
# screen, and the window is put there (desk rc.xml), so outerWidth/Height, screen and screen.avail* stay one
# picture (screenX/Y are Camoufox's own pick each launch, as before).
DESKTOP_WINDOW = (1776, 890)
WINDOW_MARGIN = 72
# Browser chrome only (no page can read them): the browser draws its own title bar, its tabs with its minimize
# and maximize buttons beside them, as a browser looks on a desktop (vmbrowser_ctl starts it with
# MOZ_GTK_TITLEBAR_DECORATION=client, as openbox has no compositor), and no close button: the agent's browser is
# minimized (its button, Alt+F4), never closed under a person's hands; Ctrl+Q does not end it either. What a page
# reads changes with it: outerWidth/Height are GTK's edge (5 px a side) larger and innerHeight about 35 px taller,
# real sizes of a real window, inside the pinned screen (docs/ENVIRONMENTS.md).
DESKTOP_PREFS = {"browser.tabs.inTitlebar": 1, "ui.GtkCSDCloseButton": 0, "browser.quitShortcut.disabled": True}
# The browser's own saves go to /work/downloads (openajar.environments.vmdownloads), without asking where Firefox
# lets them. A site's download goes through Playwright as before. The PDF viewer's Save and Ctrl+S always ask
# (Firefox's rule), in a Save As window that now opens on that folder. The PDF viewer stays on.
DOWNLOAD_PREFS = {"browser.download.useDownloadDir": True, "browser.download.folderList": 2,
                  "browser.download.dir": DOWNLOADS_ON_VM, "browser.download.always_ask_before_handling_new_types": False,
                  "browser.download.alwaysOpenPanel": False, "pdfjs.disabled": False}
DESKTOP_DIR = "desktop"
WALLPAPER_MAX = 8 * 1024 * 1024
WALLPAPER_NAMES = ("wallpaper.svg", "wallpaper.png", "wallpaper.jpg", "wallpaper.jpeg")
# The OS the machine's browser claims when the node's engine names none (openajar local has no engine of its own):
# Linux, what the machine is and what a server node's lanes claim by default (lane_os).
VM_IDENTITY_OS = ["linux"]
# Playwright drives the machine's browser from here: the `local` extra of openajar (scripts/install.sh) has it.
NO_PLAYWRIGHT = ("this openajar has no Playwright, which drives the environment's browser on its machine. Install "
                 "openajar again with scripts/install.sh, or add the local extra: uv tool install --force "
                 "'openajar[local] @ git+https://github.com/sabalys-ramunas/openajar.git'")


def _text(name: str) -> str:
    return resources.files("openajar.environments").joinpath(name).read_text()


def ctl_script() -> str:
    return _text("vmbrowser_ctl.py")


def ctl_command() -> str:
    return f"{VENV_PY} -I -c {shlex.quote(BOOT)}"


def install_script(*, again: bool = False) -> str:
    """browser.sh with its requirements, as one script for a shell on the machine: both files are
    written next to setup.sh, then browser.sh runs (it does nothing where the stack is ready). `again`:
    the machine's stack is at other versions (vmpin.stale), so its venv and ready marker go first."""
    out = ["set -eu", "mkdir -p /usr/local/lib/agentos-env"]
    if again:
        out.append(f"rm -rf {BROWSER_DIR}/venv {READY}")
    for path, body, mark in ((REQUIREMENTS_ON_VM, _text("browser-requirements.txt"), "AGENTOS_BROWSER_REQUIREMENTS"),
                             (SCRIPT_ON_VM, _text("browser.sh"), "OPENAJAR_BROWSER_SCRIPT")):
        out.append(f"cat > {path} <<'{mark}'\n{body.rstrip()}\n{mark}")
    out.append(f"sh {SCRIPT_ON_VM}")
    return "\n".join(out) + "\n"


def desktop_window(size: tuple[int, int] = DESKTOP) -> tuple[int, int]:
    """The browser's window on a desktop of `size`: DESKTOP_WINDOW on the usual screen, else the same
    margins (WINDOW_MARGIN around the frame, its title bar and the dock's band below)."""
    if tuple(size) == DESKTOP:
        return DESKTOP_WINDOW
    width, height = size
    return max(640, width - 2 * WINDOW_MARGIN), max(480, height - 2 * WINDOW_MARGIN - 46)


def desktop_install_script() -> str:
    """desktop.sh as one script for a shell on the machine (it does nothing where its version is there)."""
    body = _text("desktop.sh").rstrip()
    return (f"set -eu\nmkdir -p /usr/local/lib/agentos-env\n"
            f"cat > {DESKTOP_SCRIPT_ON_VM} <<'OPENAJAR_DESKTOP_SCRIPT'\n{body}\nOPENAJAR_DESKTOP_SCRIPT\n"
            f"sh {DESKTOP_SCRIPT_ON_VM}\n")


def browser_options(spec: LaneSpec, *, identity_os: list[str] | None = None,
                    size: tuple[int, int] = DESKTOP, desktop: bool = False) -> dict[str, Any]:
    """What the machine's Camoufox starts with: the node's own prefs for a lane (openajar.node.camoufox.
    engine), a desktop window that fills the screen, as an environment's browser has on the node, or on
    the desktop session a window with the wallpaper around it (desktop_window). No proxy option and no PAC:
    on the machine, localhost is the machine (an owner's proxy is a Firefox pref, network.vm_browser_prefs)."""
    width, height = size
    window = desktop_window(size) if desktop else (width, height)
    prefs = {**CACHE_PREFS, **STEADY_CHROME_PREFS, **WHEEL_PREFS, **LINUX_HOST_PREFS, **DOWNLOAD_PREFS, CANVAS_PREF: ""}
    if desktop:
        prefs.update(DESKTOP_PREFS)
    opts: dict[str, Any] = {
        "headless": False, "humanize": True, "locale": spec.locale, "enable_cache": True,
        "i_know_what_im_doing": True, "geoip": False, "screen": [width, height], "window": list(window),
        "firefox_user_prefs": prefs,
    }
    opts["os"] = list(identity_os or VM_IDENTITY_OS)
    return opts


def desktop_files() -> dict[str, str]:
    """The desktop's files (openajar/environments/desktop), by their path in it, as text."""
    root = resources.files("openajar.environments").joinpath(DESKTOP_DIR)
    out: dict[str, str] = {}

    def walk(node: Any, prefix: str) -> None:
        for child in node.iterdir():
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                if child.name != "__pycache__":
                    walk(child, rel + "/")
            elif not child.name.startswith("wallpaper") and not child.name.endswith((".pyc", ".png", ".jpg")):
                out[rel] = child.read_text()

    walk(root, "")
    return out


def wallpaper(home: Path | None, size: tuple[int, int] = DESKTOP) -> dict[str, str]:
    """The wallpaper the machine draws: the owner's own ($OPENAJAR_HOME/desktop/wallpaper.svg, .png or .jpg,
    at most 8 MB), else ours: on the usual screen the picture of it made ahead (wallpaper-1920x1080.jpg: a new
    machine would spend over a second drawing the SVG), at any other size the SVG. {kind: svg|image, data:
    base64}."""
    import base64

    for name in WALLPAPER_NAMES if home is not None else ():
        path = Path(home) / DESKTOP_DIR / name
        try:
            if path.is_file() and path.stat().st_size <= WALLPAPER_MAX:
                return {"kind": "svg" if name.endswith(".svg") else "image",
                        "data": base64.b64encode(path.read_bytes()).decode()}
            if path.is_file():
                log.warning("%s is over %d MB; the desktop keeps its own wallpaper", path, WALLPAPER_MAX >> 20)
        except OSError as e:
            log.warning("could not read %s: %s", path, e)
    ours = resources.files("openajar.environments").joinpath(DESKTOP_DIR)
    made = ours.joinpath(f"wallpaper-{size[0]}x{size[1]}.jpg")
    if made.is_file():
        return {"kind": "image", "data": base64.b64encode(made.read_bytes()).decode()}
    return {"kind": "svg", "data": base64.b64encode(ours.joinpath("wallpaper.svg").read_bytes()).decode()}


def desktop_request(home: Path | None, size: tuple[int, int] = DESKTOP, title: str = "",
                    browser_fills: bool = False) -> dict[str, Any]:
    """What the display action needs for the desktop session: its files, the wallpaper, the browser's window,
    and the environment's title for the top bar. `browser_fills`: the desktop comes under a browser that was
    started on the bare screen (filling it, with no title bar of its own): openbox frames it maximized."""
    window = size if browser_fills else desktop_window(size)
    return {"files": desktop_files(), "wallpaper": wallpaper(home, size), "window": list(window),
            "title": title, "own_titlebar": not browser_fills}


class LaunchSteps:
    """How far a browser launch on the machine got, and how long each step took: for its error, its log
    line, and a stuck call's hint (VmBrowsers.launch_hint)."""

    def __init__(self, lane_id: str, env_id: str, clock: Any = None):
        self.lane_id, self.env_id = lane_id, env_id
        self._clock = clock or asyncio.get_running_loop().time
        self.began = self._clock()
        self.done: list[tuple[str, float]] = []
        self.current: str | None = None
        self._step_began = self.began
        self.stopped: float | None = None  # when the call that opened it was stopped (the lane's wall)

    async def run(self, name: str, coro: Any) -> Any:
        """Run one step within its limit (STEP_LIMIT_S). A step that fails or does not answer raises
        LaunchFailed with its name and how long it ran."""
        self.current, self._step_began = name, self._clock()
        limit = STEP_LIMIT_S[name]
        try:
            out = await asyncio.wait_for(coro, limit)
        except TimeoutError:
            raise LaunchFailed(self, name, self._clock() - self._step_began, f"no answer within {limit:g} s") from None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # every failure of a step is named
            raise LaunchFailed(self, name, self._clock() - self._step_began,
                               str(getattr(e, "message", None) or e).strip() or type(e).__name__) from e
        self.done.append((name, self._clock() - self._step_began))
        self.current = None
        return out

    @property
    def elapsed(self) -> float:
        return self._clock() - self.began

    def done_words(self) -> str:
        return ", ".join(f"{STEP_DONE[n]} in {t:.1f} s" for n, t in self.done)

    def now_words(self) -> str:
        """While it runs (or when it was stopped): the steps done and the one it is at, for a stuck call."""
        end = self.stopped if self.stopped is not None else self._clock()
        at = (f"{STEP_DOING[self.current]} for {end - self._step_began:.0f} s" if self.current
              else "between its steps")
        done = self.done_words()
        what = "was still opening when the call was stopped" if self.stopped is not None else "was opening"
        return f"The browser on the machine {what}: {done + '; then ' if done else ''}{at}."


class LaunchFailed(Exception):
    def __init__(self, steps: LaunchSteps, step: str, took: float, why: str):
        super().__init__(why)
        self.steps, self.step, self.took, self.why = steps, step, took, why

    def words(self, kills: list[dict] | None = None) -> str:
        """The error: which step failed and why, how long each took; a kernel's oom kill during the launch first."""
        from openajar.env import oom

        doing = f"{STEP_DOING[self.step]} failed after {self.took:.1f} s ({self.why[:400]})"
        done = self.steps.done_words()
        tail = f" Before it: {done}." if done else ""
        head = f"{self.steps.lane_id}: the browser on environment {self.steps.env_id}'s machine did not open"
        note = oom.note(kills, running=False, what="browser launch") if kills else None
        if note:
            return f"{head}: {note} ({doing[0].upper() + doing[1:]}.{tail})"
        return f"{head}: {doing}.{tail}"


class RemoteSession(CamoufoxSession):
    """A lane's browser that runs on its environment's machine: the same session as on the node (pages,
    routes, dialogs, the egress guard), with no process here. It is alive while Playwright is connected,
    and closing it also stops the browser on the machine (`on_close`)."""

    def __init__(self, playwright: Any, browser: Any, context: Any, profile_dir: Path, close_timeout: float, *,
                 on_close: Any, before_close: Any = None, **kw: Any):
        super().__init__(playwright, context, profile_dir, None, close_timeout, **kw)
        self._browser = browser
        self._on_close = on_close
        self._before_close = before_close  # reads the sign-ins while the context is still there
        browser.on("disconnected", lambda *_: setattr(self, "_ctx_closed", True))

    @property
    def alive(self) -> bool:
        return not (self._closed or self._ctx_closed) and bool(self._browser.is_connected())

    @property
    def pids(self) -> list[int]:
        return []

    async def close(self) -> None:
        if self._closed:
            return
        if self._before_close is not None and self.alive:
            with contextlib.suppress(Exception):
                await self._before_close(self._ctx)
        try:
            await super().close()
        finally:
            await run_to_end(self._stop_remote())

    async def _stop_remote(self) -> None:
        try:
            await self._on_close()
        except Exception as e:  # noqa: BLE001 - the machine may be gone already; deleting it ends the browser
            log.info("the environment's browser did not stop on its machine: %s", e)


class VmBrowsers:
    """The machine side of "vm" browsers, for the environments service `svc` (its store, terminal
    lanes and tunnels). `engine` is the lane manager's own engine: its file and dialog settings."""

    def __init__(self, svc: Any):
        self.svc = svc
        self.engine: Any = None
        self._viewers: dict[str, ViewerInfo] = {}
        self.desktops: dict[str, bool] = {}  # lane -> its screen runs the desktop session
        # environment -> the size of its screen, while that screen runs the desktop: it stays up from when the
        # environment is ready (screen_soon) until it closes (drop_screen), and the browser comes and goes on it
        self._screens: dict[str, tuple[int, int]] = {}
        self._screen_locks: dict[str, asyncio.Lock] = {}
        self._screen_tasks: dict[str, asyncio.Task] = {}
        self._installing: dict[str, asyncio.Task] = {}  # environment -> its desktop being installed
        self.launching: dict[str, LaunchSteps] = {}  # lane -> its launch in progress (launch_hint)
        self._unanswered: dict[str, float] = {}  # environment -> when its machine last failed a stop
        # environment -> the Camoufox build its machine launches, and the camoufox package there (vmpin)
        self.builds: dict[str, str | None] = {}
        self.camoufox: dict[str, str | None] = {}
        self._stale: set[str] = set()  # environments whose machine has the browser at other versions

    # ---- which lanes
    def env_for(self, lane_id: str) -> Any:
        found = self.svc.env_of_lane(lane_id) if isinstance(lane_id, str) else None
        if found is None or found[1] != "browser" or found[0].browser != "vm":
            return None
        return found[0]

    def handles(self, lane_id: str) -> bool:
        return self.env_for(lane_id) is not None

    def _require(self, lane_id: str) -> tuple[Any, Any]:
        env = self.env_for(lane_id)
        if env is None:
            raise EngineError(f"{lane_id} is not the browser of an environment on its machine")
        tunnel = self.svc.tunnel_of(env.id)
        if tunnel is None or not tunnel.alive or not getattr(tunnel, "vm_browser", False):
            from openajar.environments.errors import EnvironmentNotReady

            raise EnvironmentNotReady(f"environment {env.id}'s connection to its machine is down; the node starts it "
                                      "again within a minute. Nothing ran")
        return env, tunnel

    # ---- the machine
    async def _exec(self, env: Any, cmd: str, **kw: Any) -> Any:
        """A command of the node's own on the machine's terminal lane: the screen's control script, its checks
        and installs. It is no use of the machine: the lane's last use is put back as it was, so the idle rule
        (environments/activity.py _last_used) never sees the node's housekeeping (a screen checked every pass
        would otherwise keep a machine from ever closing for idleness). An agent's call counts on its own
        (last_call_at). sshaccess.ssh_seen and updates.quiet_upgrade do the same."""
        envs = self.svc.envs()
        store = getattr(envs, "store", None)
        before = None
        with contextlib.suppress(Exception):
            before = store.last_used(env.terminal_lane) if store is not None else None
        try:
            from openajar.environments.activity import own

            return await envs.exec(env.terminal_lane, cmd, **kw, **own(envs))
        finally:
            if store is not None:
                with contextlib.suppress(Exception):
                    store.touch(env.terminal_lane, at=before or env.created_at)

    async def ctl(self, env: Any, action: str, *, timeout_s: float = CTL_TIMEOUT_S, check: bool = True,
                  max_bytes: int | None = None, **args: Any) -> dict[str, Any]:
        """`max_bytes`: an answer larger than the node's usual output limit (the browser's config read back)."""
        req = json.dumps({"script": ctl_script(), "action": action, **args})
        res = await self._exec(env, ctl_command(), stdin=req, timeout_s=timeout_s,
                               **({"max_bytes": max_bytes} if max_bytes else {}))
        line = next((ln for ln in reversed((res.stdout or "").splitlines()) if ln.startswith("{")), "")
        try:
            out = json.loads(line) if line else {}
        except ValueError:
            out = {}
        if not check and res.exit_code == 0 and out:
            return out
        if res.exit_code != 0 or not out.get("ok"):
            why = out.get("error") or (res.stderr or res.stdout or "").strip()[-600:] or f"exit {res.exit_code}"
            raise EngineError(f"environment {env.id}: the browser's {action} on its machine failed: {why}")
        return out

    async def installed(self, env: Any) -> bool:
        """The browser's stack is on the machine at the versions browser-requirements.txt pins (a machine of an
        older image has another: vmpin.stale). Notes the Camoufox build it launches, for the pin's version."""
        from openajar.environments import vmpin

        res = await self._exec(env, READY_CHECK, timeout_s=30)
        if res.exit_code != 0:
            return False
        out = res.stdout or ""
        self.builds[env.id] = vmpin.active_build(out) or self.builds.get(env.id)
        self.camoufox[env.id] = vmpin.marker_camoufox(out) or self.camoufox.get(env.id)
        off = vmpin.stale(out)
        if off:
            log.warning("environment %s: the browser on its machine is %s; installing it again", env.id,
                        ", ".join(f"{name} {have}, not {want}" for name, (have, want) in sorted(off.items())))
            self._stale.add(env.id)
            return False
        return True

    def wants_desktop(self) -> bool:
        return bool(getattr(getattr(self.svc, "settings", None), "desktop", True))

    async def ensure_installed(self, env: Any) -> None:
        """The browser's stack on the machine: there already on the docker image and the `dev`
        snapshot; installed now on any other (a few minutes). Then the desktop's, when it is on: a machine
        without it (or with an older one) gets it in the background, so the environment opens as fast as
        before; a screen that starts meanwhile waits for it a while (acquire), and when it fails the screen
        stays the bare browser."""
        await self._ensure_browser(env)
        if self.wants_desktop() and env.id not in self._installing:
            task = asyncio.create_task(self.ensure_desktop(env), name=f"desktop-install-{env.id}")
            self._installing[env.id] = task
            task.add_done_callback(lambda _t, i=env.id: self._installing.pop(i, None))

    async def ensure_desktop(self, env: Any) -> bool:
        """desktop.sh on the machine, at DESKTOP_VERSION. Never raises: a desktop that cannot be installed is
        logged, and the screen starts without it."""
        try:
            res = await self._exec(env, f"head -n 1 {DESKTOP_READY} 2>/dev/null",
                                             timeout_s=30)
            if (res.stdout or "").strip() == str(DESKTOP_VERSION):
                return True
            log.info("environment %s: installing the desktop on its machine (version %d)", env.id, DESKTOP_VERSION)
            res = await self._exec(env, "AGENTOS_DESKTOP_LIVE=1 sh -s", stdin=desktop_install_script(),
                                             timeout_s=DESKTOP_INSTALL_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - the browser works without the desktop
            log.warning("environment %s: the desktop was not installed on its machine (%s); its screen shows the "
                        "browser alone", env.id, e)
            return False
        if res.exit_code != 0:
            tail = (res.stderr or res.stdout or "").strip()[-400:]
            log.warning("environment %s: installing the desktop on its machine failed (exit %s): %s; its screen "
                        "shows the browser alone", env.id, res.exit_code, tail)
            return False
        return True

    async def _ensure_browser(self, env: Any) -> None:
        if await self.installed(env):
            return
        again = env.id in self._stale
        if not again:
            log.info("environment %s: installing the browser on its machine (no image has it)", env.id)
        res = await self._exec(env, "sh -s", stdin=install_script(again=again),
                                         timeout_s=INSTALL_TIMEOUT_S)
        if res.exit_code != 0:
            tail = (res.stderr or res.stdout or "").strip()[-600:]
            raise EngineError(f"environment {env.id}: installing the browser on its machine failed "
                              f"(exit {res.exit_code}): {tail}")
        self._stale.discard(env.id)
        if again:  # the build it launches now, for the pin's version
            with contextlib.suppress(Exception):
                await self.installed(env)

    # ---- the environment's screen
    def screen_soon(self, env: Any) -> None:
        """Start the environment's screen and its desktop now, in the background (environment_open never waits
        for it): from then on until the environment closes, its live view shows the desktop (the wallpaper,
        the dock, the agent's commands), with or without the browser. Nothing with the desktop off."""
        if getattr(env, "browser", None) != "vm" or not self.wants_desktop() or env.id in self._screens:
            return
        running = self._screen_tasks.get(env.id)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._screen_up(env), name=f"screen-{env.id}")
        self._screen_tasks[env.id] = task
        task.add_done_callback(lambda t, i=env.id: self._screen_tasks.pop(i, None) if self._screen_tasks.get(i)
                               is t else None)

    async def _screen_up(self, env: Any) -> None:
        pending = self._installing.get(env.id)
        if pending is not None:  # a machine that gets the desktop now: the screen waits for it, out of anyone's way
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(pending), DESKTOP_INSTALL_TIMEOUT_S)
        try:
            self._require(env.browser_lane)
            await self._show(env, DESKTOP, unless_in_use=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the screen starts again with the browser
            log.warning("environment %s: its screen did not start (%s); it starts when the agent first uses the "
                        "browser", env.id, getattr(e, "message", e))

    async def _show(self, env: Any, size: tuple[int, int], *, unless_in_use: bool = False,
                    then: Any = None) -> dict[str, Any]:
        """The machine's screen at `size`, one call at a time per environment. With the desktop on, a screen that
        already runs at that size stays as it is (keep): the browser and every window a person opened stay, and
        what of it is gone (its desktop) starts again on it. Else it starts afresh, as before. `unless_in_use`:
        while the browser lane holds the screen it is never started afresh, only mended (a screen started before
        the desktop was installed gets its desktop under the running browser). `then` runs under the same lock
        once the screen is there (the lane takes it)."""
        lock = self._screen_locks.setdefault(env.id, asyncio.Lock())
        async with lock:
            held = self._viewers.get(env.browser_lane)
            if unless_in_use and held is not None:
                if not self.wants_desktop():
                    return {}
                return await self._display(env, (held.width or DESKTOP[0], held.height or DESKTOP[1]),
                                           only_keep=True)
            out = await self._display(env, size)
            if then is not None:
                then()
            return out

    async def _display(self, env: Any, size: tuple[int, int], *, only_keep: bool = False) -> dict[str, Any]:
        """The display action itself, and what the node keeps of its answer (under the lock). `only_keep`: the
        screen under the browser lane is mended, never started afresh."""
        width, height = size
        # a browser the lane started on the bare screen (the desktop came late) fills it: the desktop frames it so
        fills = only_keep and not self.desktops.get(env.browser_lane, False)
        desktop = (desktop_request(getattr(self.svc, "home", None), size, title=getattr(env, "title", "") or "",
                                   browser_fills=fills) if self.wants_desktop() else None)
        out = await self.ctl(env, "display", width=width, height=height,
                             **({"desktop": desktop, "keep": True} if desktop else {}),
                             **({"only_keep": True} if only_keep else {}))
        if out.get("untouched"):
            return out
        on = bool(out.get("desktop"))
        self.desktops[env.browser_lane] = on
        if on:
            self._screens[env.id] = (width, height)
        else:
            self._screens.pop(env.id, None)
        mended = out.get("mended") or []
        if out.get("kept") and "desktop" not in mended and not (desktop and not on):
            if on:  # the same helper, still running (a node restart finds it): a link to it if there is none
                from openajar.environments import desklink

                desklink.install(self.svc, self).greet(env)
            return out
        if desktop and not on:
            log.warning("environment %s: the desktop did not start on its machine (%s); its screen shows the browser "
                        "alone", env.id, out.get("desktop_error") or "no reason given")
        elif desktop:
            log.info("environment %s: the desktop started%s in %.2f s (%s)", env.id,
                     " again on its running screen" if out.get("kept") else "", float(out.get("desktop_s") or 0),
                     out.get("server") or "x11vnc")
            from openajar.environments import desklink

            links = desklink.install(self.svc, self)  # the env manager's hooks, in case it was not up when made
            links.forget(env.id)  # a fresh helper on the machine: a link to the one before is stale
            links.hello(env)
        return out

    def check_screen(self, env: Any) -> None:
        """The node's regular pass over its environments (openajar.environments.manager): an environment whose desktop
        is up gets its screen asked, in the background, and a screen that died on its own (Xvnc or the session)
        starts again, so its live view comes back without anyone opening the browser. Nothing while the screen is
        being started, the browser is launching or the desktop installs."""
        if env.id not in self._screens or env.id in self._installing or env.browser_lane in self.launching:
            return
        running = self._screen_tasks.get(env.id)
        lock = self._screen_locks.get(env.id)
        if (running is not None and not running.done()) or (lock is not None and lock.locked()):
            return
        task = asyncio.create_task(self._screen_mend(env), name=f"screen-check-{env.id}")
        self._screen_tasks[env.id] = task
        task.add_done_callback(lambda t, i=env.id: self._screen_tasks.pop(i, None) if self._screen_tasks.get(i)
                               is t else None)

    async def _screen_mend(self, env: Any) -> None:
        try:
            self._require(env.browser_lane)
            st = await self.ctl(env, "status", timeout_s=30)
            if st.get("display") and st.get("desktop"):
                return
            log.warning("environment %s: its screen %s on its machine; starting it again", env.id,
                        "lost its desktop" if st.get("display") else "stopped")
            # a browser that still runs keeps its screen (only what is missing starts); one that went with the
            # screen leaves nothing to keep, and the lane opens it again on the new screen at its next call
            await self._show(env, self._screens.get(env.id) or DESKTOP, unless_in_use=bool(st.get("browser")))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the next pass asks again
            log.warning("environment %s: its screen could not be started again (%s)", env.id, getattr(e, "message", e))

    def screen_size(self, env_id: str) -> tuple[int, int] | None:
        """The size of the environment's screen while it runs the desktop, else None."""
        return self._screens.get(env_id)

    def screen(self, lane_id: str) -> ViewerInfo | None:
        """The screen of the environment whose browser lane this is, while it runs the desktop, with the browser
        or without it: the live view of a cold browser lane shows the desktop (openajar.node.lanes)."""
        env = self.env_for(lane_id)
        size = self._screens.get(env.id) if env is not None and env.open else None
        if size is None:
            return None
        tunnel = self.svc.tunnel_of(env.id)
        if tunnel is None or not tunnel.alive or not getattr(tunnel, "vnc_path", None):
            return None
        return ViewerInfo(display=":99", vnc_socket=tunnel.vnc_path, width=size[0], height=size[1])

    async def drop_screen(self, env: Any) -> None:
        """The environment closes: its screen stops on the machine while the tunnel still reaches it."""
        task = self._screen_tasks.pop(env.id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        had = self._screens.pop(env.id, None) is not None
        self.desktops.pop(env.browser_lane, None)
        self._screen_locks.pop(env.id, None)
        links = getattr(self.svc, "_desk_links", None)
        if links is not None:
            links.forget(env.id)
        failed = self._unanswered.pop(env.id, None)
        if had and failed is not None and time.monotonic() - failed < UNANSWERED_S:
            log.info("environment %s: its machine did not answer the browser's stop a moment ago; its screen is "
                     "not asked again (the machine goes with the environment)", env.id)
        elif had:
            with contextlib.suppress(Exception):  # a machine that is gone has no screen to stop
                await self.ctl(env, "stop", what=["browser", "display"], timeout_s=30)

    # ---- displays
    async def acquire(self, lane_id: str, size: tuple[int, int] | None = None) -> ViewerInfo:
        env, tunnel = self._require(lane_id)
        width, height = size or DESKTOP
        pending = self._installing.get(env.id)
        if pending is not None and self.wants_desktop():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(pending), DESKTOP_INSTALL_WAIT_S)
        viewer = ViewerInfo(display=":99", vnc_socket=tunnel.vnc_path, width=width, height=height)
        await self._show(env, (width, height), then=lambda: self._viewers.__setitem__(lane_id, viewer))
        return viewer

    async def release(self, lane_id: str) -> None:
        if self._viewers.pop(lane_id, None) is None:
            return
        env = self.env_for(lane_id)
        if env is not None and env.open and env.id in self._screens:
            # the screen is the environment's: only the browser goes, the desktop stays until the environment closes
            try:
                await self.ctl(env, "stop", what=["browser"], timeout_s=30)
            except Exception:  # noqa: BLE001 - a machine that does not answer: its close does not wait for it again
                self._unanswered[env.id] = time.monotonic()
            return
        self.desktops.pop(lane_id, None)
        links = getattr(self.svc, "_desk_links", None)
        if links is not None and env is not None:
            links.forget(env.id)
        if env is not None and env.open:
            with contextlib.suppress(Exception):  # a machine that is gone has no screen to stop
                await self.ctl(env, "stop", what=["browser", "display"], timeout_s=30)

    def get(self, lane_id: str) -> ViewerInfo | None:
        return self._viewers.get(lane_id)

    def healthy(self, lane_id: str) -> bool:
        viewer = self._viewers.get(lane_id)
        env = self.env_for(lane_id)
        tunnel = self.svc.tunnel_of(env.id) if env is not None else None
        return bool(viewer and tunnel is not None and tunnel.alive and viewer.vnc_socket == tunnel.vnc_path
                    and viewer.vnc_socket and os.path.exists(viewer.vnc_socket))

    async def release_all(self) -> None:
        for lane_id in list(self._viewers):
            await self.release(lane_id)

    async def read_clipboard(self, lane_id: str) -> str:
        """What the machine's browser copied, for the live view's copy button (openajar.node.lane_clipboard
        reads a screen on the node the same way, with the same limits and errors)."""
        from openajar.node import lane_clipboard as lc

        env, _tunnel = self._require(lane_id)
        out = await self.ctl(env, "clipboard", check=False, timeout_s=30, limit=lc.MAX_BYTES,
                             timeout=lc.TIMEOUT_SECONDS, nothing=[w.decode() for w in lc.NOTHING_COPIED])
        if out.get("ok"):
            return str(out.get("text") or "")
        if out.get("kind") == "too_large":
            raise lc.ClipboardTooLarge(f"What is copied on the screen is over {lc.MAX_BYTES // (1024 * 1024)} MB, too "
                                       "much to bring over. Copy a smaller part.")
        if out.get("kind") == "timeout":
            raise lc.ClipboardUnreadable("The screen did not hand over what was copied in time. Copy again on the "
                                         "screen, then try once more.")
        raise lc.ClipboardUnreadable(f"The machine could not read the screen's clipboard ({out.get('error') or 'no '
                                     'reason given'}).")

    # ---- the browser
    async def launch(self, spec: LaneSpec, *, profile_dir: Path) -> RemoteSession:
        """Open the lane's browser on its machine, in four timed steps (LAUNCH_STEPS). A step that fails
        or hangs fails the launch with its name, how long each step took, and what the kernel killed for
        memory meanwhile when it did; the log has the same line, and a launch that works logs its times."""
        try:
            import playwright.async_api  # noqa: F401
        except ModuleNotFoundError as e:
            if e.name is None or not e.name.startswith("playwright"):
                raise
            raise EngineError(NO_PLAYWRIGHT) from None

        env, tunnel = self._require(spec.id)
        steps = LaunchSteps(spec.id, env.id)
        self.launching[spec.id] = steps
        try:
            session = await self._launch(spec, env, tunnel, steps, profile_dir=profile_dir)
        except LaunchFailed as e:
            self.launching.pop(spec.id, None)
            kills = await self._launch_kills(env, steps)
            text = e.words(kills)
            log.warning("%s (after %.1f s in all)", text, steps.elapsed)
            raise EngineError(text) from e.__cause__
        except asyncio.CancelledError:  # the lane's wall: its error asks launch_hint how far this got
            steps.stopped = steps._clock()
            log.warning("%s: the launch on environment %s's machine was stopped. %s", spec.id, env.id,
                        steps.now_words())
            raise
        except BaseException:
            self.launching.pop(spec.id, None)
            raise
        self.launching.pop(spec.id, None)
        log.info("%s: the browser runs on environment %s's machine (%s; %.1f s in all)", spec.id, env.id,
                 steps.done_words(), steps.elapsed)
        return session

    def launch_hint(self, lane_id: str) -> str | None:
        """For a call that was stuck on a lane whose browser is opening on its machine, or was stopped
        while it opened in the last LAUNCH_HINT_KEEP_S (openajar.node.lanes slow_hints): the steps done,
        with their times, and the one it was at."""
        steps = self.launching.get(lane_id)
        if steps is None:
            return None
        if steps.stopped is not None and steps._clock() - steps.stopped > LAUNCH_HINT_KEEP_S:
            self.launching.pop(lane_id, None)
            return None
        return steps.now_words()

    async def _launch_kills(self, env: Any, steps: LaunchSteps) -> list[dict] | None:
        """What the machine's kernel killed for memory during the launch (openajar.env.oom), the browser's
        processes first; None when it killed nothing then, or could not be asked."""
        from openajar.env import oom
        from openajar.vms.ssh import PGID_DIR

        try:
            res = await asyncio.wait_for(self._exec(env, oom.probe_script(
                PGID_DIR, "launch-none", final=False), timeout_s=oom.PROBE_TIMEOUT_S), oom.PROBE_TIMEOUT_S + 2)
            kills = oom.parse(res.stdout or "", elapsed_s=steps.elapsed)
        except Exception:  # noqa: BLE001 - the machine may be the thing that does not answer
            return None
        if not kills:
            return None

        def browsers(k: dict) -> bool:
            return any(p in str(k.get("name", "")).lower() for p in BROWSER_PROCS)

        return sorted(kills, key=browsers)  # oom.note names the newest three: the browser's go last

    async def _launch(self, spec: LaneSpec, env: Any, tunnel: Any, steps: LaunchSteps, *,
                      profile_dir: Path) -> RemoteSession:
        from playwright.async_api import async_playwright

        from openajar.node.egress import TRACE_NAME, EgressTrace, LaneGuard
        from openajar.node.files import FilesConfig, LaneFiles
        from openajar.node.interaction.dialogs import DIALOG_TIMEOUT

        engine = self.engine
        viewer = self._viewers.get(spec.id)
        size = (viewer.width or DESKTOP[0], viewer.height or DESKTOP[1]) if viewer else DESKTOP
        ws_path = "/" + secrets.token_urlsafe(24)
        on_desktop = self.desktops.get(spec.id, False)
        options = browser_options(spec, identity_os=getattr(engine, "identity_os", None), size=size,
                                  desktop=on_desktop)
        from openajar.environments.network import vm_browser_prefs

        # the owner's proxy (openajar.environments.browserproxy): checked first, and no browser without it
        proxied = await getattr(self.svc, "browser_proxy_options", _no_proxy)(env)
        options["firefox_user_prefs"].update(vm_browser_prefs(getattr(env, "network", "full"), proxied=bool(proxied)))
        from openajar.environments import machineplace, vmkeys, vmpin

        options.update(proxied or await machineplace.place_options(self.svc, env))  # no proxy: the machine's place

        # the environment's one device: its pin, or the first launch's draw read back (vmpin)
        pinning = vmpin.prepare(self.svc, env, spec.id, options, build=self.builds.get(env.id))
        started = await steps.run("start", self.ctl(env, "browser", options=options, port=_vm_port(), ws_path=ws_path,
                                                    timeout_s=STEP_LIMIT_S["start"] - 5, max_bytes=READBACK_BYTES,
                                                    **pinning.request, **vmkeys.request(options, spec.locale),
                                                    **({"own_titlebar": True} if on_desktop else {})))
        self._settle_pin(pinning, started)
        profile_dir.mkdir(parents=True, exist_ok=True)

        async def stop_remote() -> None:
            await vmkeys.detach(env.id)
            if self.svc.store.get(env.id).open:
                await self.ctl(env, "stop", what=["browser"], timeout_s=30)

        pw = await async_playwright().start()
        browser = None
        try:
            browser = await steps.run("connect", pw.firefox.connect(f"ws://127.0.0.1:{tunnel.browser_port}{ws_path}",
                                                                    timeout=CONNECT_TIMEOUT_MS))
            from openajar.environments import signins

            state = signins.for_launch(self.svc, env)  # the sign-ins a save or the lane's last close left
            context = await steps.run("context", browser.new_context(
                no_viewport=True, **({"storage_state": state} if state else {})))

            async def keep_signins(ctx: Any) -> None:
                if signins.enabled(self.svc, self.svc.store.get(env.id)):
                    signins.remember(self.svc, env.id, await signins.read_state(ctx))

            session = RemoteSession(pw, browser, context, profile_dir, float(getattr(engine, "close_timeout", 15.0)),
                                    on_close=stop_remote, before_close=keep_signins,
                                    files=LaneFiles(profile_dir.parent / "files", getattr(engine, "files", None)
                                                    or FilesConfig()), machine=MachineDownloads(self, env),
                                    dialog_timeout=float(getattr(engine, "dialog_timeout", DIALOG_TIMEOUT)))
        except BaseException as e:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pw.stop(), 10)
            await run_to_end(_quietly(stop_remote()))
            if isinstance(e, (asyncio.CancelledError, LaunchFailed)):
                raise
            raise LaunchFailed(steps, steps.current or "connect", 0.0, str(e)) from e
        try:
            session.guard = LaneGuard(spec.id, sites=spec.sites, strict=spec.egress_strict,
                                      trace=EgressTrace(profile_dir.parent / TRACE_NAME)
                                      ).attach(session.context, session.routes)
            await vmkeys.attach(env.id, tunnel, session.context, started)  # letters the US layout lacks, as keys
            # a window on the screen at once, as a browser on the node shows one
            await steps.run("page", session.page())
        except BaseException:
            await run_to_end(session.close())
            raise
        return session


    def _settle_pin(self, pinning: Any, started: Any) -> None:
        from openajar.environments import vmpin

        running = started.get("running") if isinstance(started, dict) else None
        if not self.svc.store.get(pinning.env_id).open:
            return  # closed while its browser started: its pin was dropped, and none is written after it
        if isinstance(running, dict) and running.get("build"):
            self.builds[pinning.env_id] = str(running["build"])
        try:
            vmpin.settle(self.svc, pinning, running, camoufox=self.camoufox.get(pinning.env_id))
        except Exception:
            log.exception("%s: the browser's fingerprint was not settled", pinning.lane_id)


async def _no_proxy(env: Any) -> dict[str, Any]:
    """A service with no browser proxies (a test's fake). An environment that has one never starts without it."""
    if getattr(env, "browser_proxy", None):
        raise EngineError(f"the browser's proxy {env.browser_proxy} cannot be set up here, so the browser did not start")
    return {}


def _vm_port() -> int:
    from openajar.environments.tunnels import VM_BROWSER_PORT

    return VM_BROWSER_PORT


async def _quietly(coro: Any) -> None:
    with contextlib.suppress(Exception):
        await coro


# ---------------------------------------------------------------- in front of the lane manager

class _Front:
    """Everything not routed goes to the object behind (reads and writes alike)."""

    def __init__(self, inner: Any, vm: VmBrowsers):
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "vm", vm)

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "vm"):  # not set yet (copy, pickle): never recurse
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "inner"), name, value)


class RoutedEngine(_Front):
    """The lane manager's engine: a "vm" environment's browser lane launches on its machine."""

    async def launch(self, spec: LaneSpec, *, profile_dir: Path, fingerprint_path: Path, display: str,
                     xauthority: str | None = None, proxy: ProxyConfig | None) -> Any:
        if self.vm.handles(spec.id):
            return await self.vm.launch(spec, profile_dir=profile_dir)
        return await self.inner.launch(spec, profile_dir=profile_dir, fingerprint_path=fingerprint_path,
                                       display=display, xauthority=xauthority, proxy=proxy)


class RoutedDisplays(_Front):
    """The lane manager's displays: a "vm" environment's browser lane gets its machine's screen."""

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        if self.vm.handles(lane_id):
            return await self.vm.acquire(lane_id, size)
        return await self.inner.acquire(lane_id, size=size)

    async def release(self, lane_id: str) -> None:
        if self.vm.get(lane_id) is not None:
            return await self.vm.release(lane_id)
        return await self.inner.release(lane_id)

    def get(self, lane_id: str) -> ViewerInfo | None:
        return self.vm.get(lane_id) or self.inner.get(lane_id)

    def screen(self, lane_id: str) -> ViewerInfo | None:
        """A screen that outlives the lane's browser (an environment's desktop), for the live view of a cold
        lane; None for every lane on the node."""
        return self.vm.screen(lane_id) if self.vm.handles(lane_id) else None

    def healthy(self, lane_id: str) -> bool:
        if self.vm.get(lane_id) is not None:
            return self.vm.healthy(lane_id)
        inner = getattr(self.inner, "healthy", None)
        return inner(lane_id) if callable(inner) else True

    async def read_clipboard(self, lane_id: str, viewer: ViewerInfo) -> str:
        """What the lane copied, for the live view: on the machine for a "vm" lane, else on the node's screen."""
        if self.vm.get(lane_id) is not None:
            return await self.vm.read_clipboard(lane_id)
        from openajar.node import lane_clipboard

        return await lane_clipboard.read_clipboard(viewer.display, viewer.xauthority)

    async def release_all(self) -> None:
        await self.vm.release_all()
        inner = getattr(self.inner, "release_all", None)
        if callable(inner):
            await inner()


def install(lanes: Any, vm: VmBrowsers) -> bool:
    """Put the routers in front of the lane manager's engine and displays, once. False when this lane
    manager has neither (the dev node's simulated lanes): there the browser stays on the node."""
    if lanes is None or getattr(lanes, "engine", None) is None or getattr(lanes, "displays", None) is None:
        return False
    if not isinstance(lanes.engine, RoutedEngine):
        vm.engine = lanes.engine
        lanes.engine = RoutedEngine(lanes.engine, vm)
    if not isinstance(lanes.displays, RoutedDisplays):
        lanes.displays = RoutedDisplays(lanes.displays, vm)
    return True
