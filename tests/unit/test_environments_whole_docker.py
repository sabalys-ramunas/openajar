"""An environment on a machine handed over as a whole, for real (openajar.vms.whole), and Docker installed
on a machine without it: the machine is a container that plays a fresh Ubuntu 24.04 server
(tests/fixtures/whole_machine: systemd, sshd, root by key, a sudo login, no Docker), reached over ssh on a
loopback port of this computer.

Skipped unless docker answers and the stand-in is built (`docker build -t openajar-test-server
tests/fixtures/whole_machine`), so CI skips it. It needs the internet: `machine add --whole` runs the real
setup there once for the module (apt, Node, the browser: a few minutes), and the Docker test runs Docker's
own install script.

What it proves: `machine add --whole` over ssh (fresh check, setup, the words); an environment takes the
machine itself as root, clones through the node's gateway, serves a page through its tunnel and pushes a
branch; a close ends what it left running, empties /work and takes its key away, and leaves the rest of
the machine; a second environment is refused while one is open, and a closed network always; a save holds
the files and resumes onto the emptied machine; a machine that stops answering is waited for; `machine
remove` takes openajar's key and folder away. And on a second machine: `machine add --install-docker`."""

from __future__ import annotations

import asyncio
import contextlib
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
from openajar.environments import network as net
from openajar.environments.errors import EnvironmentFull
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.git import keys
from openajar.git.service import GitService
from openajar.vms import own, whole
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmSettings

SERVER_IMAGE = "openajar-test-server"
HOST, REPO = "example.test", "me/shop"
NAME = "server"


def _docker(*args: str, stdin: bytes | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=timeout, check=False)


def _why_not() -> str | None:
    if not shutil.which("docker") or not shutil.which("git") or not shutil.which("ssh"):
        return "needs docker, git and ssh"
    try:
        if not _docker("image", "ls", "-q", SERVER_IMAGE, timeout=20).stdout.strip():
            return f"needs the {SERVER_IMAGE} image (see this file's docstring)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker does not answer"
    return None


pytestmark = pytest.mark.skipif(_why_not() is not None, reason=_why_not() or "")


class Server:
    """The stand-in server: a container this test made, and removes."""

    def __init__(self, container: str, port: int):
        self.container, self.port = container, port
        self.added = ""  # what `machine add --whole` printed

    def sh(self, script: str, *, stdin: bytes | None = None, timeout: float = 120) -> str:
        done = _docker("exec", "-i", self.container, "sh", "-c", script, stdin=stdin, timeout=timeout)
        return done.stdout.decode(errors="replace")


def start_server() -> Server:
    """A fresh server: systemd needs the host's cgroups and its own /run; /var/lib/docker and
    /var/lib/containerd are volumes of its own, so a Docker installed there can run containers (an overlay
    cannot sit on this container's own overlay). They are removed with the container."""
    name = f"openajar-test-server-{uuid.uuid4().hex[:8]}"
    made = _docker("run", "-d", "--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                   "--tmpfs", "/run", "--tmpfs", "/run/lock", "-v", "/var/lib/docker", "-v", "/var/lib/containerd",
                   "--name", name,
                   "-p", "127.0.0.1::22", SERVER_IMAGE)
    assert made.returncode == 0, made.stderr.decode()
    try:
        for _ in range(120):
            if _docker("exec", name, "systemctl", "is-active", "ssh", timeout=20).stdout.strip() == b"active":
                break
            time.sleep(0.5)
        else:
            pytest.fail("the stand-in server's sshd did not start: " + _docker("logs", "--tail", "20", name).stderr.decode())
        port = int(_docker("port", name, "22").stdout.decode().strip().rsplit(":", 1)[1])
    except BaseException:
        _docker("rm", "-f", "-v", name)
        raise
    return Server(name, port)


@contextlib.contextmanager
def person(tmp: Path, server: Server, login: str, monkeypatch: pytest.MonkeyPatch):
    """The machine's owner: a key of theirs the login takes, held by an ssh agent (their "own ssh")."""
    key = tmp / "person"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "the-person"], check=True)
    folder = "/root/.ssh" if login == "root" else f"/home/{login}/.ssh"
    server.sh(f"umask 077; mkdir -p {folder}; cat >> {folder}/authorized_keys; chown -R {login}: {folder}",
              stdin=key.with_suffix(".pub").read_bytes())
    sock = Path("/tmp") / f"oa-agent-{uuid.uuid4().hex[:8]}"
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", str(sock)], stdout=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
        subprocess.run(["ssh-add", "-q", str(key)], check=True, env={**os.environ, "SSH_AUTH_SOCK": str(sock)})
        yield
    finally:
        agent.kill()
        agent.wait(timeout=10)
        with contextlib.suppress(OSError):
            sock.unlink()


@pytest.fixture(scope="module")
def box(tmp_path_factory):
    """The server, handed over once with the real command (its setup runs here, a few minutes)."""
    from openajar.environments.cli import app

    b = start_server()
    patch = pytest.MonkeyPatch()
    try:
        tmp = tmp_path_factory.mktemp("whole")
        home = private_home(tmp)
        with person(tmp, b, "root", patch):
            added = CliRunner().invoke(app, ["--home", str(home), "machine", "add", "first", f"root@127.0.0.1:{b.port}",
                                             "--whole", "--yes"])
        assert added.exit_code == 0, added.output
        b.added = added.output
        b.first_home = home  # type: ignore[attr-defined]
        yield b
    finally:
        patch.undo()
        _docker("rm", "-f", "-v", b.container)


def register(home: Path, box: Server, name: str = NAME) -> own.Machine:
    """What `machine add --whole` leaves on the node, with the key put on the machine by hand."""
    public = own.make_key(home, name)
    box.sh("umask 077; mkdir -p /root/.ssh; cat >> /root/.ssh/authorized_keys",
           stdin=(own.authorized_line(public) + "\n").encode())
    own.pin(home, name, own.scan_host_keys("127.0.0.1", box.port))
    m = own.Machine(name=name, host="127.0.0.1", port=box.port, user="root", mode="whole")
    facts = asyncio.run(own.read_facts(home, m))
    m.cores, m.memory_gb, m.disk_gb, m.arch = facts.host_cores, facts.host_memory_gb, facts.disk_gb, facts.arch
    own.save(home, m)
    return m


def unhold(box: Server) -> None:
    box.sh(f"rm -f {whole.SERVER}; sed -i '/openajar-machine-{NAME}/d; /agentos-vm_/d' /root/.ssh/authorized_keys; "
           "rm -rf /work/* /work/.[!.]*; true")


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
    await asyncio.to_thread(register, home, box)
    vms = VmManager(home, runtime=rt, settings=VmSettings(provider="own", location=NAME),
                    node_name=f"t-{uuid.uuid4().hex[:8]}")
    rt.register("vms", vms)
    rt.register("lanes", FakeLaneManager())
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings())
    rt.register("environments", mgr)
    grant, _ = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                            specials={"vm": {}, "exec": {}})
    yield {"rt": rt, "mgr": mgr, "vms": vms, "envs": envs, "grant": grant, "upstream": upstream, "home": home,
           "box": box}
    for env in mgr.store.open():
        await mgr.close(env.id, discard=True, reason="test end")
    await mgr.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    await gitsvc.stop()
    await vault.stop()
    policy.close()
    unhold(box)


async def _open(node, session: str, title: str, *, repo: str | None = None, keep: str = "none"):
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id(f"session:{session}", title)
    plan = await mgr.plan(eid, size=None, hours=1)
    await mgr.check_room(plan)
    env = await mgr.create(eid, plan, binding=f"session:{session}", title=title, grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=repo, ref=None, keep=keep)
    env = await mgr.wait_ready(env.id, 300)
    assert env.state == "ready", env.error
    return env


def test_machine_add_whole_checks_sets_up_and_says_what_it_takes(box):
    from openajar.environments.cli import app

    for words in ("The host key of 127.0.0.1", "ssh reaches root@127.0.0.1", "Ubuntu 24.04 on",
                  "openajar takes this machine as a whole", "Setting first up", "first is set up",
                  "New environments open on first", "the agent is root on it", "One environment is open on it at a time"):
        assert words in box.added, (words, box.added)
    assert "does not look fresh" not in box.added
    home = box.first_home
    m = own.load(home, "first")
    assert m.mode == "whole" and m.installed_key and not m.sudo and m.cores > 0 and m.disk_gb > 0
    assert 'provider = "own"' in (home / "vms.toml").read_text()
    # the setup a rented VM gets: its tools, the browser and the desktop, and /work
    have = box.sh("ls /var/lib/agentos-env/ready /opt/agentos-browser/ready /opt/agentos-desktop/ready; "
                  "node --version; command -v git; command -v pnpm; command -v uv; ls -d /work")
    for words in ("/var/lib/agentos-env/ready", "/opt/agentos-browser/ready", "/opt/agentos-desktop/ready", "v22.",
                  "/usr/bin/git", "pnpm", "/usr/local/bin/uv", "/work"):
        assert words in have, (words, have)
    run = CliRunner()
    base = ["--home", str(home), "machine"]
    listed = run.invoke(app, [*base, "list"])
    assert "first" in listed.output and "whole" in listed.output and "free" in listed.output, listed.output
    checked = run.invoke(app, [*base, "check", "first"])
    assert checked.exit_code == 0 and "No environment is open on it" in checked.output, checked.output
    assert "first is ready" in checked.output
    removed = run.invoke(app, [*base, "remove", "first"])
    assert removed.exit_code == 0 and "openajar's key is gone from it" in removed.output, removed.output
    assert "What the setup installed stays" in removed.output
    after = box.sh(f"cat /root/.ssh/authorized_keys; ls -d {whole.STATE_DIR} 2>&1; ls /opt/agentos-browser/ready")
    assert "openajar-machine-first" not in after and "the-person" in after
    assert "No such file" in after and "/opt/agentos-browser/ready" in after  # openajar's folder went, the setup stays


def test_a_machine_in_use_is_not_handed_over_without_a_person_saying_so(tmp_path, box, monkeypatch):
    """The signs of use are named, and --yes never answers that question."""
    from openajar.environments.cli import app

    home = private_home(tmp_path)
    box.sh("mkdir -p /var/www/shop && echo hi > /var/www/shop/index.html && useradd -m deploy 2>/dev/null; true")
    try:
        with person(tmp_path, box, "root", monkeypatch):
            added = CliRunner().invoke(app, ["--home", str(home), "machine", "add", "used", f"root@127.0.0.1:{box.port}",
                                             "--whole", "--yes"])
        assert added.exit_code == 1, added.output
        for words in ("This machine does not look fresh", "it holds files: /var/www", "user accounts", "deploy",
                      "leave out --whole", "never answered by --yes", "Nothing was added"):
            assert words in added.output, (words, added.output)
        assert own.every(home) == [] and "openajar-machine-used" not in box.sh("cat /root/.ssh/authorized_keys")
    finally:
        box.sh("rm -rf /var/www; userdel -r deploy 2>/dev/null; sed -i '/the-person/d' /root/.ssh/authorized_keys; true")


async def test_the_whole_story_on_a_whole_machine(node):
    mgr, envs, vms, grant, box = node["mgr"], node["envs"], node["vms"], node["grant"], node["box"]
    env = await _open(node, "s1", "Shop page", repo=f"{HOST}/{REPO}")
    assert env.workdir == "/work/shop" and env.via_gateway and env.branch == "main"
    assert env.machine.provider == "own" and env.machine.location == NAME and env.price_hourly_eur == 0.0
    rec = vms.store.get(env.vm_id)
    assert (rec.host, rec.port, rec.ssh_user, rec.via) == ("127.0.0.1", box.port, "root", None)
    lane = env.terminal_lane
    who = await envs.exec(lane, "id -un; cat /etc/hostname; ls -d /work/shop/.git")
    assert who.stdout.split()[0] == "root" and "/work/shop/.git" in who.stdout
    held = whole.holder(box.sh(f"cat {whole.SERVER}"))
    assert held["labels"]["agentos-vm"] == rec.id and held["labels"]["agentos-home"] == vms.home_id
    assert f"agentos-{rec.id}" in box.sh("cat /root/.ssh/authorized_keys")  # this environment's own key

    # a page on the machine's localhost, read through the environment's tunnel
    marker = f"served on the whole machine {uuid.uuid4().hex[:6]}"
    await envs.job_start(lane, f"mkdir -p /srv/t && echo '<title>T</title><h1>{marker}</h1>' > "
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

    # a branch pushed through the node's gateway (a reverse tunnel into the machine), no token on the machine
    res = await envs.exec(lane, "cd /work/shop && git checkout -q -b sale && echo more >> index.html && "
                                "git commit -qam 'Thirty percent off' && git push -q origin sale 2>&1 && "
                                "git rev-parse HEAD", grant_id=grant.id, timeout_s=120)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert git("rev-parse", "sale", cwd=node["upstream"]).strip() == res.stdout.strip().splitlines()[-1]
    seen = await envs.exec(lane, "cat ~/.gitconfig /work/shop/.git/config; env | grep -i token || true")
    assert "github_pat_" not in seen.stdout

    # the agent's own shell in local mode: `ssh openajar-<id>` reaches the machine directly
    from openajar.environments import sshaccess

    key, public = sshaccess.make_key(node["home"], env.id)
    done = await envs.exec(lane, "sh -s", stdin=sshaccess.authorize_script(public), timeout_s=60)
    assert done.exit_code == 0, done.stdout + done.stderr
    sshaccess.write_entry(node["home"], env.id, vms.remote(rec), key)
    entry = sshaccess.config_path(node["home"]).read_text()
    assert "ProxyCommand" not in entry
    mine = await asyncio.to_thread(subprocess.run, ["ssh", "-F", str(sshaccess.config_path(node["home"])),
                                                     sshaccess.alias(env.id), "echo from my own shell; id -un"],
                                   capture_output=True, text=True, timeout=60, check=False)
    assert mine.returncode == 0 and "from my own shell" in mine.stdout, mine.stderr
    assert sshaccess.remove_entry(node["home"], env.id)

    # what the agent leaves behind: the job above, a server started outside any job (it works in /work), an
    # ignored file in /work, and a file elsewhere on the machine
    await envs.exec(lane, "cd /work/shop && (setsid nohup python3 -m http.server 3001 >/dev/null 2>&1 &) ; "
                          "echo cache > .git/info/exclude && echo big > cache; "
                          "echo mine > /usr/local/share/agent-wrote-this; sleep 1; true")
    assert "3001" in box.sh("ss -Htln | awk '{print $4}'")
    closed = await mgr.close(env.id)
    assert closed.state == "closed" and [p.branch for p in closed.pushed] == ["sale"]
    # the machine is free and clean: no hold, that environment's key gone, /work empty, nothing of it running
    after = box.sh(f"ls {whole.SERVER} 2>&1; echo keys:; cat /root/.ssh/authorized_keys; echo work:; ls -A /work; "
                   "echo ports:; ss -Htln | awk '{print $4}'; echo procs:; pgrep -af 'http.server' || true")
    assert "No such file" in after.split("keys:")[0]
    keys_left = after.split("keys:")[1].split("work:")[0]
    assert f"agentos-{rec.id}" not in keys_left and f"openajar-machine-{NAME}" in keys_left
    assert after.split("work:")[1].split("ports:")[0].strip() == ""
    assert ":3000" not in after and ":3001" not in after and "http.server" not in after.split("procs:")[1]
    # and what openajar cannot tell from the system's own stays: said in the docs, not hidden
    assert box.sh("cat /usr/local/share/agent-wrote-this").strip() == "mine"
    assert box.sh("ls -d /work").strip() == "/work"  # the folder itself stays for the next environment
    box.sh("rm -rf /usr/local/share/agent-wrote-this /srv/t")


async def test_one_environment_at_a_time_and_never_a_closed_network(node):
    mgr = node["mgr"]
    first = await _open(node, "s1", "First")
    eid = mgr.planned_id("session:s2", "Second")
    plan = await mgr.plan(eid, size=None, hours=1)
    with pytest.raises(EnvironmentFull) as full:
        await mgr.check_room(plan)
    for words in ("handed over whole", f'"First" ({first.id})', "environment_close", "Nothing was opened"):
        assert words in full.value.message, full.value.message
    with pytest.raises(VmError, match="one environment at a time"):  # and the machine itself refuses a second
        await node["vms"].create("dev/second", ttl_minutes=30)
    with pytest.raises(net.NetworkNotAllowed) as closed:
        mgr.check_network_plan("limited", dict(plan))
    assert "nothing outside it can close its network" in closed.value.message
    assert mgr.check_network_plan("full", dict(plan))["network"] == "full"
    await mgr.close(first.id, discard=True)
    await mgr.check_room(plan)  # free again


async def test_a_save_holds_the_files_and_resumes_onto_the_emptied_machine(node):
    mgr, envs, grant, box = node["mgr"], node["envs"], node["grant"], node["box"]
    env = await _open(node, "s1", "Kept work", repo=f"{HOST}/{REPO}", keep="save")
    assert not mgr.snapshots_for(env)  # no disk snapshot on a whole machine
    res = await envs.exec(env.terminal_lane, "cd /work/shop && echo kept >> index.html && git commit -qam 'A local "
                                             "commit' && echo draft > notes.txt && git rev-parse HEAD", timeout_s=60)
    assert res.exit_code == 0, res.stdout + res.stderr
    head = res.stdout.strip().splitlines()[-1]
    closed = await mgr.close(env.id)
    assert closed.state == "closed" and closed.last_save_id
    m, _path = mgr.saves().find(closed.last_save_id)
    assert m.snapshot is None and m.files >= 1
    assert box.sh("ls -A /work").strip() == "" and "No such file" in box.sh(f"ls {whole.SERVER} 2>&1")

    saved = mgr.save_to_resume(closed.family, "Kept work", f"{HOST}/{REPO}")
    assert saved is not None and saved.id == m.id
    eid = mgr.planned_id("session:s2", "Kept work")
    plan = await mgr.plan_restore(eid, saved, hours=1, keep="save")
    assert plan["restore"]["from"] == "files" and plan["location"] == NAME
    again = await mgr.create(eid, plan, binding="session:s2", title="Kept work", grant_id=grant.id,
                             grant_root=grant.root_id, agent="agent-1", client=None, repo=saved.repo, ref=saved.ref,
                             keep="save", resumed=mgr.resuming(saved, plan))
    again = await mgr.wait_ready(again.id, 300)
    assert again.state == "ready", again.error
    out = await envs.exec(again.terminal_lane, "cd /work/shop && git rev-parse HEAD && cat notes.txt && "
                                               "tail -n 1 index.html")
    assert out.stdout.split() == [head, "draft", "kept"], out.stdout + out.stderr
    await mgr.close(again.id, discard=True)
    assert m.id in (await mgr.forget(None, "Kept work"))["forgot"]


async def test_a_whole_machine_that_stops_answering_is_waited_for_not_ended(node):
    mgr, envs, vms, box = node["mgr"], node["envs"], node["vms"], node["box"]
    env = await _open(node, "s1", "Patient")
    rec = vms.store.get(env.vm_id)
    await envs.exec(env.terminal_lane, "echo still here > /work/a.txt")
    box.sh("systemctl stop ssh.socket ssh.service 2>/dev/null; pkill -f 'sshd: ' ; true")
    try:
        with pytest.raises(VmError, match="did not answer over ssh"):
            await vms.provider().get_server(rec.provider_id)
        assert await mgr.machine_gone(mgr.get(env.id), cause="a test") is None
        with pytest.raises(VmError):
            await vms.reap_once()
        assert vms.store.get(rec.id).state == "ready" and mgr.get(env.id).gone_at is None
    finally:
        box.sh("systemctl start ssh.service")
    for _ in range(40):
        with contextlib.suppress(VmError):
            if (await vms.provider().get_server(rec.provider_id)) is not None:
                break
        await asyncio.sleep(0.5)
    out = await envs.exec(env.terminal_lane, "cat /work/a.txt", timeout_s=60)
    assert out.stdout.strip() == "still here"


async def test_a_hold_ended_on_the_machine_is_said_in_plain_words(node):
    mgr, box = node["mgr"], node["box"]
    env = await _open(node, "s1", "Short lived")
    box.sh(f"rm -f {whole.SERVER}")
    mgr.gone_recheck_s = 0.1
    why = await mgr.machine_gone(mgr.get(env.id), cause="a test")
    assert why is not None and "no longer holds this environment" in why, why
    closed = await mgr.close(env.id, discard=True)
    assert closed.state == "closed"


def test_docker_is_installed_on_a_machine_without_it_when_its_owner_says_so(tmp_path, monkeypatch):
    """A second fresh server, brought in with containers through a sudo login: `--install-docker` runs
    Docker's own script as root, the login joins the docker group, and Docker answers that login."""
    from openajar.environments.cli import app

    b = start_server()
    try:
        home = private_home(tmp_path)
        run = CliRunner()
        base = ["--home", str(home), "machine"]
        with person(tmp_path, b, "ubuntu", monkeypatch):
            plain = run.invoke(app, [*base, "add", "nodocker", f"ubuntu@127.0.0.1:{b.port}", "--yes", "--no-image",
                                     "--no-use"])
            # not at a terminal and no flag: nothing is installed, and the way to do it is said
            assert plain.exit_code == 1 and "Docker is not installed there" in plain.output, plain.output
            assert b.sh("command -v docker").strip() == ""
            added = run.invoke(app, [*base, "check", "nodocker", "--install-docker"])
        assert "Installing Docker on nodocker" in added.output and "Installed: Docker version" in added.output, added.output
        assert "The image is not ready there" in added.output  # Docker is there; the image is the next step
        m = own.load(home, "nodocker")
        assert m.installed_docker and m.docker == ["docker"] and m.docker_version
        side = own.MachineDocker(home, m, VmSettings(provider="own"))
        said = asyncio.run(side._run("run", "--rm", "hello-world", timeout_s=180))
        assert "Hello from Docker!" in said
        removed = run.invoke(app, [*base, "remove", "nodocker"])
        assert removed.exit_code == 0 and "Docker, which openajar installed there when you said so, stays" in removed.output
        assert b.sh("command -v docker").strip() != ""
    finally:
        _docker("rm", "-f", "-v", b.container)
