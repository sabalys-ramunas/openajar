"""What an environment's browser on its machine (browser = "vm") shows a page that looks, made to read as an
ordinary Firefox: the node's hardening of its own lanes (openajar.node.camoufox.pin.harden, docs/design/
redteam.md "Fixes"), applied where vmpin pins the device. Measured on a Hetzner machine with the red-team
bench before it: the quota of a small disk (4 GB), WebGL limits a page can catch lying, the position
handed out with no prompt, and the window claimed at one place on the desktop while it sits at another.

- Every launch (`every_launch`): the browser never gets a position and the geolocation permission is
  "prompt" (permissions.default.geo = 0), what an untouched Firefox shows at any address. A page that asks
  waits, as for a person who has not answered the prompt. Camoufox's way (the exit's position in the
  config) answers at once and says "granted", which a site never visited cannot be; the position with the
  permission at "prompt" still answers at once (Camoufox #769), so the position goes too. On the desktop the
  window is claimed where the desktop puts it, centred (vmbrowser_ctl places it so), whatever the pin says:
  where a window sits is not part of a device, and a pin of an older launch claimed another place.
- The first launch (`first_draw`), so the pin keeps it: the quota pref (pages read 10 GiB, as about 92% of
  Firefox users do) and, for a Linux identity, a GPU from pin.SOFTWARE_HOST_GPUS, whose limits are what the
  machine's software renderer enforces (the machine has no GPU), with the canvas uncovered: Camoufox would
  cover it with new random data on every read once a GPU is named, and the device would not be one. The core count is the machine's own, set
  by the control script (vmbrowser_ctl SERVE), which knows it.
- A pin made before this (`upgrade`, once): only the quota pref is added. Its GPU stays: the renderer is
  what a site knows the device by, and a new one is a new device. The meta records what was kept.
"""

from __future__ import annotations

import random
from typing import Any

from openajar.node.camoufox.pin import (
    GEO_PREF,
    GEOLOCATION_PREFIX,
    HARDENING,
    QUOTA_10_GIB_KB,
    QUOTA_PREF,
    SOFTWARE_HOST_GPUS,
    hardened,
)

KEPT_ON_UPGRADE = ["webgl"]  # what an older pin keeps as it was (see above)


def every_launch(options: dict[str, Any]) -> None:
    """No position for the browser, the permission at "prompt", and on the desktop the window claimed where
    it is."""
    config = {k: v for k, v in (options.get("config") or {}).items() if not k.startswith(GEOLOCATION_PREFIX)}
    screen, window = list(options.get("screen") or []), list(options.get("window") or [])
    if len(screen) == 2 and len(window) == 2 and screen != window:
        config["window.screenX"] = max(0, (screen[0] - window[0]) // 2)
        config["window.screenY"] = max(0, (screen[1] - window[1]) // 2)
    options["config"] = config
    options.setdefault("firefox_user_prefs", {})[GEO_PREF] = 0


def first_draw(options: dict[str, Any]) -> None:
    """A first launch's options, so the device it draws (and vmpin pins) is hardened."""
    options.setdefault("firefox_user_prefs", {})[QUOTA_PREF] = QUOTA_10_GIB_KB
    if list(options.get("os") or []) == ["linux"] and not options.get("webgl_config"):
        options["webgl_config"] = list(random.choice(SOFTWARE_HOST_GPUS))
        # a named GPU makes Camoufox cover canvas readback with random data (a new picture on every read, so
        # not one device); uncovered, the canvas is what the machine draws, as it was before a GPU was named
        options.setdefault("canvas_noise", False)


def upgrade(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The meta of a pin made before the hardening, with what it can take without becoming another device;
    None when it has it already."""
    if hardened(meta):
        return None
    prefs = dict(meta.get("prefs") if isinstance(meta.get("prefs"), dict) else {})
    prefs[QUOTA_PREF] = QUOTA_10_GIB_KB
    return {**meta, "prefs": prefs, "hardening": HARDENING, "hardening_kept": list(KEPT_ON_UPGRADE)}
