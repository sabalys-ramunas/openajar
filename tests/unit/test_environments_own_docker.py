"""An environment on a person's own machine, for real (openajar.vms.own): the machine is a container
with its own sshd and its own Docker (tests/fixtures/own_machine), reached over ssh on a loopback port
of this computer, exactly as a box at home or a server elsewhere is reached.

Skipped unless docker answers and both images are here: `agentos-env:latest` (`openajar environments
setup --pull`) and the stand-in machine (`docker build -t openajar-test-machine
tests/fixtures/own_machine`). The machine's Docker keeps its images in the volume
`openajar-test-machine-docker`, so the 4 GB environments image is copied into it once (a minute or two)
and later runs start in seconds; `docker volume rm openajar-test-machine-docker` frees it.

What it proves: `machine add` over ssh (the key installed by the person's own ssh, the host key pinned);
an environment comes up as a container there and is reached through the machine; a repository is cloned
and a branch pushed through the node's git gateway; a page on the environment's localhost is read through
its tunnel; the environment reaches neither the machine nor another environment; a save stays in the
machine's Docker and resumes there; a machine that stops answering is not read as deleted; and `machine
remove` leaves nothing of openajar behind while the person's own things stay."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from test_environments_docker import git
from test_policy_support import make_service
from test_vault_support import make_vault, private_home
from typer.testing import CliRunner

from openajar.core.models import Effect
from openajar.dev.lanes import FakeLaneManager
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.git import keys
from openajar.git.service import GitService
from openajar.vms import own
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmSettings

MACHINE_IMAGE = "openajar-test-machine"
ENV_IMAGE = "agentos-env:latest"
VOLUME = "openajar-test-machine-docker"   # the machine's /var/lib/docker: keeps the environments image between runs
HOST, REPO = "example.test", "me/shop"
SSHD = ("/usr/sbin/sshd -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no "
        "-o PermitRootLogin=prohibit-password -o AllowTcpForwarding=yes -o MaxStartups=100")


def _docker(*args: str, stdin: bytes | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=timeout, check=False)


def _why_not() -> str | None:
    if not shutil.which("docker") or not shutil.which("git") or not shutil.which("ssh"):
        return "needs docker, git and ssh"
    try:
        for image in (ENV_IMAGE, MACHINE_IMAGE):
            if not _docker("image", "ls", "-q", image, timeout=20).stdout.strip():
                return f"needs the {image} image (see this file's docstring)"
        if _docker("ps", "-q", "--filter", f"volume={VOLUME}", timeout=20).stdout.strip():
            return f"another run uses the volume {VOLUME} now"
    except (OSError, subprocess.TimeoutExpired):
        return "docker does not answer"
    return None


pytestmark = pytest.mark.skipif(_why_not() is not None, reason=_why_not() or "")


class Box:
    """The stand-in machine: a container this test made, and removes."""

    def __init__(self, container: str, port: int):
        self.container, self.port = container, port

    def sh(self, script: str, *, stdin: bytes | None = None, timeout: float = 120) -> str:
        done = _docker("exec", "-i", self.container, "sh", "-c", script, stdin=stdin, timeout=timeout)
        return done.stdout.decode(errors="replace")

    def containers(self, label: str = "agentos-home") -> list[str]:
        return self.sh(f"docker ps -aq --filter label={label}").split()

    def images(self) -> list[str]:
        return self.sh("docker images --format '{{.Repository}}:{{.Tag}}'").split()


@pytest.fixture(scope="module")
def box():
    name = f"openajar-test-machine-{uuid.uuid4().hex[:8]}"
    made = _docker("run", "-d", "--privileged", "--name", name, "-p", "127.0.0.1::22", "-v",
                   f"{VOLUME}:/var/lib/docker", MACHINE_IMAGE)
    assert made.returncode == 0, made.stderr.decode()
    b = None
    try:
        for _ in range(120):  # its Docker starts within a few seconds
            if _docker("exec", name, "docker", "info", timeout=20).returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("the stand-in machine's Docker did not start: " + _docker("logs", "--tail", "20", name).stderr.decode())
        port = int(_docker("port", name, "22").stdout.decode().strip().rsplit(":", 1)[1])
        b = Box(name, port)
        # what an earlier run that died left in the machine's Docker (it is this test's own volume)
        b.sh("docker rm -f -v $(docker ps -aq) 2>/dev/null; docker network prune -f >/dev/null 2>&1; "
             "docker images --format '{{.Repository}}:{{.Tag}}' | grep -v ':latest$' | xargs -r docker rmi -f "
             ">/dev/null 2>&1; true")
        here = _docker("image", "inspect", "-f", "{{.Id}}", ENV_IMAGE).stdout.decode().strip()
        there = b.sh(f"docker image inspect -f '{{{{.Id}}}}' {ENV_IMAGE} 2>/dev/null").strip()
        if here != there:  # the first run, or after the image here changed: copy it in (a minute or two)
            save = subprocess.Popen(["docker", "save", ENV_IMAGE], stdout=subprocess.PIPE)
            load = subprocess.run(["docker", "exec", "-i", name, "docker", "load"], stdin=save.stdout,
                                  capture_output=True, timeout=1800, check=False)
            save.wait(timeout=60)
            assert load.returncode == 0, load.stderr.decode()
        yield b
    finally:
        _docker("rm", "-f", "-v", name)


def register(home: Path, box: Box, name: str = "testbox") -> own.Machine:
    """What `machine add` leaves on the node, with the key put on the machine by hand."""
    public = own.make_key(home, name)
    box.sh("umask 077; mkdir -p /root/.ssh; cat >> /root/.ssh/authorized_keys",
           stdin=(own.authorized_line(public) + "\n").encode())
    own.pin(home, name, own.scan_host_keys("127.0.0.1", box.port))
    m = own.Machine(name=name, host="127.0.0.1", port=box.port, user="root")
    own.save(home, m)
    return m


@pytest.fixture
async def node(tmp_path, box):
    home = private_home(tmp_path)
    policy, rt = make_service(home)
    vault = make_vault(home)
    await vault.start(control=False)
    rt.register("vault", vault)
    gitsvc = GitService(rt, home, allow_file=True, host_port=0)
    rt.register("git", gitsvc)
    upstream, seed = tmp_path / "up.git", tmp_path / "seed"
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
    register(home, box)
    node_name = f"t-{uuid.uuid4().hex[:8]}"
    vms = VmManager(home, runtime=rt, settings=VmSettings(provider="own", location="testbox"), node_name=node_name)
    rt.register("vms", vms)
    rt.register("lanes", FakeLaneManager())
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings())
    rt.register("environments", mgr)
    grant, _ = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                            specials={"vm": {}, "exec": {}})
    yield {"rt": rt, "mgr": mgr, "vms": vms, "envs": envs, "grant": grant, "upstream": upstream, "home": home,
           "box": box, "policy": policy, "node_name": node_name}
    for env in mgr.store.open():
        await mgr.close(env.id, discard=True, reason="test end")
    await mgr.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    await gitsvc.stop()
    await vault.stop()
    policy.close()
    box.sh(f"docker rm -f -v $(docker ps -aq --filter label=agentos-home={vms.home_id}) 2>/dev/null; true")
    box.sh("sed -i '/openajar-machine-/d' /root/.ssh/authorized_keys 2>/dev/null; true")


async def _open(node, session: str, title: str, *, repo: str | None = None, keep: str = "none"):
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id(f"session:{session}", title)
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding=f"session:{session}", title=title, grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=repo, ref=None, keep=keep)
    env = await mgr.wait_ready(env.id, 180)
    assert env.state == "ready", env.error
    return env


async def test_the_whole_story_on_your_own_machine(node):
    mgr, envs, vms, grant, box = node["mgr"], node["envs"], node["vms"], node["grant"], node["box"]
    env = await _open(node, "s1", "Shop page", repo=f"{HOST}/{REPO}")
    assert env.workdir == "/work/shop" and env.via_gateway and env.branch == "main"
    assert env.machine.provider == "own" and env.machine.location == "testbox" and env.price_hourly_eur == 0.0
    rec = vms.store.get(env.vm_id)
    name, cid = own.split_server(rec.provider_id)
    assert name == "testbox" and rec.host == "127.0.0.1" and rec.via["port"] == box.port
    # the container is on the machine, not on this computer, with no folder of the machine in it
    assert _docker("ps", "-aq", "--filter", f"label=agentos-vm={rec.id}").stdout.strip() == b""
    there = json.loads(box.sh(f"docker inspect {cid}"))[0]
    assert there["Mounts"] == [] and not there["HostConfig"]["Privileged"]
    assert there["HostConfig"]["PortBindings"]["22/tcp"][0]["HostIp"] == "127.0.0.1"
    lane = env.terminal_lane

    # a page on the environment's own localhost, read through its tunnel (which goes through the machine)
    marker = f"served on my machine {uuid.uuid4().hex[:6]}"
    job = await envs.job_start(lane, f"mkdir -p /srv/t && echo '<title>T</title><h1>{marker}</h1>' > "
                                     "/srv/t/index.html && cd /srv/t && exec python3 -m http.server 3000 "
                                     "--bind 127.0.0.1")
    proxy = mgr.proxy_for(env)
    from openajar.dev.proxied import FetchError, socks_fetch

    body = ""
    for _ in range(60):
        try:
            status, _url, body = await socks_fetch("http://localhost:3000/", proxy.host, proxy.port)
            if status == 200:
                break
        except FetchError:
            pass
        await asyncio.sleep(0.25)
    assert marker in body

    # a branch pushed through the node's gateway (a reverse tunnel into the container), no token on the machine
    res = await envs.exec(lane, "cd /work/shop && git checkout -q -b sale && echo more >> index.html && "
                                "git commit -qam 'Thirty percent off' && git push -q origin sale 2>&1 && "
                                "git rev-parse HEAD", grant_id=grant.id, timeout_s=120)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert git("rev-parse", "sale", cwd=node["upstream"]).strip() == res.stdout.strip().splitlines()[-1]
    seen = await envs.exec(lane, "cat ~/.gitconfig /work/shop/.git/config; env | grep -i token || true")
    assert "github_pat_" not in seen.stdout

    # the agent's own shell in local mode: `ssh openajar-<id>` reaches the container through the machine too
    from openajar.environments import sshaccess

    key, public = sshaccess.make_key(node["home"], env.id)
    done = await envs.exec(lane, "sh -s", stdin=sshaccess.authorize_script(public), timeout_s=60)
    assert done.exit_code == 0, done.stdout + done.stderr
    sshaccess.write_entry(node["home"], env.id, vms.remote(rec), key)
    mine = await asyncio.to_thread(subprocess.run, ["ssh", "-F", str(sshaccess.config_path(node["home"])),
                                                     sshaccess.alias(env.id), "echo from my own shell; hostname"],
                                   capture_output=True, text=True, timeout=60, check=False)
    assert mine.returncode == 0 and "from my own shell" in mine.stdout, mine.stderr
    assert sshaccess.remove_entry(node["home"], env.id)

    await envs.job_cancel(job.id)
    closed = await mgr.close(env.id)
    assert closed.state == "closed" and [p.branch for p in closed.pushed] == ["sale"]
    assert box.containers(f"agentos-home={vms.home_id}") == []
    assert box.sh("docker network ls -q --filter label=agentos-home=" + vms.home_id).split() == []
    assert ENV_IMAGE in box.images()  # the image stays for the next environment


async def test_an_environment_reaches_neither_the_machine_nor_another_environment(node):
    """The walls on a person's machine: the machine's own services (its sshd here, on every address it
    has), its network, and another environment are all out of reach, and root in the container cannot
    take the wall down."""
    envs, box = node["envs"], node["box"]
    a = await _open(node, "s1", "Env A")
    b = await _open(node, "s2", "Env B")
    await envs.job_start(a.terminal_lane, "mkdir -p /srv/t && echo SECRET-OF-A > /srv/t/index.html && cd /srv/t "
                                          "&& exec python3 -m http.server 3000 --bind 0.0.0.0")
    ip_a = (await envs.exec(a.terminal_lane, "hostname -I")).stdout.split()[0]
    up = "for i in $(seq 50); do curl -sf -m 5 http://127.0.0.1:3000/ && break; sleep 0.2; done"
    assert "SECRET-OF-A" in (await envs.exec(a.terminal_lane, up)).stdout
    machine_ips = box.sh("ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1").split()
    assert len(machine_ips) >= 3, machine_ips  # loopback, its own network, its Docker's bridges
    banner = "timeout 4 bash -c 'exec 3<>/dev/tcp/{ip}/22 && head -c 4 <&3' 2>/dev/null; echo"
    gw = "$(ip route | awk '/default/ {print $3}')"
    probes = {"the machine's sshd through the gateway": banner.format(ip=gw),
              "A's environment": f"curl -s -m 4 http://{ip_a}:3000/"}
    for ip in machine_ips:
        if ip != "127.0.0.1":
            probes[f"the machine's sshd at {ip}"] = banner.format(ip=ip)
    for what, cmd in probes.items():
        res = await envs.exec(b.terminal_lane, cmd + "; echo exit=$?", timeout_s=30)
        assert "SSH-" not in res.stdout and "SECRET-OF-A" not in res.stdout, (what, res.stdout)
    # the same probe does see an sshd where one is reachable (the container's own), so a miss above is the wall
    own_sshd = await envs.exec(b.terminal_lane, banner.format(ip="127.0.0.1"))
    assert "SSH-" in own_sshd.stdout, own_sshd.stdout + own_sshd.stderr
    res = await envs.exec(b.terminal_lane, "iptables -F OUTPUT; echo exit=$?; grep CapBnd /proc/self/status")
    assert "exit=0" not in res.stdout, res.stdout
    bounding = int(res.stdout.split("CapBnd:")[1].split()[0], 16)
    assert not bounding & (1 << 12) and not bounding & (1 << 13), hex(bounding)  # cap_net_admin, cap_net_raw
    # the internet still works from a full-network environment
    out = await envs.exec(b.terminal_lane, "getent hosts example.com >/dev/null && echo resolves")
    assert "resolves" in out.stdout


async def test_a_save_stays_in_the_machines_docker_and_resumes_there(node):
    mgr, envs, vms, grant, box = node["mgr"], node["envs"], node["vms"], node["grant"], node["box"]
    env = await _open(node, "s1", "Kept work", keep="save")
    await envs.exec(env.terminal_lane, "echo installed > /usr/local/share/marker && mkdir -p /work/notes && "
                                       "echo hello > /work/notes/a.txt")
    closed = await mgr.close(env.id)
    assert closed.state == "closed" and closed.last_save_id
    m, _path = mgr.saves().find(closed.last_save_id)
    assert m.snapshot is not None and m.snapshot.provider == "own"
    machine, image = own.split_image(m.snapshot.id)
    assert machine == "testbox" and image in box.images()
    assert box.containers(f"agentos-home={vms.home_id}") == []  # the container is gone, the image is the save
    from openajar.environments.saves import public

    assert public(m, 0.012)["cost_eur_month"] == 0.0  # nothing is rented: a save on your machine costs nothing

    # another chat brings it back: a new container from that image, on that machine
    saved = mgr.save_to_resume(closed.family, "Kept work", None)
    assert saved is not None and saved.id == m.id
    eid = mgr.planned_id("session:s2", "Kept work")
    plan = await mgr.plan_restore(eid, saved, hours=1, keep="save")
    assert plan["image"] == m.snapshot.id and plan["location"] == "testbox"
    again = await mgr.create(eid, plan, binding="session:s2", title="Kept work", grant_id=grant.id,
                             grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None,
                             keep="save", resumed=mgr.resuming(saved, plan))
    again = await mgr.wait_ready(again.id, 180)
    assert again.state == "ready", again.error
    out = await envs.exec(again.terminal_lane, "cat /usr/local/share/marker /work/notes/a.txt")
    assert out.stdout.split() == ["installed", "hello"]
    await mgr.close(again.id, discard=True)
    gone = await mgr.forget(None, "Kept work")
    assert m.id in gone["forgot"]
    assert [i for i in box.images() if ":save-" in i] == []


async def test_a_machine_that_stops_answering_is_waited_for_not_ended(node):
    mgr, envs, vms, box = node["mgr"], node["envs"], node["vms"], node["box"]
    env = await _open(node, "s1", "Patient")
    rec = vms.store.get(env.vm_id)
    # the machine drops off the network: its own sshd and every session through it (by its pid file: the
    # machine sees its containers' processes too, and their sshd must live on)
    box.sh("p=$(cat /run/sshd.pid); pkill -P $p; kill $p; true")
    try:
        with pytest.raises(VmError, match="did not answer over ssh"):
            await vms.provider().get_server(rec.provider_id)
        assert await mgr.machine_gone(mgr.get(env.id), cause="a test") is None
        with pytest.raises(VmError):
            await vms.reap_once()
        assert vms.store.get(rec.id).state == "ready" and mgr.get(env.id).gone_at is None
    finally:
        box.sh(SSHD)
    for _ in range(40):  # it answers again: the same container, still running what it ran
        with contextlib.suppress(VmError):
            if (await vms.provider().get_server(rec.provider_id)) is not None:
                break
        await asyncio.sleep(0.5)
    out = await envs.exec(env.terminal_lane, "echo back", timeout_s=60)
    assert out.stdout.strip() == "back"


async def test_a_container_that_stopped_on_the_machine_is_started_again_with_its_files(node):
    """What a restart of the machine does to an open environment: its container stops with its disk whole.
    It was called gone and the environment closed without a save (the 0.7.0 audit, S1-04); now the node
    starts it again through the machine's Docker, on the port Docker gives it there, and the files are where
    they were. A container removed there is gone, said in plain words, and closes without a save."""
    mgr, envs, vms, box = node["mgr"], node["envs"], node["vms"], node["box"]
    env = await _open(node, "s1", "Short lived")
    started = time.monotonic()
    for _ in range(5):
        assert (await envs.exec(env.terminal_lane, "true", timeout_s=30)).exit_code == 0
    print(f"one command through the machine and into the container: {(time.monotonic() - started) / 5:.2f} s")
    assert (await envs.exec(env.terminal_lane, "echo kept > /work/a.txt", timeout_s=30)).exit_code == 0
    rec = vms.store.get(env.vm_id)
    _name, cid = own.split_server(rec.provider_id)
    box.sh(f"docker stop -t 1 {cid}")
    mgr.gone_recheck_s = 0.1
    assert await mgr.machine_gone(mgr.get(env.id), cause="a test") is None
    assert (await vms.provider().get_server(rec.provider_id)).status == "running"
    out = await envs.exec(env.terminal_lane, "cat /work/a.txt", timeout_s=60)
    assert out.stdout.strip() == "kept" and mgr.get(env.id).gone_at is None
    box.sh(f"docker rm -f {cid}")
    mgr._alive_seen().clear()
    why = await mgr.machine_gone(mgr.get(env.id), cause="a test")
    assert why is not None and "Your machine no longer has its container" in why, why
    closed = await mgr.close(env.id, discard=True)
    assert closed.state == "closed" and box.containers(f"agentos-home={vms.home_id}") == []


def test_machine_add_check_and_remove_over_ssh(tmp_path, box, monkeypatch):
    """The CLI against the machine: add (openajar's key installed by the person's own ssh, here an agent
    that holds their key), list, check, and remove, which takes openajar's things away and leaves theirs."""
    from openajar.environments.cli import app

    home = private_home(tmp_path)
    person = tmp_path / "person"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(person), "-C", "the-person"], check=True)
    box.sh("umask 077; mkdir -p /root/.ssh; cat >> /root/.ssh/authorized_keys", stdin=person.with_suffix(".pub").read_bytes())
    box.sh("docker run -d --name theirs --label theirs=1 --entrypoint sleep " + ENV_IMAGE + " 3600")
    sock = Path("/tmp") / f"oa-agent-{uuid.uuid4().hex[:8]}"
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", str(sock)], stdout=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
        subprocess.run(["ssh-add", "-q", str(person)], check=True, env={**os.environ, "SSH_AUTH_SOCK": str(sock)})
        run = CliRunner()
        base = ["--home", str(home), "machine"]
        added = run.invoke(app, [*base, "add", "garage", f"root@127.0.0.1:{box.port}", "--yes"])
        assert added.exit_code == 0, added.output
        for words in ("The host key of 127.0.0.1", "Adding it to root@127.0.0.1 with your own ssh",
                      "ssh reaches root@127.0.0.1", "Linux on", "The environments image is on garage already",
                      "New environments open on garage", "shares that machine's kernel"):
            assert words in added.output, (words, added.output)
        m = own.load(home, "garage")
        assert m.installed_key and not m.installed_image and m.cores > 0 and m.docker
        assert "provider = \"own\"" in (home / "vms.toml").read_text()
        lines = box.sh("cat /root/.ssh/authorized_keys").splitlines()
        assert any(ln.startswith("restrict,port-forwarding ssh-ed25519 ") and "openajar-machine-garage" in ln
                   for ln in lines), lines
        again = run.invoke(app, [*base, "add", "garage", f"root@127.0.0.1:{box.port}", "--yes"])
        assert again.exit_code == 1 and "set up already" in again.output

        listed = run.invoke(app, [*base, "list"])
        assert "garage" in listed.output and f"root@127.0.0.1:{box.port}" in listed.output
        assert "new environments open here" in listed.output
        checked = run.invoke(app, [*base, "check", "garage"])
        assert checked.exit_code == 0 and "garage is ready" in checked.output, checked.output

        removed = run.invoke(app, [*base, "remove", "garage"])
        assert removed.exit_code == 0 and "Nothing else there was touched" in removed.output, removed.output
        assert "openajar's key" in removed.output and "the environments image" not in removed.output
        after = box.sh("cat /root/.ssh/authorized_keys")
        assert "openajar-machine-garage" not in after and "the-person" in after
        assert ENV_IMAGE in box.images() and box.containers("theirs=1") != []  # theirs, and an image openajar found there
        assert not own.folder(home, "garage").exists()
        assert "No machine of your own is set up" in run.invoke(app, [*base, "list"]).output
    finally:
        agent.terminate()
        agent.wait(timeout=10)
        sock.unlink(missing_ok=True)
        box.sh("docker rm -f theirs; sed -i '/the-person/d' /root/.ssh/authorized_keys; true")


def test_a_machine_whose_sshd_refuses_forwarding_is_told_so(tmp_path):
    """An sshd with AllowTcpForwarding no cannot carry an environment's connection: add says it."""
    name = f"openajar-test-machine-{uuid.uuid4().hex[:8]}"
    made = _docker("run", "-d", "--privileged", "--name", name, "-e", "MACHINE_NO_FORWARDING=1", "-p",
                   "127.0.0.1::22", MACHINE_IMAGE)
    assert made.returncode == 0, made.stderr.decode()
    try:
        port = int(_docker("port", name, "22").stdout.decode().strip().rsplit(":", 1)[1])
        b = Box(name, port)
        home = private_home(tmp_path)
        for _ in range(60):
            try:
                m = register(home, b, "strict")
                break
            except VmError:
                time.sleep(0.5)
        else:
            pytest.fail("the machine's sshd did not start")
        refused = own.forwarding_refused(home, m, 22)
        assert refused is not None and "AllowTcpForwarding" in refused
    finally:
        _docker("rm", "-f", "-v", name)
