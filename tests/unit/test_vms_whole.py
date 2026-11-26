"""A machine of your own handed over as a whole (openajar.vms.whole), and Docker installed on one that
lacks it (openajar.vms.own.install_docker), without a machine: what `machine add --whole` reads and refuses,
and the provider over a fake machine that answers the scripts openajar sends over ssh.
tests/unit/test_environments_whole_docker.py runs the same against a real one (a container that plays a
fresh Ubuntu 24.04 server)."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess

import pytest
from test_vms_own import FakeDocker, ok_probe

from openajar.core.models import utcnow
from openajar.core.runtime import Runtime
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher
from openajar.vms import own, whole
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmSettings
from openajar.vms.provider import capable_at

HOST_KEY = "ssh-ed25519 " + "B" * 68  # a made-up host key, put together here


class FakeServer:
    """One whole machine, as far as openajar's scripts use it: who holds it, and root's authorized_keys."""

    def __init__(self):
        self.held: str | None = None
        self.keys: list[str] = []
        self.down = False
        self.commands: list[str] = []
        self.released: list[str] = []
        self.forgotten = False

    def run(self, command: str, stdin: bytes | None) -> tuple[int, str, str]:
        if self.down:
            return 255, "", "ssh: connect to host 192.0.2.20 port 22: Connection timed out"
        self.commands.append(command)
        script = (stdin or b"").decode()
        if "set -C" in script:  # take_script
            if self.held:
                return 0, f"state=held\nheld={self.held}\n", ""
            self.held = base64.b64decode(re.search(r"echo (\S+) \| base64 -d", script).group(1)).decode()
            self.keys.append(re.search(r"\necho '?(ssh-ed25519 [^'\n]+?)'? >> ", script).group(1))
            return 0, "state=made\n", ""
        if "state=released" in script:  # release_script
            marker = re.search(r"grep -qF '([^']+)'", script).group(1)
            if self.held and marker not in self.held:
                return 0, "state=other\n", ""
            blob = re.search(r"grep -vF (\S+) ", script)
            self.keys = [k for k in self.keys if blob is None or blob.group(1) not in k]
            self.released.append(marker)
            self.held = None
            return 0, "state=released\n", ""
        if script.startswith("rm -rf"):
            self.forgotten = True
            return 0, "done=1\n", ""
        return 0, f"held={self.held or ''}\nread=1\n", ""


@pytest.fixture
def machines(tmp_path, monkeypatch):
    """A whole machine (`server`, where new environments open) beside a machine of containers (`homebox`)."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    server, docker = FakeServer(), FakeDocker()
    own.save(home, own.Machine(name="server", host="server.test", user="root", mode="whole", cores=8,
                               memory_gb=16.0, disk_gb=160.0, arch="x86_64"))
    own.save(home, own.Machine(name="homebox", host="homebox.test", user="me"))
    for name in ("server", "homebox"):
        own.make_key(home, name)
        own.pin(home, name, [HOST_KEY])

    async def run(_home, m, command, *, stdin=None, timeout_s=0):
        if m.name == "server":
            return server.run(command, stdin)
        import shlex

        return docker.run(shlex.split(command)[len(m.docker):], stdin)

    monkeypatch.setattr(own, "run", run)
    return home, server, docker


@pytest.fixture
async def node(machines):
    home, server, docker = machines
    envs = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    runtime = Runtime(envs=envs)
    vms = VmManager(home, runtime=runtime, settings=VmSettings(provider="own", location="server"),
                    node_name="node-1", probe=ok_probe)
    runtime.register("vms", vms)
    yield vms, envs, server, docker, home
    await vms.shutdown()
    await envs.shutdown()


# ---------------------------------------------------------------- what `machine add --whole` reads

def _facts(**over) -> own.Facts:
    said = {"system": "Linux", "arch": "x86_64", "user": "root", "uid": 0, "root": True, "os_id": "ubuntu",
            "os_version": "24.04", "host_cores": 4, "host_memory_gb": 8.0, "disk_gb": 80.0, "root_free_gb": 70.0,
            "sshd_port": 22}
    return own.Facts(**{**said, **over})


def test_a_fresh_ubuntu_server_can_be_handed_over():
    got = whole.parse_fresh("rootlogin=prohibit-password\nheld=\nusers=ubuntu \nports=22 \ncontainers=0\nwork=\ndata=\n")
    assert whole.problems(_facts(), got) == [] and whole.not_fresh(_facts(), got) == []
    sudo = _facts(user="ubuntu", uid=1000)  # a cloud image's login with passwordless sudo
    assert whole.problems(sudo, got) == [] and own.as_root(sudo) == "sudo -n sh -s" and own.as_root(_facts()) == "sh -s"


@pytest.mark.parametrize("facts, got, words", [
    (_facts(system="Darwin"), {}, "macOS"),
    (_facts(os_id="debian", os_version="12"), {}, "must be a fresh Ubuntu 24.04"),
    (_facts(os_version="22.04"), {}, "leave out --whole"),
    (_facts(arch="riscv64"), {}, "x86-64 and arm64"),
    (_facts(user="me", uid=1000, root=False), {}, "Add it as root@host"),
    (_facts(), {"rootlogin": "no"}, "PermitRootLogin prohibit-password"),
    (_facts(root_free_gb=4.0), {}, "4 GB free"),
    (_facts(), {"held": '{"id": "agentos-vm-1", "name": "x", "labels": {"agentos-home": "other"}}'},
     "another openajar holds it"),
])
def test_what_keeps_a_machine_from_being_handed_over_is_said_with_its_fix(facts, got, words):
    wrong = whole.problems(facts, got, "mine")
    assert len(wrong) == 1 and words in wrong[0], wrong


def test_a_machine_this_openajar_holds_itself_is_not_refused():
    held = {"held": '{"id": "agentos-vm-1", "name": "x", "labels": {"agentos-home": "mine"}}'}
    assert whole.problems(_facts(), held, "mine") == []


@pytest.mark.parametrize("got, words", [
    ({"containers": "3"}, "its Docker has 3 containers"),
    ({"ports": "22 80 443"}, "listens on ports 80, 443"),
    ({"users": "ubuntu deploy"}, "2 user accounts (ubuntu, deploy)"),
    ({"work": "shop notes"}, "its /work folder is not empty (shop, notes)"),
    ({"data": "/var/www /home/ubuntu/site"}, "it holds files: /var/www, /home/ubuntu/site"),
])
def test_the_signs_of_a_machine_in_use_are_named(got, words):
    signs = whole.not_fresh(_facts(), got)
    assert len(signs) == 1 and words in signs[0], signs


def test_what_a_fresh_machine_has_is_no_sign_of_use():
    # its own sshd (also on another port), one login, and the /work an earlier setup of openajar made
    assert whole.not_fresh(_facts(sshd_port=2222), {"ports": "22 2222", "users": "ubuntu", "containers": "0"}) == []
    assert whole.not_fresh(_facts(), {"work": "old", "setup": "1"}) == []


def test_who_holds_a_machine_is_read_from_its_file():
    assert whole.holder("") is None and whole.holder("  \n") is None
    state = whole.holder('{"id": "agentos-vm-ab", "name": "agentos-vm-ab", "labels": {"agentos-vm": "vm_ab"}}')
    assert state is not None and state["labels"]["agentos-vm"] == "vm_ab"
    assert whole.holder("not json") == {"id": "", "name": "", "labels": {}}  # held by nobody openajar can name


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_the_scripts_openajar_runs_on_a_machine_are_valid_shell():
    key = "ssh-ed25519 " + "C" * 68 + " agentos-vm_1"
    scripts = {"check": own.CHECK_SCRIPT, "fresh": whole.FRESH_SCRIPT, "install docker": own.INSTALL_DOCKER,
               "take": whole.take_script({"id": "agentos-vm-1", "labels": {"a": "it's"}}, key),
               "release": whole.release_script("agentos-vm-1", key), "release, no key": whole.release_script("x")}
    for name, script in scripts.items():
        done = subprocess.run(["sh", "-n"], input=script.encode(), capture_output=True, timeout=30, check=False)
        assert done.returncode == 0, (name, done.stderr.decode())


def test_a_release_names_its_server_and_removes_only_that_environments_key():
    key = "ssh-ed25519 " + "C" * 68 + " agentos-vm_1"
    script = whole.release_script("agentos-vm-1", key)
    assert '\'"id": "agentos-vm-1"\'' in script and "state=other" in script  # another server's hold is left
    assert f"grep -vF {'C' * 68} " in script  # by the key itself: the person's own lines stay
    assert "find /work -mindepth 1 -maxdepth 1" in script and "rm -rf /work " not in script  # the folder stays
    for never in ("reboot", "shutdown", "poweroff", "docker rm", "docker stop", "apt-get"):
        assert never not in script


# ---------------------------------------------------------------- the provider

async def test_an_environment_takes_the_whole_machine(node):
    vms, envs, server, docker, _ = node
    plan = await vms.plan("dev/a", ttl_minutes=60)
    assert (plan["server_type"], plan["location"], plan["price_hourly_eur"]) == ("own", "server", 0.0)
    assert plan["specs"]["cores"] == 8 and plan["specs"]["memory_gb"] == 16.0  # the machine's own, not a limit
    rec = await vms.create("dev/a", plan=plan)
    assert rec.state == "ready" and rec.provider == "own" and rec.location == "server"
    # the machine itself, reached directly as root, with the environment's own key and the pinned host key
    assert (rec.host, rec.port, rec.ssh_user, rec.via) == ("server.test", 22, "root", None)
    assert rec.provider_id == f"server:agentos-{rec.id.replace('_', '-')}"
    lane = envs.get("dev/a")
    assert lane.remote.via is None and lane.remote.key_path == str(vms.dir / rec.id / "id_ed25519")
    assert (vms.dir / rec.id / "known_hosts").read_text() == f"agentos-{rec.id} {HOST_KEY}\n"
    held = whole.holder(server.held or "")
    assert held["labels"]["agentos-home"] == vms.home_id and held["labels"]["agentos-vm"] == rec.id
    public = (vms.dir / rec.id / "id_ed25519").exists() and server.keys
    assert public and len(server.keys) == 1 and server.keys[0].startswith("ssh-ed25519 ")
    assert all(c == "sh -s" for c in server.commands) and docker.calls == []  # as root, and no Docker anywhere
    # closing frees the machine: that environment's key goes, and its hold
    await vms.destroy("dev/a")
    assert server.held is None and server.keys == [] and len(server.released) == 1


async def test_a_whole_machine_holds_one_environment_at_a_time(node):
    vms, _, server, _, _ = node
    first = await vms.create("dev/a", ttl_minutes=60)
    prov = vms.provider()
    assert await prov.busy("server") == "dev.a" and await prov.busy("homebox") is None
    with pytest.raises(VmError) as e:
        await vms.create("dev/b", ttl_minutes=60)
    assert "one environment at a time" in e.value.message and "dev.a" in e.value.message
    assert whole.holder(server.held or "")["labels"]["agentos-vm"] == first.id and len(server.keys) == 1
    await vms.destroy("dev/a")
    assert await prov.busy("server") is None
    again = await vms.create("dev/b", ttl_minutes=60)
    assert again.state == "ready"


async def test_any_size_asked_for_gets_the_whole_machine(node):
    vms, _, _, _, _ = node
    types = {t["name"]: t for t in await vms.provider().server_types()}
    assert set(types) == {"own", "own-small", "own-medium", "own-large", "own-xlarge"}
    assert {(t["cores"], t["memory_gb"], t["disk_gb"]) for t in types.values()} == {(8, 16.0, 160)}
    rec = await vms.create("dev/a", size={"server_type": "own-small"}, ttl_minutes=60)
    assert rec.state == "ready" and rec.server_type == "own-small"
    # and it boots no image: the plain system is what an environment plans, never a snapshot by name
    assert await vms.provider().resolve_image("ubuntu-24.04") == "server//machine"
    with pytest.raises(VmError, match="boots no image"):
        await vms.provider().resolve_image("snapshot:dev")


async def test_a_whole_machine_has_no_snapshot_and_no_wall(node):
    vms, _, _, _, _ = node
    prov = vms.provider()
    assert prov.whole("server") and not prov.whole("homebox") and not prov.whole("gone")
    assert not capable_at(prov, "snapshot", "server") and not capable_at(prov, "network_policy", "server")
    assert capable_at(prov, "snapshot", "homebox") and capable_at(prov, "network_policy", "homebox")
    rec = await vms.create("dev/a", ttl_minutes=60)
    with pytest.raises(VmError) as e:
        await vms.snapshot(rec.id, description="s", labels={"agentos-save": "s1"})
    assert "no disk snapshot" in e.value.message
    assert await vms.list_images({"agentos-home": vms.home_id}) == []


async def test_a_whole_machine_that_does_not_answer_is_never_read_as_free_or_deleted(node):
    vms, _, server, _, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    server.down = True
    prov = vms.provider()
    for call in (prov.get_server(rec.provider_id), prov.list_servers({}), prov.busy("server")):
        with pytest.raises(VmError) as e:
            await call
        assert e.value.provider_code == "unreachable" and "did not answer" in e.value.message
    with pytest.raises(VmError):
        await vms.reap_once()
    assert vms.store.get(rec.id).state == "ready"  # the reaper ends nothing it cannot see
    server.down = False
    out = await vms.reap_once()
    assert out["gone"] == [] and out["orphans"] == [] and vms.store.get(rec.id).state == "ready"
    assert (await prov.get_server(rec.provider_id)).status == "running"


async def test_a_hold_this_node_does_not_know_is_not_another_environments_to_end(node):
    vms, _, server, _, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    prov = vms.provider()
    await prov.delete_server("server:agentos-vm-other")  # not the one that holds it: nothing happens
    assert server.held is not None and server.released == []
    assert await prov.get_server("server:agentos-vm-other") is None
    assert [s.id for s in await prov.list_servers({"agentos-home": vms.home_id})] == [rec.provider_id]
    assert await prov.list_servers({"agentos-home": "another-home"}) == []


async def test_a_hold_of_another_openajar_home_is_never_released_by_its_id(node):
    """S5b-05, S5b-06 on a whole machine: a release empties /work and ends what runs there. The machine says who
    holds it, and a delete by an id that matches is still refused unless the hold carries this home's label."""
    import json

    vms, _, server, _, _ = node
    prov = vms.provider()
    server.held = json.dumps({"id": "agentos-vm-theirs", "name": "agentos-vm-theirs", "key": "ssh-ed25519 " + "C" * 68,
                              "labels": {"agentos-home": "someoneelse", "agentos-vm": "vm_theirs"}})
    with pytest.raises(VmError, match="another openajar home's") as e:
        await prov.delete_server("server:agentos-vm-theirs")
    assert e.value.provider_code == "not_this_home" and server.released == [] and server.held is not None


async def test_both_ways_work_side_by_side(node):
    from openajar.environments.machines_cli import use_machine

    vms, _, server, docker, home = node
    on_server = await vms.create("dev/a", ttl_minutes=60)
    use_machine(home, "homebox")  # `machine use homebox`: new environments open there, as containers
    in_container = await vms.create("dev/b", ttl_minutes=60)
    assert on_server.via is None and in_container.via is not None and in_container.via["host"] == "homebox.test"
    assert len(docker.containers) == 1 and server.held is not None
    found = await vms.provider().list_servers({"agentos-home": vms.home_id})
    assert {s.id for s in found} == {on_server.provider_id, in_container.provider_id}
    await vms.destroy("dev/a")
    await vms.destroy("dev/b")
    assert docker.containers == {} and server.held is None


async def test_remove_frees_a_whole_machine_and_takes_openajars_folder(node):
    vms, _, server, _, _ = node
    await vms.create("dev/a", ttl_minutes=60)
    side = vms.provider().on("server")
    found = await side.remove_mine({"agentos-home": vms.home_id})
    assert len(found["containers"]) == 1 and server.held is None
    await side.forget()
    assert server.forgotten


def test_a_sudo_login_runs_openajars_scripts_through_sudo(machines):
    home, server, _ = machines
    m = own.load(home, "server")
    m.sudo = True
    side = whole.WholeMachine(home, m, VmSettings(provider="own"))
    import asyncio

    assert asyncio.run(side.held()) is None and server.commands == ["sudo -n sh -s"]


def test_an_overdue_whole_machine_names_the_command_that_frees_it():
    from openajar.vms.models import VmRecord
    from openajar.vms.provider import by_hand_words

    now = utcnow()
    rec = VmRecord(id="vm_ab12", provider="own", provider_id="server:agentos-vm-ab12", lane_id="env/a", node="n",
                   profile="small", server_type="own", location="server", image="x", created_at=now, expires_at=now)
    assert "machine check server --free" in by_hand_words(rec) and "docker" not in by_hand_words(rec)


# ---------------------------------------------------------------- Docker installed for a machine of containers

def _no_docker(**over) -> own.Facts:
    return own.Facts(**{"system": "Linux", "arch": "x86_64", "user": "me", "uid": 1000, "root": True, **over})


def test_docker_is_installed_only_where_openajar_can_be_root():
    assert own.cannot_install_docker(_no_docker()) is None
    assert own.cannot_install_docker(_no_docker(user="root", uid=0)) is None
    assert "no passwordless sudo" in own.cannot_install_docker(_no_docker(root=False))
    assert own.cannot_install_docker(_no_docker(docker_found=True)) == "nothing to install"
    assert own.cannot_install_docker(_no_docker(system="Darwin")) == "nothing to install"


async def test_dockers_own_script_runs_as_root_and_the_login_joins_its_group(machines, monkeypatch):
    home, _, _ = machines
    seen: list[tuple[str, bytes]] = []

    async def run(_home, m, command, *, stdin=None, timeout_s=0):
        seen.append((command, stdin or b""))
        return 0, "Docker version 27.5.1, build 9f9e405\n", ""

    monkeypatch.setattr(own, "run", run)
    m = own.load(home, "homebox")
    assert await own.install_docker(home, m, _no_docker()) == "Docker version 27.5.1, build 9f9e405"
    assert seen[0][0] == "sudo -n sh -s -- me" and b"https://get.docker.com" in seen[0][1]
    assert b'usermod -aG docker "$1"' in seen[0][1]
    await own.install_docker(home, m, _no_docker(user="root", uid=0))
    assert seen[1][0] == "sh -s -- root"


async def test_a_failed_install_says_what_the_script_said(machines, monkeypatch):
    home, _, _ = machines

    async def run(_home, m, command, *, stdin=None, timeout_s=0):
        return 1, "", "E: Unable to locate package docker-ce\n"

    monkeypatch.setattr(own, "run", run)
    with pytest.raises(VmError) as e:
        await own.install_docker(home, own.load(home, "homebox"), _no_docker())
    assert "Unable to locate package docker-ce" in e.value.message


def test_docker_is_never_installed_without_a_yes(machines, monkeypatch):
    from openajar.environments import machines_cli

    home, _, _ = machines
    machines_cli.set_home(home)
    ran: list[str] = []

    async def install(_home, m, f):
        ran.append(m.name)
        return "Docker version 27.5.1"

    monkeypatch.setattr(own, "install_docker", install)
    m = own.load(home, "homebox")
    try:
        assert machines_cli._install_docker(m, _no_docker(), None) is False  # not at a terminal: never on its own
        assert machines_cli._install_docker(m, _no_docker(), False) is False
        assert machines_cli._install_docker(m, _no_docker(root=False), True) is False and ran == []
        assert machines_cli._install_docker(m, _no_docker(), True) is True and ran == ["homebox"]
        assert own.load(home, "homebox").installed_docker
    finally:
        machines_cli.set_home(None)
