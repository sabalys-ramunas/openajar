"""One device per environment for its browser on the machine (browser = "vm"): the pinned fingerprint.

The browser on the machine is a fresh Camoufox each time its lane opens (a browser server serves no
persistent profile), and Camoufox draws a new identity on every launch it is not given one for. A site
would then see the same cookies and the same exit IP come from another computer each time the browser
restarts or the environment resumes. So the environment keeps one, as a server node's lanes do
(openajar.node.camoufox.engine, pin.py), with the work split between the node and the machine because a
node may have no Camoufox at all (openajar local):

- First launch: the machine draws the identity as before. Once its browser runs, the control script
  reads the config the browser process was started with back from its environment (CAMOU_CONFIG_<n>,
  as engine.read_browser_config reads a browser on the node) and the identity prefs Camoufox worked out
  (CAMOU_PREFS_<n>), and the node pins them (`settle`).
- The pin lives on the node, never on the machine: `<home>/environments/<id>/fingerprint.json` (0600),
  in pin.py's format (a flat Camoufox config with openajar's record under PIN_META). A machine can be
  replaced, and the agent is root on it.
- Every later launch (`prepare`): the browser gets exactly the pinned config. The control script still
  lets Camoufox resolve the launch (its prefs and launcher keys follow the installed build), then puts
  the pinned config and prefs in place of what it drew, keeping only the per-launch keys from the draw.
- A save carries the pin (PAYLOAD, sealed with the rest of its payload) and a resume puts it back under
  the new environment's id, so the environment resumed in another chat, on another machine, is the same
  device. A new environment gets no pin, so it draws a device of its own.

What a pin never holds, so it follows each launch instead (`per_launch`): pin.LAUNCHER_KEYS (how a launch
behaves), and where the browser is: the timezone, the position and the WebRTC address come from the
owner's proxy's exit on every launch (openajar.environments.browserproxy), and the geolocation
permission (pin.GEO_PREF) goes with them; so do the launch's languages (the locale:* keys, Accept-Language
and the browser's own language, intl.locale.requested), which follow the exit's country with a proxy, and
the speech voices Camoufox draws for them. The control script takes each of these from the launch's own
resolve, never from the pin. The screen and window keys follow the launch only when the
launch asks for another screen or window than the pin was made with (full screen or a desktop window:
vmbrowser.desktop_window); otherwise they are the pinned ones. Nothing of the owner's proxy can be in a
pin: its location keys are left out, the readback has no proxy option (the proxy is a forwarder on the
machine's loopback), and only the identity prefs of pin.PIN_PREFS are read back.

A pin is what the first launch showed, as it showed it: unlike a node lane's first launch, nothing is
completed or hardened afterwards (config.complete_config, pin.harden), because that would change a device
a site has already seen. A key the pin lacks is not drawn again on a later launch: the pinned config goes
in whole, so the browser shows its own value for it, as on the first launch.

Version change (as engine.bump_firefox): when the machine's Camoufox build is a newer Firefox than the
pin's user agent says, the pin's Firefox version moves to it, as a real Firefox updates itself, and the
device stays (node.fingerprint.bump changes only the user agent's version tokens; the old pin is kept
next to it as fingerprint.json.firefox-<old>.bak, and the move is recorded in firefox_updates). The same,
an older or an unknown version leaves the pin as it is, and so does a new Camoufox package with the same
Firefox (the pin records the package it was made with).

The machine's browser stack itself: `stale` compares the versions browser.sh recorded in its ready
marker with browser-requirements.txt, so an old image's browser is installed again at bring-up.
"""

from __future__ import annotations

import json
import logging
import re
from importlib import resources
from pathlib import Path
from typing import Any

from openajar.core import clock
from openajar.core.errors import EngineError
from openajar.node.camoufox.config import canvas_covered, pinned_prefs
from openajar.node.camoufox.pin import (
    CANVAS_PREF,
    CANVAS_TARGETS,
    GEO_KEYS,
    GEO_PREF,
    GEOLOCATION_PREFIX,
    HARDENING,
    LAUNCHER_KEYS,
    PIN_META,
    PIN_PREFS,
    PIN_VERSION,
    WEBRTC_KEYS,
    Pin,
    backup_pin,
    load_pin,
    os_of,
    split_pin,
    write_private_json,
)

log = logging.getLogger("openajar.environments")

PIN_FILE = "fingerprint.json"
PAYLOAD = "fingerprint.json"  # in a save's payload, sealed like every payload file
ENV_DIR = "environments"
# The prefs the machine reads back from the browser: the identity prefs a pin keeps, and the canvas decision.
REPORT_PREFS = (*PIN_PREFS, CANVAS_PREF)
# The window's and the screen's size and place: they follow a launch that asks for another screen or window.
GEOMETRY_PREFIXES = ("screen.",)
GEOMETRY_KEYS = frozenset({"window.outerWidth", "window.outerHeight", "window.innerWidth", "window.innerHeight",
                           "window.screenX", "window.screenY"})
# What the launch's locale decides (browserproxy.exit_options, else the lane's locale): the languages a page sees
# and the voices drawn for them.
LOCALE_PREFIXES = ("locale:", "voices")
LOCALE_KEYS = frozenset({"headers.Accept-Language"})
LAUNCH_PREFS = (GEO_PREF, "intl.locale.requested")  # follow the launch's position and locale
MAX_PIN_BYTES = 4 << 20


def pin_path(home: Path | str, env_id: str) -> Path:
    return Path(home) / ENV_DIR / env_id / PIN_FILE


def location_key(key: str) -> bool:
    """Where the browser is: the exit's timezone, position and WebRTC address."""
    return key in GEO_KEYS or key in WEBRTC_KEYS or key.startswith(GEOLOCATION_PREFIX)


def per_launch(key: str) -> bool:
    return key in LAUNCHER_KEYS or location_key(key) or key in LOCALE_KEYS or key.startswith(LOCALE_PREFIXES)


def geometry_key(key: str) -> bool:
    return key.startswith(GEOMETRY_PREFIXES) or key in GEOMETRY_KEYS


def _home(svc: Any) -> Path | None:
    home = getattr(svc, "home", None)
    return Path(home) if home else None


def load(svc: Any, env_id: str, lane_id: str) -> Pin | None:
    """The environment's pin, or None before its first launch (or on a service with no home). A pin that
    does not read raises EngineError, as on the node: deleting it pins a new device."""
    home = _home(svc)
    path = pin_path(home, env_id) if home is not None else None
    if path is None or not path.exists():
        return None
    return load_pin(path, lane_id)


# ---------------------------------------------------------------- a launch


class Prepared:
    """What `prepare` decided for one launch: the pin it uses (None: the first launch draws one), what
    the control script gets, and the screen and window the launch asked for."""

    def __init__(self, env_id: str, lane_id: str, pin: Pin | None, request: dict[str, Any],
                 screen: list[int], window: list[int]):
        self.env_id, self.lane_id, self.pin = env_id, lane_id, pin
        self.request, self.screen, self.window = request, screen, window


def prepare(svc: Any, env: Any, lane_id: str, options: dict[str, Any], *, build: str | None) -> Prepared:
    """Make `options` (vmbrowser.browser_options with the launch's network prefs and the proxy's location
    config) launch the environment's pinned device, and say what the control script needs for it. The
    proxy's location keys in options["config"] win over the pin (it never holds any). `build`: the
    Camoufox build on the machine as far as the node knows it, for the version move."""
    from openajar.environments import vmharden

    screen, window = list(options.get("screen") or []), list(options.get("window") or [])
    request: dict[str, Any] = {"report_prefs": list(REPORT_PREFS)}
    vmharden.every_launch(options)
    pin = load(svc, env.id, lane_id)
    if pin is None:
        vmharden.first_draw(options)
        return Prepared(env.id, lane_id, None, request, screen, window)
    moved = bump(svc, env.id, lane_id, pin, build)
    if moved is not None:
        pin = moved
    pin = harden_once(svc, env.id, lane_id, pin)
    resized = pin.meta.get("screen") != screen or pin.meta.get("window") != window
    config = {k: v for k, v in pin.config.items() if not per_launch(k) and not (resized and geometry_key(k))}
    config.update(options.get("config") or {})  # the exit's location, this launch's
    prefs = held_prefs(pin.meta)
    options["config"] = config
    options["os"] = [os_of(pin.config)]
    if isinstance(pin.meta.get("canvas_noise"), bool):
        options["canvas_noise"] = pin.meta["canvas_noise"]
    options.setdefault("firefox_user_prefs", {}).update(prefs)
    keep = sorted(LAUNCHER_KEYS | LOCALE_KEYS | {*GEO_KEYS, *WEBRTC_KEYS} | (GEOMETRY_KEYS if resized else frozenset()))
    prefixes = [GEOLOCATION_PREFIX, *LOCALE_PREFIXES, *(GEOMETRY_PREFIXES if resized else ())]
    request["pin"] = {"config": config, "keep": keep, "keep_prefixes": prefixes, "prefs": prefs}
    if resized:
        log.info("%s: the browser opens at %s with a %s window; the pinned device keeps everything but its "
                 "screen and window, which follow this launch", lane_id, "x".join(map(str, screen)),
                 "x".join(map(str, window)))
    return Prepared(env.id, lane_id, pin, request, screen, window)


def harden_once(svc: Any, env_id: str, lane_id: str, pin: Pin) -> Pin:
    """A pin made before the hardening (vmharden.upgrade) takes what it can without becoming another device,
    once: written, the old one kept beside it. On a write that fails, this launch still gets it."""
    from openajar.environments import vmharden

    meta = vmharden.upgrade(pin.meta) if pin.version == PIN_VERSION else None
    if meta is None:
        return pin
    hardened = Pin(pin.config, meta)
    home = _home(svc)
    if home is not None:
        path = pin_path(home, env_id)
        try:
            backup_pin(path, pin.version, label="unhardened")
            write_private_json(path, hardened.to_json())
            log.info("%s: the pinned device takes the hardening it can keep its identity with (the quota); its "
                     "GPU stays", lane_id)
        except OSError as e:
            log.warning("%s: could not write the hardened pin (%s); this launch has it anyway", lane_id, e)
    return hardened


def held_prefs(meta: dict[str, Any]) -> dict[str, Any]:
    """The pinned identity prefs and the pinned canvas decision, as every launch sets them outright."""
    prefs = pinned_prefs(meta.get("prefs") if isinstance(meta.get("prefs"), dict) else {})
    for key in LAUNCH_PREFS:
        prefs.pop(key, None)
    canvas = meta.get("canvas_noise")
    if isinstance(canvas, bool):
        prefs[CANVAS_PREF] = ",".join(CANVAS_TARGETS) if canvas else ""
    return prefs


def settle(svc: Any, plan: Prepared, running: Any, *, camoufox: str | None = None) -> None:
    """After the browser started, with what the machine read back from it (`running`: config, prefs and
    build; None when it found no browser process). A first launch pins it; a later one is checked
    against the pin, and a newer Firefox build moves the pin's version for the next launch. Never stops
    the launch: a browser that could not be read is pinned at its next launch."""
    home = _home(svc)
    if home is None:
        return
    running = running if isinstance(running, dict) else {}
    config = running.get("config") if isinstance(running.get("config"), dict) else None
    if config is None:
        log.warning("%s: the machine could not read its browser's config back (%s); %s", plan.lane_id,
                    running.get("error") or "no browser process found",
                    "the device is pinned at the next launch" if plan.pin is None else "the pin stays as it is")
        return
    if plan.pin is None:
        _pin_first(home, plan, config, running, camoufox)
        return
    want = plan.request["pin"]["config"]
    differs = sorted(k for k in want if not per_launch(k) and config.get(k) != json.loads(json.dumps(want[k])))
    if differs:
        log.warning("%s: the browser on the machine runs with a config other than the pinned one (%s)",
                    plan.lane_id, ", ".join(differs[:10]))
    else:
        log.info("%s: launched with the pinned fingerprint", plan.lane_id)
    bump(svc, plan.env_id, plan.lane_id, plan.pin, running.get("build"))


def _pin_first(home: Path, plan: Prepared, config: dict[str, Any], running: dict[str, Any],
               camoufox: str | None) -> None:
    identity = {k: v for k, v in config.items() if not per_launch(k)}
    if not identity:
        log.warning("%s: the browser's config read back empty; the device is pinned at the next launch",
                    plan.lane_id)
        return
    prefs = running.get("prefs") if isinstance(running.get("prefs"), dict) else {}
    kept = {k: v for k, v in pinned_prefs(prefs).items() if k not in LAUNCH_PREFS}
    meta = {"version": PIN_VERSION, "pinned_at": clock.iso(z=True, timespec="seconds"), "camoufox": camoufox,
            "browser": running.get("build"), "prefs": kept, "canvas_noise": canvas_covered(prefs),
            "screen": plan.screen, "window": plan.window, "where": "machine", "hardening": HARDENING}
    path = pin_path(home, plan.env_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private_json(path, {**identity, PIN_META: meta}, exclusive=True)
    except FileExistsError:
        log.info("%s: a pin was written while this first launch started; that pin is the device from the next "
                 "launch on", plan.lane_id)
        return
    except OSError as e:
        log.warning("%s: could not keep the browser's fingerprint (%s); it is pinned at the next launch",
                    plan.lane_id, e)
        return
    log.info("%s: fingerprint pinned on first launch (%d keys, %s, Firefox build %s)", plan.lane_id, len(identity),
             os_of(identity), running.get("build") or "unknown")


def bump(svc: Any, env_id: str, lane_id: str, pin: Pin, build: Any) -> Pin | None:
    """The pin with its Firefox version moved to the machine's build when that is newer (written, the old
    one kept beside it), else None: engine.CamoufoxEngine.bump_firefox's rule for a node lane."""
    from openajar.node.fingerprint import browser_major, pinned_major
    from openajar.node.fingerprint import bump as bump_config

    home = _home(svc)
    installed, pinned = browser_major(str(build) if build else None), pinned_major(pin.config)
    if home is None or installed is None or pinned is None or installed <= pinned or pin.version != PIN_VERSION:
        return None
    config, changed = bump_config(pin.config, installed)
    if not changed:
        return None
    update = {"from": pinned, "to": installed, "browser": str(build), "keys": changed,
              "at": clock.iso(z=True, timespec="seconds")}
    earlier = pin.meta.get("firefox_updates") if isinstance(pin.meta.get("firefox_updates"), list) else []
    moved = Pin(config, {**pin.meta, "firefox_updates": [*earlier, update]})
    path = pin_path(home, env_id)
    try:
        backup = backup_pin(path, pin.version, label=f"firefox-{pinned}")
        write_private_json(path, moved.to_json())
    except OSError as e:
        log.warning("%s: could not move the pin to Firefox %d (%s); it stays as it was", lane_id, installed, e)
        return None
    log.info("%s: the machine's Camoufox is now Firefox %d; the pin's Firefox %d moves with it (%s), as a real "
             "Firefox updates itself; the device stays. The old pin is kept at %s", lane_id, installed, pinned,
             ", ".join(changed), backup)
    return moved


# ---------------------------------------------------------------- saves


def for_save(svc: Any, env: Any) -> bytes | None:
    """The environment's pin as it is stored, for a save's payload; None when it has none."""
    home = _home(svc)
    if home is None:
        return None
    path = pin_path(home, env.id)
    try:
        raw = path.read_bytes()
        split_pin(json.loads(raw), env.browser_lane)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EngineError) as e:
        log.warning("environment %s: its pinned fingerprint does not read (%s), so the save holds none", env.id, e)
        return None
    return raw


def write_payload(folder: Path, raw: bytes | None) -> bool:
    """fingerprint.json in a save's temp folder (0600, sealed with the payload next)."""
    if not raw:
        return False
    write_private_json(Path(folder) / PAYLOAD, json.loads(raw))
    return True


def restore(svc: Any, env: Any, path: Path | None) -> bool:
    """A resume: the save's pin becomes the new environment's, so its browser is the same device. A pin
    there already (the bring-up ran twice) is kept as it is."""
    home = _home(svc)
    if home is None or path is None:
        return False
    try:
        raw = Path(path).read_bytes()
        if len(raw) > MAX_PIN_BYTES:
            raise ValueError("too large")
        data = json.loads(raw)
        split_pin(data, env.browser_lane)
    except (OSError, ValueError, EngineError) as e:
        log.warning("environment %s: the save's pinned fingerprint does not read (%s); its browser draws a new "
                    "device", env.id, e)
        return False
    dest = pin_path(home, env.id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private_json(dest, data, exclusive=True)
    except FileExistsError:
        return True
    except OSError as e:
        log.warning("environment %s: could not put the save's fingerprint back (%s)", env.id, e)
        return False
    return True


def drop(svc: Any, env_id: str) -> None:
    """A closed environment: the node's copy goes (a save holds its own)."""
    home = _home(svc)
    if home is None:
        return
    folder = pin_path(home, env_id).parent
    for path in (folder.glob(f"{PIN_FILE}*") if folder.is_dir() else ()):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- the machine's browser stack

_MARKER_RE = re.compile(r"\b(camoufox|playwright) ([0-9][0-9A-Za-z.+-]*)")
_ACTIVE_RE = re.compile(r'"active_version"\s*:\s*"([^"]+)"')


def wanted_versions() -> dict[str, str]:
    """The camoufox and playwright versions browser-requirements.txt pins."""
    text = resources.files("openajar.environments").joinpath("browser-requirements.txt").read_text()
    return dict(re.findall(r"^(camoufox|playwright)==(\S+)", text, re.MULTILINE))


def stale(check_output: str) -> dict[str, tuple[str, str]]:
    """What the ready marker names at another version than browser-requirements.txt: {name: (on the
    machine, wanted)}. Empty when they agree, or when the marker names no versions (it cannot tell)."""
    found = dict(_MARKER_RE.findall(check_output or ""))
    return {name: (found[name], want) for name, want in wanted_versions().items()
            if name in found and found[name] != want}


def active_build(check_output: str) -> str | None:
    """The Camoufox build the machine launches, from camoufox's config.json ("158.0.1-beta.41-60e25f6e")."""
    found = _ACTIVE_RE.search(check_output or "")
    return found.group(1).rstrip("/").rsplit("/", 1)[-1] if found else None


def marker_camoufox(check_output: str) -> str | None:
    return dict(_MARKER_RE.findall(check_output or "")).get("camoufox")
