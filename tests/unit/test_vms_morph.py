"""Morph Cloud (openajar.vms.morph) against a fake Morph API (httpx.MockTransport): the key check,
sizes and prices, create, get, list, delete and snapshots with metadata labels, image names, the
SSH command line (the key never on it), pause and resume, the TTL backstop, and the settings that
pick the provider. No real Morph account is used."""

from __future__ import annotations

import itertools
import json
import os
import tomllib
from datetime import timedelta

import httpx
import pytest

from openajar.core.runtime import Runtime
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments.tunnels import git_tunnel_argv, tunnel_argv
from openajar.vms import morph, providers, ssh
from openajar.vms.manager import VmManager
from openajar.vms.models import MORPH_TOKEN_FILE, VmError, VmSettings
from openajar.vms.morph import MorphProvider, mcu_per_hour
from openajar.vms.provider import ServerSpec, capable

KEY = "morph_SECRETKEY_123"
GATEWAY = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGatewayHostKeyForTests"]
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\ninstance-key\n-----END OPENSSH PRIVATE KEY-----\n"


class FakeMorph:
    """Morph's API as far as openajar uses it: instances, snapshots, pause, keys, TTLs and metadata."""

    def __init__(self):
        self.instances: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.ids = itertools.count(1)
        self.fail: dict[str, list[int]] = {}   # "METHOD /path": status codes to answer first
        self.created = 1_800_000_000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _instance(self, spec: dict, meta: dict, ttl: dict, snapshot_id: str = "", image_id: str = "") -> dict:
        iid = f"morphvm_{next(self.ids):04d}"
        self.created += 10
        d = {"id": iid, "object": "instance", "created": self.created, "status": "ready", "spec": spec,
             "refs": {"snapshot_id": snapshot_id, "image_id": image_id}, "networking": {"http_services": []},
             "metadata": dict(meta or {}), "ttl": {"ttl_seconds": ttl.get("ttl_seconds"),
                                                   "ttl_action": ttl.get("ttl_action")},
             "wake_on": {"wake_on_ssh": False, "wake_on_http": False}}
        self.instances[iid] = d
        return d

    def snapshot(self, spec: dict, meta: dict, status: str = "ready") -> dict:
        sid = f"snapshot_{next(self.ids):04d}"
        self.created += 10
        d = {"id": sid, "object": "snapshot", "created": self.created, "status": status, "spec": spec,
             "refs": {"image_id": "morphvm-minimal"}, "metadata": dict(meta or {}), "ttl": {}}
        self.snapshots[sid] = d
        return d

    @staticmethod
    def _filter(items: list[dict], params: httpx.QueryParams) -> list[dict]:
        want = {k[len("metadata["):-1]: v for k, v in params.items() if k.startswith("metadata[")}
        return [i for i in items if all(i["metadata"].get(k) == v for k, v in want.items())]

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == f"Bearer {KEY}"
        path = req.url.path.removeprefix("/api")
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, dict(req.url.params), body))
        codes = self.fail.get(f"{req.method} {path}")
        if codes:
            return httpx.Response(codes.pop(0), json={"detail": "busy"})
        parts = path.strip("/").split("/")
        if req.method == "GET" and path == "/instance":
            return httpx.Response(200, json={"data": self._filter(list(self.instances.values()), req.url.params)})
        if req.method == "GET" and path == "/snapshot":
            return httpx.Response(200, json={"data": self._filter(list(self.snapshots.values()), req.url.params)})
        if req.method == "POST" and path == "/instance":
            snap = self.snapshots[body["snapshot_id"]]
            return httpx.Response(200, json=self._instance(snap["spec"], body.get("metadata"), body, snap["id"]))
        if req.method == "POST" and parts[0] == "image" and parts[-1] == "boot":
            spec = {k: body[k] for k in ("vcpus", "memory", "disk_size")}
            return httpx.Response(200, json=self._instance(spec, body.get("metadata"), body, image_id=parts[1]))
        if req.method == "POST" and parts[0] == "snapshot" and parts[-1] == "boot":
            spec = {k: body[k] for k in ("vcpus", "memory", "disk_size")}
            return httpx.Response(200, json=self._instance(spec, body.get("metadata"), body, parts[1]))
        if parts[0] == "snapshot" and len(parts) == 2:
            snap = self.snapshots.get(parts[1])
            if req.method == "DELETE":
                self.snapshots.pop(parts[1], None)
                return httpx.Response(200, json={} if snap else {"detail": "gone"}) if snap else \
                    httpx.Response(404, json={"detail": "no such snapshot"})
            return httpx.Response(200, json=snap) if snap else httpx.Response(404, json={"detail": "no such snapshot"})
        if parts[0] == "instance" and len(parts) >= 2:
            inst = self.instances.get(parts[1])
            if inst is None:
                return httpx.Response(404, json={"detail": "no such instance"})
            what = parts[2] if len(parts) > 2 else ""
            if req.method == "GET" and what == "":
                return httpx.Response(200, json=inst)
            if req.method == "DELETE":
                self.instances.pop(parts[1])
                return httpx.Response(200, json={})
            if what == "ssh":
                return httpx.Response(200, json={"object": "instance_ssh_key", "private_key": PRIVATE,
                                                 "public_key": "ssh-ed25519 AAAA", "password": "pw-SECRET",
                                                 "access_token": "tok-SECRET"})
            if what == "snapshot":
                return httpx.Response(200, json=self.snapshot(inst["spec"], (body or {}).get("metadata")))
            if what == "pause":
                inst["status"] = "paused"
                return httpx.Response(200, json=inst)
            if what == "resume":
                inst["status"] = "ready"
                return httpx.Response(200, json=inst)
            if what == "wake-on":
                inst["wake_on"].update(body)
                return httpx.Response(200, json=inst)
            if what == "ttl":
                inst["ttl"] = dict(body)
                return httpx.Response(200, json=inst)
        return httpx.Response(404, json={"detail": f"the fake has no {req.method} {path}"})


HOME = "h1"  # the home the provider deletes for (a VmManager hands it vms/home-id)


def provider(api: FakeMorph, home_id: str | None = HOME, **kw) -> MorphProvider:
    """None for home_id: as providers.make makes it, and the VmManager hands it vms/home-id."""
    p = MorphProvider(KEY, transport=api.transport(), host_keys=GATEWAY, **kw)
    p.retry_delays = (0.0, 0.0, 0.0)
    p.home_id = home_id
    return p


SMALL = {"vcpus": 2, "memory": 4096, "disk_size": 32768}


# ------------------------------------------------------------------ the key, sizes and prices


async def test_the_key_is_checked_and_never_shown():
    api = FakeMorph()
    await provider(api).check()
    assert api.calls[0][:2] == ("GET", "/instance")
    api.fail["GET /instance"] = [401]
    with pytest.raises(VmError) as e:
        await provider(api).check()
    assert "API key was refused" in e.value.message and "openajar vm setup --provider morph" in e.value.message
    assert KEY not in e.value.message and KEY not in repr(provider(api))
    with pytest.raises(VmError, match="no Morph API key"):
        MorphProvider("  ")


async def test_sizes_and_prices_are_morphs_mcus_in_eur_and_marked_estimates():
    p = provider(FakeMorph(), usd_per_mcu=0.05, eur_per_usd=0.9)
    types = {t["name"]: t for t in await p.server_types()}
    assert list(types) == ["morph-small", "morph-medium", "morph-large", "morph-xlarge"]
    small = types["morph-small"]
    assert (small["cores"], small["memory_gb"], small["disk_gb"]) == (2, 4.0, 32) and small["estimate"] is True
    assert small["price_hourly_eur"].get("nbg1") == small["price_hourly_eur"].get("morph") == round(2 * 0.05 * 0.9, 4)
    assert await p.price_hourly("morph-xlarge", "anywhere") == round(16 * 0.05 * 0.9, 4)
    assert mcu_per_hour(1, 16, 8) == 4 and mcu_per_hour(1, 1, 40) == 3
    assert await p.paused_eur_month("morph-small") == round((4 + 32) * 0.0073 * 0.9, 4)
    with pytest.raises(VmError, match="no server type"):
        await p.price_hourly("cx23", "nbg1")
    assert capable(p, "pause") and capable(p, "snapshot") and not capable(p, "cloud_init")
    assert not capable(p, "firewall") and not capable(p, "network_policy") and p.firewall is False


async def test_the_node_plans_the_cheapest_size_that_fits(tmp_path):
    api = FakeMorph()
    vms = VmManager(tmp_path, provider=provider(api), settings=VmSettings(provider="morph"), node_name="node-1")
    assert vms.settings.location == "morph"
    plan = await vms.plan("dev/a")
    assert plan["server_type"] == "morph-small" and plan["price_hourly_eur"] == round(2 * 0.05 * 0.86, 4)
    plan = await vms.plan("dev/a", size={"min_cpu": 6})
    assert plan["server_type"] == "morph-large" and plan["specs"]["memory_gb"] == 16.0


# ------------------------------------------------------------------ servers


async def test_create_from_the_base_image_boots_the_size_with_labels_ttl_and_the_instances_own_key():
    api = FakeMorph()
    p = provider(api)
    s = await p.create_server(ServerSpec(name="openajar-vm-1", server_type="morph-medium",
                                         image=await p.resolve_image("ubuntu-24.04"), location="morph",
                                         user_data="#cloud-config\n", labels={"agentos-node": "n1", "agentos-vm": "vm_1"},
                                         ttl_s=7200))
    method, path, _params, body = api.calls[0]
    assert (method, path) == ("POST", "/image/morphvm-minimal/boot")
    assert body["vcpus"] == 4 and body["memory"] == 8192 and body["disk_size"] == 65536
    assert body["metadata"] == {"agentos-node": "n1", "agentos-vm": "vm_1", "openajar-name": "openajar-vm-1"}
    assert body["ttl_seconds"] == 7200 and body["ttl_action"] == "stop"
    assert "user_data" not in body  # no cloud-init on Morph
    assert s.id.startswith("morphvm_") and s.status == "running" and s.server_type == "morph-medium"
    assert s.ipv4 == "ssh.cloud.morph.so" and s.ssh_user == s.id and s.ssh_port == 22
    assert s.ssh_key == PRIVATE and s.host_keys == GATEWAY and s.name == "openajar-vm-1"
    assert "instance-key" not in repr(s)  # the private key never in a log line


async def test_create_from_a_snapshot_starts_warm_at_its_size_and_boots_cold_into_another():
    api = FakeMorph()
    p = provider(api)
    snap = api.snapshot(SMALL, {"agentos-image": "dev"})
    await p.create_server(ServerSpec(name="a", server_type="morph-small", image=snap["id"], location="morph",
                                     user_data="", labels={}))
    assert next(c[:2] for c in api.calls if c[0] == "POST") == ("POST", "/instance")
    api.calls.clear()
    s = await p.create_server(ServerSpec(name="b", server_type="morph-large", image=snap["id"], location="morph",
                                         user_data="", labels={}))
    post = next(c for c in api.calls if c[0] == "POST")
    assert post[1] == f"/snapshot/{snap['id']}/boot" and post[3]["vcpus"] == 8 and s.server_type == "morph-large"


async def test_create_refuses_without_a_pinned_gateway_and_deletes_an_instance_it_cannot_reach():
    api = FakeMorph()
    p = MorphProvider(KEY, transport=api.transport())
    with pytest.raises(VmError, match="not pinned"):
        await p.create_server(ServerSpec(name="a", server_type="morph-small", image="morphvm-minimal",
                                         location="morph", user_data="", labels={}))
    p = provider(api)
    real = api.handle

    def no_key(req):
        if req.url.path.endswith("/ssh/key"):
            return httpx.Response(500, json={"detail": "not yet"})
        return real(req)

    p._transport = httpx.MockTransport(no_key)
    morph.KEY_WAIT_S, old = 0.0, morph.KEY_WAIT_S
    try:
        with pytest.raises(VmError, match="no SSH key"):  # made with this home's label, as the node labels it
            await p.create_server(ServerSpec(name="a", server_type="morph-small", image="morphvm-minimal",
                                             location="morph", user_data="", labels={"agentos-home": HOME}))
    finally:
        morph.KEY_WAIT_S = old
    assert api.instances == {}  # deleted, never left billing


async def test_get_list_by_metadata_and_delete():
    api = FakeMorph()
    p = provider(api)
    mine = api._instance(SMALL, {"agentos-node": "n1", "agentos-home": "h1"}, {})
    api._instance(SMALL, {"agentos-node": "other"}, {})
    listed = await p.list_servers({"agentos-node": "n1", "agentos-home": "h1"})
    assert [s.id for s in listed] == [mine["id"]] and listed[0].labels["agentos-home"] == "h1"
    params = [c[2] for c in api.calls if c[1] == "/instance"][-1]
    assert params == {"metadata[agentos-home]": "h1", "metadata[agentos-node]": "n1"}
    got = await p.get_server(mine["id"])
    assert got is not None and got.created.startswith("20") and got.server_type == "morph-small"
    await p.delete_server(mine["id"])
    assert await p.get_server(mine["id"]) is None
    await p.delete_server(mine["id"])  # gone already: fine


async def test_snapshots_carry_labels_as_metadata_and_resolve_by_name():
    api = FakeMorph()
    p = provider(api)
    inst = api._instance(SMALL, {}, {})
    sid = await p.create_snapshot(inst["id"], "openajar dev (node-1)", {"agentos-image": "dev", "agentos-home": "h1"})
    assert api.snapshots[sid]["metadata"] == {"agentos-image": "dev", "agentos-home": "h1",
                                              "openajar-description": "openajar dev (node-1)"}
    newer = api.snapshot(SMALL, {"agentos-image": "dev"})
    api.snapshot(SMALL, {"agentos-image": "dev"}, status="pending")  # not ready: never picked
    assert await p.resolve_image("snapshot:dev") == newer["id"]
    assert await p.resolve_image(sid) == sid
    with pytest.raises(VmError, match="openajar vm image build web"):
        await p.resolve_image("snapshot:web")
    info = await p.image_info("snapshot:dev")
    assert info == {"architecture": "x86", "disk_gb": 32.0}
    img = await p.get_image(sid)
    assert img["status"] == "available" and img["gb"] == 36.0 and img["labels"]["agentos-image"] == "dev"
    assert [i["id"] for i in await p.list_images({"agentos-home": "h1"})] == [sid]
    await p.delete_image(sid)
    assert await p.get_image(sid) is None
    await p.delete_image(sid)


async def test_busy_and_unreachable_calls_are_tried_again_and_errors_name_morph():
    api = FakeMorph()
    p = provider(api)
    api.fail["GET /instance"] = [503, 429]
    assert await p.list_servers({}) == []
    api.fail["POST /instance/x/pause"] = [503]
    with pytest.raises(VmError) as e:  # a 5xx on a write is not repeated: it may have happened
        await p.pause_server("x")
    assert "Morph" in e.value.message
    y = api._instance(SMALL, {"agentos-home": HOME}, {})["id"]
    api.fail[f"DELETE /instance/{y}"] = [402]
    with pytest.raises(VmError, match="no credit left"):
        await p.delete_server(y)


# ------------------------------------------------------------------ pause, resume, TTL


async def test_pause_turns_wake_on_off_first_and_resume_reads_the_key_again():
    api = FakeMorph()
    p = provider(api)
    inst = api._instance(SMALL, {}, {})
    await p.pause_server(inst["id"])
    assert [c[1] for c in api.calls[-2:]] == [f"/instance/{inst['id']}/wake-on", f"/instance/{inst['id']}/pause"]
    assert api.calls[-2][3] == {"wake_on_ssh": False, "wake_on_http": False}
    assert (await p.get_server(inst["id"])).status == "paused"
    s = await p.resume_server(inst["id"])
    assert s.status == "running" and s.ssh_key == PRIVATE and s.ipv4 == "ssh.cloud.morph.so"
    await p.set_ttl(inst["id"], 3600, "stop")
    assert api.instances[inst["id"]]["ttl"] == {"ttl_seconds": 3600, "ttl_action": "stop"}


# ------------------------------------------------------------------ the node: keys, SSH, settings


@pytest.fixture
async def node(tmp_path):
    home = tmp_path / "home"
    envs = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    api = FakeMorph()
    runtime = Runtime(envs=envs)

    async def ok_probe(remote, *, timeout_s=15):
        return True, ""

    vms = VmManager(home, runtime=runtime, provider=provider(api, home_id=None), settings=VmSettings(provider="morph"),
                    node_name="node-1", probe=ok_probe)
    runtime.register("vms", vms)
    yield vms, envs, api
    await vms.shutdown()
    await envs.shutdown()


async def test_a_morph_vm_signs_in_with_its_own_key_and_the_api_key_is_never_on_a_command_line(node):
    vms, envs, api = node
    rec = await vms.create("dev/m1", ttl_minutes=60)
    assert rec.state == "ready" and rec.host == "ssh.cloud.morph.so" and rec.ssh_user == rec.provider_id
    inst = api.instances[rec.provider_id]
    assert inst["metadata"]["agentos-vm"] == rec.id and inst["metadata"]["agentos-home"] == vms.home_id
    assert inst["ttl"]["ttl_action"] == "stop" and inst["ttl"]["ttl_seconds"] >= 3600  # the backstop
    key = vms.dir / rec.id / "id_ed25519"
    assert key.read_text() == PRIVATE and oct(key.stat().st_mode & 0o777) == "0o600"
    known = (vms.dir / rec.id / "known_hosts").read_text()
    assert known == f"agentos-{rec.id} {GATEWAY[0]}\n"
    remote = envs.get("dev/m1").remote
    tunnel = tunnel_argv(remote, socks_path="/tmp/s.sock", gateway_port=4000,
                         forwards=(("/tmp/vnc", "/run/agentos-browser/vnc.sock"),))
    target = f"{rec.provider_id}@ssh.cloud.morph.so"
    for argv in (ssh.ssh_base(remote), tunnel, git_tunnel_argv(remote, gateway_port=4000)):
        line = " ".join(argv)
        assert argv[-1] == target and "StrictHostKeyChecking=yes" in line and f"HostKeyAlias=agentos-{rec.id}" in line
        assert KEY not in line and "SECRET" not in line and str(key) in line
    assert "-R" in tunnel and "-D" in tunnel
    # extending the machine moves Morph's own TTL along
    before = api.instances[rec.provider_id]["ttl"]["ttl_seconds"]
    await vms.extend(rec.id, 60)
    assert api.instances[rec.provider_id]["ttl"]["ttl_seconds"] > before
    await vms.destroy(rec.id)
    assert rec.provider_id not in api.instances


async def test_the_reaper_deletes_orphans_by_metadata_but_never_a_paused_machine(node):
    vms, _envs, api = node
    rec = await vms.create("dev/m2", ttl_minutes=60)
    orphan = api._instance(SMALL, vms._labels("vm_gone"), {})
    api.instances[orphan["id"]]["created"] -= 3600
    got = await vms.pause(rec.id, why="test", keep_days=30)
    assert got["eur_month"] and api.instances[rec.provider_id]["status"] == "paused"
    out = await vms.reap_once()
    assert out["orphans"] == [orphan["id"]] and rec.provider_id in api.instances
    assert vms.store.get(rec.id).state == "paused" and not (vms.dir / rec.id).exists()
    assert api.instances[rec.provider_id]["ttl"]["ttl_seconds"] == 31 * 86400  # the paused one's backstop


def test_settings_pick_morph_and_its_key_file(tmp_path):
    s = VmSettings(provider="morph")
    assert providers.token_path(tmp_path, s) == tmp_path / MORPH_TOKEN_FILE
    assert not providers.configured(tmp_path, s)
    with pytest.raises(VmError, match="--provider morph"):
        providers.make(tmp_path, s)
    (tmp_path / "secrets").mkdir()
    (tmp_path / MORPH_TOKEN_FILE).write_text(KEY + "\n")
    os.chmod(tmp_path / MORPH_TOKEN_FILE, 0o600)
    (tmp_path / "vms").mkdir()
    (tmp_path / "vms" / morph.KNOWN_HOSTS_FILE).write_text(f"ssh.cloud.morph.so {GATEWAY[0]}\n")
    p = providers.make(tmp_path, s)
    assert isinstance(p, MorphProvider) and p.host_keys == GATEWAY and KEY not in repr(p)
    assert providers.configured(tmp_path, s)
    hetzner = VmSettings()
    assert hetzner.location == "nbg1" and providers.token_path(tmp_path, hetzner).name == "hetzner.token"


def test_pinning_the_gateway_keeps_its_keys_and_says_their_fingerprints(tmp_path):
    fake = tmp_path / "keyscan"
    fake.write_text("#!/bin/sh\necho '# ssh.cloud.morph.so:22 SSH-2.0'\n"
                    f"echo 'ssh.cloud.morph.so {GATEWAY[0].split()[0]} AAAAC3NzaC1lZDI1NTE5AAAAIHkGvFakeKeyBytesForTestsOnly0123456789ab'\n")
    fake.chmod(0o755)
    prints = morph.pin_gateway(tmp_path, keyscan=str(fake))
    kept = morph.gateway_known_hosts(tmp_path)
    assert len(kept) == 1 and kept[0].startswith("ssh-ed25519 ")
    assert oct((tmp_path / "vms" / morph.KNOWN_HOSTS_FILE).stat().st_mode & 0o777) == "0o600"
    assert isinstance(prints, list)
    empty = tmp_path / "nothing"
    empty.write_text("#!/bin/sh\nexit 1\n")
    empty.chmod(0o755)
    with pytest.raises(VmError, match="no SSH host key"):
        morph.pin_gateway(tmp_path, keyscan=str(empty))


def test_backstop_is_past_the_limit_and_the_saves_grace(tmp_path):
    vms = VmManager(tmp_path, provider=provider(FakeMorph()), settings=VmSettings(provider="morph"))
    from openajar.core.models import utcnow
    from openajar.vms.models import VmRecord

    now = utcnow()
    rec = VmRecord(id="vm_1", lane_id="dev/x", node="n", profile="small", server_type="morph-small",
                   location="morph", image="i", created_at=now, expires_at=now + timedelta(hours=2))
    assert 2 * 3600 + 15 * 60 + 3600 - 5 <= vms.backstop_s(rec) <= 2 * 3600 + 15 * 60 + 3600


async def test_an_image_build_boots_the_base_image_keeps_ssh_and_labels_the_snapshot(node, monkeypatch):
    vms, _envs, api = node
    ran: list[str] = []

    async def fake_run(remote, script, *, timeout_s=900.0):
        assert remote.user.startswith("morphvm_") and remote.host == "ssh.cloud.morph.so"
        ran.append(script)
        return 0, "done"

    monkeypatch.setattr(ssh, "run", fake_run)
    out = await vms.build_image("dev", script="set -eu\necho build\n")
    boot = next(c for c in api.calls if c[1].endswith("/boot"))
    assert boot[1] == "/image/morphvm-minimal/boot" and boot[3]["vcpus"] == 2
    assert ran == ["export OPENAJAR_KEEP_SSH=1\nset -eu\necho build\n"]
    snap = api.snapshots[out["snapshot_id"]]
    assert snap["metadata"]["agentos-image"] == "dev" and snap["metadata"]["agentos-home"] == vms.home_id
    assert api.instances == {}  # the build machine is deleted
    assert await vms.provider().resolve_image("snapshot:dev") == out["snapshot_id"]
    plan = await vms.plan("dev/after", image="snapshot:dev")
    assert plan["server_type"] == "morph-small"


def test_vm_setup_with_morph_keeps_the_key_pins_the_gateway_and_picks_the_provider(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from openajar.vms import cli as vm_cli

    async def ok(self):
        return None

    def pinned(home, host=morph.SSH_HOST, **kw):
        (home / "vms").mkdir(parents=True, exist_ok=True)
        (home / "vms" / morph.KNOWN_HOSTS_FILE).write_text(f"{host} {GATEWAY[0]}\n")
        return ["256 SHA256:abc ssh.cloud.morph.so (ED25519)"]

    monkeypatch.setattr(MorphProvider, "check", ok)
    monkeypatch.setattr(morph, "pin_gateway", pinned)
    (tmp_path / "vms.toml").write_text("max_live = 2\n")
    res = CliRunner().invoke(vm_cli.app, ["--home", str(tmp_path), "setup", "--provider", "morph", "--token-stdin"],
                             input=KEY + "\n")
    assert res.exit_code == 0, res.output
    assert KEY not in res.output and "SHA256:abc" in res.output
    assert (tmp_path / MORPH_TOKEN_FILE).read_text().strip() == KEY
    assert oct((tmp_path / MORPH_TOKEN_FILE).stat().st_mode & 0o777) == "0o600"
    s = VmSettings(**tomllib.loads((tmp_path / "vms.toml").read_text()))
    assert s.provider == "morph" and s.max_live == 2 and s.location == "morph"
    bad = CliRunner().invoke(vm_cli.app, ["--home", str(tmp_path), "setup", "--provider", "aws", "--token-stdin"],
                             input="x\n")
    assert bad.exit_code != 0
