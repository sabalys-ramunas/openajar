"""A lane's browser identity is the same on every launch, as a page reads it.

Run on the node:  OPENAJAR_TEST_SLOT=31 scripts/node-test.sh fpin tests/node/test_fingerprint_pin_node.py -q
Throwaway lanes only, under an OPENAJAR_HOME in .node-home/ of this checkout, on the slot's first
display; one browser at a time. A small server on the slot's last viewer port serves the fixture
site (tests/fixtures/site/fingerprint.html, whose own script dumps navigator, screen, window,
WebGL, fonts, canvas, audio, voices, timezone, languages and the request headers) and echoes
request headers at /headers. Only these lanes may reach it.

Set OPENAJAR_TEST_EVIDENCE to a directory to keep every dump as JSON there.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from openajar.core.config import NodeConfig
from openajar.core.models import LaneSpec
from openajar.node.camoufox.engine import (
    AVAIL_TOP,
    LAUNCHER_KEYS,
    PIN_META,
    PIN_VERSION,
    CamoufoxEngine,
    browser_pids_under,
    browser_view,
    complete_config,
    os_of,
    read_browser_config,
    write_private_json,
)
from openajar.node.display import DisplayManagerImpl, kill_processes

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

ROOT = Path(os.environ.get("OPENAJAR_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
DISPLAY_BASE = int(os.environ.get("OPENAJAR_TEST_DISPLAY_BASE", "310"))
VNC_BASE = int(os.environ.get("OPENAJAR_TEST_VNC_BASE", "5310"))
VIEWER_BASE = int(os.environ.get("OPENAJAR_TEST_VIEWER_BASE", "6310"))
SITE_PORT = VIEWER_BASE + 9
SITE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "site"
EVIDENCE = os.environ.get("OPENAJAR_TEST_EVIDENCE")
LAUNCHES = 3

# The keys Camoufox drew afresh on a pinned relaunch before the full pin.
ONCE_RANDOM = ("screen.availLeft", "screen.availTop", "window.screenX", "navigator.maxTouchPoints")


class _Site(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/headers":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()
                               if k.lower() not in ("host", "connection")}, sort_keys=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), _Site)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{SITE_PORT}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def rig():
    home = ROOT / f"fpin-{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True)
    config = NodeConfig(home=home, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=9, self_check=False)
    displays = DisplayManagerImpl(config)
    await displays.sweep()
    engine = CamoufoxEngine()
    engine.allow_private_network = True  # only so these lanes can reach the fixture server
    yield config, displays, engine
    await displays.release_all()
    leftover = browser_pids_under(home)
    await kill_processes(leftover)
    shutil.rmtree(home, ignore_errors=True)
    assert not leftover, f"browsers left running after the test: {leftover}"


async def launch_and_dump(config: NodeConfig, displays: DisplayManagerImpl, engine: CamoufoxEngine,
                          spec: LaneSpec, url: str) -> tuple[dict, dict, list]:
    """One launch of the lane: the fingerprint dump its page made, the config the browser process
    carries, and what the launch did to the pin."""
    fp = config.fingerprint_path(spec.id)
    viewer = await displays.acquire(spec.id, size=engine.display_size(fp))
    session = await engine.launch(spec, profile_dir=config.profile_dir(spec.id), fingerprint_path=fp,
                                  display=viewer.display, xauthority=viewer.xauthority, proxy=None)
    try:
        running = read_browser_config(session.pid)
        page = await session.page()
        await page.goto(url + "/fingerprint.html")
        # 60 s: the probe page's slowest bounds add up to about 30 s (mediaDevices alone may take 20 s on a
        # runner with no sound server, src/openajar/stealth/stealth_probe.html)
        await page.wait_for_selector("#dump[data-state=done]", state="attached", timeout=60_000)
        dump = json.loads(await page.text_content("#dump"))
        return dump, running, list(session.pin_notes)
    finally:
        await session.close()
        await displays.release(spec.id)


def keep(name: str, data: Any) -> None:
    if EVIDENCE:
        path = Path(EVIDENCE)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps(data, indent=1, sort_keys=True))


def comparable(dump: dict, canvas_noise: bool) -> dict:
    """The dump without what changes by design. Firefox's own baseline fingerprinting protection
    (EfficientCanvasRandomization, on by default) gives 2D canvas readback a new noise key every
    session, the same within one, as stock Firefox does for every user; the text metrics and the
    WebGL readback stay. With Camoufox's canvas cover on, every canvas and WebGL readback is fresh
    random data (as with resistFingerprinting). assert_same_device checks each within a page."""
    out = json.loads(json.dumps(dump))
    out["canvas"].pop("digest")
    out["canvas"].pop("sameWithinPage")
    if canvas_noise:
        for kind in ("webgl", "webgl2"):
            if isinstance(out.get(kind), dict):
                out[kind].pop("readback")
    return out


def differences(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(a.keys() | b.keys()) for d in differences(a.get(k), b.get(k), f"{path}.{k}")]
    return [] if a == b else [f"{path}: {json.dumps(a)[:80]} != {json.dumps(b)[:80]}"]


def assert_same_device(dumps: list[dict], canvas_noise: bool) -> None:
    first = comparable(dumps[0], canvas_noise)
    for n, dump in enumerate(dumps[1:], start=2):
        diff = differences(first, comparable(dump, canvas_noise))
        assert diff == [], f"launch {n} differs from launch 1:\n" + "\n".join(diff)
    for dump in dumps:
        for section, value in dump.items():
            # On a CI runner with no sound server, enumerateDevices can outwait even the probe's 20 s bound. The
            # same timeout on every launch is still one device (the comparison above saw no difference), and the
            # section reads the host, not the pin, so only that timeout is let through, and only on CI.
            if section == "mediaDevices" and value == "error: timeout" and os.environ.get("CI"):
                continue
            assert not (isinstance(value, str) and value.startswith("error")), f"{section}: {value}"
        assert dump["canvas"]["sameWithinPage"] is (not canvas_noise)


@pytest.mark.parametrize("target_os", ["windows", "macos", "linux"])
async def test_three_launches_show_one_device(rig, site, target_os):
    config, displays, engine = rig
    engine.extra_options = {"os": target_os}  # the first launch's draw; relaunches take the pin's os
    spec = LaneSpec(id=f"test/fp-{target_os}")
    fp = config.fingerprint_path(spec.id)
    dumps, notes = [], []
    for n in range(1, LAUNCHES + 1):
        dump, running, launch_notes = await launch_and_dump(config, displays, engine, spec, site)
        stored = json.loads(fp.read_text())
        pinned = {k: v for k, v in stored.items() if k != PIN_META}
        # what the browser was handed: a hardened lane's position stays in the pin (browser_view)
        handed = browser_view(pinned, stored[PIN_META])
        assert {k: v for k, v in running.items() if k not in LAUNCHER_KEYS} == handed, f"launch {n}"
        dumps.append(dump)
        notes.append(launch_notes)
        keep(f"{target_os}-launch{n}", {"dump": dump, "pin_notes": launch_notes})
    meta = stored[PIN_META]
    assert meta["version"] == PIN_VERSION
    assert [kind for kind, _ in notes[0]] == ["fingerprint_pinned"] and notes[1:] == [[], []]
    assert all(k in pinned for k in ONCE_RANDOM)
    keep(f"{target_os}-pin", stored)
    assert_same_device(dumps, meta["canvas_noise"])
    first = dumps[0]
    assert first["navigator"]["userAgent"] == pinned["navigator.userAgent"]
    assert first["navigator"]["maxTouchPoints"] == pinned["navigator.maxTouchPoints"] == 0
    assert first["media"]["anyCoarse"] is False and first["media"]["touch"] is False
    # the browser places the work area itself (a pinned second-monitor availLeft never shows)
    assert (first["screen"]["availLeft"], first["screen"]["availTop"]) == (0, AVAIL_TOP[target_os])
    assert (first["window"]["screenX"], first["window"]["screenY"]) == \
        (pinned["window.screenX"], pinned["window.screenY"])
    assert first["intl"]["timeZone"] == pinned["timezone"]
    assert first["webgl"]["renderer"] == pinned["webGl:renderer"]


async def test_a_version_1_pin_is_completed_and_then_stays_put(rig, site):
    """A pin made the old way (the browser's CAMOU_CONFIG, without the keys a 0 draw left out,
    with the launcher keys of the install that made it) is completed on its next launch, keeps
    every key it had, and every launch after that shows the same device."""
    config, displays, engine = rig
    spec = LaneSpec(id="test/fp-legacy")
    fp = config.fingerprint_path(spec.id)
    await launch_and_dump(config, displays, engine, spec, site)  # makes a full pin to start from
    full = json.loads(fp.read_text())
    full.pop(PIN_META)
    legacy = {k: v for k, v in full.items() if k not in ONCE_RANDOM}  # what an old pin lacked
    legacy.update(addons=["/root/agentos/.venv/lib/python3.12/site-packages/camoufox/addons/UBO"], humanize=True)
    write_private_json(fp, legacy)
    original = fp.read_bytes()

    dumps, notes = [], []
    for n in range(1, LAUNCHES + 1):
        dump, running, launch_notes = await launch_and_dump(config, displays, engine, spec, site)
        dumps.append(dump)
        notes.append(launch_notes)
        keep(f"legacy-launch{n}", {"dump": dump, "pin_notes": launch_notes})
    stored = json.loads(fp.read_text())
    new_meta = stored.pop(PIN_META)
    keep("legacy-pin-before", legacy)
    keep("legacy-pin-after", {**stored, PIN_META: new_meta})
    assert [kind for kind, _ in notes[0]] == ["fingerprint_completed"] and notes[1:] == [[], []]
    assert new_meta["version"] == PIN_VERSION and new_meta["completed_from"] == 1
    assert {k: stored[k] for k in legacy} == legacy  # nothing pinned before was changed
    assert sorted(set(stored) - set(legacy)) == sorted(ONCE_RANDOM)
    rules, _ = complete_config(legacy)  # a single-monitor desktop without a touchscreen
    assert {k: stored[k] for k in ONCE_RANDOM} == {k: rules[k] for k in ONCE_RANDOM}
    assert stored["screen.availLeft"] == stored["navigator.maxTouchPoints"] == 0
    assert stored["screen.availTop"] == AVAIL_TOP[os_of(legacy)]
    backup = fp.with_name("fingerprint.json.pin-v1.bak")
    assert backup.read_bytes() == original
    assert {k: v for k, v in running.items() if k not in LAUNCHER_KEYS} == \
        {k: v for k, v in stored.items() if k not in LAUNCHER_KEYS}
    assert_same_device(dumps, new_meta["canvas_noise"])
