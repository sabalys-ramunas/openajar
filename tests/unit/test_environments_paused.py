"""Saves that keep the machine (openajar.environments.pausing): on a provider with the "pause"
capability and no cloud-init (Morph), a save that ends the machine pauses it, and environment_open
resumes the same machine with its processes running. The cloud is test_environments_saves's disk
cloud, made to pause; every command the node sends runs for real on the machine's folder."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from test_environments_saves import (
    REPO,
    TITLE,
    DiskCloud,
    FakeTunnel,
    FakeVault,
    LocalMachines,
    git,
    make_remote,
    ok_probe,
    readable_repo,
    work_on_it,
)
from test_policy_support import Clock, make_service

from openajar.core.models import Effect, utcnow
from openajar.dev.lanes import FakeLaneManager
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.models import ExecResult
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments import bringup, machine, pausing
from openajar.environments.errors import SaveFailed
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.environments.tools import tool_specs as env_tool_specs
from openajar.policy.wrap import ToolCallError, bind_request_token, wrap_tool
from openajar.vms.manager import VmManager
from openajar.vms.models import VmSettings
from openajar.vms.provider import DEFAULT_CAPABILITIES

MORPH_LIKE = frozenset({"snapshot", "pause", "branch", "wake_on_http"})  # no cloud-init, no firewall


@dataclass
class PausingCloud(DiskCloud):
    """The disk cloud, with pause and without cloud-init: like Morph."""
    capabilities: frozenset[str] = MORPH_LIKE
    firewall: bool = False


class Machines(LocalMachines):
    """LocalMachines, plus: the token scrub runs for real, and the setup check says what a machine with no
    cloud-init says until the node ran the setup on it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setups: list[str] = []
        self.unreachable_checks = 0      # setup checks whose ssh fails first (exit 255), as while sshd starts

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None, oom_check=False):
        if cmd == pausing.TOKEN_SCRUB:
            disk = self.disk_of(lane_id)
            return await asyncio.to_thread(self.run, ["bash", "-c", cmd], disk, None)
        if cmd == machine.SETUP_STATE and self.unreachable_checks:
            self.unreachable_checks -= 1
            return ExecResult(exit_code=255, stdout="", stderr="ssh: connect to host: Connection refused",
                              duration_s=0.01)
        if cmd == machine.SETUP_STATE:
            done = (self.disk_of(lane_id) / "ready-marker").exists()
            return ExecResult(exit_code=0, stdout="ready\n" if done else "no cloud-init and no setup marker\n",
                              stderr="", duration_s=0.01)
        if cmd == "sh -s" and stdin and "openajar" in str(stdin):
            self.setups.append(lane_id)
            (self.disk_of(lane_id) / "ready-marker").write_text("1")
            return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
        return await super().exec(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, grant_id=grant_id,
                                  max_bytes=max_bytes, oom_check=oom_check)


@pytest.fixture
async def node(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    policy, rt = make_service(home, clock=Clock())
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(), settings=TermSettings(proxy_port=0))
    rt.register("envs", envs)
    cloud = PausingCloud(root=tmp_path / "cloud")
    (cloud.root / "snapshots").mkdir(parents=True)
    vms = VmManager(home, runtime=rt, provider=cloud, settings=VmSettings(ssh_from=["198.51.100.7/32"]),
                    node_name="node-1", probe=ok_probe)
    rt.register("vms", vms)
    machines = Machines(tmp_path, vms, cloud, make_remote(tmp_path))
    for name in ("exec", "put_archive", "get_archive", "job_start", "job_list", "job_cancel"):
        monkeypatch.setattr(envs, name, getattr(machines, name), raising=False)
    rt.register("lanes", FakeLaneManager())
    rt.register("vault", FakeVault())
    monkeypatch.setattr(bringup, "Tunnel", FakeTunnel)
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings(allow_keep_live=True))
    mgr.repo_probe = readable_repo
    monkeypatch.setattr(mgr, "_machine_root", lambda env: str(cloud.disk(vms.store.get(env.vm_id).provider_id)))
    told: list[tuple[str, str]] = []

    async def notify(title, body):
        told.append((title, body))

    monkeypatch.setattr(mgr, "_notify", notify)
    rt.register("environments", mgr)
    _grant, token = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                 specials={"vm": {}, "exec": {}})
    calls = {s.name: wrap_tool(s, rt) for s in env_tool_specs(rt)}

    async def call(name, *, session="chat-1", **kw):
        with bind_request_token(token, session=session):
            return await calls[name](**kw)

    yield {"rt": rt, "mgr": mgr, "vms": vms, "cloud": cloud, "machines": machines, "envs": envs, "call": call,
           "told": told}
    await mgr.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    policy.close()


def disk(node, env_id: str) -> Path:
    return node["cloud"].disk(node["vms"].store.get(node["mgr"].get(env_id).vm_id).provider_id)


async def opened(node, **kw):
    out = await node["call"]("environment_open", title=kw.pop("title", TITLE), repo=kw.pop("repo", REPO), **kw)
    assert out["state"] == "ready", out
    return out


def token_file(d: Path) -> Path:
    f = d / "root" / ".config" / "agentos" / "gitconfig"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("[http]\n\textraHeader = X-Agentos-Token: tok\n")
    return f


# ------------------------------------------------------------------ the round trip


async def test_close_pauses_the_machine_and_open_resumes_it_with_its_processes(node):
    """keep "save" on a provider that pauses: close pauses the machine (no snapshot, no delete), the save
    says so and what it costs, and a new chat resumes the same machine: its files as they were, its dev
    server still running (checked, not started again), a new gateway token, `from` paused."""
    mgr, vms, cloud, machines = node["mgr"], node["vms"], node["cloud"], node["machines"]
    out = await opened(node, keep="save")
    assert machines.setups == [f"env/{out['id']}"]  # no cloud-init: the node ran the setup itself
    d = disk(node, out["id"])
    head = work_on_it(d / "work" / "shop")
    gitconfig = token_file(d)
    await node["envs"].job_start(f"env/{out['id']}", "npm run dev -- --port 3000", cwd="/work/shop")
    old_vm = mgr.get(out["id"]).vm_id
    server = vms.store.get(old_vm).provider_id

    closed = await node["call"]("environment_close", note="wire the coupon field next")
    assert closed["machine"] == "paused" and server in cloud.servers and cloud.paused == {server}
    assert cloud.snapshots == {} and closed["saved"]["snapshot"] is None
    assert closed["saved"]["paused"] == {"machine": old_vm, "cost_eur_month": 0.12, "estimate": True}
    assert "paused with its memory" in closed["pause"] and "resumes in seconds" in closed["pause"]
    assert not gitconfig.exists()  # the gateway token's files go before the pause (the token is revoked)
    rec = vms.store.get(old_vm)
    assert rec.state == "paused" and not rec.live and rec.deleted_at is not None
    assert not (vms.dir / old_vm).exists() and (vms.dir / "paused" / old_vm / "id_ed25519").exists()
    assert cloud.ttls[server][1] == "stop" and cloud.ttls[server][0] >= 30 * 86400  # the paused backstop
    jobs_before = len(machines.jobs)

    listed = await node["call"]("environment", session="chat-2")
    [row] = listed["saved"]
    assert row["paused"] is True and row["cost_eur_month"] == 0.12 and row["snapshot_gb"] is None
    assert mgr.snapshots_eur_month() == 0.12

    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    r = again["resumed"]
    assert again["state"] == "ready" and r["from"] == "paused", again
    new_vm = mgr.get(again["id"]).vm_id
    assert new_vm != old_vm and vms.store.get(new_vm).provider_id == server and cloud.paused == set()
    assert vms.store.get(old_vm).state == "deleted" and "resumed as" in vms.store.get(old_vm).delete_reason
    assert (vms.dir / new_vm / "known_hosts").read_text().startswith(f"openajar-{new_vm} ")
    assert r["services"] == [{"cmd": "npm run dev -- --port 3000", "cwd": "/work/shop", "port": 3000,
                              "running": True, "listening": True, "kept": True}]
    assert len(machines.jobs) == jobs_before  # nothing was started again: it never stopped
    assert r["branch"] == "sale" and r["head"] == head[:12] and r["missing"] == []
    assert "paused with its memory" in r["note"] and "wire the coupon field next" in r["note"]
    w = disk(node, again["id"]) / "work" / "shop"
    assert git(w, "rev-parse", "HEAD") == head and (w / "notes.md").exists() and not (w / "old.txt").exists()
    assert mgr.get(again["id"]).workdir == "/work/shop"


async def test_a_paused_save_carries_the_browsers_pinned_device(node):
    from openajar.environments import vmpin

    mgr = node["mgr"]
    out = await opened(node, keep="save")
    path = vmpin.pin_path(mgr.home, out["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    vmpin.write_private_json(path, {"fonts": ["Pinned Sans"], "agentos:pin": {"version": 2}})
    closed = await node["call"]("environment_close")
    assert closed["machine"] == "paused"
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert again["resumed"]["from"] == "paused"
    assert vmpin.load(mgr, again["id"], "lane").config == {"fonts": ["Pinned Sans"]}


async def test_without_cloud_init_the_setup_waits_for_a_check_that_ran(node, monkeypatch):
    """A setup check whose ssh failed (exit 255) is no answer: the node asks again, and runs the setup
    only once the machine said it has none (the docker e2e of 2026-11-18 ran it on a failed check)."""
    monkeypatch.setattr(bringup, "SETUP_POLL_S", 0.01)
    node["machines"].unreachable_checks = 2
    out = await opened(node)
    assert node["machines"].setups == [f"env/{out['id']}"] and node["machines"].unreachable_checks == 0


async def test_forget_deletes_the_paused_machine(node):
    cloud, vms = node["cloud"], node["vms"]
    out = await opened(node, keep="save")
    vm_id = node["mgr"].get(out["id"]).vm_id
    server = vms.store.get(vm_id).provider_id
    await node["call"]("environment_close")
    assert server in cloud.servers
    gone = await node["call"]("environment_forget", title=TITLE, session="chat-2")
    assert len(gone["forgot"]) == 1 and server not in cloud.servers and server in cloud.deleted
    assert vms.store.get(vm_id).state == "deleted" and not (vms.dir / "paused" / vm_id).exists()


async def test_forget_keeps_the_paused_machine_while_a_save_does_not_read(node):
    """S1-05 with U5-03: another save may name the same paused machine, which is not known while one does not read.
    The forget deletes nothing and keeps the save, saying why; once every save reads, it goes as before."""
    cloud, vms, mgr = node["cloud"], node["vms"], node["mgr"]
    out = await opened(node, keep="save")
    server = vms.store.get(mgr.get(out["id"]).vm_id).provider_id
    await node["call"]("environment_close")
    sid = mgr.saves().all()[0].id
    stray = mgr.saves().root / "g_other" / "t_other" / "s0000000000" / "manifest.json"
    stray.parent.mkdir(parents=True)
    stray.write_text("{")  # a save that does not read
    held = await mgr.forget(None, None, sid)
    assert held["forgot"] == [] and "does not read" in held["kept"][0]["why"] and server in cloud.servers
    stray.unlink()
    gone = await mgr.forget(None, None, sid)
    assert gone["forgot"] == [sid] and server not in cloud.servers


async def test_close_with_forget_deletes_the_paused_machine_of_an_earlier_save(node):
    cloud, vms = node["cloud"], node["vms"]
    first = await opened(node, keep="save")
    server = vms.store.get(node["mgr"].get(first["id"]).vm_id).provider_id
    await node["call"]("environment_close")
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert again["resumed"]["from"] == "paused"
    done = await node["call"]("environment_close", forget=True, discard=True, session="chat-2")
    assert done["machine"] == "deleted" and server not in cloud.servers
    assert node["mgr"].saves().all() == []


async def test_retention_never_deletes_a_machine_that_was_resumed_and_saved_again(node):
    """keep_last 1: the first save (whose paused machine was resumed) goes when the second is made, and the
    machine, paused again for the second, stays. saved_days then removes the second with its machine."""
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    mgr.settings.keep_last = 1
    out = await opened(node, keep="save")
    server = vms.store.get(mgr.get(out["id"]).vm_id).provider_id
    first = (await node["call"]("environment_close"))["saved"]["save_id"]
    await node["call"]("environment_open", title=TITLE, session="chat-2")
    second = (await node["call"]("environment_close", session="chat-2"))["saved"]["save_id"]
    assert [m.id for m in mgr.saves().all()] == [second] and first != second
    assert server in cloud.servers and cloud.paused == {server}
    m, _ = mgr.saves().find(second)
    days = timedelta(days=mgr.settings.saved_days)
    await mgr.prune_saves(m.saved_at + days - timedelta(hours=12))
    got = await mgr.prune_saves(m.saved_at + days + timedelta(hours=1))
    assert got["removed"] == [second] and server not in cloud.servers


# ------------------------------------------------------------------ the reaper's paths


async def test_idle_pauses_and_a_rescue_save_pauses_too(node):
    mgr, cloud = node["mgr"], node["cloud"]
    out = await opened(node, keep="save")
    got = await mgr.reap_once(utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1))
    env = mgr.get(out["id"])
    assert got["closed"] == [out["id"]] and env.vm_state == "paused"
    assert mgr.saves().find(env.last_save_id)[0].paused is not None

    other = await opened(node, title="Hotfix", session="chat-3", keep="none")  # with work not pushed
    work_on_it(disk(node, other["id"]) / "work" / "shop")
    server = node["vms"].store.get(mgr.get(other["id"]).vm_id).provider_id
    got = await mgr.reap_once(mgr.get(other["id"]).expires_at + timedelta(hours=5))
    m, _ = mgr.saves().find(got["saved"][0]["save"])
    assert m.kind == "rescue" and m.paused is not None and m.snapshot is None and m.commits == 2
    assert server in cloud.paused and mgr.get(other["id"]).vm_state == "paused"


async def test_environment_save_without_closing_still_takes_a_snapshot(node):
    out = await opened(node, keep="save")
    saved = await node["call"]("environment_save")
    assert saved["snapshot"]["id"].startswith("snap-") and "paused" not in saved
    assert node["mgr"].get(out["id"]).state == "ready" and node["cloud"].paused == set()


async def test_pause_saves_false_saves_as_on_any_other_provider(node):
    node["mgr"].settings.pause_saves = False
    out = await opened(node, keep="save")
    closed = await node["call"]("environment_close")
    assert closed["machine"] == "deleted" and closed["saved"]["snapshot"] is not None
    assert node["cloud"].paused == set() and node["mgr"].get(out["id"]).vm_state == "deleted"


# ------------------------------------------------------------------ failures


async def test_a_pause_that_fails_keeps_the_machine_working(node):
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    out = await opened(node, keep="save")
    cloud.fail_pause = 1
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_close")
    assert e.value.code == "save_failed"
    env = mgr.get(out["id"])
    assert env.state == "ready" and vms.store.get(env.vm_id).state == "ready" and cloud.paused == set()
    assert mgr.saves().all() == []


async def test_a_save_that_fails_after_the_pause_resumes_the_machine(node, monkeypatch):
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    out = await opened(node, keep="save")

    async def broken(*a, **kw):
        raise SaveFailed("the node's disk is full")

    monkeypatch.setattr(mgr, "_commit_save", broken)
    with pytest.raises(ToolCallError):
        await node["call"]("environment_close")
    env = mgr.get(out["id"])
    rec = vms.store.get(env.vm_id)
    assert env.state == "ready" and rec.state == "ready" and rec.deleted_at is None and cloud.paused == set()
    assert (vms.dir / rec.id / "id_ed25519").exists()  # its lane reaches it again


async def test_a_paused_machine_gone_at_the_provider_resumes_from_the_files(node):
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    out = await opened(node, keep="save")
    head = work_on_it(disk(node, out["id"]) / "work" / "shop")
    server = vms.store.get(mgr.get(out["id"]).vm_id).provider_id
    await node["call"]("environment_close")
    await cloud.delete_server(server)  # Morph's TTL ran out, or the owner deleted it there
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    r = again["resumed"]
    assert r["from"] == "files" and r["missing"][0].startswith("the paused machine is gone at the provider")
    assert "paused_missing" not in r
    w = disk(node, again["id"]) / "work" / "shop"
    assert git(w, "rev-parse", "HEAD") == head


async def test_the_owner_wakes_a_paused_machine_held_by_no_chat_and_the_next_open_takes_it(node):
    """The owner's resume (openajar.environments.waking) on a provider that pauses: the same machine wakes,
    its dev server still running, no chat holds it, and an agent's environment_open of the title takes it."""
    mgr, cloud, vms, machines = node["mgr"], node["cloud"], node["vms"], node["machines"]
    out = await opened(node, keep="save")
    await node["envs"].job_start(f"env/{out['id']}", "npm run dev -- --port 3000", cwd="/work/shop")
    server = vms.store.get(mgr.get(out["id"]).vm_id).provider_id
    await node["call"]("environment_close", note="back soon")
    assert cloud.paused == {server}
    jobs = len(machines.jobs)
    env, how = await mgr.resume_saved(TITLE)
    assert how == "resumed" and env.binding == f"kept:{env.id}" and env.lease is None
    env = await mgr.wait_ready(env.id, 30)
    assert env.state == "ready" and env.resumed["from"] == "paused" and cloud.paused == set()
    assert vms.store.get(env.vm_id).provider_id == server and len(machines.jobs) == jobs  # woken, not restarted
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert again["id"] == env.id and again["opened"] == "resumed" and again["resumed"]["from"] == "paused"


async def test_a_paused_machine_no_save_points_to_is_deleted_and_the_owner_told(node):
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    rec = await vms.create("dev/stray", ttl_minutes=60)
    await vms.pause(rec.id, why="a save that never finished", keep_days=30)
    mgr.saves().discard(mgr.saves().tmp())  # the save that paused it made its folder first (S1-05: none is held)
    soon = await mgr.sweep_snapshots(utcnow() + timedelta(minutes=5), force=True)
    assert "paused_deleted" not in soon and rec.provider_id in cloud.servers
    later = await mgr.sweep_snapshots(utcnow() + timedelta(hours=1), force=True)
    assert later["paused_deleted"] == [rec.id] and rec.provider_id not in cloud.servers
    assert any("paused machines" in t[0] for t in node["told"])


# ------------------------------------------------------------------ the other providers


def test_hetzner_and_docker_keep_their_capabilities():
    from openajar.vms.docker import DockerProvider
    from openajar.vms.hetzner import HetznerProvider
    from openajar.vms.provider import FakeProvider, capable

    assert HetznerProvider.capabilities == DEFAULT_CAPABILITIES
    assert capable(DockerProvider, "network_policy") and not capable(DockerProvider, "pause")
    assert capable(HetznerProvider, "network_policy") and not capable(HetznerProvider, "pause")
    assert capable(object(), "firewall") and not capable(object(), "pause")  # a provider that says nothing
    assert FakeProvider().capabilities == DEFAULT_CAPABILITIES


def test_the_image_cleanup_keeps_morphs_ssh_keys():
    from openajar.vms.manager import IMAGE_CLEANUP
    from openajar.vms.morph import MorphProvider

    script = "set -eu\n" + IMAGE_CLEANUP
    # a cloud-init that does nothing, first on PATH: the script must never run this machine's own (an
    # Ubuntu CI runner has one, and `cloud-init clean` there is refused, or worse, obeyed)
    stub = Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip())
    (stub / "cloud-init").write_text("#!/bin/sh\nexit 0\n")
    (stub / "cloud-init").chmod(0o755)
    for prefix, kept in (("", False), (MorphProvider.script_prefix, True)):
        root = Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip())
        (root / "etc" / "ssh").mkdir(parents=True)
        (root / "etc" / "ssh" / "ssh_host_ed25519_key").write_text("k")
        (root / "root" / ".ssh").mkdir(parents=True)  # never this machine's /root (a CI runner refuses it)
        body = prefix + script.replace("/etc/ssh", str(root / "etc" / "ssh")).replace("/root/.ssh", str(root / "root" / ".ssh"))
        body = "\n".join(ln for ln in body.splitlines() if ln.startswith(("set", "export", "if")))
        subprocess.run(["bash", "-c", body], check=True, env={"PATH": f"{stub}:/usr/bin:/bin"})
        assert (root / "etc" / "ssh" / "ssh_host_ed25519_key").exists() == kept, shlex.quote(prefix)
