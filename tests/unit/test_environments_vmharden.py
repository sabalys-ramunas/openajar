"""The hardening of an environment's browser on its machine (openajar.environments.vmharden): what every
launch, the first draw and an older pin get, and how vmpin keeps it."""

from __future__ import annotations

import json
import types

from openajar.environments import vmharden, vmpin
from openajar.node.camoufox.pin import (
    GEO_PREF,
    HARDENING,
    PIN_META,
    PIN_VERSION,
    QUOTA_10_GIB_KB,
    QUOTA_PREF,
    SOFTWARE_HOST_GPUS,
    Pin,
    load_pin,
    write_private_json,
)

LANE = "env/e1-browser"
EXIT_CONFIG = {"timezone": "Europe/Vilnius", "geolocation:latitude": 54.69, "geolocation:longitude": 25.28,
               "geolocation:accuracy": 3000, "webrtc:ipv4": "203.0.113.9"}


def test_every_launch_gives_no_position_and_asks_as_an_untouched_firefox():
    options = {"config": dict(EXIT_CONFIG), "firefox_user_prefs": {}, "screen": [1920, 1080], "window": [1920, 1080]}
    vmharden.every_launch(options)
    # the exit's clock and address stay; its position never reaches the browser (with it, Camoufox answers a
    # page at once, "granted" or not: measured), and the permission is "prompt"
    assert options["config"] == {"timezone": "Europe/Vilnius", "webrtc:ipv4": "203.0.113.9"}
    assert options["firefox_user_prefs"][GEO_PREF] == 0


def test_on_the_desktop_the_window_is_claimed_where_the_desktop_puts_it():
    options = {"screen": [1920, 1080], "window": [1776, 890]}
    vmharden.every_launch(options)
    assert (options["config"]["window.screenX"], options["config"]["window.screenY"]) == (72, 95)
    full = {"screen": [1920, 1080], "window": [1920, 1080]}
    vmharden.every_launch(full)
    assert "window.screenX" not in full["config"]  # a browser filling the screen: Camoufox's own 0, 0


def test_the_first_draw_takes_the_quota_and_a_gpu_the_machine_can_honour():
    options = {"os": ["linux"], "firefox_user_prefs": {}}
    vmharden.first_draw(options)
    assert options["firefox_user_prefs"][QUOTA_PREF] == QUOTA_10_GIB_KB
    assert tuple(options["webgl_config"]) in SOFTWARE_HOST_GPUS
    assert options["canvas_noise"] is False  # the canvas the machine draws, the same on every read
    other = {"os": ["windows"], "firefox_user_prefs": {}}
    vmharden.first_draw(other)
    assert "webgl_config" not in other  # the software-host GPUs are Linux ones


def test_an_older_pin_takes_the_quota_once_and_keeps_its_gpu(tmp_path):
    svc = types.SimpleNamespace(home=tmp_path)
    path = vmpin.pin_path(tmp_path, "e1")
    path.parent.mkdir(parents=True)
    config = {"navigator.userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0",
              "webGl:renderer": "Intel(R) HD Graphics, or similar"}
    meta = {"version": PIN_VERSION, "prefs": {QUOTA_PREF: 4_000_000}, "canvas_noise": False}
    write_private_json(path, {**config, PIN_META: meta})
    pin = load_pin(path, LANE)

    hardened = vmpin.harden_once(svc, "e1", LANE, pin)
    assert hardened.meta["hardening"] == HARDENING and hardened.meta["hardening_kept"] == ["webgl"]
    assert hardened.meta["prefs"][QUOTA_PREF] == QUOTA_10_GIB_KB
    assert hardened.config == pin.config  # the same device: its GPU and everything else as a site saw it
    on_disk = load_pin(path, LANE)
    assert on_disk.meta["hardening"] == HARDENING and vmpin.held_prefs(on_disk.meta)[QUOTA_PREF] == QUOTA_10_GIB_KB
    assert json.loads(path.with_name(f"{path.name}.unhardened.bak").read_text())[PIN_META]["prefs"][QUOTA_PREF] \
        == 4_000_000  # the pin as it was, kept beside it

    again = vmpin.harden_once(svc, "e1", LANE, on_disk)
    assert again is on_disk  # once only


def test_a_hardened_pin_is_left_as_it_is():
    pin = Pin({"navigator.userAgent": "x"}, {"version": PIN_VERSION, "hardening": HARDENING, "prefs": {}})
    assert vmharden.upgrade(pin.meta) is None


def test_a_linux_identity_claims_the_whole_screen_as_its_work_area():
    # an untouched Firefox on Linux reports the work area as the whole screen (its fingerprinting protection),
    # also on the machine's desktop with its top bar and dock: a pin that drew 1032 shows what none shows
    options = {"screen": [1920, 1080], "config": {"screen.availHeight": 1032, "timezone": "Europe/Vilnius"}}
    vmharden.work_area(options, "linux")
    assert options["config"] == {"screen.availWidth": 1920, "screen.availHeight": 1080, "screen.availLeft": 0,
                                 "screen.availTop": 0, "timezone": "Europe/Vilnius"}
    windows = {"screen": [1920, 1080], "config": {"screen.availHeight": 1032}}
    vmharden.work_area(windows, "windows")
    assert windows["config"] == {"screen.availHeight": 1032}  # its taskbar is its own


def test_a_pinned_launch_takes_the_work_area_of_its_screen(tmp_path):
    svc = types.SimpleNamespace(home=tmp_path)
    path = vmpin.pin_path(tmp_path, "e1")
    path.parent.mkdir(parents=True)
    config = {"navigator.platform": "Linux x86_64", "screen.width": 1920, "screen.height": 1080,
              "screen.availWidth": 1920, "screen.availHeight": 1050}
    meta = {"version": PIN_VERSION, "hardening": HARDENING, "prefs": {}, "screen": [1920, 1080],
            "window": [1776, 890]}
    write_private_json(path, {**config, PIN_META: meta})
    env = types.SimpleNamespace(id="e1")
    options = {"screen": [1920, 1080], "window": [1776, 890], "config": {}, "firefox_user_prefs": {}}
    prepared = vmpin.prepare(svc, env, LANE, options, build=None)
    sent = prepared.request["pin"]["config"]
    assert (sent["screen.availWidth"], sent["screen.availHeight"], sent["screen.availTop"]) == (1920, 1080, 0)
    assert sent is options["config"]
