"""An environment's browser on its own machine, for real: the docker provider's container with the
browser stack (deploy/environments, browser.sh), the node's real lane manager with its routing, and
Playwright on this computer driving the machine's Camoufox through the environment's SSH tunnel.

Skipped unless docker answers and the image has the browser (label org.agentos.browser). Build it with
`openajar environments setup --build`, or name another image with OPENAJAR_TEST_ENV_IMAGE. What it proves:
the environment opens with browser = "vm"; a page served on the machine's localhost loads in the
machine's browser and reads back as an outline; a screenshot at a phone width is kept as proof on the
node; the live view's socket speaks VNC; closing the environment stops it all and deletes the container.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import uuid

import pytest
from test_policy_support import make_service

from openajar.core.config import NodeConfig
from openajar.core.models import Effect
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments import vmbrowser
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.vms.manager import VmManager
from openajar.vms.models import VmSettings

IMAGE = os.environ.get("OPENAJAR_TEST_ENV_IMAGE", "agentos-env:latest")


def _ready() -> bool:
    if not shutil.which("docker") or not shutil.which("ssh"):
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    try:
        out = subprocess.run(["docker", "image", "inspect", "-f", '{{index .Config.Labels "org.agentos.browser"}}',
                              IMAGE], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "1"


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs docker with the {IMAGE} image built with its browser")


def containers(node_name: str) -> list[str]:
    return subprocess.run(["docker", "ps", "-aq", "--filter", f"label=openajar-node={node_name}"],
                          capture_output=True, text=True, check=False).stdout.split()


@contextlib.asynccontextmanager
async def docker_node(tmp_path, settings: EnvSettings | None = None, home_setup=None):
    """A node with the docker provider, the real lane manager and the environments service (`settings`,
    browser = "vm" by default); `home_setup(home)` writes into its home first. Closes and removes all."""
    from openajar.node.lanes import build_lane_manager

    home = tmp_path / "h"  # short: the tunnel's sockets live in a temp folder, the lanes' under home
    home.mkdir()
    if home_setup is not None:
        home_setup(home)
    policy, rt = make_service(home)
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0, max_timeout_s=600))
    rt.register("envs", envs)
    node_name = f"t-{uuid.uuid4().hex[:8]}"  # this test's containers only
    vms = VmManager(home, runtime=rt, settings=VmSettings(provider="docker", location="local", docker_image=IMAGE,
                                                          docker_memory_gb=4),
                    node_name=node_name)
    rt.register("vms", vms)
    lanes = build_lane_manager(NodeConfig(home=home, self_check=False), node_ip_probe=None)
    rt.register("lanes", lanes)
    await lanes.start()
    mgr = EnvironmentManager(home, runtime=rt, settings=settings or EnvSettings(browser="vm"))
    rt.register("environments", mgr)
    grant, _ = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                            specials={"vm": {}, "exec": {}})
    try:
        yield {"mgr": mgr, "envs": envs, "lanes": lanes, "grant": grant, "node_name": node_name, "home": home}
    finally:
        for env in mgr.store.open():
            await mgr.close(env.id, discard=True, reason="test end")
        await lanes.shutdown()
        await mgr.shutdown()
        await vms.shutdown()
        await envs.shutdown()
        policy.close()
        left = await asyncio.to_thread(containers, node_name)
        if left:
            await asyncio.to_thread(subprocess.run, ["docker", "rm", "-f", *left], capture_output=True, check=False)


@pytest.fixture
async def node(tmp_path):
    async with docker_node(tmp_path) as got:
        yield got


async def _rfb_banner(path: str) -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), 10)
    try:
        return await asyncio.wait_for(reader.readexactly(12), 10)
    finally:
        writer.close()


async def test_the_browser_runs_on_the_machine(node):
    from openajar.node.interaction import create_interaction
    from openajar.node.tools.shots import png_size, shot_at_width

    mgr, envs, lanes, grant = node["mgr"], node["envs"], node["lanes"], node["grant"]
    eid = mgr.planned_id("session:s1", "Shop page")
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding="session:s1", title="Shop page", grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
    env = await mgr.wait_ready(env.id, 600)
    assert env.state == "ready", env.error
    assert env.browser == "vm" and isinstance(lanes.engine, vmbrowser.RoutedEngine)

    marker = f"served on the vm {uuid.uuid4().hex[:6]}"
    await envs.job_start(env.terminal_lane, f"mkdir -p /srv/t && printf '%s' '<title>On the VM</title>"
                                            f"<main><h1>{marker}</h1><a href=\"/next\">Next</a></main>' > "
                                            "/srv/t/index.html && cd /srv/t && exec python3 -m http.server 3000 "
                                            "--bind 127.0.0.1")
    await asyncio.sleep(1.0)

    lane = env.browser_lane
    info = await lanes.open(lane)
    assert info.viewer is not None and info.viewer.vnc_socket == mgr.tunnel_of(env.id).vnc_path
    assert (await _rfb_banner(info.viewer.vnc_socket)).startswith(b"RFB 003.")  # the live view reaches its VNC server

    interaction = create_interaction()
    session = lanes.live_session(lane)
    page = await session.page()
    nav = await interaction.navigate(page, "http://localhost:3000/")
    assert nav.url.startswith("http://localhost:3000"), nav
    snap = await interaction.snapshot(page)
    assert marker in snap.text and "Next" in snap.text  # rendered and read on the machine
    wide = await interaction.screenshot(page)
    on_desktop = mgr._vm_browsers.desktops.get(lane, False)  # an image with desktop.sh: a window on a desktop
    width = png_size(wide)[0]
    assert width == await page.evaluate("innerWidth")  # the picture is the page's own viewport
    if on_desktop:
        # the window's own edge (GTK draws 5 px each side) counts inside the window with some draws of the
        # fingerprint and outside it with others, so the page is the window's width or 10 px less
        assert width in (vmbrowser.DESKTOP_WINDOW[0], vmbrowser.DESKTOP_WINDOW[0] - 10)
    else:
        assert width == 1920
    if on_desktop:  # a command's GUI goes to that screen
        shown = await mgr.envs().exec(env.terminal_lane, 'echo "$DISPLAY"', timeout_s=30)
        assert (shown.stdout or "").strip() == ":99"
    phone = await shot_at_width(interaction, page, 390, full_page=False)
    assert png_size(phone)[0] == 390
    proof = mgr.keep_proof(mgr.get(env.id), phone, title="the page on a phone", page_url=page.url,
                           width=390, height=png_size(phone)[1])
    assert mgr.proof_png(env.id, proof["id"]) == phone

    # the live view's copy button: what the machine's browser copied, read on the machine
    await page.evaluate("() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('h1'));"
                        " const s = getSelection(); s.removeAllRanges(); s.addRange(r); }")
    await page.keyboard.press("Control+c")
    await asyncio.sleep(0.5)
    copied = await lanes.displays.read_clipboard(lane, info.viewer)
    assert copied in (marker, "")  # "" where the driver's key press does not reach the system clipboard
    print(f"copied from the machine's browser: {copied!r}")

    ps = await envs.exec(env.terminal_lane, "ps -eo comm | sort -u", timeout_s=30)
    screen = ("Xvnc",) if "Xvnc" in ps.stdout else ("Xvfb", "x11vnc")  # Xvnc where desktop.sh put it
    assert all(p in ps.stdout for p in screen) and "camoufox-bin" in ps.stdout
    if on_desktop:
        assert "openbox" in ps.stdout and "Xvnc" in ps.stdout
    listening = await envs.exec(env.terminal_lane, "ss -ltnH", timeout_s=30)
    assert not [ln for ln in listening.stdout.splitlines() if ":59" in ln.split()[3]]  # no VNC port on TCP
    browser_port = [ln for ln in listening.stdout.splitlines() if ":47613 " in ln]
    assert browser_port and all("127.0.0.1:47613" in ln for ln in browser_port)  # loopback only
    mem = await envs.exec(env.terminal_lane, "echo $(( $(cat /sys/fs/cgroup/memory.current) / 1048576 )) MB in all; "
                          "ps -eo rss,comm --sort=-rss | head -8", timeout_s=30)
    print(f"the machine with its browser and one page open: {mem.stdout.strip()}")  # docs/ENVIRONMENTS.md

    closed = await mgr.close(env.id, discard=True, reason="test")
    assert closed.state == "closed" and closed.vm_state in ("deleted", "deleting")
    assert lane not in [i.spec.id for i in lanes.list()]
    assert await asyncio.to_thread(containers, node["node_name"]) == []


def test_unix_socket_paths_fit(tmp_path):
    """macOS allows 104 bytes for a socket path: the tunnel's live view socket is in a short temp folder."""
    import tempfile

    folder = tempfile.mkdtemp(prefix="agentos-env-")
    try:
        path = os.path.join(folder, "vnc")
        assert len(os.fsencode(path)) < 104
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


GEOMETRY = """() => new Promise(res => {
  addEventListener('mousemove', e => res({sx: e.screenX - e.clientX, sy: e.screenY - e.clientY,
    ix: mozInnerScreenX, iy: mozInnerScreenY, x: screenX, y: screenY,
    chromeX: (outerWidth - innerWidth) / 2, chromeY: outerHeight - innerHeight}), {once: true});
})"""

# the page keeps what it saw in the DOM: Camoufox runs Playwright's scripts in a world of their own
KEYS_PAGE = ("<title>Keys</title><input id=t autofocus><script>const seen = []; addEventListener('keydown', "
             "e => { seen.push([e.key, e.code, e.isTrusted]); document.body.dataset.seen = JSON.stringify(seen); }, "
             "true)</script>")


async def _open(node, title: str):
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id(f"session:{title}", title)
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding=f"session:{title}", title=title, grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
    env = await mgr.wait_ready(env.id, 600)
    assert env.state == "ready", env.error
    return env


async def test_the_window_is_where_the_page_works_it_out(node):
    """A page reads the content's place on the screen three ways: from where the window says it is and its
    chrome, from mozInnerScreenX/Y, and from a mouse event's screenX/Y less its clientX/Y. On the desktop the
    browser is a window placed so all three agree (vmharden.every_launch, vmbrowser_ctl.browser_place)."""
    from openajar.node.interaction import create_interaction

    lanes = node["lanes"]
    env = await _open(node, "Geometry")
    await node["envs"].job_start(env.terminal_lane, "mkdir -p /srv/g && echo '<title>G</title><p>g</p>' > /srv/g/index.html"
                                 " && cd /srv/g && exec python3 -m http.server 3001 --bind 127.0.0.1")
    await asyncio.sleep(1.0)
    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    await create_interaction().navigate(page, "http://localhost:3001/")
    seen = asyncio.ensure_future(page.evaluate(GEOMETRY))
    await asyncio.sleep(0.3)
    await page.mouse.move(400, 300)
    await page.mouse.move(420, 310)
    got = await asyncio.wait_for(seen, 10)
    print("geometry", got)
    assert (got["sx"], got["sy"]) == (got["ix"], got["iy"])  # a mouse event's screen place is the content's
    assert abs(got["ix"] - got["x"] - got["chromeX"]) <= 2 and abs(got["iy"] - got["y"] - got["chromeY"]) <= 2, got
    if node["mgr"]._vm_browsers.desktops.get(env.browser_lane):
        w, h = vmbrowser.DESKTOP_WINDOW
        assert (got["x"], got["y"]) == ((1920 - w) // 2, (1080 - h) // 2)  # centred, where the desktop put it


async def test_letters_the_us_layout_lacks_are_real_keys_on_the_machine(node, monkeypatch):
    """With a Lithuanian browser the machine's screen gets the lt layout and the keyboard helper presses ą on
    its key (Digit1), Š with Shift, through the X server; an emoji still goes in as text."""
    from openajar.environments import vmkeys
    from openajar.node import human

    monkeypatch.setattr(vmkeys, "layout_for", lambda locale: "lt")  # as with a Lithuanian exit (browser_language)
    lanes = node["lanes"]
    env = await _open(node, "Keys")
    await node["envs"].job_start(env.terminal_lane, f"mkdir -p /srv/k && printf '%s' \"{KEYS_PAGE}\" > /srv/k/index.html"
                                 " && cd /srv/k && exec python3 -m http.server 3002 --bind 127.0.0.1")
    await asyncio.sleep(1.0)
    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    await page.goto("http://localhost:3002/")
    kb = human.x_keyboard(page)
    assert kb is not None and kb.has("ą") and kb.has("Š") and not kb.has("a")
    await page.click("#t")
    await human.type_text(page, "Ąžuolas ėjo š 😀")
    assert await page.input_value("#t") == "Ąžuolas ėjo š 😀"
    seen = json.loads(await page.get_attribute("body", "data-seen"))
    letters = {k: (c, t) for k, c, t in seen if k in "ĄžėŠš"}
    assert letters["Ą"] == ("Digit1", True) and letters["ž"] == ("Equal", True), seen
    assert not kb.broken


async def _link_back(mgr, env) -> None:
    """What the node does after a tunnel dropped: the drop itself starts it again (openajar.environments.relink),
    with no reaper pass."""
    for _ in range(300):
        tunnel = mgr.tunnel_of(env.id)
        if tunnel is not None and tunnel.alive and mgr.link_down(env.id) is None:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"the link did not come back: {mgr.link_down(env.id)}")


async def test_a_tunnel_whose_ssh_was_killed_comes_back_with_its_browser_and_live_view(node):
    """The link to the machine dropped (its ssh killed on this computer). The node starts the tunnel again at once on
    the same ports (the dead one stopped first: before, "port is taken" on every try), and the browser that ran
    starts again with it: the next browser call finds it working, with its live view, the machine's localhost and
    the terminal as before."""
    from openajar.node.interaction import create_interaction

    mgr, envs, lanes = node["mgr"], node["envs"], node["lanes"]
    env = await _open(node, "Link")
    await envs.job_start(env.terminal_lane, "mkdir -p /srv/l && echo '<title>L</title><h1>still here</h1>' > "
                         "/srv/l/index.html && cd /srv/l && exec python3 -m http.server 3003 --bind 127.0.0.1")
    await asyncio.sleep(1.0)
    await lanes.open(env.browser_lane)
    page = await lanes.live_session(env.browser_lane).page()
    await create_interaction().navigate(page, "http://localhost:3003/")
    before = mgr.tunnel_of(env.id)
    ports = (before.socks_port, before.browser_port)
    before.proc.kill()  # the link dropped
    await before.proc.wait()
    assert not before.alive

    await _link_back(mgr, env)
    assert mgr.link_view(mgr.get(env.id))["state"] == "back"
    after = mgr.tunnel_of(env.id)
    assert after.alive and (after.socks_port, after.browser_port) == ports
    info = await lanes.open(env.browser_lane)  # the browser call after the drop: its dead browser starts again
    assert info.viewer.vnc_socket == after.vnc_path
    assert (await _rfb_banner(info.viewer.vnc_socket)).startswith(b"RFB 003.")  # the live view is back
    interaction = create_interaction()
    page = await lanes.live_session(env.browser_lane).page()
    await interaction.navigate(page, "http://localhost:3003/")
    assert "still here" in (await interaction.snapshot(page)).text
    ran = await envs.exec(env.terminal_lane, "echo ok", timeout_s=30)
    assert ran.stdout.strip() == "ok"
