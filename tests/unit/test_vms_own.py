"""Your own machine as the computer of environments (openajar.vms.own), without a machine: the ssh hop,
what `machine add` reads and refuses, and the provider's routing over a fake Docker that answers the
docker commands the provider sends over ssh. tests/unit/test_environments_own_docker.py runs the same
against a real machine (a container with its own sshd and Docker)."""

from __future__ import annotations

import json
import shlex
from datetime import timedelta

import pytest

from openajar.core.errors import InvalidRequest
from openajar.core.models import utcnow
from openajar.core.runtime import Runtime
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.models import RemoteHost, RemoteVia
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher
from openajar.vms import own, providers, ssh
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmSettings
from openajar.vms.provider import NOT_OURS

GB = 2**30


class FakeDocker:
    """One machine's Docker, as far as the provider uses it: answers `docker ...` command lines."""

    def __init__(self, cpus: int = 8, memory_gb: int = 16):
        self.cpus, self.memory = cpus, memory_gb * GB
        self.images: dict[str, dict[str, str]] = {"agentos-env:latest": {"org.agentos.environment": "2"}}
        self.containers: dict[str, dict] = {}
        self.networks: dict[str, dict[str, str]] = {}
        self.down = False
        self.calls: list[list[str]] = []
        self._n = 0

    def run(self, args: list[str], stdin: bytes | None) -> tuple[int, str, str]:
        if self.down:
            return 255, "", "ssh: connect to host 192.0.2.10 port 22: Connection refused"
        self.calls.append(args)
        handler = getattr(self, f"_{args[0]}", None)
        if handler is None:
            return 125, "", f"unknown docker command {args[0]}"
        return handler(args[1:], stdin)

    @staticmethod
    def _labels(args: list[str], flag: str = "--label") -> dict[str, str]:
        out = {}
        for i, a in enumerate(args):
            if a == flag and i + 1 < len(args):
                k, _, v = args[i + 1].removeprefix("label=").partition("=")
                out[k] = v
        return out

    def _match(self, labels: dict[str, str], args: list[str]) -> bool:
        return all(labels.get(k) == v for k, v in self._labels(args, "--filter").items())

    def _info(self, args, stdin):
        return 0, json.dumps({"NCPU": self.cpus, "MemTotal": self.memory, "Architecture": "x86_64",
                              "ServerVersion": "27.5.1", "OSType": "linux"}) + "\n", ""

    def _image(self, args, stdin):
        name = args[-1]
        if args[0] == "ls":
            return 0, (name + "\n") if name in self.images else "", ""
        if name not in self.images:
            return 1, "", f"Error: No such image: {name}"
        if "{{.Size}}" in args:
            return 0, f"{3 * GB}\n", ""
        if "{{json .Config.Labels}}" in args:
            return 0, json.dumps(self.images[name]) + "\n", ""
        return 0, self.images[name].get("org.agentos.environment", "") + "\n", ""

    def _network(self, args, stdin):
        if args[0] == "create":
            self.networks[args[-1]] = self._labels(args)
            return 0, args[-1] + "\n", ""
        if args[0] == "rm":
            self.networks.pop(args[-1], None)
            return 0, "", ""
        if args[0] == "ls":
            return 0, "".join(n + "\n" for n, lab in self.networks.items() if self._match(lab, args)), ""
        used = sum(1 for c in self.containers.values() if c["network"] == args[-1])
        return 0, f"{used}\n", ""

    def _create(self, args, stdin):
        self._n += 1
        cid = f"{self._n:012x}"
        name = args[args.index("--name") + 1]
        self.containers[cid] = {"name": name, "labels": self._labels(args), "running": False, "image": args[-1],
                                "network": args[args.index("--network") + 1], "port": 40000 + self._n}
        return 0, cid + "\n", ""

    def _cp(self, args, stdin):
        return 0, "", ""

    def _start(self, args, stdin):
        self.containers[args[-1]]["running"] = True
        return 0, "", ""

    def _logs(self, args, stdin):
        return 0, "", ""

    def _inspect(self, args, stdin):
        out = []
        for cid in args:
            c = self.containers.get(cid)
            if c is not None:
                out.append({"Id": cid, "Name": "/" + c["name"], "Created": utcnow().isoformat(),
                            "Config": {"Labels": c["labels"]},
                            "State": {"Running": c["running"], "Status": "running" if c["running"] else "exited"},
                            "HostConfig": {"NetworkMode": c["network"]},
                            "NetworkSettings": {"Ports": {"22/tcp": [{"HostIp": "127.0.0.1",
                                                                      "HostPort": str(c["port"])}]}}})
        missing = [cid for cid in args if cid not in self.containers]  # docker's own words, as it says them
        return (1 if missing else 0), json.dumps(out), "".join(f"Error: No such object: {c}\n" for c in missing)

    def _ps(self, args, stdin):
        return 0, "".join(cid + "\n" for cid, c in self.containers.items() if self._match(c["labels"], args)), ""

    def _rm(self, args, stdin):
        for cid in [a for a in args if not a.startswith("-")]:
            self.containers.pop(cid, None)
        return 0, "", ""

    def _commit(self, args, stdin):
        labels = dict(self.images.get(self.containers[args[-2]]["image"], {}))  # a commit keeps the image's labels
        for i, a in enumerate(args):
            if a == "--change":
                k, _, v = args[i + 1].removeprefix("LABEL ").partition("=")
                labels[k] = v
        self.images[args[-1]] = labels
        return 0, "sha256:" + "ab" * 32 + "\n", ""

    def _images(self, args, stdin):
        return 0, "".join(f"{n}\t2026-11-25 10:00:00 +0000 UTC\n" for n, lab in self.images.items()
                          if self._match(lab, args) and self._labels(args, "--filter")), ""

    def _rmi(self, args, stdin):
        for name in [a for a in args if not a.startswith("-")]:
            self.images.pop(name, None)
        return 0, "", ""


@pytest.fixture
def machines(tmp_path, monkeypatch):
    """Two registered machines, each with a fake Docker behind `own.run`."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    dockers = {"homebox": FakeDocker(), "attic": FakeDocker(cpus=2, memory_gb=4)}
    for name in dockers:
        own.save(home, own.Machine(name=name, host=f"{name}.test", user="me"))
        own.make_key(home, name)
        own.pin(home, name, ["ssh-ed25519 " + "A" * 68])  # a made-up host key, put together here

    async def run(_home, m, command, *, stdin=None, timeout_s=0):
        args = shlex.split(command)
        assert args[:len(m.docker)] == m.docker, command
        return dockers[m.name].run(args[len(m.docker):], stdin)

    monkeypatch.setattr(own, "run", run)
    return home, dockers


async def ok_probe(remote, *, timeout_s=15):
    return True, ""


@pytest.fixture
async def node(machines):
    home, dockers = machines
    envs = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    runtime = Runtime(envs=envs)
    settings = VmSettings(provider="own", location="homebox")
    vms = VmManager(home, runtime=runtime, settings=settings, node_name="node-1", probe=ok_probe)
    runtime.register("vms", vms)
    yield vms, envs, dockers, home
    await vms.shutdown()
    await envs.shutdown()


# ---------------------------------------------------------------- the ssh hop

def _remote(via: RemoteVia | None) -> RemoteHost:
    return RemoteHost(vm_id="vm_1", host="127.0.0.1", port=40001, key_path="/h/vms/vm_1/id_ed25519",
                      known_hosts="/h/vms/vm_1/known_hosts", host_alias="agentos-vm_1", via=via)


def test_a_machine_behind_another_ssh_host_is_reached_through_it():
    via = RemoteVia(host="box.example.com", port=2222, user="me", key_path="/h/my keys/100%/id",
                    known_hosts="/h/kh", host_alias="openajar-machine-homebox")
    argv = ssh.ssh_base(_remote(via))
    assert argv[-2:] == ["-T", "root@127.0.0.1"]  # the tunnels cut the argv at its end, hop or not
    hop = next(a for a in argv if a.startswith("ProxyCommand="))
    assert argv[argv.index(hop) - 1] == "-o"
    words = shlex.split(hop.removeprefix("ProxyCommand="))
    assert words[:3] == ["ssh", "-F", "/dev/null"] and words[-3:] == ["-W", "%h:%p", "me@box.example.com"]
    # as strict as the machine's own connection: its key, its pinned host key, no agent, no password
    for need in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "StrictHostKeyChecking=yes",
                 "UserKnownHostsFile=/h/kh", "GlobalKnownHostsFile=/dev/null",
                 "HostKeyAlias=openajar-machine-homebox"):
        assert need in words, need
    assert words[words.index("-p") + 1] == "2222"
    assert words[words.index("-i") + 1] == "/h/my keys/100%%/id"  # ssh expands % in a ProxyCommand: doubled
    zoned = via.model_copy(update={"host": "fe80::1%eth0"})  # an IPv6 zone has a % of its own
    assert ssh.via_command(zoned).endswith(" -W %h:%p me@fe80::1%%eth0")
    assert not any(a.startswith("ProxyCommand=") for a in ssh.ssh_base(_remote(None)))


def test_the_tunnels_and_the_agents_own_ssh_keep_the_hop():
    from openajar.environments import sshaccess
    from openajar.environments.tunnels import git_tunnel_argv, tunnel_argv

    via = RemoteVia(host="box.example.com", user="me", key_path="/h/k", known_hosts="/h/kh", host_alias="a")
    remote = _remote(via)
    for argv in (tunnel_argv(remote, socks_path="/tmp/s", gateway_port=3129), git_tunnel_argv(remote, gateway_port=3129)):
        assert any(a.startswith("ProxyCommand=") for a in argv) and argv[-1] == "root@127.0.0.1"
        assert "ClearAllForwardings=yes" not in argv
    entry = sshaccess.entry("e7k2", remote, key="/h/ssh/e7k2/id", known_hosts="/h/ssh/e7k2/known_hosts")
    line = next(ln for ln in entry.splitlines() if ln.strip().startswith("ProxyCommand "))
    assert line.strip().endswith("-W %h:%p me@box.example.com")
    assert "ProxyCommand" not in sshaccess.entry("e7k2", _remote(None), key="/k", known_hosts="/kh")


# ---------------------------------------------------------------- what `machine add` reads

@pytest.mark.parametrize("text, want", [
    ("me@192.168.1.20", ("me", "192.168.1.20", None)),
    ("root@box.example.com:2222", ("root", "box.example.com", 2222)),
    ("homebox", (None, "homebox", None)),
    ("ssh://me@box:22", ("me", "box", 22)),
    ("me@[2001:db8::1]:2222", ("me", "2001:db8::1", 2222)),
    ("me@2001:db8::1", ("me", "2001:db8::1", None)),
])
def test_an_ssh_address_is_read_as_ssh_reads_it(text, want):
    assert own.parse_target(text) == want


@pytest.mark.parametrize("text", ["", "me@", "@box", "-oProxyCommand=x", "me@-box", "me@box extra", "me@box:99999"])
def test_a_bad_ssh_address_is_refused(text):
    with pytest.raises(InvalidRequest):
        own.parse_target(text)


def test_ids_carry_their_machine():
    assert own.split_image("homebox//agentos-env:save-ab12") == ("homebox", "agentos-env:save-ab12")
    assert own.split_image("agentos-env:latest") == (None, "agentos-env:latest")
    assert own.split_image("ghcr.io/me/agentos-env:1") == (None, "ghcr.io/me/agentos-env:1")
    assert own.split_server("homebox:0123456789ab") == ("homebox", "0123456789ab")
    assert own.split_server("12345") == (None, "12345")


def _said(**over) -> str:
    info = {"ServerVersion": "27.5.1", "NCPU": 4, "MemTotal": 8 * GB, "OSType": "linux", "SecurityOptions": []}
    info.update(over.pop("info", {}))
    lines = {"system": "Linux", "arch": "x86_64", "user": "me", "sshd_port": "22", "docker_found": "1",
             "docker": "docker", "info": json.dumps(info), "free_kb": str(40 * 2**20)}
    lines.update(over)
    return "".join(f"{k}={v}\n" for k, v in lines.items())


def test_a_good_machine_has_no_problems():
    facts = own.parse_check(_said())
    assert own.problems(facts) == []
    assert (facts.cores, facts.memory_gb, facts.free_gb, facts.docker, facts.sshd_port) == (4, 8.0, 40.0, ["docker"], 22)
    sudo = own.parse_check(_said(docker="sudo -n /usr/bin/docker"))
    assert sudo.docker == ["sudo", "-n", "/usr/bin/docker"] and own.problems(sudo) == []


@pytest.mark.parametrize("said, words", [
    (_said(system="Darwin"), "macOS"),
    (_said(arch="riscv64"), "x86-64 and arm64"),
    (_said(docker_found="", docker=""), "Docker is not installed there"),
    (_said(docker="", docker_error="permission denied while trying to connect"), "usermod -aG docker me"),
    (_said(info={"SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless"]}), "rootless"),
    (_said(info={"OSType": "windows"}), "windows containers"),
    (_said(free_kb=str(3 * 2**20)), "3 GB free"),
])
def test_what_a_machine_lacks_is_said_with_its_fix(said, words):
    wrong = own.problems(own.parse_check(said))
    assert len(wrong) == 1 and words in wrong[0], wrong


def test_podman_is_told_from_docker():
    said = "system=Linux\narch=x86_64\nuser=me\ndocker_found=1\ndocker=docker\ninfo=" + json.dumps({"host": {}}) + "\n"
    assert "Podman" in own.problems(own.parse_check(said))[0]


def test_the_key_openajar_installs_runs_commands_and_forwards_and_is_removed_alone():
    line = own.authorized_line("ssh-ed25519 AAAAKEY openajar-machine-homebox")
    assert line == "restrict,port-forwarding ssh-ed25519 AAAAKEY openajar-machine-homebox"
    script = own.remove_key_script("ssh-ed25519 AAAAKEY openajar-machine-homebox")
    assert "grep -vF AAAAKEY" in script and "authorized_keys" in script


def test_the_persons_own_ssh_installs_the_key_against_the_pinned_host_key(tmp_path):
    argv = own.install_key_argv(tmp_path, "homebox", "me", "192.168.1.20", 2222)
    assert argv[0] == "ssh" and "-F" not in argv and "BatchMode=yes" not in argv  # their config, their prompt
    assert "StrictHostKeyChecking=yes" in argv and "HostKeyAlias=openajar-machine-homebox" in argv
    assert f"UserKnownHostsFile={own.known_hosts_path(tmp_path, 'homebox')}" in argv
    assert argv[-2] == "me@192.168.1.20" and "authorized_keys" in argv[-1]


def test_a_machines_files_are_private_and_hold_no_secret_but_its_key(machines):
    home, _ = machines
    d = own.folder(home, "homebox")
    for name in (own.RECORD, own.KEY, own.KNOWN_HOSTS):
        assert oct((d / name).stat().st_mode & 0o777) == "0o600", name
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    record = (d / own.RECORD).read_text()
    assert "PRIVATE KEY" not in record and "password" not in record
    assert (d / own.KNOWN_HOSTS).read_text().split()[0] == "openajar-machine-homebox"
    with pytest.raises(InvalidRequest):
        own.folder(home, "../etc")


# ---------------------------------------------------------------- the provider

def test_the_own_provider_is_configured_by_a_machine_and_rents_nothing(machines, tmp_path):
    home, _ = machines
    settings = VmSettings(provider="own", location="homebox")
    prov = providers.make(home, settings)
    assert prov.name == "own" and prov.firewall is False and prov.closes_network is True
    assert providers.configured(home, settings) and prov.name in providers.FREE
    assert not providers.configured(tmp_path / "empty", settings)
    assert VmSettings(provider="own").location == "own"
    assert own.default_name(home, VmSettings(provider="own")) is None  # two machines, none named
    own.forget(home, "attic")
    assert own.default_name(home, VmSettings(provider="own")) == "homebox"  # the only one


async def test_an_environment_opens_as_a_container_on_the_default_machine(node):
    vms, envs, dockers, home = node
    plan = await vms.plan("dev/a", ttl_minutes=60)
    assert (plan["server_type"], plan["location"], plan["price_hourly_eur"]) == ("own", "homebox", 0.0)
    assert plan["specs"]["cores"] == 2 and plan["specs"]["memory_gb"] == 4.0  # vms.toml's docker_cpus, docker_memory_gb
    rec = await vms.create("dev/a", plan=plan)
    assert rec.state == "ready" and rec.provider == "own" and rec.location == "homebox"
    name, cid = own.split_server(rec.provider_id)
    assert name == "homebox" and cid in dockers["homebox"].containers and not dockers["attic"].containers
    made = dockers["homebox"].containers[cid]
    assert made["labels"]["agentos-home"] == vms.home_id and made["labels"]["agentos-vm"] == rec.id
    # the container's ssh is on the machine's loopback, reached through the machine with the machine's key
    assert (rec.host, rec.port) == ("127.0.0.1", made["port"])
    lane = envs.get("dev/a")
    assert lane.remote.via is not None and lane.remote.via.host == "homebox.test" and lane.remote.via.user == "me"
    assert lane.remote.via.key_path == str(own.folder(home, "homebox") / own.KEY)
    assert lane.remote.via.host_alias == "openajar-machine-homebox"
    assert lane.remote.key_path == str(vms.dir / rec.id / "id_ed25519")  # the container's own key, not the machine's
    create = next(c for c in dockers["homebox"].calls if c[0] == "create")
    assert create[create.index("-p") + 1] == "127.0.0.1::22" and "--privileged" not in create
    assert "--cap-drop" in create and "-v" not in create and "--volume" not in create  # nothing of the machine's inside
    # closing removes the container and its network there, and nothing else
    await vms.destroy("dev/a")
    assert dockers["homebox"].containers == {} and dockers["homebox"].networks == {}
    assert "agentos-env:latest" in dockers["homebox"].images


async def test_a_size_is_a_container_with_those_limits(node):
    vms, _, _, _ = node
    prov = vms.provider()
    assert prov.size_type("large") == "own-large"
    types = {t["name"]: t for t in await prov.server_types()}
    assert set(types) == {"own", "own-small", "own-medium", "own-large", "own-xlarge"}
    assert (types["own-large"]["cores"], types["own-large"]["memory_gb"]) == (8, 16.0)
    assert types["own-xlarge"]["cores"] == 8  # never more than the machine has
    plan = await vms.plan("dev/b", size={"server_type": "own-medium"})
    assert plan["specs"]["cores"] == 4 and plan["price_hourly_eur"] == 0.0


async def test_a_save_stays_on_its_machine_and_resumes_there(node, tmp_path):
    vms, _, dockers, home = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    snap = await vms.snapshot(rec.id, description="openajar save s1", labels={"agentos-save": "s1"})
    assert snap["id"] == "homebox//agentos-env:save-s1" and snap["gb"] == 3.0
    assert "agentos-env:save-s1" in dockers["homebox"].images
    assert [i["id"] for i in await vms.list_images({"agentos-save": "s1"})] == ["homebox//agentos-env:save-s1"]
    await vms.destroy("dev/a")
    # new environments now open on the other machine, and the save still resumes where its image is
    from openajar.environments.machines_cli import use_machine

    use_machine(home, "attic")
    assert (await vms.plan("dev/new"))["location"] == "attic"
    plan = await vms.plan("dev/back", image=snap["id"])
    assert plan["location"] == "homebox"
    back = await vms.create("dev/back", plan=plan)
    assert own.split_server(back.provider_id)[0] == "homebox" and not dockers["attic"].containers
    made = next(iter(dockers["homebox"].containers.values()))
    assert made["image"] == "agentos-env:save-s1"
    await vms.delete_image(snap["id"])
    assert "agentos-env:save-s1" not in dockers["homebox"].images and await vms.image(snap["id"]) is None


async def test_a_machine_that_does_not_answer_is_never_read_as_deleted(node):
    """Switched off, unplugged or out of reach: its environments wait. The reaper must not end them, and
    the provider must not say their containers are gone."""
    vms, envs, dockers, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    dockers["homebox"].down = True
    prov = vms.provider()
    with pytest.raises(VmError, match="did not answer over ssh") as err:
        await prov.get_server(rec.provider_id)
    assert err.value.provider_code == "unreachable" and "machine check homebox" in err.value.message
    with pytest.raises(VmError, match="did not answer"):
        await prov.list_servers({"agentos-home": vms.home_id})
    with pytest.raises(VmError):
        await vms.reap_once()
    assert vms.store.get(rec.id).state == "ready" and envs.has("dev/a")
    assert await vms.list_images({}) == []  # images: skipped, which only ever deletes fewer
    dockers["homebox"].down = False
    out = await vms.reap_once()
    assert out["gone"] == [] and out["orphans"] == [] and vms.store.get(rec.id).state == "ready"


async def test_the_reaper_sweeps_only_this_homes_containers(node):
    vms, _, dockers, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    box = dockers["homebox"]
    # the person's own container, another openajar home's, and one of ours that no record knows
    box.run(["create", "--name", "postgres", "--network", "bridge", "postgres:16"], None)
    box.run(["create", "--name", "agentos-vm-other", "--network", "n", "--label", "agentos-node=node-1", "--label",
             "agentos-home=someoneelse", "agentos-env:latest"], None)
    box.run(["create", "--name", "agentos-vm-lost", "--network", "n", "--label", "agentos-node=node-1", "--label",
             f"agentos-home={vms.home_id}", "agentos-env:latest"], None)
    out = await vms.reap_once(utcnow() + timedelta(minutes=30))
    assert len(out["orphans"]) == 1 and out["orphans"][0].startswith("homebox:")
    assert sorted(c["name"] for c in box.containers.values()) == sorted(
        ["postgres", "agentos-vm-other", f"agentos-{rec.id.replace('_', '-')}"])


async def test_a_removed_machines_environment_is_gone_not_stuck(node):
    vms, _, _, home = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    own.forget(home, "homebox")
    prov = vms.provider()
    assert await prov.get_server(rec.provider_id) is None
    await prov.delete_server(rec.provider_id)  # nothing to reach: no error
    assert await prov.get_image("homebox//agentos-env:save-x") is None
    with pytest.raises(VmError, match="no longer set up"):
        await prov.resolve_image("homebox//agentos-env:save-x")


async def test_no_machine_says_how_to_add_one(tmp_path):
    prov = own.OwnProvider(tmp_path, VmSettings(provider="own"))
    with pytest.raises(VmError, match="machine add NAME user@host"):
        await prov.check()


async def test_a_missing_or_old_image_names_the_command(node):
    vms, _, dockers, _ = node
    dockers["homebox"].images.clear()
    with pytest.raises(VmError, match="machine homebox: no image agentos-env:latest.*machine check homebox --image"):
        await vms.provider().check()
    dockers["homebox"].images["agentos-env:latest"] = {"org.agentos.environment": "1"}
    with pytest.raises(VmError, match="older than this openajar"):
        await vms.provider().check()
    assert "older" in await vms.provider().on("homebox").has_image()


async def test_remove_takes_away_only_what_this_home_made(node):
    vms, _, dockers, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    await vms.snapshot(rec.id, description="s", labels={"agentos-save": "s1"})
    box = dockers["homebox"]
    box.run(["create", "--name", "postgres", "--network", "bridge", "postgres:16"], None)
    box.images["postgres:16"] = {}
    docker = vms.provider().on("homebox")
    found = await docker.remove_mine({"agentos-home": vms.home_id})
    assert len(found["containers"]) == 1 and found["images"] == ["agentos-env:save-s1"]
    assert [c["name"] for c in box.containers.values()] == ["postgres"]
    assert set(box.images) == {"agentos-env:latest", "postgres:16"} and box.networks == {}


def test_an_overdue_machine_names_where_to_remove_it_by_hand():
    from openajar.vms.models import VmRecord
    from openajar.vms.provider import by_hand_words

    now = utcnow()
    base = {"lane_id": "env/a", "node": "n", "profile": "small", "server_type": "own", "location": "homebox",
            "image": "x", "created_at": now, "expires_at": now}
    mine = by_hand_words(VmRecord(id="vm_ab12", provider="own", provider_id="homebox:0123", **base))
    assert "on your machine homebox" in mine and "docker rm -f agentos-vm-ab12" in mine
    assert "Hetzner console" in by_hand_words(VmRecord(id="vm_cd34", provider="hetzner", **base))


# ---------------------------------------------------------------- the reapers on your own machine (F1)

async def test_a_container_that_stopped_with_its_machine_is_started_again_not_ended(node):
    """S1-04 on your own machine: the machine restarted, and its containers stopped with their disks whole.
    Exited read as gone, and the environment was ended unsaved; now its container is started again, on the
    loopback port docker gives it there, and the lane still reaches it through the machine."""
    vms, envs, dockers, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    box = dockers["homebox"]
    cid = own.split_server(rec.provider_id)[1]
    box.containers[cid].update(running=False, port=45678)  # stopped, and a new port once it starts
    prov = vms.provider()
    assert (await prov.get_server(rec.provider_id)).status == "exited"
    assert await vms.start_machine(rec.id) is True
    assert box.containers[cid]["running"] and len(box.containers) == 1
    fresh = vms.store.get(rec.id)
    assert (fresh.state, fresh.host, fresh.port) == ("ready", "127.0.0.1", 45678) and fresh.via == rec.via
    lane = envs.get("dev/a")
    assert lane.remote.port == 45678 and lane.remote.via is not None and lane.remote.via.host == "homebox.test"


async def test_a_container_of_your_machine_after_a_switch_away_is_lost_and_named_where_it_is(node, tmp_path):
    """S5b-05 with your own machine: after vms.toml switched to another provider, a record of a container on
    your machine is out of reach. Nothing is deleted through the new provider, and the owner is told where it
    is: on that machine, never "in its console"."""
    from openajar.vms.provider import FakeProvider

    vms, _envs, dockers, home = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    told: list[tuple[str, str]] = []

    async def notify(title, body, *, urgent=False):
        told.append((title, body))

    other = VmManager(home, runtime=vms.runtime, provider=FakeProvider(), settings=VmSettings(provider="hetzner"),
                      node_name="node-1", probe=ok_probe)
    other._notify = notify
    out = await other.reap_once()
    got = other.store.get(rec.id)
    assert out["lost"] == [rec.id] and got.lost and got.state == "deleted" and len(dockers["homebox"].containers) == 1
    assert len(told) == 1 and "your machine homebox" in told[0][1] and "console" not in told[0][1]
    assert "made with Your machine" not in got.delete_reason and "your own machine" in got.delete_reason
    await other.shutdown()


# ---------------------------------------------------------------- a delete needs positive evidence (F1)

NO_DAEMON = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"


async def test_docker_there_that_does_not_answer_is_an_error_never_no_such_container(node):
    """The 0.7.0 audit, S1-04, on your own machine: `docker inspect` with no daemon prints [] and exits 1, and
    the docker provider read that as "deleted outside openajar". Over ssh it is the same docker: only its own
    "No such ..." says a container or an image is gone, and any other failure decides nothing."""
    vms, _, dockers, _ = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    snap = await vms.snapshot(rec.id, description="s", labels={"agentos-save": "s1"})
    box, prov = dockers["homebox"], vms.provider()
    real = box.run
    box.run = lambda args, stdin: (1, "[]", NO_DAEMON)  # the machine answers, its Docker does not
    for call in (prov.get_server(rec.provider_id), prov.delete_server(rec.provider_id), prov.get_image(snap["id"]),
                 prov.delete_image(snap["id"])):
        with pytest.raises(VmError, match="Cannot connect"):
            await call
    with pytest.raises(VmError, match="Cannot connect"):
        await vms.reap_once()
    box.run = real
    cid = own.split_server(rec.provider_id)[1]
    assert vms.store.get(rec.id).state == "ready" and cid in box.containers and "agentos-env:save-s1" in box.images
    # docker's own words: gone, and a delete of what is gone is done
    box.containers.pop(cid)
    assert await prov.get_server(rec.provider_id) is None
    await prov.delete_server(rec.provider_id)
    box.images.pop("agentos-env:save-s1")
    assert await prov.get_image(snap["id"]) is None
    await prov.delete_image(snap["id"])


async def test_a_container_or_an_image_without_this_homes_label_is_never_deleted_there(node):
    """S5b-05, S5b-06: deletes went by bare id, so an id from a damaged or restored vms.db, or a save imported
    from another node, could remove a container or an image openajar of this home never made. Docker there reads
    the labels back first, and refuses without this home's; the record waits, and the owner hears of it."""
    vms, _, dockers, _ = node
    box, prov = dockers["homebox"], vms.provider()
    box.run(["create", "--name", "postgres", "--network", "bridge", "postgres:16"], None)
    box.run(["create", "--name", "agentos-vm-other", "--network", "n", "--label", "agentos-home=someoneelse",
             "agentos-env:latest"], None)
    pg, other = list(box.containers)
    box.images["postgres:16"] = {}
    box.images["agentos-env:save-theirs"] = {"agentos-home": "someoneelse"}
    for cid, words in ((pg, "not openajar's"), (other, "another openajar home's")):
        with pytest.raises(VmError, match=words) as e:
            await prov.delete_server(f"homebox:{cid}")
        assert e.value.provider_code == NOT_OURS and "remove it yourself" in e.value.message
    for image in ("postgres:16", "agentos-env:save-theirs"):
        with pytest.raises(VmError, match="does not delete it") as e:
            await prov.delete_image(f"homebox//{image}")
        assert e.value.provider_code == NOT_OURS
    # a record that names the person's container (a vms.db from another home's backup): refused, kept to retry
    rec = await vms.create("dev/a", ttl_minutes=60)
    rec.provider_id = f"homebox:{pg}"
    vms.store.put(rec)
    after = await vms._delete(rec, "time limit")
    assert after.state == "deleting" and "not openajar's" in (after.error or "")
    assert {c["name"] for c in box.containers.values()} >= {"postgres", "agentos-vm-other"}
    assert {"postgres:16", "agentos-env:save-theirs"} <= set(box.images)
    assert not any(c[0] in ("rm", "rmi") and (pg in c or other in c) for c in box.calls)


async def test_a_machine_whose_record_does_not_read_is_never_called_gone(node):
    """S2-01 on your own machine: a machine record that does not read (a hand edit, a newer version's) is not
    a machine `machine remove` took out. Its containers and images are not known to be gone: the provider says
    so with an error, and the reaper ends and forgets nothing there."""
    vms, envs, dockers, home = node
    rec = await vms.create("dev/a", ttl_minutes=60)
    snap = await vms.snapshot(rec.id, description="s", labels={"agentos-save": "s1"})
    (own.folder(home, "homebox") / own.RECORD).write_text("{ not json")
    prov = vms.provider()
    for call in (prov.get_server(rec.provider_id), prov.delete_server(rec.provider_id), prov.get_image(snap["id"]),
                 prov.delete_image(snap["id"])):
        with pytest.raises(VmError, match="cannot be read"):
            await call
    out = await vms.reap_once()  # its container is missing from the list: read back by id, which says nothing
    assert out["gone"] == [] and out["orphans"] == [] and vms.store.get(rec.id).state == "ready"
    assert len(dockers["homebox"].containers) == 1 and "agentos-env:save-s1" in dockers["homebox"].images
    assert envs.has("dev/a")


async def test_remove_never_says_it_removed_what_is_still_there(node):
    """`machine remove` took exit 1 of `docker rm` as removed (and said so). Only docker's "No such ..." is."""
    vms, _, dockers, _ = node
    await vms.create("dev/a", ttl_minutes=60)
    box = dockers["homebox"]
    real = box.run
    box.run = lambda args, stdin: (1, "", NO_DAEMON) if args[0] in ("rm", "rmi") else real(args, stdin)
    docker = vms.provider().on("homebox")
    with pytest.raises(VmError, match="Cannot connect"):
        await docker.remove_mine({"agentos-home": vms.home_id})
    assert len(box.containers) == 1
    box.run = real
    found = await docker.remove_mine({"agentos-home": vms.home_id})
    assert len(found["containers"]) == 1 and box.containers == {}


def test_remove_deletes_no_saved_image_while_a_save_does_not_read(machines, capsys):
    """S1-05 for `machine remove`: a save that does not read may keep its disk on that machine, and unread is
    not "none". Without --force the remove stops and names the saves folder; nothing there is touched."""
    import typer

    from openajar.environments import machines_cli
    from openajar.environments.saves import MANIFEST, SaveStore

    home, dockers = machines
    box = dockers["homebox"]
    box.images["agentos-env:save-s1"] = {"agentos-home": "x"}
    bad = SaveStore(home).root / "fam" / "title" / "s1abc"
    bad.mkdir(parents=True)
    (bad / MANIFEST).write_text("{ not json")
    machines_cli.set_home(home)
    try:
        with pytest.raises(typer.Exit):
            machines_cli.remove("homebox", force=False, keep_image=False)
    finally:
        machines_cli.set_home(None)
    said = capsys.readouterr().err
    assert "does not read" in said and str(SaveStore(home).root) in said and "--force" in said
    assert box.calls == [] and own.load(home, "homebox").name == "homebox"
