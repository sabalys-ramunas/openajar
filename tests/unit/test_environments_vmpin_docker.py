"""One device per environment, for real (openajar.environments.vmpin): the docker provider's machine with
its Camoufox, the node's lane manager and Playwright driving the machine's browser through the tunnel.

What a page measures of the device (fingerprint_summary) is the same after the browser lane is closed and
opened again, and differs in a second environment; with the owner's proxy, the pin on the node holds
nothing of it (its address, its credentials, its exit). Skipped as test_environments_vmbrowser_docker is.
"""

from __future__ import annotations

import json

import pytest
from test_environments_browserproxy_docker import EXIT, FAKE_PASS, FAKE_USER, _open_env, proxied  # noqa: F401
from test_environments_vmbrowser_docker import _ready, docker_node

from openajar.environments import vmpin
from openajar.node.camoufox.pin import PIN_META

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker with the environment image built with its browser")

# What a site reads to tell one device from another. Every property is compared; none had to be left out.
SUMMARY = """async (families) => {
  const hash = async (text) => {  // FNV-1a: about:blank is no secure context, so it has no crypto.subtle
    let h = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return `${h.toString(16)}:${text.length}`;
  };
  const c = document.createElement('canvas');
  c.width = 280; c.height = 60;
  const x = c.getContext('2d');
  x.textBaseline = 'top'; x.font = '16px Arial';
  x.fillStyle = '#f60'; x.fillRect(120, 5, 60, 30);
  x.fillStyle = '#069'; x.fillText('openajar device check', 4, 18);
  const m = document.createElement('canvas').getContext('2d');
  const widths = families.map((f) => { m.font = `20px "${f}", monospace`; return m.measureText('mmmmmmlli10OOwW').width; });
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  const audio = new AudioContext();
  const rate = audio.sampleRate;
  audio.close();
  return {
    userAgent: navigator.userAgent, platform: navigator.platform, cores: navigator.hardwareConcurrency,
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth],
    webgl: gl ? [gl.getParameter(dbg ? dbg.UNMASKED_VENDOR_WEBGL : gl.VENDOR),
                 gl.getParameter(dbg ? dbg.UNMASKED_RENDERER_WEBGL : gl.RENDERER)] : null,
    canvas: await hash(c.toDataURL()), fonts: await hash(JSON.stringify(widths)),
    fontsShown: widths.filter((w, i) => w !== widths[families.indexOf('monospace')]).length, audioRate: rate,
    languages: navigator.languages.join(','), voices: speechSynthesis.getVoices().length,
  };
}"""


def families() -> list[str]:
    """Every family a Linux identity may show (Camoufox's fonts.json), measured one by one: an identity's own
    font list decides which of them a page can find."""
    try:
        from importlib import resources

        listed = json.loads(resources.files("camoufox").joinpath("fonts.json").read_text())["lin"]
    except (ImportError, OSError, KeyError, ValueError):
        listed = ["Arial", "Courier New", "Times New Roman", "DejaVu Sans", "Liberation Sans", "Noto Sans"]
    return ["monospace", *sorted(set(listed) - {"monospace"})]


async def fingerprint_summary(lanes, lane: str) -> dict:
    page = await lanes.live_session(lane).page()
    await page.goto("about:blank")
    return await page.evaluate(SUMMARY, families())


async def _new_env(node, title: str):
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id(f"session:{title}", title)
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding=f"session:{title}", title=title, grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
    env = await mgr.wait_ready(env.id, 600)
    assert env.state == "ready", env.error
    return env


async def test_the_machines_browser_is_the_same_device_each_time_it_opens(tmp_path):
    async with docker_node(tmp_path) as node:
        mgr, lanes = node["mgr"], node["lanes"]
        env = await _new_env(node, "Same device")
        lane = env.browser_lane
        await lanes.open(lane)
        first = await fingerprint_summary(lanes, lane)
        pin = json.loads(vmpin.pin_path(mgr.home, env.id).read_text())
        assert pin["navigator.userAgent"] == first["userAgent"] and pin[PIN_META]["browser"]
        await lanes.close(lane)
        await lanes.open(lane)
        again = await fingerprint_summary(lanes, lane)
        print(f"the device, twice: {json.dumps(first)}")
        assert again == first

        other = await _new_env(node, "Another device")
        await lanes.open(other.browser_lane)
        theirs = await fingerprint_summary(lanes, other.browser_lane)
        print(f"another environment's device: {json.dumps(theirs)}")
        assert theirs != first
        differs = sorted(k for k in first if first[k] != theirs[k])
        print(f"the two environments differ in {', '.join(differs)}")


async def test_a_proxied_browsers_pin_holds_nothing_of_the_proxy(proxied):  # noqa: F811
    node, fake = proxied, proxied["fake"]
    mgr, lanes = node["mgr"], node["lanes"]
    env = await _open_env(node)
    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    assert await page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone") == EXIT["timezone"]
    text = vmpin.pin_path(mgr.home, env.id).read_text()
    for leak in (FAKE_USER, FAKE_PASS, f":{fake.port}", f'"{fake.port}"', "127.0.0.1", "network.proxy", EXIT["ip"],
                 EXIT["timezone"], "geolocation:", "webrtc:"):
        assert leak not in text, leak
    first = await fingerprint_summary(lanes, env.browser_lane)
    await lanes.close(env.browser_lane)
    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    assert await page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone") == EXIT["timezone"]
    assert await fingerprint_summary(lanes, env.browser_lane) == first
