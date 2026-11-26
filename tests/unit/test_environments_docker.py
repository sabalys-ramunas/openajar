"""An environment on a real machine: the docker provider's container with sshd (deploy/environments).

Skipped unless docker answers and the image is built (`openajar environments setup --build`). What it
proves, for real: the machine comes up over SSH with the node's pinned keys; a declared repository
is cloned and a branch pushed through the node's git gateway over the reverse tunnel, with the
credential never on the machine; a test page served on the machine's localhost is read through
the SOCKS tunnel (and by studio's review browser when Chromium is installed); close deletes the
container."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from test_policy_support import make_service
from test_vault_support import make_vault, private_home

from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.dev.lanes import FakeLaneManager
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.git import keys
from openajar.git.service import GitService
from openajar.vms.manager import VmManager
from openajar.vms.models import VmSettings

IMAGE = "agentos-env:latest"
HOST, REPO = "example.test", "me/shop"


def _docker_ready() -> bool:
    if not shutil.which("docker") or not shutil.which("git") or not shutil.which("ssh"):
        return False
    try:
        out = subprocess.run(["docker", "image", "ls", "-q", IMAGE], capture_output=True, text=True, timeout=20,
                             check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and bool(out.stdout.strip())


def containers(node_name: str) -> list[str]:
    """The containers of this test's node (its own label), running or not."""
    return subprocess.run(["docker", "ps", "-aq", "--filter", f"label=openajar-node={node_name}"],
                          capture_output=True, text=True, check=False).stdout.split()


pytestmark = pytest.mark.skipif(not _docker_ready(), reason=f"needs docker with the {IMAGE} image")


def git(*args: str, cwd: Path | None = None) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


@pytest.fixture
async def node(tmp_path):
    home = private_home(tmp_path)
    policy, rt = make_service(home)
    vault = make_vault(home)
    await vault.start(control=False)
    rt.register("vault", vault)
    gitsvc = GitService(rt, home, allow_file=True, host_port=0)
    rt.register("git", gitsvc)
    upstream = tmp_path / "up.git"
    seed = tmp_path / "seed"
    git("init", "-q", "--bare", "-b", "main", str(upstream))
    git("init", "-q", "-b", "main", str(seed))
    (seed / "index.html").write_text("<title>Shop</title><h1>Twenty percent off</h1>\n")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "first", cwd=seed)
    git("push", "-q", str(upstream), "main", cwd=seed)
    await vault.put_secret("git:shop", "github_pat_" + "Q" * 40, kind=keys.TOKEN_KIND, label="t",
                           scope=keys.token_scope(HOST, [REPO], None))
    gitsvc.remotes.allow("env", f"{HOST}/{REPO}", url=str(upstream))
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0, max_timeout_s=120))
    rt.register("envs", envs)
    node_name = f"t-{uuid.uuid4().hex[:8]}"  # this test's containers only
    vms = VmManager(home, runtime=rt, settings=VmSettings(provider="docker", location="local"), node_name=node_name)
    rt.register("vms", vms)
    lanes = FakeLaneManager()
    rt.register("lanes", lanes)
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings())
    rt.register("environments", mgr)
    grant, _ = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                            specials={"vm": {}, "exec": {}})
    yield {"rt": rt, "mgr": mgr, "vms": vms, "envs": envs, "grant": grant, "upstream": upstream, "policy": policy,
           "node_name": node_name}
    for env in mgr.store.open():
        await mgr.close(env.id, discard=True, reason="test end")
    await mgr.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    await gitsvc.stop()
    await vault.stop()
    policy.close()
    left = await asyncio.to_thread(containers, node_name)
    if left:
        await asyncio.to_thread(subprocess.run, ["docker", "rm", "-f", *left], capture_output=True, check=False)


async def test_the_whole_story_on_a_real_machine(node):
    mgr, envs, grant = node["mgr"], node["envs"], node["grant"]
    eid = mgr.planned_id("session:s1", "Shop page")
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding="session:s1", title="Shop page", grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=f"{HOST}/{REPO}", ref=None)
    env = await mgr.wait_ready(env.id, 120)
    assert env.state == "ready", env.error
    assert env.workdir == "/work/shop" and env.via_gateway and env.branch == "main"
    lane = env.terminal_lane

    # a test page on the machine's own localhost, read through the environment's tunnel
    marker = f"served on the vm {uuid.uuid4().hex[:6]}"
    job = await envs.job_start(lane, f"mkdir -p /srv/t && echo '<title>On the VM</title><h1>{marker}</h1>' > "
                                     "/srv/t/index.html && cd /srv/t && exec python3 -m http.server 3000 "
                                     "--bind 127.0.0.1")
    proxy = mgr.proxy_for(env)
    assert proxy is not None and proxy.server.startswith("socks5://127.0.0.1:")
    from openajar.dev.proxied import FetchError, socks_fetch

    body = ""
    for _ in range(40):
        try:
            status, _url, body = await socks_fetch("http://localhost:3000/", proxy.host, proxy.port)
            if status == 200:
                break
        except FetchError:
            pass
        await asyncio.sleep(0.25)
    assert marker in body
    ports = await mgr.ports(env)
    assert {"port": 3000, "process": "python3", "link": None} in ports

    # studio's review browser through the same tunnel, when this computer has Chromium
    await _review_browser_sees(mgr, env, marker)

    # a branch pushed through the node's gateway (the reverse tunnel on the machine)
    res = await envs.exec(lane, "cd /work/shop && git checkout -q -b sale && echo more >> index.html && "
                                "git commit -qam 'Thirty percent off' && git push -q origin sale 2>&1 && "
                                "git rev-parse HEAD", grant_id=grant.id, timeout_s=120)
    assert res.exit_code == 0, res.stdout + res.stderr
    head = res.stdout.strip().splitlines()[-1]
    assert git("rev-parse", "sale", cwd=node["upstream"]).strip() == head
    seen = await envs.exec(lane, "cat ~/.gitconfig /work/shop/.git/config; env | grep -i token || true")
    assert "github_pat_" not in seen.stdout
    assert [p["branch"] for p in mgr.pushes(env)] == ["sale"]

    # unsaved work stops a close; then close deletes the machine
    await envs.exec(lane, "cd /work/shop && echo dirty > new.txt")
    from openajar.environments.errors import UnsavedWork

    with pytest.raises(UnsavedWork, match="new.txt"):
        await mgr.close(env.id)
    await envs.exec(lane, "cd /work/shop && git add new.txt && git stash -q && mkdir -p /work/scratch/x && "
                          "cd /work/scratch/x && git init -q && git commit -q --allow-empty -m 'Local only'")
    with pytest.raises(UnsavedWork, match="stash@") as err:  # a stash, and a repository the agent made
        await mgr.close(env.id)
    assert "Local only" in str(err.value)
    await envs.exec(lane, "cd /work/shop && git stash drop -q && rm -rf /work/scratch")
    await envs.job_cancel(job.id)
    closed = await mgr.close(env.id)
    assert closed.state == "closed" and [p.branch for p in closed.pushed] == ["sale"]
    assert await asyncio.to_thread(containers, node["node_name"]) == []


async def test_a_stopped_container_is_started_again_with_its_work_never_deleted(node):
    """S1-04 on a real container: `docker stop` (a reboot, a Docker Desktop restart) leaves the container exited with
    its disk whole. 0.7.0 called it gone and deleted it with the work. It is started again on a new SSH port, the
    link comes back, and the file written before the stop is there."""
    mgr, envs, vms, grant = node["mgr"], node["envs"], node["vms"], node["grant"]
    eid = mgr.planned_id("session:s2", "Stopped shop")
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding="session:s2", title="Stopped shop", grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=f"{HOST}/{REPO}", ref=None)
    env = await mgr.wait_ready(env.id, 120)
    assert env.state == "ready", env.error
    lane = env.terminal_lane
    wrote = await envs.exec(lane, "cd /work/shop && echo 'not pushed' > kept.txt && git add kept.txt")
    assert wrote.exit_code == 0, wrote.stderr
    rec = vms.store.get(env.vm_id)
    stopped = await asyncio.to_thread(subprocess.run, ["docker", "stop", "-t", "2", rec.provider_id],
                                      capture_output=True, text=True, check=False)
    assert stopped.returncode == 0, stopped.stderr
    assert await mgr.machine_gone(mgr.get(env.id), cause="test") is None  # stopped is not gone
    state = await asyncio.to_thread(subprocess.run, ["docker", "inspect", "-f", "{{.State.Status}}", rec.provider_id],
                                    capture_output=True, text=True, check=False)
    assert state.stdout.strip() == "running"
    assert vms.store.get(rec.id).state == "ready" and mgr.get(env.id).gone_at is None
    for _ in range(20):  # its ssh -N ended with the stop: the reaper's relink brings it back on the new port
        await mgr.reap_once()
        res = await envs.exec(lane, "cat /work/shop/kept.txt", timeout_s=30)
        if res.exit_code == 0:
            break
        await asyncio.sleep(1)
    assert res.exit_code == 0 and res.stdout.strip() == "not pushed", res.stdout + res.stderr
    assert mgr.get(env.id).open and mgr.tunnel_of(env.id) is not None and mgr.tunnel_of(env.id).alive


async def _review_browser_sees(mgr, env, marker: str) -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return
    from openajar.environments import routing
    from openajar.studio.browser.pool import ReviewPool
    from openajar.studio.contracts import Screen, Target

    pool = ReviewPool()
    try:
        with routing.studio_proxy(mgr.runtime, "ui_check", {"url": "x"}, session="s1", grant_id=env.grant_id):
            async with pool.page(Target(url="http://localhost:3000/"), Screen(width=360)) as page:
                assert marker in await page.inner_text("h1")
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "engine_missing" in str(getattr(e, "code", "")):
            return
        raise
    finally:
        await pool.close()


async def _open(node, session: str, title: str):
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id(f"session:{session}", title)
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding=f"session:{session}", title=title, grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
    env = await mgr.wait_ready(env.id, 150)
    assert env.state == "ready", env.error
    return env


async def _host_server() -> tuple[asyncio.AbstractServer, int]:
    """A server on this computer's loopback, like the node's own listeners."""
    async def answer(reader, writer):
        await reader.read(1024)
        writer.write(b"HTTP/1.0 200 OK\r\ncontent-length: 8\r\n\r\nthe-node")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _inspect(kind: str, name: str) -> dict:
    import json

    out = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]


async def test_a_machine_reaches_neither_the_node_nor_another_machine(node):
    """The docker provider's walls: a machine cannot reach this computer's loopback (the node's git
    gateway, another environment's SOCKS tunnel, any server here), nor another machine; it has its own
    network, resource limits and no capability to change its firewall."""
    mgr, envs, vms = node["mgr"], node["envs"], node["vms"]
    gitsvc = node["rt"].get("git")
    await gitsvc.start()
    server, port = await _host_server()
    a = await _open(node, "s1", "Env A")
    b = await _open(node, "s2", "Env B")
    try:
        socks_a = mgr.proxy_for(a).port
        await envs.job_start(a.terminal_lane, "mkdir -p /srv/t && echo SECRET-OF-A > /srv/t/index.html && cd /srv/t "
                                              "&& exec python3 -m http.server 3000 --bind 0.0.0.0")
        ip_a = (await envs.exec(a.terminal_lane, "hostname -I")).stdout.split()[0]
        # the server is up within 10 s: asked again every 0.2 s until it answers
        up = "for i in $(seq 50); do curl -sf -m 5 http://127.0.0.1:3000/ && break; sleep 0.2; done"
        assert "SECRET-OF-A" in (await envs.exec(a.terminal_lane, up)).stdout
        gw = "$(ip route | awk '/default/ {print $3}')"
        probes = {
            "this computer's loopback (Docker Desktop)": f"curl -s -m 4 http://host.docker.internal:{port}/",
            "this computer through the gateway (Linux)": f"curl -s -m 4 http://{gw}:{port}/",
            "the node's git gateway": f"curl -s -m 4 http://host.docker.internal:{gitsvc.host_port}/",
            "A's SOCKS tunnel": f"curl -s -m 4 --socks5-hostname host.docker.internal:{socks_a} http://localhost:3000/",
            "A's machine": f"curl -s -m 4 http://{ip_a}:3000/",
        }
        for what, cmd in probes.items():
            res = await envs.exec(b.terminal_lane, cmd + "; echo exit=$?", timeout_s=30)
            assert "the-node" not in res.stdout and "SECRET-OF-A" not in res.stdout, (what, res.stdout)
        # the machine cannot open its own wall again: no NET_ADMIN, even for root
        res = await envs.exec(b.terminal_lane, "iptables -F OUTPUT; echo exit=$?; grep CapBnd /proc/self/status")
        assert "exit=0" not in res.stdout, res.stdout
        bounding = int(res.stdout.split("CapBnd:")[1].split()[0], 16)
        assert not bounding & (1 << 12) and not bounding & (1 << 13), hex(bounding)  # cap_net_admin, cap_net_raw
        # limits and an own network per machine, from vms.toml
        rec_a, rec_b = (vms.store.get(mgr.get(e.id).vm_id) for e in (a, b))
        hc = _inspect("container", rec_b.provider_id)["HostConfig"]
        assert hc["Memory"] == 4 * 2**30 and hc["NanoCpus"] == 2 * 10**9 and hc["PidsLimit"] == 512, hc
        assert "ALL" in (hc.get("CapDrop") or []) and not hc.get("Privileged")
        nets = [_inspect("container", r.provider_id)["HostConfig"]["NetworkMode"] for r in (rec_a, rec_b)]
        assert nets[0] != nets[1] and not {"bridge", "default", "host"} & set(nets), nets
        assert _inspect("network", nets[1])["Options"].get("com.docker.network.bridge.enable_icc") == "false"
    finally:
        server.close()
    await mgr.close(a.id, discard=True)
    await mgr.close(b.id, discard=True)
    left = await asyncio.to_thread(subprocess.run, ["docker", "network", "ls", "-q", "--filter", f"name={nets[0]}"],
                                   capture_output=True, text=True, check=False)
    assert left.stdout.split() == [], "a closed machine's network is removed with it"


def test_the_docker_provider_needs_no_cloud(tmp_path):
    vms = VmManager(tmp_path, runtime=Runtime(), settings=VmSettings(provider="docker"))
    assert vms.configured and vms.provider().name == "docker" and vms.provider().firewall is False


def test_a_docker_machine_is_here_not_in_a_cloud_location():
    assert VmSettings(provider="docker").location == "local"
    assert VmSettings(provider="docker", location="garage").location == "garage"
    assert VmSettings().location == "nbg1"
