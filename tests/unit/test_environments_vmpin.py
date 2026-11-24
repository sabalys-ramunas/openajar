"""One device per environment for its browser on the machine (openajar.environments.vmpin): the first
launch's draw is read back and pinned on the node, every later launch is that device, the proxy's exit
says where it is, a save carries it to the resumed environment, a newer Firefox moves only its version,
and a machine whose browser stack is at other versions gets it installed again.

The machine is a fake that answers the control script as the real one does: it draws a new device for
a launch without a pin, and with one it does what SERVE does (the pinned config, with only the per-launch
keys from its draw). test_environments_vmpin_docker.py does it with a real Camoufox."""

from __future__ import annotations

import json
import random
import sys
import types
from pathlib import Path

import pytest
from test_environments_vmbrowser import FakeMachine, _create, _env, routed  # noqa: F401 - the fixture

from openajar.env.models import ExecResult
from openajar.environments import vmbrowser, vmpin
from openajar.node.camoufox.pin import CANVAS_PREF, GEO_PREF, PIN_META

LANE = "env/e1234567890-browser"
EXIT = {"timezone": "Europe/Vilnius", "geolocation:latitude": 54.69, "geolocation:longitude": 25.28,
        "webrtc:ipv4": "203.0.113.9"}
BUILD = "158.0.1-beta.41-60e25f6e"


def _ua(major: int) -> str:
    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{major}.0) Gecko/20100101 Firefox/{major}.0"


class DrawingMachine(FakeMachine):
    """Answers `browser` with what the launched browser would run with (`running`), as the control script's
    readback does, and the ready check with a marker and a build."""

    def __init__(self, *, marker: str = "camoufox 0.5.9 playwright 1.62.0", build: str = BUILD):
        super().__init__()
        self.marker, self.build = marker, build
        self.answers: list[dict] = []

    def draw(self, options: dict) -> tuple[dict, dict]:
        rnd = random.Random()
        w, h = options["screen"]
        ww, wh = options["window"]
        config = {"navigator.userAgent": _ua(158), "navigator.platform": "Linux x86_64",
                  "navigator.hardwareConcurrency": rnd.choice([4, 8, 12, 16]),
                  "webGl:renderer": f"GPU {rnd.randrange(10**6)}", "fonts": [f"Font{rnd.randrange(10**6)}"],
                  "screen.width": w, "screen.height": h, "window.outerWidth": ww, "window.outerHeight": wh,
                  "humanize": True, "addons": [f"/opt/addon-{rnd.randrange(10**6)}"],
                  "locale:language": str(options.get("locale") or "en-US").split("-")[0],
                  "voices": [f"Voice {rnd.randrange(10**6)}"], "voices:blockIfNotDefined": True,
                  **(options.get("config") or {})}
        prefs = {"webgl.enable-webgl2": rnd.choice([True, False]), "dom.quotaManager.temporaryStorage.fixedLimit": 4096,
                 CANVAS_PREF: "", **dict(options.get("firefox_user_prefs") or {})}
        if "geolocation:latitude" in config:
            prefs.setdefault(GEO_PREF, 1)
        return config, prefs

    def serve(self, req: dict) -> dict:
        """vmbrowser_ctl.SERVE: Camoufox resolves the launch, then a pin replaces what it drew."""
        config, prefs = self.draw(req["options"])
        pin = req.get("pin")
        if pin is not None:
            keep, prefixes = set(pin["keep"]), tuple(pin["keep_prefixes"])
            final = dict(pin["config"])
            final.update({k: v for k, v in config.items() if k in keep or (prefixes and k.startswith(prefixes))})
            config = final
            prefs.update(pin["prefs"])
        return {"config": json.loads(json.dumps(config)),
                "prefs": {k: prefs[k] for k in req.get("report_prefs") or [] if k in prefs}, "build": self.build}

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None, oom_check=False):
        if cmd == vmbrowser.READY_CHECK:
            self.ran.append(cmd)
            if not self.installed:
                return ExecResult(exit_code=1, stdout="", stderr="", duration_s=0.01)
            out = f'{self.marker}\n2026-11-16T13:58:20Z\n\n{{"active_version": "browsers/official/{self.build}"}}'
            return ExecResult(exit_code=0, stdout=out, stderr="", duration_s=0.01)
        if cmd == vmbrowser.ctl_command():
            req = json.loads(stdin)
            if req["action"] == "browser":
                req.pop("script")
                self.asked.append(req)
                answer = {"ok": True, "port": req["port"], "running": self.serve(req)}
                self.answers.append(answer)
                return ExecResult(exit_code=0, stdout=json.dumps(answer) + "\n", stderr="", duration_s=0.01)
        return await super().exec(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, grant_id=grant_id,
                                  max_bytes=max_bytes, oom_check=oom_check)


@pytest.fixture
def pinned(routed, tmp_path):  # noqa: F811
    machine = DrawingMachine()
    routed.svc.machine = machine
    routed.machine = machine
    routed.svc.home = tmp_path / "node-home"
    return routed


def _running(r) -> list[dict]:
    return [a["running"] for a in r.machine.answers]


def _identity(config: dict) -> dict:
    return {k: v for k, v in config.items() if not vmpin.per_launch(k)}


async def _open_close(r, lane: str = LANE) -> None:
    await r.mgr.open(lane)
    await r.mgr.close(lane)


def _pin(r, env_id: str = "e1234567890") -> dict:
    return json.loads(vmpin.pin_path(r.svc.home, env_id).read_text())


async def test_the_first_launch_pins_what_the_browser_ran_with(pinned):
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _open_close(r)
        [req] = [a for a in r.machine.asked if a["action"] == "browser"]
        assert "pin" not in req and req["report_prefs"] == list(vmpin.REPORT_PREFS)  # the machine draws
        path = vmpin.pin_path(r.svc.home, "e1234567890")
        assert (path.stat().st_mode & 0o777) == 0o600
        data = _pin(r)
        meta = data.pop(PIN_META)
        [ran] = _running(r)
        assert data == _identity(ran["config"]) and data["navigator.userAgent"] == _ua(158)
        assert meta["screen"] == [1920, 1080] and meta["window"] == [1920, 1080] and meta["browser"] == BUILD
        assert meta["prefs"]["webgl.enable-webgl2"] == ran["prefs"]["webgl.enable-webgl2"]
        assert meta["canvas_noise"] is False and meta["version"] == 2
    finally:
        await r.mgr.shutdown()


async def test_every_later_launch_is_the_same_device(pinned):
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        for _ in range(3):
            await _open_close(r)
        first, *later = _running(r)
        assert all(_identity(run["config"]) == _identity(first["config"]) for run in later)
        assert all(run["prefs"]["webgl.enable-webgl2"] == first["prefs"]["webgl.enable-webgl2"] for run in later)
        reqs = [a for a in r.machine.asked if a["action"] == "browser"]
        assert all(req["pin"]["config"] == _identity(first["config"]) for req in reqs[1:])
        assert reqs[1]["options"]["os"] == ["linux"] and reqs[1]["options"]["canvas_noise"] is False
        assert "screen." not in reqs[1]["pin"]["keep_prefixes"]  # the same screen and window: the pinned ones
    finally:
        await r.mgr.shutdown()


async def test_another_window_keeps_the_device_and_takes_the_launchs_geometry(pinned):
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _open_close(r)
        original = r.vm._launch

        async def on_desktop(spec, env, tunnel, steps, *, profile_dir):
            r.vm.desktops[spec.id] = True  # the desktop session came up: the browser is a window on it
            return await original(spec, env, tunnel, steps, profile_dir=profile_dir)

        r.vm._launch = on_desktop
        await _open_close(r)
        first, second = _running(r)
        assert second["config"]["window.outerWidth"] == vmbrowser.DESKTOP_WINDOW[0]
        assert {k: v for k, v in _identity(second["config"]).items() if not vmpin.geometry_key(k)} == \
            {k: v for k, v in _identity(first["config"]).items() if not vmpin.geometry_key(k)}
        assert _pin(r)[PIN_META]["window"] == [1920, 1080]  # the pin stays as it was made
    finally:
        await r.mgr.shutdown()


async def test_the_proxys_location_wins_and_is_never_pinned(pinned, monkeypatch):
    r = pinned
    secret_port = 31337

    async def proxy_options(env):
        return {"config": dict(EXIT)}

    r.svc.browser_proxy_options = proxy_options
    r.svc.by_id["e1234567890"].browser_proxy = "lt1"
    from openajar.environments import network

    real = network.vm_browser_prefs
    monkeypatch.setattr(network, "vm_browser_prefs", lambda level, proxied=False: {
        **real(level, proxied=proxied), "network.proxy.http_port": secret_port})
    try:
        await r.mgr.start()
        await _create(r.mgr, LANE)
        await _open_close(r)
        assert next(a for a in r.machine.asked if a["action"] == "browser")["options"]["firefox_user_prefs"][
            "network.proxy.http_port"] == secret_port  # the launch had the proxy's prefs
        text = vmpin.pin_path(r.svc.home, "e1234567890").read_text()
        for leak in ("network.proxy", str(secret_port), "127.0.0.1", EXIT["webrtc:ipv4"],
                     EXIT["timezone"], "geolocation:", "webrtc:", GEO_PREF):
            assert leak not in text, leak
        # a pin from elsewhere that holds a location: the exit's wins on the launch
        data = _pin(r)
        data.update({"timezone": "Asia/Tokyo", "webrtc:ipv4": "198.51.100.1"})
        vmpin.write_private_json(vmpin.pin_path(r.svc.home, "e1234567890"), data)
        await _open_close(r)
        last = [a for a in r.machine.asked if a["action"] == "browser"][-1]
        # the exit's place wins, but never its position: the browser has none and asks (vmharden.every_launch)
        place = {k: v for k, v in EXIT.items() if not k.startswith("geolocation:")}
        assert {k: last["options"]["config"][k] for k in place} == place
        assert {k: _running(r)[-1]["config"][k] for k in place} == place
        assert not [k for k in last["options"]["config"] if k.startswith("geolocation:")]
        assert last["options"]["firefox_user_prefs"][GEO_PREF] == 0
        assert GEO_PREF not in last["pin"]["prefs"]
    finally:
        await r.mgr.shutdown()


async def test_per_launch_keys_are_not_pinned(pinned):
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _open_close(r)
        await _open_close(r)
        data = _pin(r)
        assert "humanize" not in data and "addons" not in data  # pin.LAUNCHER_KEYS
        first, second = _running(r)
        assert second["config"]["addons"] != first["config"]["addons"]  # each launch's own install paths
        assert "addons" in [a for a in r.machine.asked if a["action"] == "browser"][1]["pin"]["keep"]
    finally:
        await r.mgr.shutdown()


async def test_the_launchs_languages_and_voices_follow_the_launch(pinned):
    """The locale (the exit country's, with a proxy) is the launch's: its keys and the voices drawn for it
    come from the machine's resolve of this launch, never from the pin, and are never left out."""
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _open_close(r)
        assert not [k for k in _pin(r) if k.startswith(("locale:", "voices"))]
        original = r.vm._launch

        async def in_lithuanian(spec, env, tunnel, steps, *, profile_dir):
            return await original(spec.model_copy(update={"locale": "lt-LT"}), env, tunnel, steps,
                                  profile_dir=profile_dir)

        r.vm._launch = in_lithuanian
        await _open_close(r)
        first, second = _running(r)
        assert second["config"]["locale:language"] == "lt" and second["config"]["voices:blockIfNotDefined"] is True
        assert second["config"]["voices"] and _identity(second["config"]) == _identity(first["config"])
    finally:
        await r.mgr.shutdown()


async def test_a_new_environment_draws_its_own_device(pinned):
    r = pinned
    other = _env("e5555555555")
    r.svc.by_id[other.id] = other
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _create(r.mgr, "env/e5555555555-browser")
        await _open_close(r)
        await _open_close(r, "env/e5555555555-browser")
        reqs = [a for a in r.machine.asked if a["action"] == "browser"]
        assert "pin" not in reqs[1]  # its first launch draws, and never gets the other's pin
        assert _pin(r)["fonts"] != _pin(r, "e5555555555")["fonts"]
    finally:
        await r.mgr.shutdown()


async def test_a_save_carries_the_device_and_a_resume_uses_it(pinned, tmp_path):
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _open_close(r)
        env = r.svc.by_id["e1234567890"]
        folder = tmp_path / "save"
        folder.mkdir()
        assert vmpin.write_payload(folder, vmpin.for_save(r.svc, env))
        resumed = _env("e7777777777")  # a resume is a new environment
        r.svc.by_id[resumed.id] = resumed
        assert vmpin.restore(r.svc, resumed, folder / vmpin.PAYLOAD)
        vmpin.drop(r.svc, env.id)  # the first one closed: the node's copy goes
        assert not vmpin.pin_path(r.svc.home, env.id).exists()
        await _create(r.mgr, "env/e7777777777-browser")
        await _open_close(r, "env/e7777777777-browser")
        first, again = _running(r)
        assert _identity(again["config"]) == _identity(first["config"])
    finally:
        await r.mgr.shutdown()


async def test_a_newer_firefox_moves_only_the_pins_version(pinned):
    r = pinned
    await r.mgr.start()
    try:
        await _create(r.mgr, LANE)
        await _open_close(r)
        path = vmpin.pin_path(r.svc.home, "e1234567890")
        data = _pin(r)
        data["navigator.userAgent"] = _ua(150)
        vmpin.write_private_json(path, data)
        r.machine.build = "160.0.1-beta.3-0abc1234"
        assert await r.vm.installed(r.svc.by_id["e1234567890"])  # the bring-up's check learns the build
        await _open_close(r)
        req = [a for a in r.machine.asked if a["action"] == "browser"][-1]
        assert req["pin"]["config"]["navigator.userAgent"] == _ua(160)
        moved = _pin(r)
        assert moved["navigator.userAgent"] == _ua(160) and moved["fonts"] == data["fonts"]
        assert moved[PIN_META]["firefox_updates"][0]["from"] == 150
        assert json.loads(path.with_name("fingerprint.json.firefox-150.bak").read_text())["navigator.userAgent"] == \
            _ua(150)
        r.machine.build = "155.0-beta.1-0abc1234"  # never moved back
        r.vm.builds["e1234567890"] = r.machine.build
        await _open_close(r)
        assert _pin(r)["navigator.userAgent"] == _ua(160)
    finally:
        await r.mgr.shutdown()


async def test_a_stale_browser_marker_installs_the_browser_again(pinned):
    r = pinned
    machine = r.machine
    machine.marker = "camoufox 0.5.8 playwright 1.61.0"
    env = r.svc.by_id["e1234567890"]
    installs: list[str] = []
    plain = machine.exec

    async def exec_(lane_id, cmd, *, stdin=None, **kw):
        if cmd == "sh -s":
            installs.append(stdin)
            machine.marker = "camoufox 0.5.9 playwright 1.62.0"
            return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
        return await plain(lane_id, cmd, stdin=stdin, **kw)

    machine.exec = exec_
    await r.vm.ensure_installed(env)
    assert installs == [vmbrowser.install_script(again=True)]
    assert f"rm -rf {vmbrowser.BROWSER_DIR}/venv {vmbrowser.READY}" in installs[0]
    await r.vm.ensure_installed(env)  # now current: one check, no install
    assert len(installs) == 1 and machine.ran.count(vmbrowser.READY_CHECK) == 3
    machine.marker = "a marker of an older browser.sh"  # names no versions: it cannot tell, so it stays
    assert await r.vm.installed(env)


def test_the_marker_is_compared_with_the_requirements():
    want = vmpin.wanted_versions()
    assert set(want) == {"camoufox", "playwright"}
    same = f"camoufox {want['camoufox']} playwright {want['playwright']}\n2026-11-16T13:58:20Z\n"
    assert vmpin.stale(same) == {}
    assert vmpin.stale(same.replace(want["playwright"], "0.1")) == {"playwright": ("0.1", want["playwright"])}
    assert vmpin.active_build('{"active_version": "browsers/official/158.0.1-beta.41-60e25f6e"}') == \
        "158.0.1-beta.41-60e25f6e"


def _serve(tmp_path, monkeypatch, drawn: dict, pin: dict | None, cores: int = 2) -> tuple[dict, dict, Path]:
    """The control script's SERVE with a stand-in for Camoufox, on a machine with `cores` cores: the config the
    browser's environment carries, what the server was given, and the options file."""
    import os

    ns: dict = {"__name__": "not-the-machine"}
    exec(compile(vmbrowser.ctl_script(), "vmbrowser_ctl", "exec"), ns)  # noqa: S102 - our own file
    seen: dict = {}
    blob = json.dumps(drawn)

    def launch_options(**kw):
        return {"env": {"CAMOU_CONFIG_1": blob[:10], "CAMOU_CONFIG_2": blob[10:], "CAMOU_PREFS_1": "{}", "PATH": "/b"},
                "firefox_user_prefs": {"a": 1, "webgl.enable-webgl2": False}}

    def launch_server(**kw):
        seen.update(server.launch_options(**kw))

    server = types.SimpleNamespace(launch_options=launch_options, launch_server=launch_server)
    camoufox = types.ModuleType("camoufox")
    modules = {"camoufox": camoufox, "camoufox.server": server,
               "camoufox.fingerprints": types.SimpleNamespace(Screen=lambda **kw: kw),
               "camoufox.utils": types.SimpleNamespace(get_pref_env_vars=lambda p: {"CAMOU_PREFS_1": json.dumps(p)})}
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(cores)), raising=False)
    camoufox.server = server
    opts = tmp_path / "opts.json"
    opts.write_text(json.dumps({"screen": [1920, 1080], "window": [1920, 1080],
                                **({"openajar_pin": pin} if pin is not None else {})}))
    monkeypatch.setattr(sys, "argv", ["serve", str(opts)])
    exec(compile(ns["SERVE"], "serve", "exec"), {"__name__": "__main__"})  # noqa: S102 - our own script
    config = json.loads("".join(v for k, v in sorted(seen["env"].items()) if k.startswith("CAMOU_CONFIG_")))
    return config, seen, opts


def test_serve_puts_the_pin_in_place_of_what_camoufox_drew(tmp_path, monkeypatch):
    """The browser's environment carries the pinned config and prefs, with only the kept keys from the draw."""
    drawn = {"navigator.userAgent": "drawn", "fonts": ["Drawn"], "addons": ["/new/addon"], "screen.width": 1, "x": 1}
    pin = {"config": {"navigator.userAgent": "pinned", "fonts": ["Pinned"], "timezone": "Europe/Vilnius",
                      "navigator.hardwareConcurrency": 2},
           "keep": ["addons"], "keep_prefixes": ["screen."], "prefs": {"webgl.enable-webgl2": True}}
    config, seen, opts = _serve(tmp_path, monkeypatch, drawn, pin)
    assert config == {"navigator.userAgent": "pinned", "fonts": ["Pinned"], "timezone": "Europe/Vilnius",
                      "navigator.hardwareConcurrency": 2, "addons": ["/new/addon"], "screen.width": 1}
    assert json.loads(seen["env"]["CAMOU_PREFS_1"]) == {"a": 1, "webgl.enable-webgl2": True}
    assert seen["firefox_user_prefs"]["webgl.enable-webgl2"] is True and seen["env"]["PATH"] == "/b"
    assert not opts.exists()  # the options file is gone once read


@pytest.mark.parametrize(("pinned_cores", "machine", "shown"), [
    (None, 2, 2),   # a first draw claims the machine's own cores (Camoufox drew 16)
    (8, 2, 2),      # a pin claiming more than this machine runs is lowered to it: a timing test sees the difference
    (2, 8, 2),      # a pin claiming fewer keeps its device on a bigger machine
])
def test_serve_claims_no_more_cores_than_the_machine_runs(tmp_path, monkeypatch, pinned_cores, machine, shown):
    drawn = {"navigator.userAgent": "drawn", "navigator.hardwareConcurrency": 16}
    pin = None if pinned_cores is None else {
        "config": {"navigator.userAgent": "pinned", "navigator.hardwareConcurrency": pinned_cores},
        "keep": [], "keep_prefixes": [], "prefs": {}}
    config, _seen, _opts = _serve(tmp_path, monkeypatch, drawn, pin, cores=machine)
    assert config["navigator.hardwareConcurrency"] == shown
    assert config["navigator.userAgent"] == ("drawn" if pin is None else "pinned")


def test_the_readback_reads_the_browsers_own_environment(tmp_path, monkeypatch):
    ns: dict = {"__name__": "not-the-machine"}
    exec(compile(vmbrowser.ctl_script(), "vmbrowser_ctl", "exec"), ns)  # noqa: S102 - our own file
    config = {"navigator.userAgent": "x" * 40000}
    blob = json.dumps(config)
    environ = "\0".join([f"CAMOU_CONFIG_2={blob[32767:]}", f"CAMOU_CONFIG_1={blob[:32767]}",
                         'CAMOU_PREFS_1={"webgl.enable-webgl2":true,"network.proxy.http_port":3132}',
                         "PATH=/bin"]).encode()
    ns["_browser_pid"] = lambda root: 4242
    real_open = open

    def fake_open(path, *a, **kw):
        if path == "/proc/4242/environ":
            import io

            return io.BytesIO(environ)
        return real_open(path, *a, **kw)

    ns["open"] = fake_open
    monkeypatch.setattr(ns["os"], "readlink", lambda p: f"/opt/cache/camoufox/browsers/official/{BUILD}/camoufox-bin")
    got = ns["_readback"](1, ["webgl.enable-webgl2", "dom.webgpu.enabled"])
    assert got == {"config": config, "prefs": {"webgl.enable-webgl2": True}, "build": BUILD}  # no proxy pref
    ns["_browser_pid"] = lambda root: None
    ns["READBACK_WAIT_S"] = 0.0
    assert "error" in ns["_readback"](1, [])


def test_a_pin_that_does_not_read_stops_the_launch_with_what_to_do(tmp_path):
    from openajar.core.errors import EngineError

    svc = types.SimpleNamespace(home=tmp_path)
    path = vmpin.pin_path(tmp_path, "e1234567890")
    path.parent.mkdir(parents=True)
    path.write_text("{")
    with pytest.raises(EngineError, match="Restore it from a backup"):
        vmpin.load(svc, "e1234567890", LANE)
    assert vmpin.load(types.SimpleNamespace(home=None), "e1234567890", LANE) is None
    assert not Path(tmp_path / "x").exists()
