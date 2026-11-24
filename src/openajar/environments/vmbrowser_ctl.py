"""Runs on an environment's machine, never on the node: it starts and stops the environment's screen
and browser there (environments.toml browser = "vm"). The node sends this file with each call, over the
SSH connection it holds, as the `script` of a JSON request on stdin (openajar.environments.vmbrowser), so
the machine needs only what browser.sh (and for the desktop, desktop.sh) installed and never runs an older
copy of it.

Actions, each printing one JSON line:

- display {width, height, desktop, server, keep, only_keep}: the screen :99, with a cookie of its own and no TCP listener,
  and its VNC server on the unix socket /run/agentos-browser/vnc.sock (a folder only root can open, the
  socket 0600) with no TCP listener either: TigerVNC's Xvnc where desktop.sh installed it (one process, no
  framebuffer polling), else Xvfb with x11vnc. The node's live view reaches the socket through a forward
  over SSH. With `desktop` (the files of
  openajar/environments/desktop, the wallpaper and the browser's window), a desktop session on that screen:
  openbox and the desk helper (desktop/desk.py: wallpaper, dock, the agent's terminal, focus), each in a
  process group of its own. A desktop that cannot start leaves the bare screen, and says why. With `keep`, a
  screen that already runs at that size (with its desktop, when one is asked for) stays as it is, the
  browser and every window on it too, and the answer says `kept`: the node starts the screen when the
  environment is ready and keeps it until the environment closes, and the browser comes and goes on it.
  What of a kept screen is gone (x11vnc, or the desktop session) starts again on it, and `mended` names it.
  With `only_keep` a screen that is not running at that size is left alone (`untouched`): the node never
  starts a screen afresh under a browser that runs.
- browser {options, port, ws_path, pin, report_prefs, keys}: Camoufox's browser server (camoufox.server.launch_server)
  on that screen, listening on 127.0.0.1:<port> only, at a path the node made at random. With `pin`, the browser
  is the environment's pinned device (SERVE). With `report_prefs`, the answer's `running` is what the browser
  process was started with, read back from its environment: the node pins it on a first launch
  (openajar.environments.vmpin). With `keys` {layout}, the answer's `keys` is the keyboard helper (KEYS).
- clipboard {limit, timeout, nothing}: the text the browser copied, for the live view's "copy" button.
- stop {what: ["browser", "display"]}: ends them (each runs in a process group of its own).
- status: which of them run.

The desk helper's own socket (desk.sock, beside vnc.sock) is how the node shows the agent's commands and
brings a window to the front; this script only starts it.

Standard library only: it runs before and outside the browser's own imports.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time

DIR = "/run/agentos-browser"
DISPLAY_NUM = 99
VNC_SOCK = DIR + "/vnc.sock"
XAUTH = DIR + "/xauth"
VENV_PY = "/opt/agentos-browser/venv/bin/python"
CACHE = "/opt/agentos-browser/cache"
X_SOCKET = f"/tmp/.X11-unix/X{DISPLAY_NUM}"
START_WAIT_S = 20.0
BROWSER_WAIT_S = 90.0
DESK = DIR + "/desktop"            # the session's files, written fresh each time the screen starts
DESK_SOCK = DIR + "/desk.sock"
DESK_READY = DESK + "/desk.ready"
DESK_WAIT_S = 8.0
SERVER_FILE = DIR + "/screen.server"  # which VNC server the screen has: "xvnc" or "x11vnc"
SIZE_FILE = DIR + "/screen.size"      # the size it was started at, "<width>x<height>", for `keep`
WALL_CACHE = "/var/cache/agentos-desktop"
DESKTOP_READY = "/opt/agentos-desktop/ready"
DESKTOP_TOOLS = ("openbox", "lxterminal", "pcmanfm", "rsvg-convert", "/usr/bin/python3")
TITLE_H = 34                       # the frame's title bar (openbox/themerc), above the browser's own window
TOP_BAR_H = 54                     # the desktop's top bar (desktop/deskart.py TOP_H): openbox places windows below it
CSD_EDGE = 5                       # the edge GTK draws round a window that draws its own title bar (no compositor)
GTK_THEME = "Adwaita"              # the desktop's GTK apps, light (desklink.SCREEN_ENV gives the agent's commands the same)
# x11vnc, where the machine has no Xvnc: updates gathered for 5 ms (not its 20 ms default) and polled every 5 ms
# (not 20), and a window moved or resized shows as it is, not as an outline (measured on a desktop scrolling a
# page: the default flags gave about half the frames a second for the same CPU)
X11VNC_SPEED = ("-defer", "5", "-wait", "5", "-nowf")
FAMILY_WILD = 65535  # an Xauthority entry for any host: the machine's name does not matter
# A video proof's recorder (recorder_ctl.py, openajar.environments.recording) records this screen: it
# ends with the screen, so it finishes its file first
RECORDER_PID = "/var/lib/agentos-env/recording/ffmpeg.pid"

# The browser server: the options the node sent, with the screen and window as Camoufox wants them. With the
# environment's pin (openajar.environments.vmpin), Camoufox still resolves the launch, and then the browser gets
# the pinned config and prefs in place of what it drew: only the keys the node names in `keep` (how a launch
# behaves, and the screen and window when this launch asks for others) come from this launch's draw, as the
# node's engine does for a lane (openajar.node.camoufox.config.launch_config). Either way the core count is the
# machine's own (cores_fit, as the node's engine hardens a lane: pin.harden).
SERVE = """
import json, os, sys
import camoufox.server as server
from camoufox.fingerprints import Screen
from camoufox.utils import get_pref_env_vars
path = sys.argv[1]
with open(path) as f:
    o = json.load(f)
os.unlink(path)
pin = o.pop("openajar_pin", None)
w, h = o.pop("screen")
o["screen"] = Screen(min_width=w, max_width=w, min_height=h, max_height=h)
o["window"] = tuple(o["window"])
o["env"] = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
resolve = server.launch_options

def cores_fit(config, first):
    # the machine's own cores: a first draw claims them; a pin claiming more than this machine runs is
    # lowered to them (a timing test sees the difference), one claiming fewer keeps its device
    have = len(os.sched_getaffinity(0)) or 1
    claim = config.get("navigator.hardwareConcurrency")
    if first or not isinstance(claim, int) or claim > have:
        config["navigator.hardwareConcurrency"] = have

def pinned(**kw):
    r = resolve(**kw)
    env = r["env"]
    parts = sorted((int(k.rsplit("_", 1)[1]), v) for k, v in env.items() if k.startswith("CAMOU_CONFIG_"))
    drawn = json.loads("".join(v for _, v in parts) or "{}")
    prefs = dict(r.get("firefox_user_prefs") or {})
    if pin is None:
        config = drawn
    else:
        keep, prefixes = set(pin["keep"]), tuple(pin["keep_prefixes"])
        config = dict(pin["config"])
        config.update({k: v for k, v in drawn.items() if k in keep or (prefixes and k.startswith(prefixes))})
        prefs.update(pin["prefs"])
    cores_fit(config, pin is None)
    env = {k: v for k, v in env.items() if not k.startswith(("CAMOU_CONFIG_", "CAMOU_PREFS_"))}
    blob = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    env.update({"CAMOU_CONFIG_%d" % (i // 32767 + 1): blob[i:i + 32767] for i in range(0, len(blob), 32767)})
    env.update(get_pref_env_vars(prefs))
    r["env"], r["firefox_user_prefs"] = env, prefs
    return r

server.launch_options = pinned
server.launch_server(**o)
"""
# The keyboard helper (openajar.environments.vmkeys): the screen gets the browser's language's layout, and the
# helper presses the keys for the characters it types, through XTest, for the node's relay on a socket of its
# own: one line "<code point> <hold ms>" in, "ok" or "no" out. Only characters past ASCII (the rest stay on
# Playwright). The characters it can type go to `table` once it listens. Started with the browser, stopped with it.
KEYS = r"""
import ctypes, json, os, socket, subprocess, sys, time
layout, sock_path, table_path = sys.argv[1], sys.argv[2], sys.argv[3]
subprocess.run(["setxkbmap", "-layout", layout], check=True, timeout=15)
x11, xtst, xkb = (ctypes.CDLL(n) for n in ("libX11.so.6", "libXtst.so.6", "libxkbcommon.so.0"))
x11.XOpenDisplay.restype, x11.XOpenDisplay.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
x11.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
x11.XGetKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
x11.XDisplayKeycodes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
x11.XKeysymToKeycode.restype, x11.XKeysymToKeycode.argtypes = ctypes.c_ubyte, [ctypes.c_void_p, ctypes.c_ulong]
x11.XFlush.argtypes = x11.XFree.argtypes = [ctypes.c_void_p]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
xkb.xkb_keysym_to_utf32.restype, xkb.xkb_keysym_to_utf32.argtypes = ctypes.c_uint32, [ctypes.c_uint32]
d = x11.XOpenDisplay(None)
if not d:
    sys.exit("cannot open the display")
lo, hi, per = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
x11.XDisplayKeycodes(d, ctypes.byref(lo), ctypes.byref(hi))
n = hi.value - lo.value + 1
syms = x11.XGetKeyboardMapping(d, lo.value, n, ctypes.byref(per))
shift, level3 = x11.XKeysymToKeycode(d, 0xffe1), x11.XKeysymToKeycode(d, 0xfe03)
table = {}
for i in range(n):  # the core map's columns: plain, Shift, (group 2 twice), AltGr, AltGr and Shift
    for col, mods in ((0, ()), (1, (shift,)), (4, (level3,)), (5, (shift, level3))):
        sym = syms[i * per.value + col] if col < per.value else 0
        cp = xkb.xkb_keysym_to_utf32(sym) if sym else 0
        if cp > 127 and cp not in table and all(mods):
            table[cp] = (lo.value + i, mods)
x11.XFree(syms)
try:
    os.unlink(sock_path)
except OSError:
    pass
srv = socket.socket(socket.AF_UNIX)
old = os.umask(0o177)
srv.bind(sock_path)
os.umask(old)
srv.listen(2)
with open(table_path + ".tmp", "w") as f:
    json.dump("".join(chr(c) for c in table), f)
os.replace(table_path + ".tmp", table_path)

def key(code, down):
    xtst.XTestFakeKeyEvent(d, code, 1 if down else 0, 0)
    x11.XFlush(d)

while True:
    conn, _ = srv.accept()
    io = conn.makefile("rwb", buffering=0)
    for line in io:
        try:
            cp, hold = line.split()
            code, mods = table[int(cp)]
            hold = min(max(float(hold), 0.0), 1000.0) / 1000
        except (ValueError, KeyError):
            io.write(b"no\n")
            continue
        for m in mods:
            key(m, True)
        key(code, True)
        time.sleep(hold)
        key(code, False)
        for m in reversed(mods):
            key(m, False)
        x11.XSync(d, 0)
        io.write(b"ok\n")
    conn.close()
"""
KEYS_WAIT_S = 10.0
BROWSER_BIN = "camoufox-bin"
READBACK_WAIT_S = 5.0


def _say(**out):
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _pidfile(name):
    return f"{DIR}/{name}.pid"


def _started_at(pid):
    """When process `pid` started (seconds since the epoch), from /proc; None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])  # field 22, starttime: clock ticks after boot
        with open("/proc/stat") as f:
            btime = next(int(ln.split()[1]) for ln in f if ln.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _running(name):
    """The pid in name.pid while that process runs, and is the one the file was written for: a docker save
    keeps /run (not a tmpfs there), so on a resumed machine the number can belong to another process that
    started after the file was written. That one is never signalled."""
    try:
        path = _pidfile(name)
        with open(path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        started = _started_at(pid)
        if started is not None and started > os.stat(path).st_mtime + 2:
            return None
        return pid
    except (OSError, ValueError):
        return None


def _stop(name):
    pid = _running(name)
    if pid is not None and pid > 1:  # killpg(1) or (0) would signal every process of this user, or our own group
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and _running(name):
                time.sleep(0.1)
            if not _running(name):
                break
    try:
        os.unlink(_pidfile(name))
    except OSError:
        pass


def _spawn(name, argv, env):
    with open(f"{DIR}/{name}.log", "ab") as log:  # the child keeps its own copy of the file
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                start_new_session=True, close_fds=True)
    with open(_pidfile(name), "w") as f:
        f.write(str(proc.pid))
    return proc


def _tail(name, n=1500):
    try:
        with open(f"{DIR}/{name}.log", "rb") as f:
            return f.read()[-n:].decode(errors="replace")
    except OSError:
        return ""


def _wait(proc, name, ready, wait_s):
    deadline = time.monotonic() + wait_s
    while not ready():
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited (code {proc.returncode}): {_tail(name)}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name} did not start within {wait_s:g} s: {_tail(name)}")
        time.sleep(0.1)


def _unix_listening(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(path) == 0
    finally:
        s.close()


def _tcp_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _write_xauthority(path):
    def counted(data):
        return len(data).to_bytes(2, "big") + data

    entry = (FAMILY_WILD.to_bytes(2, "big") + counted(b"") + counted(str(DISPLAY_NUM).encode())
             + counted(b"MIT-MAGIC-COOKIE-1") + counted(secrets.token_bytes(16)))
    try:
        os.unlink(path)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(entry)


def _base_env():
    # GTK_THEME: the agent's commands carry the desktop's (openajar.environments.desklink.SCREEN_ENV) and this
    # script runs as one of them; the browser keeps GTK's default look, which is part of what a page sees
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "GTK_THEME")}
    env.update(DISPLAY=f":{DISPLAY_NUM}", XAUTHORITY=XAUTH, GDK_BACKEND="x11", MOZ_ENABLE_WAYLAND="0",
               XDG_CACHE_HOME=CACHE, HOME=env.get("HOME") or "/root")
    return env


def _stop_recorder():
    try:
        with open(RECORDER_PID) as f:
            pid = int(f.read().strip())
        if pid <= 1:  # never every process of this user (killpg(1)) or our own group (killpg(0))
            return
        os.killpg(pid, signal.SIGINT)
    except (OSError, ValueError):
        return
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                    return  # exited; nobody reaped it yet
        except OSError:
            return
        time.sleep(0.1)


def _stop_desktop():
    for name in ("desk", "openbox"):
        _stop(name)


def browser_place(width, height, window, own_titlebar):
    """Where openbox puts the browser's window (rc.xml), so that its content sits where a page works it out
    from what Camoufox claims: the window centred on the screen (vmharden.every_launch claims it there), the
    content at screenX plus half of outerWidth minus innerWidth, and at screenY plus all of outerHeight minus
    innerHeight (stealth.py's check, as a site's). Openbox counts y from below the top bar (its strut).
    With its own title bar the window's X origin is its GTK edge: x is the claim itself, and y is CSD_EDGE
    lower, as the edge at the bottom counts in outerHeight but sits below the content (measured on the
    machine: placed at the claim less the edge and a pixel, a page saw the content 6 px left and 11 px up).
    With openbox's frame, the frame's title bar is above the window."""
    w, h = window
    x, y = (width - w) // 2, (height - h) // 2
    if own_titlebar:
        return max(0, x), max(0, y + CSD_EDGE - TOP_BAR_H)
    return max(0, x - 1), max(0, y - TITLE_H - 1 - TOP_BAR_H)


def _write_desktop(files, width, height, window, own_titlebar=False):
    """The session's files under DESK: config/ (XDG_CONFIG_HOME of the desktop's apps), data/themes (the
    openbox theme), openbox/rc.xml with the browser's place in it, the dock's icons and desk.py."""
    import shutil

    shutil.rmtree(DESK, ignore_errors=True)
    for rel, body in files.items():
        if rel.startswith("/") or ".." in rel.split("/"):
            raise RuntimeError(f"a desktop file outside its folder: {rel}")
        path = os.path.join(DESK, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if rel == "openbox/rc.xml":
            w, h = window
            # a browser that fills the screen started before the desktop did (it was still installing): maximized
            fills = (w, h) == (width, height)
            bx, by = browser_place(width, height, window, own_titlebar)
            body = (body.replace("AGENTOS_BROWSER_MAXIMIZED", "yes" if fills else "no")
                    .replace("AGENTOS_BROWSER_X", str(bx))
                    .replace("AGENTOS_BROWSER_Y", str(by))
                    .replace("AGENTOS_AGENT_TERM_Y", str(max(0, height // 2 - TOP_BAR_H))))
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    theme = os.path.join(DESK, "data", "themes", "openajar", "openbox-3")
    os.makedirs(theme, exist_ok=True)
    for name in os.listdir(os.path.join(DESK, "openbox")):
        if name == "themerc" or name.endswith(".xbm"):
            shutil.copy(os.path.join(DESK, "openbox", name), theme)


def _wallpaper(wall, width, height):
    """The wallpaper at the screen's size, as DESK/wallpaper.png: an SVG rendered here (kept, by its hash and
    size, so the next start skips it), a PNG or JPEG as it is (the desk helper fits it to the screen)."""
    import base64
    import hashlib
    import shutil

    data = base64.b64decode(wall["data"])
    out = os.path.join(DESK, "wallpaper.png")
    if wall.get("kind") != "svg":
        with open(out, "wb") as f:
            f.write(data)
        return
    os.makedirs(WALL_CACHE, exist_ok=True)
    cached = os.path.join(WALL_CACHE, f"{hashlib.sha256(data).hexdigest()[:20]}-{width}x{height}.png")
    if not os.path.exists(cached):
        src = os.path.join(DESK, "wallpaper.svg")
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height), src, "-o", cached + ".tmp"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=True)
        os.replace(cached + ".tmp", cached)
    shutil.copy(cached, out)


def _desktop_env():
    env = _base_env()
    # GTK_THEME: every GTK app started on the desktop (from the dock, a person's Terminal and what runs in it)
    # is light like the desktop's windows, also with no settings of its own. Never the browser's: it starts from _base_env.
    env.update(XDG_CONFIG_HOME=DESK + "/config", XDG_DATA_HOME=DESK + "/data", AGENTOS_DESK_DIR=DESK,
               AGENTOS_DESK_SOCK=DESK_SOCK, NO_AT_BRIDGE="1", GTK_OVERLAY_SCROLLING="0", GTK_THEME=GTK_THEME)
    env.pop("XDG_CACHE_HOME", None)  # the browser's cache folder is the browser's
    return env


def _desktop(spec, width, height):
    """Start the desktop session on the running screen; raise with the reason when it cannot."""
    import shutil

    missing = [t for t in DESKTOP_TOOLS if not shutil.which(t)]
    if missing or not os.path.exists(DESKTOP_READY):
        raise RuntimeError("the desktop is not installed on this machine (desktop.sh)"
                           + (f": no {', '.join(missing)}" if missing else ""))
    _write_desktop(spec["files"], width, height, spec.get("window") or (width, height), bool(spec.get("own_titlebar")))
    _wallpaper(spec["wallpaper"], width, height)
    with open(os.path.join(DESK, "desk.json"), "w", encoding="utf-8") as f:
        json.dump({"title": str(spec.get("title") or "")[:80]}, f)
    for stale in (DESK_SOCK, DESK_READY):
        try:
            os.unlink(stale)
        except OSError:
            pass
    env = _desktop_env()
    wm = _spawn("openbox", ["openbox", "--config-file", DESK + "/openbox/rc.xml"], env)
    desk = _spawn("desk", ["/usr/bin/python3", "-I", DESK + "/desk.py"], env)
    _wait(desk, "desk", lambda: os.path.exists(DESK_READY), DESK_WAIT_S)
    if wm.poll() is not None:
        raise RuntimeError(f"openbox exited (code {wm.returncode}): {_tail('openbox')}")


def _screen(req, width, height, env):
    """The X screen and its VNC server, both on unix sockets only. Xvnc (TigerVNC: one process that is the X
    server and the VNC server, so nothing polls the framebuffer) when the machine has it and the node did
    not ask for x11vnc; else Xvfb with x11vnc, as before. Returns which."""
    import shutil

    want = req.get("server") or "xvnc"
    if want == "xvnc" and shutil.which("Xvnc"):
        xvnc = _spawn("xvfb", ["Xvnc", f":{DISPLAY_NUM}", "-geometry", f"{width}x{height}", "-depth", "24",
                               "-auth", XAUTH, "-nolisten", "tcp", "-rfbport", "-1", "-rfbunixpath", VNC_SOCK,
                               "-rfbunixmode", "0600", "-SecurityTypes", "None", "-AlwaysShared",
                               "-AcceptSetDesktopSize=0", "-FrameRate", "60", "-CompareFB", "2",
                               "-desktop", "openajar"], env)
        _wait(xvnc, "Xvnc", lambda: os.path.exists(X_SOCKET) and _unix_listening(VNC_SOCK), START_WAIT_S)
        return "xvnc"
    xvfb = _spawn("xvfb", ["Xvfb", f":{DISPLAY_NUM}", "-screen", "0", f"{width}x{height}x24",
                           "-auth", XAUTH, "-nolisten", "tcp"], env)
    _wait(xvfb, "xvfb", lambda: os.path.exists(X_SOCKET), START_WAIT_S)
    _x11vnc(env)
    return "x11vnc"


def _x11vnc(env):
    # The node's own x11vnc flags (openajar.node.display): no TCP listener at all, the socket's folder is the gate.
    try:
        os.unlink(VNC_SOCK)
    except OSError:
        pass
    vnc = _spawn("x11vnc", ["x11vnc", "-display", f":{DISPLAY_NUM}", "-auth", XAUTH, "-rfbport", "0",
                            "-rfbportv6", "-1", "-unixsock", VNC_SOCK, "-noipv6", "-nopw", "-forever", "-shared",
                            "-norc", "-noremote", "-nocmds", *X11VNC_SPEED], env)
    _wait(vnc, "x11vnc", lambda: _unix_listening(VNC_SOCK), START_WAIT_S)


def _screen_runs():
    vnc = _server() == "xvnc" or _running("x11vnc")
    return bool(_running("xvfb") and vnc and _unix_listening(VNC_SOCK))


def _x_runs_at(width, height):
    """Does the screen's X server run, at this size?"""
    if not (_running("xvfb") and os.path.exists(X_SOCKET)):
        return False
    try:
        with open(SIZE_FILE) as f:
            return f.read().strip() == f"{width}x{height}"
    except OSError:
        return False


def _mend(req, width, height):
    """With `keep`: a screen whose X server runs at this size stays, the browser and every window on it too, and
    what of it is gone starts again on it: x11vnc where that is its VNC server, and the desktop session (openbox
    and the helper) when one is asked for. The answer, with `kept`; None when the screen must start afresh."""
    if not req.get("keep") or not _x_runs_at(width, height):
        return None
    out = {"display": f":{DISPLAY_NUM}", "width": width, "height": height, "desktop": False, "server": _server(),
           "kept": True, "mended": []}
    if not _unix_listening(VNC_SOCK):
        if out["server"] != "x11vnc":
            return None  # Xvnc is the screen and its VNC server: one that does not serve starts afresh
        _stop("x11vnc")
        _x11vnc(_base_env())
        out["mended"].append("x11vnc")
    want = req.get("desktop")
    if want and _running("desk") and _running("openbox"):
        out["desktop"] = True
    elif want:
        _stop_desktop()  # what is left of it: an openbox without its helper, or the other way round
        if _desktop_into(out, want, width, height):
            out["mended"].append("desktop")
    return out


def _desktop_into(out, spec, width, height):
    """Start the desktop session on the running screen, and put in `out` whether it runs, how long it took, and
    why not. A desktop that cannot start leaves the bare screen."""
    began = time.monotonic()
    try:
        _desktop(spec, width, height)
        out["desktop"] = True
    except Exception as e:  # noqa: BLE001 - the bare screen still serves; the node logs why
        _stop_desktop()
        out["desktop_error"] = str(e)[-600:]
    out["desktop_s"] = round(time.monotonic() - began, 3)
    return out["desktop"]


def display(req):
    width, height = int(req.get("width") or 1920), int(req.get("height") or 1080)
    kept = _mend(req, width, height)
    if kept is not None:
        _say(ok=True, **kept)
        return
    if req.get("only_keep"):  # the node never starts a screen afresh under a running browser
        _say(ok=True, kept=False, untouched=True, display=f":{DISPLAY_NUM}", width=width, height=height)
        return
    _stop_recorder()  # a new screen: the recording of the old one ends first, with its file finished
    _stop("keys")
    _stop("browser")
    _stop_desktop()
    _stop("x11vnc")
    _stop("xvfb")
    _write_xauthority(XAUTH)
    for stale in (X_SOCKET, f"/tmp/.X{DISPLAY_NUM}-lock", VNC_SOCK, SIZE_FILE):
        try:
            os.unlink(stale)
        except OSError:
            pass
    env = _base_env()
    server = _screen(req, width, height, env)
    with open(SERVER_FILE, "w") as f:
        f.write(server)
    with open(SIZE_FILE, "w") as f:
        f.write(f"{width}x{height}")
    out = {"display": f":{DISPLAY_NUM}", "width": width, "height": height, "desktop": False, "server": server}
    if req.get("desktop"):
        _desktop_into(out, req["desktop"], width, height)
    _say(ok=True, **out)


def browser(req):
    _stop("keys")
    _stop("browser")
    if not _running("xvfb"):
        raise RuntimeError("the screen is not running; start it first")
    port = int(req["port"])
    opts = dict(req["options"], host="127.0.0.1", port=port, ws_path=req["ws_path"])
    if req.get("pin") is not None:
        opts["openajar_pin"] = req["pin"]
    path = f"{DIR}/browser-{secrets.token_hex(6)}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(opts, f)
    env = _base_env()
    if req.get("own_titlebar"):
        # on the desktop: the browser draws its own title bar (its tabs, with its window buttons beside them) and
        # the window manager draws none (openbox has no compositor, so Firefox would otherwise keep its frame)
        env["MOZ_GTK_TITLEBAR_DECORATION"] = "client"
    proc = _spawn("browser", [VENV_PY, "-I", "-c", SERVE, path], env)
    _wait(proc, "browser", lambda: _tcp_listening(port), BROWSER_WAIT_S)
    out = {"ok": True, "port": port}
    if isinstance(req.get("keys"), dict) and req["keys"].get("layout"):
        out["keys"] = _keys(str(req["keys"]["layout"]))
    else:
        _plain_layout()
    if "report_prefs" in req:
        out["running"] = _readback(proc.pid, req.get("report_prefs") or [])
    _say(**out)


def _plain_layout():
    """A launch with no layout of its own (an English browser): the screen's layout goes back to us, so a
    layout an earlier launch set does not stay under take over and the screen tools."""
    try:
        subprocess.run(["setxkbmap", "-layout", "us"], env=_base_env(), timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - no setxkbmap, or no answer: the layout stays, and nothing else depends on it
        return


def _keys(layout):
    """The keyboard helper (KEYS) on the screen, with this layout: {socket, chars}, or {error} (the browser runs
    either way, and the node types those letters as text)."""
    table, sock = DIR + "/keys.json", DIR + "/keys.sock"
    try:
        os.unlink(table)
    except OSError:
        pass
    proc = _spawn("keys", ["/usr/bin/python3", "-I", "-c", KEYS, layout, sock, table], _base_env())
    deadline = time.monotonic() + KEYS_WAIT_S
    while not os.path.exists(table):
        if proc.poll() is not None or time.monotonic() > deadline:
            _stop("keys")
            return {"error": _tail("keys", 300).strip() or "the keyboard helper did not start"}
        time.sleep(0.05)
    with open(table) as f:
        return {"socket": sock, "chars": json.load(f), "layout": layout}


def _children():
    """Each process's children, from /proc."""
    kids = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(name))
    return kids


def _browser_pid(root):
    """The Camoufox process the browser server under `root` started (Playwright starts it in a session of its
    own, so it is found by its parents, not its process group), or None."""
    kids, todo, found = _children(), [root], []
    while todo:
        for pid in kids.get(todo.pop(), []):
            todo.append(pid)
            try:
                if os.path.basename(os.readlink(f"/proc/{pid}/exe")) == BROWSER_BIN:
                    found.append(pid)
            except OSError:
                continue
    return min(found) if found else None


def _chunks(env, prefix):
    parts = sorted((int(k[len(prefix):]), v) for k, v in env.items() if k.startswith(prefix) and k[len(prefix):].isdigit())
    return json.loads("".join(v for _, v in parts)) if parts else None


def _readback(root, want_prefs):
    """What the launched browser runs with, read from its own environment as the node reads a browser of its
    own (openajar.node.camoufox.pin.parse_camou_config): its config (CAMOU_CONFIG_<n>), the prefs the node
    asks about (CAMOU_PREFS_<n>; never the rest, which hold the network's) and its build (the folder the
    binary is in). {"error": ...} when no browser process is found."""
    deadline = time.monotonic() + READBACK_WAIT_S
    pid = _browser_pid(root)
    while pid is None and time.monotonic() < deadline:
        time.sleep(0.1)
        pid = _browser_pid(root)
    if pid is None:
        return {"error": "no browser process under the browser server"}
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        with open(f"/proc/{pid}/environ", "rb") as f:
            pairs = [kv.split("=", 1) for kv in f.read().decode(errors="replace").split("\0") if "=" in kv]
        env = {k: v for k, v in pairs if k.startswith(("CAMOU_CONFIG_", "CAMOU_PREFS_"))}
        config = _chunks(env, "CAMOU_CONFIG_")
        prefs = _chunks(env, "CAMOU_PREFS_") or {}
    except (OSError, ValueError) as e:
        return {"error": f"the browser's environment does not read: {e}"}
    if not isinstance(config, dict):
        return {"error": "the browser process carries no CAMOU_CONFIG"}
    return {"config": config, "prefs": {k: prefs[k] for k in want_prefs if k in prefs},
            "build": os.path.basename(os.path.dirname(exe))}


def clipboard(req):
    """The text on the screen's CLIPBOARD selection, as the node's live view reads a local one
    (openajar.node.lane_clipboard): "" when nothing is copied, `kind` too_large or unreadable."""
    limit = int(req.get("limit") or 1048576)
    nothing = [w.encode() for w in req.get("nothing") or []]
    try:
        out = subprocess.run(["xclip", "-o", "-selection", "clipboard", "-t", "UTF8_STRING"], env=_base_env(),
                             stdin=subprocess.DEVNULL, capture_output=True, check=False,
                             timeout=float(req.get("timeout") or 2.0))
    except FileNotFoundError:
        _say(ok=False, kind="unreadable", error="xclip is not installed on the machine")
        return
    except subprocess.TimeoutExpired:
        _say(ok=False, kind="timeout", error="the browser did not hand over what it copied in time")
        return
    if out.returncode != 0:
        err = out.stderr.strip().lower()
        if not err or any(w in err for w in nothing):
            _say(ok=True, text="")
            return
        _say(ok=False, kind="unreadable", error=out.stderr.decode(errors="replace").strip().splitlines()[0][:160])
        return
    if len(out.stdout) > limit:
        _say(ok=False, kind="too_large", error="what is copied is too large")
        return
    try:
        _say(ok=True, text=out.stdout.decode("utf-8"))
    except UnicodeDecodeError:
        _say(ok=False, kind="unreadable", error="what is copied is not UTF-8 text")


def stop(req):
    _stop_recorder()
    for name in req.get("what") or ["browser", "display"]:
        _stop("keys")  # the keyboard helper goes with the browser, and with the screen it types on
        if name == "display":
            _stop_desktop()
            _stop("x11vnc")
            _stop("xvfb")
        else:
            _stop(name)
    _say(ok=True)


def _server():
    try:
        with open(SERVER_FILE) as f:
            return f.read().strip()
    except OSError:
        return "x11vnc"


def status(_req):
    _say(ok=True, browser=bool(_running("browser")), display=_screen_runs(),
         desktop=bool(_running("desk") and _running("openbox")))


def main(req):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    action = {"display": display, "browser": browser, "clipboard": clipboard, "stop": stop,
              "status": status}.get(req.get("action"))
    if action is None:
        _say(ok=False, error=f"unknown action {req.get('action')!r}")
        return
    try:
        action(req)
    except Exception as e:  # noqa: BLE001 - the node reads the error from the JSON line
        _say(ok=False, error=str(e)[-1500:])


if __name__ == "__openajar_vm__":  # run by openajar.environments.vmbrowser.BOOT on the machine; never on import
    main(REQ)  # noqa: F821 - REQ is the request the node sent
