"""Disposable cloud VMs (openajar.vms): lifecycle on a fake cloud, the reaper, limits, the SSH
command line, the Hetzner client against a mock transport, and the tools."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from openajar.core.errors import InvalidRequest, LaneExists, PolicyDenied
from openajar.core.models import Effect, Principal, utcnow
from openajar.core.runtime import Runtime
from openajar.env.host import FakeHost
from openajar.env.lanes import SandboxError
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher, Workload
from openajar.vms import ssh
from openajar.vms.hetzner import HetznerProvider
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmLimit, VmSettings
from openajar.vms.provider import FakeProvider, Server, ServerSpec
from openajar.vms.tools import tool_specs


class Clock:
    def __init__(self):
        self.now = utcnow().replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


async def ok_probe(remote, *, timeout_s=15):
    return True, ""


@pytest.fixture
async def env(tmp_path):
    home = tmp_path / "home"
    envs = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    clock = Clock()
    prov = FakeProvider()
    settings = VmSettings(max_live=2, monthly_budget_eur=10.0, ssh_from=["198.51.100.7/32"])
    runtime = Runtime(envs=envs)
    vms = VmManager(home, runtime=runtime, provider=prov, settings=settings, node_name="node-1",
                    probe=ok_probe, clock=clock)
    runtime.register("vms", vms)
    yield vms, envs, prov, clock
    await vms.shutdown()
    await envs.shutdown()


async def test_create_makes_a_ready_vm_lane_with_pinned_keys(env):
    vms, envs, prov, _ = env
    rec = await vms.create("dev/build-1", ttl_minutes=60, purpose="run tests")
    assert rec.state == "ready" and rec.host and rec.provider_id in prov.servers
    lane = envs.get("dev/build-1")
    assert lane.remote is not None and lane.remote.vm_id == rec.id and lane.remote.host == rec.host
    assert not lane.isolated and lane.contained
    server = prov.servers[rec.provider_id]
    assert server.labels == {"agentos-node": "node-1", "agentos-home": vms.home_id, "agentos-vm": rec.id,
                             "agentos-lane": "dev.build-1"}
    # the throwaway SSH key resource is gone once the server exists; the firewall lets in this node only
    assert prov.keys == {} and prov.firewalls == {f"agentos-node-1-{vms.home_id}": ["198.51.100.7/32"]}
    # cloud-init pins the host key the node already knows; no password login
    ud = prov.user_data[rec.provider_id]
    known = (vms.dir / rec.id / "known_hosts").read_text().split()
    assert "ssh_pwauth: false" in ud and known[2] in ud and known[0] == f"agentos-{rec.id}"
    assert oct((vms.dir / rec.id / "id_ed25519").stat().st_mode & 0o777) == "0o600"


async def test_vms_gives_the_env_manager_its_ssh_launcher(env, tmp_path):
    """env never imports vms: a VM lane warms with the launcher vms hands the env manager, and an env
    manager no VM manager reached refuses a VM lane instead of running it on the node."""
    vms, envs, _, _ = env
    assert isinstance(envs.remote_launcher, ssh.SshLauncher)
    rec = await vms.create("dev/build-2", ttl_minutes=60, purpose="run tests")
    alone = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                       settings=EnvSettings(proxy_port=0), use_lock=False)
    assert alone.remote_launcher is None
    with pytest.raises(SandboxError, match="VM lane"):
        await alone.warm(rec.lane_id)
    await alone.shutdown()


async def test_destroy_deletes_the_server_the_lane_and_the_keys(env):
    vms, envs, prov, _ = env
    rec = await vms.create("dev/a", ttl_minutes=30)
    out = await vms.destroy("dev/a")
    assert out.state == "deleted" and rec.provider_id in prov.deleted
    assert not envs.has("dev/a") and not (vms.dir / rec.id).exists()


async def test_env_remove_refuses_a_vm_lane(env):
    vms, envs, _, _ = env
    await vms.create("dev/a", ttl_minutes=30)
    with pytest.raises(InvalidRequest, match="vm_destroy"):
        await envs.remove("dev/a")


async def test_limits_live_count_ttl_budget_and_lane_names(env):
    vms, _, prov, _ = env
    await vms.create("dev/a", ttl_minutes=30)
    with pytest.raises(LaneExists):
        await vms.create("dev/a", ttl_minutes=30)
    await vms.create("dev/b", ttl_minutes=30)
    with pytest.raises(VmLimit, match="max_live"):
        await vms.create("dev/c", ttl_minutes=30)
    await vms.destroy("dev/b")
    with pytest.raises(VmLimit, match="max_ttl_minutes"):
        await vms.create("dev/c", ttl_minutes=24 * 60 + 1)
    prov.prices["cpx42"] = 2.0  # 10 EUR budget: 6 hours of it is too much
    vms._offers = None          # prices are cached for 30 minutes
    with pytest.raises(VmLimit, match="monthly budget"):
        await vms.create("dev/c", profile="build", ttl_minutes=6 * 60)
    with pytest.raises(InvalidRequest, match="no VM profile"):
        await vms.create("dev/c", profile="huge")


async def test_reaper_deletes_at_the_time_limit(env):
    vms, envs, _prov, clock = env
    rec = await vms.create("dev/a", ttl_minutes=30)
    clock.advance(minutes=29)
    envs.store.touch("dev/a", clock.now)
    assert (await vms.reap_once())["deleted"] == []
    clock.advance(minutes=2)
    out = await vms.reap_once()
    assert out["deleted"] == [rec.id] and vms.store.get(rec.id).delete_reason == "time limit"
    assert not envs.has("dev/a")


async def test_an_idle_vm_waits_for_the_end_of_its_paid_hour(env):
    vms, _envs, _prov, clock = env
    rec = await vms.create("dev/a", ttl_minutes=180)
    clock.advance(minutes=30)  # idle 30 min, but the hour is paid until minute 60
    assert (await vms.reap_once())["deleted"] == []
    clock.advance(minutes=26)  # minute 56 of the first hour, still idle
    out = await vms.reap_once()
    assert out["deleted"] == [rec.id] and "idle" in vms.store.get(rec.id).delete_reason


async def test_a_busy_vm_is_not_idle(env):
    vms, envs, _prov, clock = env
    await vms.create("dev/a", ttl_minutes=180)
    clock.advance(minutes=56)
    envs.store.touch("dev/a", clock.now - timedelta(minutes=2))
    assert (await vms.reap_once())["deleted"] == []


async def test_reaper_deletes_orphans_and_notices_servers_deleted_elsewhere(env):
    vms, envs, prov, _clock = env
    rec = await vms.create("dev/a", ttl_minutes=60)
    stray = await prov.create_server(ServerSpec(name="x", server_type="cx23", image="ubuntu-24.04", location="nbg1",
                                                user_data="", labels={"agentos-node": "node-1", "agentos-vm": "vm_gone",
                                                                      "agentos-home": vms.home_id}))
    other = await prov.create_server(ServerSpec(name="y", server_type="cx23", image="ubuntu-24.04", location="nbg1",
                                                user_data="", labels={"agentos-node": "another-node"}))
    out = await vms.reap_once()
    assert out["orphans"] == [stray.id] and other.id in prov.servers  # another node's VM is left alone
    del prov.servers[rec.provider_id]
    out = await vms.reap_once()
    assert out["gone"] == [rec.id] and not envs.has("dev/a")


async def test_a_server_left_out_of_one_listing_is_read_back_not_deleted(env):
    """S1-11, S5b-05: one `docker ps` that answered empty (or a renamed node, a provider hiccup) deleted every
    running machine as "deleted outside openajar". The record is read back by its id first."""
    vms, envs, prov, _clock = env
    rec = await vms.create("dev/a", ttl_minutes=60)
    real = prov.list_servers
    calls = []

    async def once_empty(labels):
        calls.append(labels)
        return [] if len(calls) == 1 else await real(labels)

    prov.list_servers = once_empty
    out = await vms.reap_once()
    assert out["gone"] == [] and vms.store.get(rec.id).state == "ready" and rec.provider_id in prov.servers
    assert prov.deleted == [] and envs.has("dev/a")


async def test_a_server_the_provider_says_is_gone_is_marked_never_deleted(env):
    vms, envs, prov, _clock = env
    rec = await vms.create("dev/a", ttl_minutes=60)
    del prov.servers[rec.provider_id]
    prov.fail_delete = 5  # a delete call would fail: none is made
    out = await vms.reap_once()
    got = vms.store.get(rec.id)
    assert out["gone"] == [rec.id] and got.state == "deleted" and got.delete_reason == "deleted outside openajar"
    assert prov.fail_delete == 5 and not envs.has("dev/a")


async def test_a_record_of_another_provider_is_lost_never_deleted_through_this_one(env):
    """S5b-05: after a switch from Hetzner to Docker (or a token for another project) the old record's id went to
    `docker rm`, the record said deleted, and the server billed on. It is `lost`, and the owner is told."""
    vms, _envs, prov, _clock = env
    rec = await vms.create("dev/a", ttl_minutes=60)
    old = vms.store.get(rec.id)
    old.provider = "hetzner"  # made before vms.toml said "fake"
    vms.store.put(old)
    told = []

    async def notify(title, body, *, urgent=False):
        told.append((title, body))

    vms._notify = notify
    out = await vms.reap_once(old.expires_at + timedelta(hours=1))  # past its limit too: still never deleted here
    got = vms.store.get(rec.id)
    assert got.lost and out["lost"] == [rec.id] and out["deleted"] == []
    assert rec.provider_id in prov.servers and prov.deleted == []
    assert len(told) == 1 and "Hetzner" in told[0][1] and "console" in told[0][1]
    assert vms.store.live() == []  # nothing this node can reach counts as running
    # R1 #3: a state of its own broke 0.7.0 after a downgrade (its store could not read the row, and every budget
    # check failed): the row says "deleted", which 0.7.0 knows, and the flag 0.7.0 ignores says lost
    row = json.loads(vms.store._q("SELECT record FROM vms WHERE id = ?", (rec.id,))[0][0])
    assert row["state"] in ("creating", "booting", "ready", "deleting", "deleted", "failed", "paused"), row["state"]
    assert row["state"] == "deleted" and row["lost"] is True
    assert (await vms.reap_once(old.expires_at + timedelta(hours=2)))["orphans"] == []  # its server is never swept


async def test_a_home_id_that_cannot_be_read_is_never_replaced(env, monkeypatch):
    """S1-11: one EMFILE while reading vms/home-id wrote a new id over the old, and every machine was an orphan."""
    vms, _envs, _prov, _clock = env
    first = vms.home_id
    vms._home_id = None
    real = Path.read_text

    def emfile(self, *a, **kw):
        if self.name == "home-id":
            raise OSError(24, "Too many open files")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", emfile)
    with pytest.raises(OSError):
        _ = vms.home_id
    monkeypatch.setattr(Path, "read_text", real)
    vms._home_id = None
    assert vms.home_id == first


async def test_two_homes_with_one_node_name_never_sweep_each_others_servers(env, tmp_path):
    """Two openajar homes on one cloud project or docker host, both named node-1 (the default): each
    home's labels carry its own random id, so neither reaper takes the other's servers for orphans."""
    vms, _envs, prov, clock = env
    rec = await vms.create("dev/a", ttl_minutes=60)
    other_envs = EnvManager(tmp_path / "home2", host=FakeHost(), launcher=DirectLauncher(),
                            settings=EnvSettings(proxy_port=0))
    other = VmManager(tmp_path / "home2", runtime=Runtime(envs=other_envs), provider=prov,
                      settings=VmSettings(ssh_from=["198.51.100.8/32"]), node_name="node-1", probe=ok_probe,
                      clock=clock)
    try:
        clock.advance(minutes=30)  # past the orphan grace
        out = await other.reap_once()
        assert out["orphans"] == [] and rec.provider_id in prov.servers
        labels = prov.servers[rec.provider_id].labels
        assert labels["agentos-home"] == vms.home_id != other.home_id and len(vms.home_id) >= 8
        # the id is the home's own: a restart reads it back
        again = VmManager(vms.home, runtime=Runtime(), provider=prov, settings=VmSettings(), node_name="node-1")
        assert again.home_id == vms.home_id
        # each home has its own firewall, so their SSH rules never overwrite each other
        mine = await other.create("dev/b", ttl_minutes=60)
        assert prov.firewalls == {f"agentos-node-1-{vms.home_id}": ["198.51.100.7/32"],
                                  f"agentos-node-1-{other.home_id}": ["198.51.100.8/32"]}
        assert (await vms.reap_once())["orphans"] == [] and mine.provider_id in prov.servers
    finally:
        await other.shutdown()
        await other_envs.shutdown()


async def test_a_failed_delete_is_retried_and_an_overdue_vm_alerts(env):
    vms, envs, prov, clock = env
    sent = []

    class Notifier:
        def notify(self, **kw):
            sent.append(kw)

    vms.runtime.register("notifier", Notifier())
    rec = await vms.create("dev/a", ttl_minutes=30)
    prov.fail_delete = 99
    clock.advance(minutes=31)
    await vms.reap_once()
    assert vms.store.get(rec.id).state == "deleting" and not envs.has("dev/a")
    clock.advance(minutes=15)
    await vms.reap_once()
    assert len(sent) == 1 and sent[0]["urgent"] and "past its time limit" in sent[0]["title"]
    prov.fail_delete = 0
    clock.advance(minutes=15)
    out = await vms.reap_once()
    assert out["deleted"] == [rec.id] and vms.store.get(rec.id).state == "deleted"


async def test_a_vm_whose_ssh_never_answers_is_deleted(env, monkeypatch):
    import openajar.vms.manager as m
    vms, envs, prov, _clock = env

    async def bad_probe(remote, *, timeout_s=15):
        return False, "Host key verification failed."

    vms._probe = bad_probe
    vms.settings = vms.settings.model_copy(update={"boot_timeout_s": 0.2})
    monkeypatch.setattr(m, "BOOT_POLL_S", 0.01)
    rec = await vms.create("dev/a", ttl_minutes=30, wait_s=5)
    fresh = vms.store.get(rec.id)
    assert fresh.state == "failed" and "Host key verification failed" in fresh.error
    assert rec.provider_id in prov.deleted and not envs.has("dev/a")


async def test_a_boot_failure_with_an_empty_message_still_says_why(env, monkeypatch, caplog):
    import openajar.vms.manager as m
    vms, _envs, _prov, _clock = env

    async def silent_probe(remote, *, timeout_s=15):
        raise TimeoutError()  # str() is "": the log line once ended in "failed to boot: "

    vms._probe = silent_probe
    monkeypatch.setattr(m, "BOOT_POLL_S", 0.01)
    with caplog.at_level("WARNING", logger="openajar.vms"):
        rec = await vms.create("dev/b", ttl_minutes=30, wait_s=5)
    fresh = vms.store.get(rec.id)
    assert fresh.state == "failed" and fresh.error.startswith("TimeoutError; the provider says the server is ")
    line = next(r.getMessage() for r in caplog.records if "failed to boot" in r.getMessage())
    assert line.endswith(fresh.error) and not line.rstrip().endswith(":")


async def test_extend_stops_at_the_node_maximum(env):
    vms, _, _, clock = env
    rec = await vms.create("dev/a", ttl_minutes=60)
    rec = await vms.extend("dev/a", 30)
    assert rec.expires_at == clock.now + timedelta(minutes=90)
    rec = await vms.extend("dev/a", 10_000)
    assert rec.expires_at == rec.created_at + timedelta(minutes=24 * 60)
    with pytest.raises(VmLimit):
        await vms.extend("dev/a", 5)


async def test_a_create_that_fails_leaves_no_keys_or_lane(env):
    vms, envs, prov, _ = env
    prov.fail_create = 1
    with pytest.raises(VmError):
        await vms.create("dev/a", ttl_minutes=30)
    recs = vms.store.recent()
    assert recs[0].state == "failed" and not envs.has("dev/a") and not (vms.dir / recs[0].id).exists()
    assert prov.keys == {}  # the SSH key resource is removed even when the server was refused


# ------------------------------------------------------------------ ssh


def test_ssh_command_pins_the_host_key_and_forwards_nothing(tmp_path):
    from openajar.env.models import RemoteHost
    r = RemoteHost(vm_id="vm_1", host="203.0.113.9", key_path="/k", known_hosts="/kh", host_alias="openajar-vm_1")
    argv = ssh.ssh_base(r)
    joined = " ".join(argv)
    for opt in ("StrictHostKeyChecking=yes", "UserKnownHostsFile=/kh", "HostKeyAlias=openajar-vm_1",
                "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none", "ForwardAgent=no",
                "PasswordAuthentication=no", "GlobalKnownHostsFile=/dev/null"):
        assert opt in joined
    assert argv[:3] == ["ssh", "-F", "/dev/null"] and argv[-1] == "root@203.0.113.9" and "-T" in argv
    assert "-tt" in ssh.ssh_base(r, tty=True)


def test_remote_command_quotes_everything():
    wl = Workload(wid="w", argv=["/bin/sh", "-c", "echo 'a b'; rm -rf $HOME"], env={"X": "1 2", "BAD-NAME": "z"},
                  cwd="/srv/my app")
    cmd = ssh.remote_command(wl)
    group = "sh -c 'echo $$ > /tmp/agentos-wl/w.pgid; \"$@\"; rc=$?; trap \"\" TERM; kill -s TERM -- -$$ 2>/dev/null; rm -f /tmp/agentos-wl/w.pgid; exit $rc' sh"
    assert cmd == ("mkdir -p /tmp/agentos-wl && cd '/srv/my app' && exec setsid -w " + group +
                   " env X='1 2' /bin/sh -c 'echo '\"'\"'a b'\"'\"'; rm -rf $HOME'")
    assert ssh.remote_command(Workload(wid="w", argv=["true"], env={})) == f"mkdir -p /tmp/agentos-wl && cd && exec setsid -w {group} true"
    assert ssh.remote_command(Workload(wid="w", argv=["true"], env={}, pty=True)) == "cd && exec true"


# ------------------------------------------------------------------ hetzner


def _mock(handler):
    return HetznerProvider("tok-SECRET", transport=httpx.MockTransport(handler))


async def test_hetzner_create_sends_the_key_firewall_labels_and_never_the_token_in_errors():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer tok-SECRET"
        if req.url.path.endswith("/servers") and req.method == "POST":
            return httpx.Response(201, json={"server": {"id": 42, "name": "n", "status": "initializing",
                                                         "public_net": {"ipv4": {"ip": "203.0.113.5"}},
                                                         "labels": {"a": "b"}}, "root_password": None})
        return httpx.Response(403, json={"error": {"code": "forbidden", "message": "insufficient permissions"}})

    p = _mock(handler)
    s = await p.create_server(ServerSpec(name="n", server_type="cx23", image="123", location="nbg1",
                                         user_data="#cloud-config\n", labels={"a": "b"}, ssh_key_id="7",
                                         firewall_id="9"))
    body = json.loads(seen[0].content)
    assert s == Server(id="42", name="n", status="initializing", ipv4="203.0.113.5", labels={"a": "b"})
    assert body["ssh_keys"] == [7] and body["firewalls"] == [{"firewall": 9}] and body["image"] == 123
    with pytest.raises(VmError) as e:
        await p.delete_server("42")
    assert "read-only token" in e.value.message and "SECRET" not in e.value.message and "SECRET" not in repr(p)


async def test_hetzner_401_says_how_to_fix_and_404_delete_is_fine():
    def handler(req):
        if req.url.path.endswith("/servers/1"):  # the read back before a delete, then the delete: gone
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "unable to authenticate"}})

    p = _mock(handler)
    await p.delete_server("1")
    with pytest.raises(VmError, match="openajar vm setup"):
        await p.check()


async def test_hetzner_lists_by_label_across_pages_and_reads_prices():
    def handler(req):
        if req.url.path.endswith("/server_types"):
            return httpx.Response(200, json={"server_types": [{"name": "cx23", "prices": [
                {"location": "nbg1", "price_hourly": {"net": "0.005", "gross": "0.006"}}]}]})
        page = int(req.url.params["page"])
        assert req.url.params["label_selector"] == "agentos-node=n1"
        return httpx.Response(200, json={"servers": [{"id": page, "status": "running", "labels": {}}],
                                         "meta": {"pagination": {"next_page": 2 if page == 1 else None}}})

    p = _mock(handler)
    assert [s.id for s in await p.list_servers({"agentos-node": "n1"})] == ["1", "2"]
    assert await p.price_hourly("cx23", "nbg1") == 0.006
    with pytest.raises(VmError, match="not offered in fsn1"):
        await p.price_hourly("cx23", "fsn1")


async def test_hetzner_types_keep_only_what_can_be_ordered_and_codes_reach_the_error():
    def price(loc, gross):
        return {"location": loc, "price_hourly": {"gross": gross}}

    def handler(req):
        if req.url.path.endswith("/server_types"):
            return httpx.Response(200, json={"server_types": [
                {"id": 1, "name": "cx23", "cores": 2, "memory": 4.0, "disk": 40, "architecture": "x86",
                 "cpu_type": "shared", "prices": [price("nbg1", "0.006"), price("fsn1", "0.006")],
                 "locations": [{"name": "fsn1", "deprecation": {"unavailable_after": "2026-01-01"}}]},
                {"id": 2, "name": "cx22", "deprecated": True, "prices": [price("nbg1", "0.005")]},
                {"id": 3, "name": "cpx22", "cores": 2, "memory": 4.0, "disk": 80, "architecture": "x86",
                 "cpu_type": "shared", "prices": [price("nbg1", "0.0095")]}]})
        if req.url.path.endswith("/datacenters"):
            return httpx.Response(200, json={"datacenters": [
                {"location": {"name": "nbg1"}, "server_types": {"available": [3], "supported": [1, 3]}},
                {"location": {"name": "fsn1"}, "server_types": {"available": [1], "supported": [1]}}]})
        if req.url.path.endswith("/servers"):
            return httpx.Response(412, json={"error": {"code": "resource_unavailable", "message": "sold out"}})
        raise AssertionError(req.url)

    p = _mock(handler)
    types = {t["name"]: t for t in await p.server_types()}
    assert set(types) == {"cx23", "cpx22"}                      # cx22 is deprecated
    assert types["cx23"]["price_hourly_eur"] == {"nbg1": 0.006}   # going away in fsn1
    assert types["cx23"]["available"] == ["fsn1"] and types["cpx22"]["available"] == ["nbg1"]
    with pytest.raises(VmError) as e:
        await p.create_server(ServerSpec(name="n", server_type="cx23", image="ubuntu-24.04", location="nbg1",
                                         user_data="", labels={}))
    assert e.value.provider_code == "resource_unavailable"


# ------------------------------------------------------------------ tools and policy


class Ctx:
    def __init__(self, runtime, deny=False):
        self.runtime = runtime
        self.principal = Principal(id="test-agent")
        self.calls: list[dict] = []
        self.traces: list[tuple] = []
        self.deny = deny

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
        self.calls.append({"effect": effect, "lane_id": lane_id, "summary": summary, "facts": facts})
        if self.deny:
            raise PolicyDenied("no")

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))

    def can_access_lane(self, lane_id):
        return lane_id.startswith("dev/")


async def test_tools_authorize_vm_with_the_price_and_profile_then_exec_works_like_a_lane(env):
    vms, envs, _prov, _ = env
    specs = {s.name: s for s in tool_specs(vms.runtime)}
    ctx = Ctx(vms.runtime)
    out = await specs["vm_create"].handler(ctx, on="dev/a", ttl_minutes=90, purpose="tests")
    call = ctx.calls[0]
    assert call["effect"] == Effect.VM and call["lane_id"] == "dev/a" and call["facts"] == {"profile": "small"}
    assert "cx23" in call["summary"] and "EUR 0.0060 an hour" in call["summary"] and "EUR 0.01 for 90 min" in call["summary"]
    assert "at most EUR 0.10 if only a dearer type has room); if it is sold out: cpx22" in call["summary"]
    assert out["state"] == "ready" and "terminal_exec" in out["next"]
    listed = await specs["vms_list"].handler(ctx)
    assert [v["lane"] for v in listed["vms"]] == ["dev/a"] and "small" in listed["profiles"]
    denied = Ctx(vms.runtime, deny=True)
    with pytest.raises(PolicyDenied):
        await specs["vm_destroy"].handler(denied, on="dev/a")
    assert envs.has("dev/a")
    await specs["vm_destroy"].handler(ctx, on="dev/a")
    assert not envs.has("dev/a")


async def test_exec_on_a_vm_lane_is_routed_as_isolated_and_runs_over_ssh(env):
    from openajar.env.tools import tool_specs as env_specs
    vms, envs, _, _ = env
    await vms.create("dev/a", ttl_minutes=30)
    Ctx(vms.runtime)
    lane = envs.get("dev/a")
    from openajar.env.tools import _facts, _where
    assert _facts(lane) == {"isolated": True} and "disposable cloud machine" in _where(lane)
    rt = await envs.warm("dev/a")
    cmd = rt.launcher.command(None, lane, Workload(wid="w", argv=["uname", "-a"], env={}), cgroup=None,
                              workdir=envs.paths.workdir("dev/a"))
    assert cmd.argv[0] == "ssh" and cmd.argv[-1].endswith(" sh uname -a") and "setsid -w" in cmd.argv[-1] and f"root@{lane.remote.host}" in cmd.argv
    assert "OPENAJAR" not in json.dumps(cmd.env)
    assert {s.name for s in env_specs(vms.runtime)} >= {"terminal_exec", "shell_open"}


def test_policy_routes_vm_to_a_person_and_grants_limit_profiles():
    from openajar.policy.grants import Specials, VmCap
    from openajar.policy.routing import RoutingTable
    table = RoutingTable()
    assert table.decide(Effect.VM, {}).then == "approval"
    assert "vm" in table.standing_effects and "vm" in table.sink_effects
    assert Specials.everything().vm == VmCap()
    with pytest.raises((ValueError, InvalidRequest)):
        VmCap(lanes=["bad pattern with spaces"])
    assert VmCap(repos=["github.com/acme/*"]).repos == ["github.com/acme/*"]
    with pytest.raises((ValueError, InvalidRequest)):
        VmCap(repos=["https://github.com/acme/*"])
    from openajar.core import globs
    from openajar.policy.grants import _cap_widening
    assert globs.repo_match("github.com/acme/*", "github.com/acme/api")
    assert not globs.repo_match("github.com/acme/*", "github.com/acme2/api")
    parent, child = VmCap(repos=["github.com/acme/*"]), VmCap(repos=["github.com/other/x"])
    assert "repos" in _cap_widening("vm", child, [parent], [], {})
    assert "repos" not in _cap_widening("vm", VmCap(repos=["github.com/acme/api"]), [parent], [], {})


# ---------------------------------------------------------------- sizing: the cheapest that fits

async def test_the_default_size_is_the_cheapest_x86_type_and_minimums_pick_again(env):
    vms, *_ = env
    plan = await vms.plan("dev/a")
    assert plan["server_type"] == "cx23" and plan["profile"] == "small"   # cax11 is cheaper but arm
    assert (await vms.plan("dev/a", size={"arch": "any"}))["server_type"] == "cax11"
    p = await vms.plan("dev/a", size={"min_cpu": 4})
    assert p["server_type"] == "cpx42" and p["profile"] == "custom"       # 8 vCPU shared beats 4 dedicated
    assert (await vms.plan("dev/a", size={"min_memory_gb": 16, "cpu_type": "dedicated"}))["server_type"] == "ccx23"
    assert (await vms.plan("dev/a", profile="build"))["server_type"] == "cpx42"
    pinned = await vms.plan("dev/a", size={"server_type": "ccx23"})
    assert pinned["server_type"] == "ccx23" and pinned["fallbacks"] == [] and pinned["price_hourly_eur"] == 0.05
    with pytest.raises(VmError, match="no server type"):
        await vms.plan("dev/a", size={"min_cpu": 64})
    with pytest.raises(InvalidRequest):
        await vms.plan("dev/a", size={"arch": "sparc"})


async def test_a_sold_out_type_falls_back_to_the_next_cheapest(env):
    vms, _, prov, _ = env
    prov.unavailable = {"cx23"}
    rec = await vms.create("dev/a", ttl_minutes=30)
    assert rec.state == "ready" and rec.server_type == "cpx22" and rec.price_hourly_eur == 0.0095
    prov.unavailable = {"cpx42"}
    with pytest.raises(VmError, match="no room for cpx42 in nbg1, fsn1 or hel1 now; try again in 5 minutes"):
        await vms.create("dev/b", profile="build", ttl_minutes=30)  # no other type has 8 CPU and 16 GB


async def test_a_sold_out_type_is_tried_in_the_other_locations_of_its_zone_first(env):
    vms, _, prov, _ = env
    prov.unavailable = {"cx23@nbg1"}
    rec = await vms.create("dev/a", ttl_minutes=30)
    assert (rec.server_type, rec.location) == ("cx23", "fsn1") and prov.made_in == [("cx23", "fsn1")]
    prov.unavailable = {"cx23@nbg1", "cx23@fsn1", "cx23@hel1"}
    rec = await vms.create("dev/b", ttl_minutes=30)
    assert (rec.server_type, rec.location) == ("cpx22", "nbg1")   # only then the next type, at home first


async def test_no_fallback_type_has_a_smaller_disk_than_the_image(env):
    vms, _, prov, _ = env
    prov.snapshots["snap-7"] = {"agentos-image": "dev"}
    prov.image_disk_gb = 60
    plan = await vms.plan("dev/a", image="snapshot:dev")
    assert plan["server_type"] == "cpx22" and plan["image"] == "snapshot:dev"   # cx23 has 40 GB
    assert all(prov.specs[t][2] >= 60 for t, _p in plan["fallbacks"])
    # a type the provider refuses for its disk is skipped in every location, and the error says so
    from openajar.vms import capacity
    calls = []

    async def make(t, loc):
        calls.append((t, loc))
        raise VmError("Hetzner: service_error: image disk is bigger than server type disk", provider_code="service_error")
    with pytest.raises(VmError, match="does not fit the disk of cx23; ask for a bigger size") as e:
        await capacity.create_first("hetzner", [("cx23", "nbg1", 1.0), ("cx23", "fsn1", 1.0)], make)
    assert calls == [("cx23", "nbg1")] and e.value.provider_code == "image_too_big"


def test_no_room_words_name_every_type_and_location_tried():
    from openajar.vms import capacity
    words = capacity.no_room_words("hetzner", [("cx23", "nbg1"), ("cx23", "fsn1"), ("cx23", "hel1"), ("cx33", "nbg1")])
    assert words == ("Hetzner has no room for cx23 in nbg1, fsn1 or hel1 now, nor for cx33 in nbg1; "
                     "try again in 5 minutes")
    assert capacity.zone_locations("fsn1", capacity.ZONES) == ["fsn1", "nbg1", "hel1"]
    assert capacity.zone_locations("ash", capacity.ZONES) == ["ash"]


# ---------------------------------------------------------------- capacity: every family, the type sold here

def hetzner_2026(prov):
    """The types of the run that needed three tries (2026-11-21): cx43 and cx53 out of room in nbg1,
    fsn1 and hel1, cpx42 with room at more than four times cx43's price, cpx41 sold only outside eu-central."""
    for name, price, spec in (("cx33", 0.0088, (4, 8.0, 80, "x86", "shared")),
                              ("cx43", 0.0304, (8, 16.0, 160, "x86", "shared")),
                              ("cx53", 0.057, (16, 32.0, 320, "x86", "shared")),
                              ("cpx41", 0.06, (8, 16.0, 240, "x86", "shared")),
                              ("cpx42", 0.135, (8, 16.0, 320, "x86", "shared")),
                              ("ccx33", 0.2, (8, 32.0, 240, "x86", "dedicated")),
                              ("cpx62", 0.25, (16, 32.0, 640, "x86", "shared"))):
        prov.prices[name], prov.specs[name] = price, spec
    eu = ["nbg1", "fsn1", "hel1"]
    prov.listed = {n: (["ash"] if n == "cpx41" else eu) for n in prov.prices}


async def test_a_size_with_no_room_in_one_family_is_made_from_another_and_says_what_it_costs(env):
    """size large had no room for cx43 or cx53 anywhere in eu-central, and cpx42 (as much CPU and memory)
    had room, but the plan only held types within twice cx43's price, so the agent found cpx42 by hand.
    A size now tries every family, cheapest first, and the VM says it costs 4.4 times the plan."""
    vms, _, prov, _ = env
    hetzner_2026(prov)
    plan = await vms.plan("dev/a", size={"min_cpu": 8, "min_memory_gb": 16})
    assert plan["server_type"] == "cx43"
    assert [t for t, _p in plan["fallbacks"]] == ["cx53", "cpx42", "ccx33", "cpx62"]  # at most 2 of a family
    prov.unavailable = {"cx43", "cx53"}
    rec = await vms.create("dev/a", plan=plan)
    assert (rec.server_type, rec.location, rec.price_hourly_eur) == ("cpx42", "nbg1", 0.135)
    assert rec.asked == ("cx43", 0.0304) and len(rec.no_room) == 6
    assert vms.brief(rec)["fell_back"] == ("No room for cx43 or cx53 now, so this is cpx42, with at least as much, "
                                           "at EUR 0.1350 an hour (4.4 times the EUR 0.0304 planned for cx43)")


async def test_the_owners_budget_bounds_the_fallbacks_not_a_price_factor(env):
    vms, _, prov, _ = env
    hetzner_2026(prov)
    vms.settings.monthly_budget_eur = 0.15  # one hour of cpx42 fits; ccx33 and cpx62 do not
    prov.unavailable = {"cx43", "cx53"}
    rec = await vms.create("dev/a", size={"min_cpu": 8, "min_memory_gb": 16}, ttl_minutes=30)
    assert rec.server_type == "cpx42" and [t for t, _p in rec.fallbacks] == ["cx53", "cpx42"]
    prov.unavailable = {"cx43", "cx53", "cpx42"}
    vms.settings.monthly_budget_eur += 0.135  # what the first one booked: the same room again
    with pytest.raises(VmError, match="no room for cx43, cx53 or cpx42 in nbg1, fsn1 or hel1 now"):
        await vms.create("dev/b", size={"min_cpu": 8, "min_memory_gb": 16}, ttl_minutes=30)
    assert prov.made_in == [("cpx42", "nbg1")]  # never ccx33 or cpx62, past what the month has left


async def test_a_type_by_name_the_location_does_not_sell_is_refused_before_the_provider_is_asked(env):
    """cpx41 in nbg1 came back as Hetzner's raw "invalid_input: unsupported location"."""
    vms, _, prov, _ = env
    hetzner_2026(prov)
    with pytest.raises(InvalidRequest) as e:
        await vms.create("dev/a", size={"server_type": "cpx41"}, ttl_minutes=30)
    assert str(e.value.message) == (
        "Fake does not sell cpx41 in nbg1 now (it sells it in ash). Sold in nbg1 with at least its 8 CPU and 16 GB: "
        "cx43 (8 CPU, 16 GB, EUR 0.0304 an hour), cpx42 (8 CPU, 16 GB, EUR 0.1350 an hour); ask for one of those. "
        "Nothing was rented")
    assert prov.made_in == [] and vms.store.recent() == []
    assert (await vms.plan("dev/a", size={"server_type": "cpx42"}))["server_type"] == "cpx42"  # sold here: as before


def test_fallbacks_take_every_family_before_a_second_of_one():
    from openajar.vms import capacity
    fit = [{"name": n, "price": p} for n, p in (("cx43", 1), ("cx53", 2), ("cx63", 3), ("cpx42", 4), ("cpx52", 5),
                                                  ("ccx33", 6), ("ccx43", 7), ("cax41", 8))]
    picked = capacity.pick_fallbacks(fit[0], fit, n=4)
    assert [t for t, _p in picked] == ["cx53", "cpx42", "ccx33", "cax41"]  # each family once, in price order
    assert [t for t, _p in capacity.pick_fallbacks(fit[0], fit)] == ["cx53", "cpx42", "cpx52", "ccx33", "cax41"]
    assert capacity.family("ccx33") == "ccx" and capacity.family("docker-large") == "docker"


# ---------------------------------------------------------------- private repos

class RepoVault:
    def __init__(self):
        self.data: dict[str, tuple[str, dict]] = {}
        self.reads: list[str] = []

    async def put_secret(self, ref, value, *, kind, label, scope):
        self.data[ref] = (value, {"ref": ref, "kind": kind, "label": label, "scope": scope, "use_count": 0,
                                  "last_used_at": None})

    async def get_secret(self, ref, *, purpose):
        self.reads.append(purpose)
        return self.data[ref][0] if ref in self.data else None

    async def delete_secret(self, ref):
        self.data.pop(ref, None)

    async def list_secrets(self):
        return [m for _v, m in self.data.values()]


def test_repo_names_are_https_on_public_hosts_without_credentials():
    from openajar.vms.repos import best_match, parse_pattern, parse_repo
    for text in ("github.com/acme/api", "https://github.com/acme/api.git", "git@github.com:acme/api.git",
                 "ssh://git@github.com/acme/api"):
        r = parse_repo(text)
        assert (r.name, r.url, r.short) == ("github.com/acme/api", "https://github.com/acme/api.git", "api")
    assert parse_repo("gitlab.com/g/sub/repo").path == "g/sub/repo"
    for bad in ("https://user:tok@github.com/acme/api", "https://x@github.com/a/b", "file:///etc/passwd",
                "127.0.0.1/a/b", "localhost/a/b", "github.com/acme", "github.com/../x", "gitlab.internal/a/b",
                "https://github.com:8443/a/b"):
        with pytest.raises(InvalidRequest):
            parse_repo(bad)
    assert parse_pattern("github.com/acme/*") == "github.com/acme/*"
    repo = parse_repo("github.com/acme/api")
    assert best_match(["github.com/acme/*", "github.com/acme/api", "github.com/*"], repo) == "github.com/acme/api"
    assert best_match(["github.com/*", "github.com/acme/*"], repo) == "github.com/acme/*"
    assert best_match(["github.com/other/*"], repo) is None


async def test_repo_tokens_live_in_the_vault_and_need_one():
    from openajar.vms.repos import RepoTokens, parse_repo
    with pytest.raises(VmError, match="vault init"):
        await RepoTokens(None).add("github.com/acme/*", "tok")
    vault = RepoVault()
    t = RepoTokens(vault)
    assert await t.add("github.com/acme/*", "ghp_secret") == {"pattern": "github.com/acme/*", "user": "x-access-token"}
    assert [r["pattern"] for r in await t.list()] == ["github.com/acme/*"]
    assert "ghp_secret" not in json.dumps(await t.list())
    assert await t.token_for(parse_repo("github.com/acme/api"), purpose="p") == \
        ("github.com/acme/*", "x-access-token", "ghp_secret")
    assert await t.token_for(parse_repo("github.com/other/api"), purpose="p") is None
    with pytest.raises(InvalidRequest):
        await t.add("github.com/acme/*", "two words")
    assert await t.remove("github.com/acme/*") and await t.list() == []


@pytest.fixture
def git_origin(tmp_path, monkeypatch):
    """A local repo standing in for github.com/acme/api, and an `ssh` that runs the remote
    command here, so the tar stream lands in tmp_path."""
    import subprocess

    from openajar.vms import repos as vmrepos
    src = tmp_path / "origin"
    src.mkdir()
    run = {"cwd": src, "capture_output": True,
           "env": {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@x"}}
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True, **run)
    (src / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], check=True, **run)
    subprocess.run(["git", "commit", "-qm", "one"], check=True, **run)
    subprocess.run(["git", "checkout", "-qb", "feature"], check=True, **run)
    (src / "feature.txt").write_text("f\n")
    subprocess.run(["git", "add", "."], check=True, **run)
    subprocess.run(["git", "commit", "-qm", "two"], check=True, **run)
    subprocess.run(["git", "checkout", "-q", "main"], check=True, **run)
    monkeypatch.setattr(vmrepos, "ALLOWED_PROTOCOLS", ("https", "file"))
    monkeypatch.setattr(vmrepos.RepoName, "url", property(lambda self: f"file://{src}"))
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text('#!/bin/sh\nfor a; do last="$a"; done\nexec sh -c "$last"\n')
    fake_ssh.chmod(0o755)
    monkeypatch.setattr(ssh, "SSH", str(fake_ssh))
    return src


async def test_vm_clone_copies_the_checkout_and_never_the_token(env, git_origin, tmp_path):
    vms, _, _, _ = env
    vault = RepoVault()
    vms.runtime.register("vault", vault)
    await vms.create("dev/a", ttl_minutes=30)
    specs = {s.name: s for s in tool_specs(vms.runtime)}
    ctx = Ctx(vms.runtime)
    with pytest.raises(Exception, match="openajar vm repo add"):
        await specs["vm_clone"].handler(ctx, on="dev/a", repo="github.com/acme/api")
    assert ctx.calls == []  # no approval asked for a clone that cannot happen
    await vms.repo_tokens().add("github.com/acme/*", "ghp_TOPSECRET")
    dest = str(tmp_path / "vm" / "src" / "api")
    out = await specs["vm_clone"].handler(ctx, on="dev/a", repo="https://github.com/acme/api", ref="feature",
                                          dest=dest)
    call = ctx.calls[-1]
    assert call["effect"] == Effect.VM and call["facts"] == {"repo": "github.com/acme/api"}
    assert "github.com/acme/*" in call["summary"] and "ghp_TOPSECRET" not in call["summary"]
    assert out["dest"] == dest and len(out["commit"]) == 40 and out["token"] == "github.com/acme/*"
    from pathlib import Path
    got = Path(dest)
    assert (got / "README.md").read_text() == "hello\n" and (got / "feature.txt").exists()
    assert "ghp_TOPSECRET" not in (got / ".git" / "config").read_text()
    assert vault.reads == [f"vm_clone github.com/acme/api to {out['vm']}"]
    assert not any((vms.dir / "clones").iterdir())  # the node's copy is gone
    with pytest.raises(VmError, match="not empty"):
        await vms.clone("dev/a", "github.com/acme/api", dest=dest)
    with pytest.raises(VmError, match="no branch"):
        await vms.clone("dev/a", "github.com/acme/api", git_ref="nope", dest=dest + "2")
    shallow = await vms.clone("dev/a", "github.com/acme/api", depth=1, dest=dest + "3")
    assert not (Path(shallow["dest"]) / "feature.txt").exists()
    with pytest.raises(InvalidRequest):
        await vms.clone("dev/a", "github.com/acme/api", dest="relative/dir")


def test_git_errors_never_show_the_token():
    from openajar.vms.repos import _scrub
    assert _scrub("fatal: https://x:ghp_abc@github.com failed", "ghp_abc") == "fatal: https://x:***@github.com failed"


def test_a_docker_machine_is_made_with_its_walls_and_vms_tomls_limits(tmp_path):
    """The docker provider's `docker create` (proven on a real docker in test_environments_docker.py)."""
    s = VmSettings(provider="docker", docker_cpus=1.5, docker_memory_gb=2, docker_pids=256)
    prov = VmManager(tmp_path, runtime=Runtime(), settings=s).provider()
    spec = ServerSpec(name="openajar-vm-abc", server_type="docker", image="agentos-env:latest", location="local",
                      user_data="", labels={"agentos-node": "n", "agentos-home": "h1"})
    args = prov.run_args(spec)
    from itertools import pairwise

    pairs = set(pairwise(args))
    assert {("--network", "openajar-vm-abc"), ("--cpus", "1.5"), ("--memory", str(2 * 2**30)),
            ("--memory-swap", str(2 * 2**30)), ("--pids-limit", "256"), ("--cap-drop", "ALL"),
            ("-e", "AGENTOS_ENV_WALL=1"), ("-p", "127.0.0.1::22"), ("--label", "agentos-home=h1")} <= pairs
    added = {b for a, b in pairs if a == "--cap-add"}
    assert "NET_ADMIN" in added and not added & {"SYS_ADMIN", "NET_RAW", "MKNOD", "SYS_PTRACE"}
    assert "--privileged" not in args and args[-1] == "agentos-env:latest"


def test_setup_says_plainly_what_the_docker_provider_does_not_isolate():
    from openajar.environments.cli import docker_isolation_words

    mac, linux = docker_isolation_words("darwin"), docker_isolation_words("linux")
    for words in (mac, linux):
        assert "own network" in words and "for your own code" in words and "isolated one" in words
        assert chr(0x2014) not in words
    assert "Docker Desktop" in mac and "another account on this Mac" in mac
    assert "Docker Desktop" not in linux


class _Environments:
    """The environments service as the VM reaper asks it: its passes finish; `hold` is what holds_machine says."""

    def __init__(self, hold: str | None = None) -> None:
        self.hold = hold

    def reap_mark(self) -> int:
        return 0

    def reaped_since(self, mark: int) -> bool:
        return True

    def holds_machine(self, env_id: str) -> str | None:
        return self.hold


def _restarted(tmp_path, envs, prov, clock, environments, awake, told) -> VmManager:
    """A new VmManager on the same home: the node's process started again, with an awake clock of its own."""
    runtime = Runtime(envs=envs)
    runtime.register("environments", environments)
    vms = VmManager(tmp_path / "home", runtime=runtime, provider=prov, node_name="node-1", probe=ok_probe, clock=clock,
                    settings=VmSettings(max_live=2, monthly_budget_eur=10.0, ssh_from=["198.51.100.7/32"]),
                    awake=awake)
    runtime.register("vms", vms)

    async def notify(title, body, *, urgent=False):
        told.append(title)

    vms._notify = notify
    return vms


async def _crash_loop(tmp_path, environments, *, restarts: int):
    """R1 #2's proof: an environment's machine two days past its limit, and a node that restarts every 10 minutes
    of awake time (a crash loop, a supervisor). Returns the record after it, and the notifications."""
    envs = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    clock, prov, told = Clock(), FakeProvider(), []
    vms = _restarted(tmp_path, envs, prov, clock, environments, lambda: 0.0, told)
    rec = await vms.create("env/e1-term", ttl_minutes=60, purpose="an environment")
    rec = vms.store.get(rec.id)
    rec.environment = "e1"
    vms.store.put(rec)
    clock.advance(days=2)
    for _ in range(restarts):
        t = [0.0]
        vms = _restarted(tmp_path, envs, prov, clock, environments, lambda t=t: t[0], told)
        for _ in range(3):
            await vms.reap_once()
            t[0] += 300
            clock.advance(minutes=5)
    await envs.shutdown()
    return vms.store.get(rec.id), told


async def test_a_node_that_restarts_often_still_lets_an_expired_machine_go_after_its_grace(tmp_path):
    """R1 #2: the grace counted awake time from this process's first sight, in memory, so a node that restarted
    more often than every 15 minutes kept an expired machine forever (0.7.0 deleted it on its first pass). The
    awake time it was kept is on the record now."""
    rec, _told = await _crash_loop(tmp_path, _Environments(), restarts=3)
    assert rec.state == "deleted" and rec.delete_reason == "time limit", (rec.state, rec.held_s)


async def test_a_held_machine_is_told_once_across_restarts(tmp_path):
    """R1 #2's other half: a machine its environment holds (its save keeps failing) bills on, and the owner hears of
    it once, never at every start and never not at all."""
    rec, told = await _crash_loop(tmp_path, _Environments(hold="its last save did not hold 1 changed file"),
                                  restarts=6)
    assert rec.state == "ready" and rec.overdue_told
    assert told.count(f"VM {rec.id} is still running past its time limit") == 1, told
