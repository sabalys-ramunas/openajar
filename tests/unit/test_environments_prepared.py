"""Prepared machines, the app started at open, and the warm pool (openajar.environments.repoconfig,
prepared, preparing, prepbuild and warmpool). The cloud is openajar.vms's FakeProvider and every command
on a machine is answered by a fake, as in test_environments.py; test_environments_docker.py and the
docker dev node run the real thing."""

from __future__ import annotations

import asyncio
import base64
import subprocess
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_policy_support import Clock, make_service

from openajar.core.models import Effect, utcnow
from openajar.dev.lanes import FakeLaneManager
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.models import ExecResult
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments import bringup, machine, prepbuild, repoconfig
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.environments.prepared import Prepared, PreparedStore
from openajar.environments.repoconfig import ConfigError, ConfigRead, blob_id, parse, prepared_key
from openajar.environments.tools import tool_specs as env_tool_specs
from openajar.policy.wrap import ToolCallError, bind_request_token, wrap_tool
from openajar.vms.manager import VmManager
from openajar.vms.models import VmSettings
from openajar.vms.provider import FakeProvider

REPO = "example.test/me/shop"
CONFIG = """
setup = "pnpm install --frozen-lockfile"
refresh = "pnpm install --prefer-offline"
start = "pnpm dev --host 0.0.0.0"
port = 3000
"""


# ------------------------------------------------------------------ the file


def test_the_file_parses_with_its_defaults():
    cfg = parse(CONFIG)
    assert cfg.setup == "pnpm install --frozen-lockfile" and cfg.port == 3000 and cfg.ready_timeout_s == 120
    assert cfg.inputs is None


@pytest.mark.parametrize("text, words", [
    ('setup = "x"\nstart_command = "y"\n', "start_command"),
    ('start = "pnpm dev"\n', "start needs port"),
    ('port = 3000\n', "port needs start"),
    ('refresh = "x"\n', "refresh needs setup"),
    ('setup = ""\n', "setup is empty"),
    ('setup = "x"\ninputs = ["../etc/passwd"]\n', "inputs"),
    ('setup = "x"\nport = "three"\nstart = "y"\n', "port"),
    ('setup = [\n', "not valid TOML"),
])
def test_a_bad_file_names_the_key_and_the_fix(text, words):
    with pytest.raises(ConfigError) as e:
        parse(text)
    assert words in str(e.value)
    assert ".openajar/environment.toml" in str(e.value)


def test_an_unknown_key_lists_the_keys_there_are():
    with pytest.raises(ConfigError) as e:
        parse('setup = "x"\nbuild = "y"\n')
    assert "build" in str(e.value) and "setup, refresh, start, port, ready_timeout_s, inputs" in str(e.value)


def test_the_default_inputs_are_the_lockfiles_the_repository_has():
    names = ["README.md", "pnpm-lock.yaml", "package.json", "requirements.txt", "requirements-dev.txt", "src"]
    assert repoconfig.wanted(None, names) == ["pnpm-lock.yaml", "requirements-dev.txt", "requirements.txt"]
    assert repoconfig.wanted(["web/package-lock.json"], names) == ["web/package-lock.json"]


def test_a_blob_id_is_gits_own(tmp_path):
    f = tmp_path / "lock"
    f.write_bytes(b"lockfileVersion: 9\n")
    want = subprocess.run(["git", "hash-object", str(f)], capture_output=True, text=True, check=True).stdout.strip()
    assert blob_id(f.read_bytes()) == want


def test_the_key_changes_with_what_decides_the_machine():
    cfg = parse(CONFIG)
    base = prepared_key(REPO, "snap-1", cfg, {"pnpm-lock.yaml": "a1"})
    assert base == prepared_key(REPO, "snap-1", parse(CONFIG), {"pnpm-lock.yaml": "a1"})
    assert base != prepared_key(REPO, "snap-2", cfg, {"pnpm-lock.yaml": "a1"})            # another base image
    assert base != prepared_key(REPO, "snap-1", cfg, {"pnpm-lock.yaml": "b2"})            # the lockfile changed
    assert base != prepared_key("example.test/me/other", "snap-1", cfg, {"pnpm-lock.yaml": "a1"})
    other = parse(CONFIG.replace("--prefer-offline", "--offline"))
    assert base != prepared_key(REPO, "snap-1", other, {"pnpm-lock.yaml": "a1"})           # refresh changed
    started = parse(CONFIG.replace("pnpm dev", "pnpm start"))
    assert base == prepared_key(REPO, "snap-1", started, {"pnpm-lock.yaml": "a1"})         # start is not on the image
    assert base != prepared_key(REPO, "snap-1", cfg, {"pnpm-lock.yaml": "a1"}, "agent/x")  # another ref's own
    assert base == prepared_key(REPO, "snap-1", cfg, {"pnpm-lock.yaml": "a1"}, None)         # the default branch


async def test_a_public_repository_is_read_from_githubs_raw_files(tmp_path):
    """No credential, at the ref, and the lockfile keyed by its git blob id."""
    asked = []
    lock = b"lockfileVersion: 9\n"

    async def fetch(url):
        asked.append(url)
        if url.endswith(".openajar/environment.toml"):
            return 200, CONFIG.encode()
        if url.endswith("/pnpm-lock.yaml"):
            return 200, lock
        return 404, b""

    mgr = EnvironmentManager(tmp_path, settings=EnvSettings())
    mgr.raw_fetch = fetch
    read = await mgr._read_github("me/shop", "feature/x")
    assert read.state == "found" and read.config.setup.startswith("pnpm install")
    assert read.inputs == {"pnpm-lock.yaml": blob_id(lock)}
    assert asked[0] == "https://raw.githubusercontent.com/me/shop/feature/x/.openajar/environment.toml"
    mgr.raw_fetch = lambda url: _answer(404)
    assert (await mgr._read_github("me/shop", None)).state == "absent"
    mgr.raw_fetch = lambda url: _answer(0)
    assert (await mgr._read_github("me/shop", None)).state == "unreadable"


async def _answer(status):
    return status, b""


async def test_a_declared_repository_is_read_from_the_nodes_mirror(tmp_path):
    """The mirror, at the ref: the file and the input blobs from git itself."""
    src = tmp_path / "src"
    run = lambda *a, cwd=src: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *a],
                                             cwd=cwd, check=True, capture_output=True)
    src.mkdir()
    run("init", "-q", "-b", "main")
    (src / ".openajar").mkdir()
    (src / ".openajar" / "environment.toml").write_text(CONFIG)
    (src / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    run("add", "-A")
    run("commit", "-q", "-m", "one")
    mirror_path = tmp_path / "mirror.git"
    run("clone", "-q", "--mirror", str(src), str(mirror_path), cwd=tmp_path)
    from openajar.git.upstream import Mirror

    mirror = Mirror(mirror_path, hooks_dir=tmp_path / "hooks")

    async def warm(lane, *, grant_id, repo):
        return True

    git = SimpleNamespace(warm=warm, gateway=SimpleNamespace(mirror=lambda remote: mirror))
    mgr = EnvironmentManager(tmp_path / "home", settings=EnvSettings())
    read = await mgr._read_mirror(git, "env/eabc", "owner", REPO, None)
    assert read.state == "found" and read.source == "mirror" and read.commit
    assert read.inputs == {"pnpm-lock.yaml": blob_id(b"lockfileVersion: 9\n")}
    assert (await mgr._read_mirror(git, "env/eabc", "owner", REPO, "no-such-branch")).state == "unreadable"
    # a mirror not made yet: its first fetch is not waited for (the file is read in the checkout)
    slow = asyncio.Event()

    async def cold_warm(lane, *, grant_id, repo):
        await slow.wait()

    cold = SimpleNamespace(warm=cold_warm, gateway=SimpleNamespace(
        mirror=lambda remote: Mirror(tmp_path / "none.git", hooks_dir=tmp_path / "hooks")))
    read = await asyncio.wait_for(mgr._read_mirror(cold, "env/eabc", "owner", REPO, None), 2)
    assert read.state == "unreadable" and "checkout" in read.why
    slow.set()


def _sh(cmd, cwd):
    return subprocess.run(["sh", "-c", cmd], cwd=cwd, capture_output=True, text=True, check=True).stdout


# legacy-name: begin (a repository that has the file where 0.4.0 read it, .agentos/environment.toml)
async def test_a_repository_with_the_file_under_its_old_name_is_read_too(tmp_path):
    """0.4.0 read .agentos/environment.toml: a repository that has it there still gets its prepared machine,
    from GitHub, from the node's mirror and from the checkout, and the new name wins when both are there."""
    async def fetch(url):
        if url.endswith("/.agentos/environment.toml"):
            return 200, CONFIG.encode()
        return 404, b""

    mgr = EnvironmentManager(tmp_path, settings=EnvSettings())
    mgr.raw_fetch = fetch
    assert (await mgr._read_github("me/shop", None)).state == "found"

    src = tmp_path / "src"
    run = lambda *a, cwd=src: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *a],
                                             cwd=cwd, check=True, capture_output=True, text=True)
    src.mkdir()
    run("init", "-q", "-b", "main")
    (src / ".agentos").mkdir()
    (src / ".agentos" / "environment.toml").write_text(CONFIG)
    run("add", "-A")
    run("commit", "-q", "-m", "one")
    mirror_path = tmp_path / "mirror.git"
    run("clone", "-q", "--mirror", str(src), str(mirror_path), cwd=tmp_path)
    from openajar.git.upstream import Mirror

    async def warm(lane, *, grant_id, repo):
        return True

    git = SimpleNamespace(warm=warm, gateway=SimpleNamespace(mirror=lambda remote: Mirror(mirror_path,
                                                                                         hooks_dir=tmp_path / "hooks")))
    read = await mgr._read_mirror(git, "env/eabc", "owner", REPO, None)
    assert read.state == "found" and read.config.setup.startswith("pnpm install")

    from openajar.environments.repoconfig import CHECKOUT_READ

    out = _sh(CHECKOUT_READ, src)
    assert out.startswith("config=") and not out.startswith("config=-")
    (src / ".openajar").mkdir()
    (src / ".openajar" / "environment.toml").write_text("setup = \"make\"\n")
    out = _sh(CHECKOUT_READ, src)
    import base64

    assert base64.b64decode(out.split("\n")[0][len("config="):]).decode() == "setup = \"make\"\n"
# legacy-name: end


# ------------------------------------------------------------------ the store


def test_the_store_keeps_records_and_the_last_image(tmp_path):
    store = PreparedStore(tmp_path)
    now = utcnow()
    store.put(Prepared(key="k1", repo=REPO, started_at=now))
    store.put(Prepared(key="k2", repo=REPO, started_at=now + timedelta(seconds=1)))
    assert [r.key for r in store.of_repo(REPO)] == ["k2", "k1"]
    store.update("k1", lambda r: setattr(r, "state", "ready"))
    assert store.get("k1").state == "ready"
    assert store.update("gone", lambda r: None) is None
    store.remove("k2")
    assert [r.key for r in store.all()] == ["k1"]
    assert store.last_image() is None
    store.set_last_image("snap-9")
    assert PreparedStore(tmp_path).last_image() == "snap-9"
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------ a node


class Machine:
    """The fake machines' answers (every VM, build machines and warm ones included)."""

    def __init__(self):
        self.ran: list[tuple[str, str, str | None]] = []
        self.setup_exit = 0
        self.token_left = ""
        self.ports = 'LISTEN 0 511 0.0.0.0:3000 0.0.0.0:* users:(("node",pid=7,fd=20))\n'

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None, oom_check=False):
        self.ran.append((lane_id, cmd, stdin))
        out, code = "ran\n", 0
        if cmd == machine.SETUP_STATE:
            out = "ready\ntools: pnpm\n"
        elif cmd == machine.SWAP_ON:
            out = "swap: none\n"
        elif "git clone" in cmd and lane_id.startswith("env/prep-"):
            out = "0123456789abcdef0123456789abcdef01234567\n"
        elif "git clone" in cmd:
            out = "main\n"
        elif cmd == prepbuild.SCRUB:
            out = "ok=1\n"
        elif cmd == prepbuild.TOKEN_LEFT:
            out = self.token_left
        elif cmd.startswith("bash -s --"):
            out = "branch=main\ncommit=fedcba9876543210\n"
        elif "bash -c" in cmd and "pnpm install --frozen-lockfile" in cmd:
            code = self.setup_exit
            out = "" if code == 0 else "ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with frozen-lockfile\n"
        elif cmd.startswith("ss "):
            out = self.ports
        return ExecResult(exit_code=code, stdout=out, stderr="", duration_s=0.01)

    def cmds(self, lane_prefix: str = "") -> list[str]:
        return [c for lane, c, _ in self.ran if lane.startswith(lane_prefix)]


class FakeTunnel:
    def __init__(self, remote, *, gateway_port, socks_port=None, **kw):
        self.remote, self.gateway_port, self.socks_port = remote, gateway_port, socks_port or 41080
        self.alive = False
        self.socks_path = None

    async def start(self):
        self.alive = True

    async def stop(self):
        self.alive = False


class FakeGit:
    """The git gateway's side for a declared repository: a listener, one token, and its revocation."""

    def __init__(self):
        self.tokens: dict[str, str] = {}
        self.down: list[str] = []
        self.remotes = SimpleNamespace(find=lambda remote, scopes: object())

    async def vm_listener(self, lane):
        return 45000

    def vm_token(self, lane, *, grant_id, repo, fetch_only=False):
        self.tokens[lane] = f"agt_{lane.replace('/', '_')}_secret_token_value"
        return self.tokens[lane]

    async def vm_down(self, lane):
        self.down.append(lane)

    async def warm(self, lane, *, grant_id, repo):
        return True


async def readable_repo(url, env, *, timeout_s=20, allow_file=False):
    return True, b""


async def ok_probe(remote, *, timeout_s=15):
    return True, ""


@pytest.fixture
async def node(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    policy, rt = make_service(home, clock=Clock())
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(), settings=TermSettings(proxy_port=0))
    fake = Machine()
    monkeypatch.setattr(envs, "exec", fake.exec)
    jobs: list[SimpleNamespace] = []
    listens = {"yes": True}

    async def job_start(lane_id, cmd, *, cwd=None, by=None, grant_id=None, **kw):
        job = SimpleNamespace(id=f"job{len(jobs) + 1}", lane=lane_id, cmd=cmd, cwd=cwd, state="running", exit_code=None,
                              stdout_bytes=40, stderr_bytes=0)
        jobs.append(job)
        return job

    async def job_wait_port(job_id, port, wait_s):
        return listens["yes"]

    async def job_log(job_id, stream, offset, n):
        return b"Error: listen EADDRINUSE\n" if stream == "stdout" else b""

    monkeypatch.setattr(envs, "job_start", job_start)
    monkeypatch.setattr(envs, "job_wait_port", job_wait_port)
    monkeypatch.setattr(envs, "job_log", job_log)
    monkeypatch.setattr(envs, "job_list", lambda lane=None: [j for j in jobs if lane in (None, j.lane)])
    rt.register("envs", envs)
    prov = FakeProvider()
    vms = VmManager(home, runtime=rt, provider=prov, settings=VmSettings(ssh_from=["198.51.100.7/32"]),
                    node_name="node-1", probe=ok_probe)
    rt.register("vms", vms)
    rt.register("lanes", FakeLaneManager())
    monkeypatch.setattr(bringup, "Tunnel", FakeTunnel)
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings())
    mgr.repo_probe = readable_repo
    mgr.slot_poll_s, mgr.slot_wait_s, mgr.ready_wait_s = 0.01, 0.05, 0.05
    reads = {"text": CONFIG, "lock": "a1"}

    async def config_probe(repo, ref):
        if reads["text"] is None:
            return ConfigRead("absent", source="test")
        read = repoconfig.from_text(reads["text"], source="test", commit="c0ffee")
        read.inputs = {"pnpm-lock.yaml": reads["lock"]}
        return read

    mgr.config_probe = config_probe
    rt.register("environments", mgr)
    _grant, token = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                specials={"vm": {}, "exec": {}})
    calls = {s.name: wrap_tool(s, rt) for s in env_tool_specs(rt)}

    async def call(name, *, session="chat-1", tok=token, **kw):
        with bind_request_token(tok, session=session):
            return await calls[name](**kw)

    yield SimpleNamespace(rt=rt, policy=policy, mgr=mgr, vms=vms, prov=prov, machine=fake, envs=envs, call=call,
                          jobs=jobs, listens=listens, reads=reads, token=token)
    await mgr.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    policy.close()


async def builds_done(mgr):
    for t in list(mgr._builds().values()):
        await t


# ------------------------------------------------------------------ open, build, open again


async def test_first_open_runs_setup_starts_the_app_and_builds_for_next_time(node):
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready", out
    p = out["prepared"]
    assert p["used"] is False and p["setup"]["ok"] is True and p["build"] == {"started": True, "key": p["build"]["key"]}
    assert "no prepared machine" in p["why"]
    # setup ran in the repository's folder on the agent's own machine
    env_lane = f"env/{out['id']}"
    assert any("cd /work/shop && CI=true" in c and "pnpm install --frozen-lockfile" in c for c in node.machine.cmds(env_lane))
    # the app is a job of the environment's lane, listening, with its link
    app = out["app"]
    assert app["listening"] is True and app["port"] == 3000 and app["job_id"] == "job1"
    assert node.jobs[0].lane == env_lane and node.jobs[0].cwd == "/work/shop" and node.jobs[0].cmd == "pnpm dev --host 0.0.0.0"
    assert "The app runs already" in out["next"]
    await builds_done(node.mgr)
    [rec] = node.mgr.prepared_store().all()
    assert rec.state == "ready" and rec.image.startswith("snap-") and rec.commit.startswith("0123456789ab")
    assert rec.repo == REPO and rec.setup_s is not None and rec.build_vm is None
    # the build machine was its own, and is gone
    build = [r for r in node.vms.store.recent(20) if r.lane_id.startswith("env/prep-")]
    assert len(build) == 1 and build[0].state == "deleted" and build[0].environment is None
    assert node.prov.snapshots[rec.image]["agentos-prepared"] == rec.key[:16]


async def test_second_open_boots_the_prepared_machine_skips_setup_and_refreshes(node):
    first = await node.call("environment_open", title="Shop", repo=REPO)
    await builds_done(node.mgr)
    await node.call("environment_close", on=first["on"], discard=True)
    [rec] = node.mgr.prepared_store().all()
    node.machine.ran.clear()
    out = await node.call("environment_open", title="Shop again", repo=REPO, session="chat-2")
    assert out["state"] == "ready", out
    p = out["prepared"]
    assert p["used"] is True and p["from"]["commit"] == rec.commit[:12] and "age" in p["from"]
    assert p["checkout"]["ok"] is True and p["refresh"]["ok"] is True
    assert p["setup_s"]["build"] == rec.setup_s
    lane = f"env/{out['id']}"
    cmds = node.machine.cmds(lane)
    assert not any("--frozen-lockfile" in c for c in cmds)            # no setup
    assert any("--prefer-offline" in c for c in cmds)                 # refresh
    env = node.mgr.get(out["id"])
    assert env.image == rec.image and node.vms.store.get(env.vm_id).image == rec.image
    assert out["app"]["listening"] is True
    assert node.mgr.prepared_store().get(rec.key).uses == 1
    assert not node.mgr._builds()  # nothing to build: it is ready


async def test_an_image_deleted_outside_openajar_is_forgotten_and_built_again(node):
    await node.call("environment_open", title="Shop", repo=REPO)
    await builds_done(node.mgr)
    [rec] = node.mgr.prepared_store().all()
    await node.prov.delete_image(rec.image)
    out = await node.call("environment_open", title="Shop 2", repo=REPO, session="chat-2")
    assert out["state"] == "ready", out
    assert out["prepared"]["used"] is False and "image is gone" in out["prepared"]["why"]
    assert out["prepared"]["setup"]["ok"] is True and out["prepared"]["build"]["started"] is True
    await builds_done(node.mgr)
    [again] = node.mgr.prepared_store().all()
    assert again.state == "ready" and again.image != rec.image


async def test_a_new_lockfile_misses_the_prepared_machine(node):
    await node.call("environment_open", title="Shop", repo=REPO)
    await builds_done(node.mgr)
    node.reads["lock"] = "b2"
    out = await node.call("environment_open", title="Shop 2", repo=REPO, session="chat-2")
    assert out["prepared"]["used"] is False and out["prepared"]["setup"]["ok"] is True
    await builds_done(node.mgr)
    # the newer build replaced the older one of the repository
    [rec] = [r for r in node.mgr.prepared_store().all() if r.state == "ready"]
    assert rec.key == out["prepared"]["build"]["key"]
    assert len(node.prov.snapshots) == 1


async def test_a_failed_setup_never_fails_the_open(node):
    node.machine.setup_exit = 1
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready"
    p = out["prepared"]
    assert p["setup"]["ok"] is False and p["setup"]["exit_code"] == 1 and "frozen-lockfile" in p["setup"]["tail"]
    assert p["build"]["started"] is False and "setup failed" in p["build"]["why"]
    assert not node.mgr.prepared_store().all()


async def test_an_app_that_does_not_listen_says_why(node):
    node.listens["yes"] = False
    node.machine.ports = 'LISTEN 0 511 0.0.0.0:5173 0.0.0.0:* users:(("node",pid=7,fd=20))\n'
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready"
    app = out["app"]
    assert app["listening"] is False and "port 3000 did not listen" in app["why"]
    assert "EADDRINUSE" in app["log_tail"] and app["listening_instead"] == [5173]
    assert "terminal_job_read" in app["next"] and "app did not come up" in out["next"]


async def test_no_file_changes_nothing_and_says_how_to_add_one(node):
    node.reads["text"] = None
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready" and "app" not in out
    assert out["prepared"]["used"] is False and ".openajar/environment.toml" in out["prepared"]["why"]
    assert not any("bash -c" in c for c in node.machine.cmds("env/"))
    assert not node.mgr._builds()


async def test_a_bad_file_says_so(node):
    node.reads["text"] = 'setup = "x"\ncommand = "y"\n'
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready" and "command" in out["prepared"]["why"]


async def test_environment_shows_prepared_and_app(node):
    out = await node.call("environment_open", title="Shop", repo=REPO)
    got = await node.call("environment", on=out["on"])
    assert got["prepared"]["setup"]["ok"] is True and got["app"]["job_id"] == "job1"
    assert "environment_app_restart" in got["app"]["restart"]


# ------------------------------------------------------------------ restarting the app


def cancels(node, monkeypatch, *, frees_port=True):
    """job_cancel ends the job (its process group); the port closes with it unless `frees_port` is false."""
    stopped = []

    async def job_cancel(job_id):
        job = next(j for j in node.jobs if j.id == job_id)
        job.state = "cancelled"
        stopped.append(job_id)
        if frees_port:
            node.machine.ports = ""
        return job

    monkeypatch.setattr(node.envs, "job_cancel", job_cancel)
    node.mgr.app_free_wait_s = 0.05
    return stopped


async def test_the_app_restarts_as_the_open_started_it(node, monkeypatch):
    stopped = cancels(node, monkeypatch)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    # a second copy the agent started by hand runs the same command: it goes too
    hand = await node.envs.job_start(f"env/{out['id']}", "pnpm dev --host 0.0.0.0", cwd="/work/shop")
    got = await node.call("environment_app_restart", on=out["on"])
    app = got["app"]
    assert stopped == ["job1", hand.id] and app["stopped"] == ["job1", hand.id]
    assert app["job_id"] == "job3" and app["listening"] is True and app["port"] == 3000
    assert node.jobs[-1].cmd == "pnpm dev --host 0.0.0.0" and node.jobs[-1].cwd == "/work/shop"
    assert "runs again as job job3" in got["next"] and "—" not in got["next"]
    assert (await node.call("environment", on=out["on"]))["app"]["job_id"] == "job3"


async def test_a_restart_reads_the_file_as_it_is_now(node, monkeypatch):
    cancels(node, monkeypatch)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    text = 'start = "pnpm dev --port 3001"\nport = 3001\n'
    real = node.machine.exec

    async def exec_(lane_id, cmd, **kw):
        if "config=" in cmd and lane_id == f"env/{out['id']}":
            return ExecResult(exit_code=0, stdout=f"config={base64.b64encode(text.encode()).decode()}\n", stderr="",
                              duration_s=0.01)
        return await real(lane_id, cmd, **kw)

    monkeypatch.setattr(node.envs, "exec", exec_)
    app = (await node.call("environment_app_restart", on=out["on"]))["app"]
    assert app["command"] == "pnpm dev --port 3001" and app["port"] == 3001 and node.jobs[-1].cmd == "pnpm dev --port 3001"

    text = 'start = "pnpm dev"\n'  # start without port
    with pytest.raises(ToolCallError, match="start needs port"):
        await node.call("environment_app_restart", on=out["on"])


async def test_a_port_the_old_app_still_holds_is_said(node, monkeypatch):
    cancels(node, monkeypatch, frees_port=False)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    app = (await node.call("environment_app_restart", on=out["on"]))["app"]
    assert "port 3000 still listened after the old app stopped (node)" in app["why"] and "fuser -k 3000/tcp" in app["why"]


async def test_no_start_command_no_restart(node, monkeypatch):
    cancels(node, monkeypatch)
    node.reads["text"] = None
    out = await node.call("environment_open", title="Shop", repo=REPO)
    with pytest.raises(ToolCallError, match="no start command"):
        await node.call("environment_app_restart", on=out["on"])


# ------------------------------------------------------------------ limits


async def test_a_build_needs_room_on_the_node(node):
    node.mgr.settings = EnvSettings(max_live=1)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready"
    build = out["prepared"]["build"]
    assert build["started"] is False and "no room for a build now" in build["why"] and "--max-live" in build["why"]
    assert not node.mgr.prepared_store().all()


async def test_a_build_and_a_warm_machine_count_against_the_budget(node):
    node.mgr.settings = EnvSettings(monthly_eur=10.0)
    before = node.mgr.booked_eur()
    plan = await node.vms.plan("env/prep-x1", ttl_minutes=90)
    await node.vms.create("env/prep-x1", plan=plan, wait_s=0)
    assert node.mgr.booked_eur() == pytest.approx(before + 2 * plan["price_hourly_eur"])


async def test_builds_can_be_turned_off(node):
    node.mgr.settings = EnvSettings(prepared_builds=False)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["prepared"]["build"]["started"] is False and "prepared_builds" in out["prepared"]["build"]["why"]


# ------------------------------------------------------------------ no secret in a snapshot


async def test_a_declared_repositorys_build_leaves_no_token_in_the_snapshot(node, monkeypatch):
    git = FakeGit()
    node.rt.register("git", git)
    order: list[str] = []
    real_exec = node.machine.exec

    async def exec_(lane_id, cmd, **kw):
        if lane_id.startswith("env/prep-"):
            order.append("scrub" if cmd == prepbuild.SCRUB else "check" if cmd == prepbuild.TOKEN_LEFT else
                         "git setup" if cmd == bringup.GIT_SETUP else cmd[:20])
            if cmd == prepbuild.TOKEN_LEFT:
                assert kw["stdin"].strip() == git.tokens[lane_id]  # the token goes on stdin, never in argv
            if cmd == bringup.GIT_SETUP:
                assert git.tokens[lane_id] in kw["stdin"] and git.tokens[lane_id] not in cmd
        return await real_exec(lane_id, cmd, **kw)

    monkeypatch.setattr(node.envs, "exec", exec_)
    snap = node.prov.create_snapshot

    async def create_snapshot(server_id, description, labels):
        order.append("snapshot")
        assert [lane for lane in git.down if lane.startswith("env/prep-")], "the token is revoked before the snapshot"
        return await snap(server_id, description, labels)

    monkeypatch.setattr(node.prov, "create_snapshot", create_snapshot)
    out = await node.mgr.prepare_now(REPO)
    assert out["started"]
    await builds_done(node.mgr)
    assert order.index("git setup") < order.index("scrub") < order.index("check") < order.index("snapshot")
    assert node.mgr.prepared_store().all()[0].state == "ready"
    assert not node.mgr._build_lanes()  # the gateway no longer serves the build lane


async def test_a_token_left_on_the_disk_means_no_snapshot(node):
    node.rt.register("git", FakeGit())
    node.machine.token_left = "/work/shop/.npmrc\n"
    await node.mgr.prepare_now(REPO)
    await builds_done(node.mgr)
    [rec] = node.mgr.prepared_store().all()
    assert rec.state == "failed" and "/work/shop/.npmrc" in rec.error
    assert not node.prov.snapshots
    assert not [r for r in node.vms.store.live() if r.lane_id.startswith("env/prep-")]  # its machine is gone


def test_the_scrub_removes_what_must_not_be_kept():
    for what in ("~/.config/agentos", "include.path", "~/.git-credentials", "~/.bash_history",
                 "/root/.ssh/authorized_keys", "/etc/ssh/ssh_host_", "/etc/agentos-env/", "/etc/machine-id"):
        assert what in prepbuild.SCRUB
    assert machine.SWAP_OFF in prepbuild.SCRUB


async def test_the_gateway_serves_a_build_lane_only_while_it_builds(node):
    assert not node.mgr.holds_terminal("env/prep-abc")
    node.mgr._build_lanes().add("env/prep-abc")
    assert node.mgr.holds_terminal("env/prep-abc")


# ------------------------------------------------------------------ retention


async def _ready(mgr, prov, key, repo, *, built, used=None, ref=None):
    sid = await prov.create_snapshot("1", "x", {"agentos-prepared": key})
    mgr.prepared_store().put(Prepared(key=key, repo=repo, state="ready", image=sid, started_at=built, built_at=built,
                                      last_used_at=used, provider="fake", ref=ref))
    return sid


async def test_retention_keeps_the_newest_per_repository_and_ref(node):
    mgr, prov, now = node.mgr, node.prov, utcnow()
    await _ready(mgr, prov, "main-old", REPO, built=now - timedelta(days=2))
    await _ready(mgr, prov, "main", REPO, built=now - timedelta(days=1))
    await _ready(mgr, prov, "branch", REPO, built=now - timedelta(hours=1), ref="agent/x")  # newer, another ref
    await mgr.prepared_reap(now)
    assert {r.key for r in mgr.prepared_store().all()} == {"main", "branch"}


async def test_retention_keeps_the_newest_per_repository_and_few_in_all(node):
    mgr, prov, now = node.mgr, node.prov, utcnow()
    old = await _ready(mgr, prov, "a-old", REPO, built=now - timedelta(days=2))
    new = await _ready(mgr, prov, "a-new", REPO, built=now - timedelta(days=1), used=now - timedelta(minutes=5))
    stale = await _ready(mgr, prov, "b", "example.test/me/blog", built=now - timedelta(days=40))
    for i in range(3):
        await _ready(mgr, prov, f"c{i}", f"example.test/me/c{i}", built=now - timedelta(hours=i + 1))
    mgr.settings = EnvSettings(prepared_days=14, prepared_max=3)
    out = await mgr.prepared_reap(now)
    keys = {r.key for r in mgr.prepared_store().all()}
    assert "a-old" not in keys and old not in prov.snapshots              # a newer build replaced it
    assert "b" not in keys and stale not in prov.snapshots                # unused for 40 days
    assert len(keys) == 3 and "c2" not in keys and new in prov.snapshots  # at most 3: the least recently used went
    assert set(out["deleted"]) >= {REPO, "example.test/me/blog"}


async def test_a_build_the_node_lost_is_failed_and_its_machine_deleted(node):
    plan = await node.vms.plan("env/prep-lost1", ttl_minutes=90)
    vm = await node.vms.create("env/prep-lost1", plan=plan, wait_s=5)
    node.mgr.prepared_store().put(Prepared(key="k", repo=REPO, started_at=utcnow(), build_vm=vm.id))
    out = await node.mgr.prepared_reap()
    assert out["lost"] == [REPO]
    rec = node.mgr.prepared_store().get("k")
    assert rec.state == "failed" and "restarted" in rec.error
    assert not node.vms.store.get(vm.id).live


async def test_forget_deletes_the_image(node):
    sid = await _ready(node.mgr, node.prov, "k", REPO, built=utcnow())
    assert await node.mgr.forget_prepared(REPO) == ["k"]
    assert sid not in node.prov.snapshots and not node.mgr.prepared_store().all()


# ------------------------------------------------------------------ the warm pool


async def _warm(node, n=1):
    node.mgr.settings = EnvSettings(warm=n)
    made = await node.mgr.fill_pool()
    for t in list(node.mgr._pool_tasks()):
        await t
    return made


async def test_the_pool_is_off_by_default(node):
    assert EnvSettings().warm == 0
    node.mgr.session_hint(node.token)
    assert await node.mgr.fill_pool() == [] and not node.mgr.warm_vms()


async def test_an_open_claims_a_matching_warm_machine(node):
    node.reads["text"] = None
    [vm_id] = await _warm(node)
    warm = node.vms.store.get(vm_id)
    assert warm.state == "ready" and warm.lane_id.startswith("env/warm-") and warm.environment is None
    assert node.envs.has(warm.lane_id)
    assert any(c == machine.SWAP_ON for c in node.machine.cmds("env/warm-"))  # its prework ran
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready", out
    env = node.mgr.get(out["id"])
    assert env.vm_id == vm_id and env.warm_claimed
    rec = node.vms.store.get(vm_id)
    assert rec.lane_id == env.terminal_lane and rec.environment == env.id and rec.expires_at == env.expires_at
    assert rec.created_by == env.grant_id
    assert node.envs.has(env.terminal_lane) and not node.envs.has(warm.lane_id)
    assert node.prov.servers[rec.provider_id].labels["agentos-lane"] == env.terminal_lane.replace("/", ".")
    assert out["prepared"]["warm"]
    for t in list(node.mgr._pool_tasks()):  # the pool fills again after the open
        await t
    [again] = node.mgr.warm_vms()
    assert again.id != vm_id
    assert len(node.prov.made_in) == 2  # the warm one the open took, and the refill: the open rented none


async def test_a_limited_open_never_takes_a_warm_machine(node):
    """A warm machine is made with the full network: an open whose network is limited or none rents its own."""
    node.reads["text"] = None
    [vm_id] = await _warm(node)
    warm = node.vms.store.get(vm_id)
    plan = {"image": warm.image, "server_type": warm.server_type, "location": warm.location}
    for level in ("limited", "none"):
        assert await node.mgr.claim_warm(None, {**plan, "network": level}) is None
    assert node.vms.store.get(vm_id).environment is None


async def test_a_paused_resume_never_takes_a_warm_machine():
    """Resuming a paused save brings back that same machine: the warm pool is not asked."""
    calls: list[str] = []
    env = SimpleNamespace(id="e1", vm_id=None, price_hourly_eur=None, machine=SimpleNamespace())
    rec = SimpleNamespace(id="v1", price_hourly_eur=0.1, server_type="morph-small", location="morph")

    async def claim_warm(e, plan):
        calls.append("claim")
        return rec

    async def make_room():
        calls.append("room")

    async def resume_paused(e, plan):
        calls.append("resume")
        return rec

    def update(_id, change):
        change(env)
        return env

    stub = SimpleNamespace(claim_warm=claim_warm, make_room=make_room, _resume_paused=resume_paused,
                           store=SimpleNamespace(update=update, get=lambda _id: env))
    await bringup.BringUp._rent(stub, env, {"image": "snap", "restore": {"from": "paused"}})
    assert calls == ["room", "resume"] and env.vm_id == "v1"


async def test_a_warm_machine_never_blocks_an_open(node):
    """A full node: the warm machine that does not match goes first, and the open rents."""
    node.reads["text"] = None
    node.vms.settings = VmSettings(ssh_from=["198.51.100.7/32"], max_live=1)
    node.mgr.settings = EnvSettings(warm=1, max_live=1)
    [vm_id] = await node.mgr.fill_pool()
    for t in list(node.mgr._pool_tasks()):
        await t
    node.mgr.settings = EnvSettings(warm=0, max_live=1)  # no refill after the open in this test
    out = await node.call("environment_open", title="Shop", repo=REPO, size="medium")  # another server type
    assert out["state"] == "ready", out
    assert not node.vms.store.get(vm_id).live
    assert node.mgr.get(out["id"]).vm_id != vm_id


async def test_the_pool_respects_the_limits(node):
    node.mgr.settings = EnvSettings(warm=3, max_live=2)
    made = await node.mgr.fill_pool()
    assert len(made) == 2  # the node's limit at once
    node.mgr.settings = EnvSettings(warm=1, monthly_eur=0.001)
    assert len(node.mgr.warm_vms()) == 2
    await node.mgr.pool_reap()  # more than warm in the pool: the oldest goes
    assert len(node.mgr.warm_vms()) == 1


async def test_a_warm_machine_is_deleted_after_warm_minutes(node):
    [vm_id] = await _warm(node)
    gone = await node.mgr.pool_reap(utcnow() + timedelta(minutes=51))
    assert gone == [vm_id] and not node.vms.store.get(vm_id).live


async def test_a_session_hint_fills_the_pool_only_for_a_grant_that_may_open(node):
    node.mgr.settings = EnvSettings(warm=1)
    _, reader = node.policy.issue(subject={"id": "reader"}, workspaces=["*"], ceiling=Effect.READ)
    node.mgr.session_hint(reader)
    assert not node.mgr._pool_tasks()
    node.mgr.session_hint(node.token)
    assert node.mgr._pool_tasks()
    for t in list(node.mgr._pool_tasks()):
        await t
    assert len(node.mgr.warm_vms()) == 1
    view = node.mgr.pool_view()
    assert view["warm"] == 1 and view["target"] == 1 and view["machines"][0]["state"] == "ready"


async def test_the_report_shows_prepared_machines_and_the_pool(node):
    await _ready(node.mgr, node.prov, "k", REPO, built=utcnow())
    rep = node.mgr.report()
    assert rep["prepared"][0]["repo"] == REPO and rep["prepared"][0]["state"] == "ready"
    assert rep["pool"]["target"] == 0 and "prepared_eur_month" in rep


async def test_a_crash_after_the_claim_still_deletes_the_machine(node):
    """The environment names the machine before the VM's record moves: its failed bring-up deletes it."""
    node.reads["text"] = None
    [vm_id] = await _warm(node)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    env = node.mgr.get(out["id"])
    node.mgr.store.update(env.id, lambda e: setattr(e, "state", "starting"))
    await node.mgr._fail(env.id, "the node restarted")
    assert not node.vms.store.get(vm_id).live


async def test_a_new_mcp_session_gives_the_hint(node):
    """openajar.mcp.http: a POST with no session id is a new session; the hint gets its token."""
    from openajar.mcp.http import BearerAuth

    seen: list[str] = []

    async def app(scope, receive, send):
        return None

    guard = BearerAuth(app, "owner-token", on_session=seen.append)
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"authorization", b"Bearer owner-token")]}
    await guard(scope, None, None)
    scope["headers"].append((b"mcp-session-id", b"abc"))
    await guard(scope, None, None)
    assert seen == ["owner-token"]
    await asyncio.sleep(0)


async def test_allow_warm_takes_effect_on_a_running_node(node):
    """`openajar environments allow --warm 1` writes environments.toml; the pool reads it when it changes."""
    from openajar.environments.cli import _write_settings

    assert node.mgr.pool_view()["target"] == 0
    _write_settings({"warm": 1, "warm_minutes": 30}, home=node.mgr.home)
    view = node.mgr.pool_view()
    assert view["target"] == 1 and view["minutes"] == 30


# ------------------------------------------------------------------ the owner's CLI and route


@pytest.fixture
def cli(node, monkeypatch, request):
    import httpx
    from node_config_support import patch_node_config
    from test_environments_downloads import _bridge
    from typer.testing import CliRunner

    from openajar.api import ApiSettings, build_api
    from openajar.environments import prepared_cli

    home = node.mgr.home
    api = build_api(node.rt, ApiSettings(lane_pump_seconds=0, panel_dir=home / "no-panel"))
    cfg = node.rt.get("config")
    toml = home / "node.toml"
    toml.write_text("")
    patch_node_config(request, monkeypatch, cfg, toml)
    monkeypatch.setattr(prepared_cli, "POLL_S", 0.05)
    monkeypatch.setattr(prepared_cli, "_managers", lambda: (node.mgr, node.vms))

    async def run(*args: str):
        transport = _bridge(asyncio.get_running_loop(), {"/api/": api})
        monkeypatch.setattr(prepared_cli, "_http_client", lambda: httpx.Client(transport=transport))
        return await asyncio.to_thread(CliRunner().invoke, prepared_cli.app, ["--home", str(home), *args])

    return SimpleNamespace(run=run, api=api)


async def test_the_cli_builds_lists_and_forgets_a_prepared_machine(node, cli):
    res = await cli.run("prepare", REPO)
    assert res.exit_code == 0, res.output
    assert " s  " in res.output and "Ready: example.test/me/shop at 0123456789ab" in res.output  # its steps, then the end
    res = await cli.run("prepare", REPO)
    assert res.exit_code == 1 and "it is ready already" in res.output and "--force" in res.output
    res = await cli.run("prepared")
    assert res.exit_code == 0 and "example.test/me/shop  ready  0123456789ab" in res.output
    assert "Warm pool: off" in res.output
    res = await cli.run("prepared", "forget", REPO)
    assert res.exit_code == 0 and "Forgot 1 prepared machine of example.test/me/shop" in res.output
    assert not node.prov.snapshots


async def test_the_cli_says_when_there_is_nothing_to_prepare(node, cli):
    node.reads["text"] = None
    res = await cli.run("prepare", REPO)
    assert res.exit_code == 1 and "nothing to prepare" in res.output and ".openajar/environment.toml" in res.output


async def test_the_prepare_route_is_the_owners_only(node, cli):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=cli.api), base_url="http://localhost") as c:
        r = await c.post("/api/environments/prepare", json={"repo": REPO},
                         headers={"Authorization": f"Bearer {node.token}"})
        assert r.status_code in (401, 403), r.text
        owner = node.rt.get("config").mcp_token
        r = await c.post("/api/environments/prepare", json={"repo": REPO}, headers={"Authorization": f"Bearer {owner}"})
        assert r.status_code == 403 and "presence_required" in r.text  # the bare node token is no person
        from openajar.commands.base import operator_presence

        person = operator_presence(node.rt.get("config"))
        r = await c.post("/api/environments/prepare", json={"repo": REPO},
                         headers={"Authorization": f"Bearer {owner}", **person})
        assert r.status_code == 200 and r.json()["started"] is True, r.text
    await builds_done(node.mgr)


async def test_the_pool_never_boots_an_image_a_newer_build_replaced(node):
    node.mgr.prepared_store().set_last_image("snap-gone")
    assert await node.mgr._warm_image() == await node.mgr._image()
    sid = await _ready(node.mgr, node.prov, "k", REPO, built=utcnow())
    node.mgr.prepared_store().set_last_image(sid)
    assert await node.mgr._warm_image() == sid


async def test_a_fill_never_deletes_a_machine_an_open_just_claimed(node):
    node.reads["text"] = None
    [vm_id] = await _warm(node)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert node.mgr.get(out["id"]).vm_id == vm_id
    await node.mgr._drop_warm(node.vms.store.get(vm_id), "a stale view of the pool")
    assert node.vms.store.get(vm_id).live  # it is the environment's now


async def test_session_hints_close_together_are_one(node):
    node.mgr.settings = EnvSettings(warm=1)
    node.mgr.session_hint(node.token)
    first = set(node.mgr._pool_tasks())
    for t in list(first):
        await t
    node.mgr.session_hint(node.token)
    assert not [t for t in node.mgr._pool_tasks() if t.get_name() == "environments-warm-fill"]  # within HINT_EVERY_S
