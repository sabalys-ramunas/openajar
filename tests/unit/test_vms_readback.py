"""Hetzner and Morph read an object back before they delete or change it by its id (the 0.7.0 audit, S5b-05 and
S5b-06): the owner's Hetzner project also holds their production servers, and a save imported from another node
names that node's snapshot. Against fake APIs (httpx.MockTransport) that hand back a foreign object for the id:
a server, a snapshot, an SSH key or a firewall without this home's agentos-home label is refused with a clear
error and never deleted. No real provider is called."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_vms_morph import SMALL, FakeMorph
from test_vms_morph import provider as morph_provider

from openajar.core.runtime import Runtime
from openajar.vms import netwall
from openajar.vms.hetzner import HetznerProvider
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmRecord, VmSettings
from openajar.vms.provider import NOT_OURS

HOME = "home0abc1234"
PRODUCTION = {"service": "shop", "env": "production"}  # the owner's own server: no openajar label at all
OTHER_HOME = {"agentos-home": "otherhome999", "agentos-node": "laptop"}
MINE = {"agentos-home": HOME, "agentos-node": "node-1"}


KINDS = {"servers": "server", "images": "image", "ssh_keys": "ssh_key", "firewalls": "firewall"}


class FakeHetzner:
    """Hetzner's API for servers, images, SSH keys and firewalls, as far as a delete or a change by id goes."""

    def __init__(self):
        self.objects: dict[str, dict[str, dict]] = {k: {} for k in KINDS}
        self.calls: list[tuple[str, str]] = []
        self.ids = itertools.count(100)
        self.down = False

    def add(self, kind: str, labels: dict, **more) -> str:
        oid = str(next(self.ids))
        self.objects[kind][oid] = {"id": int(oid), "labels": dict(labels), "status": "running", **more}
        return oid

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        path = req.url.path.removeprefix("/v1")
        self.calls.append((req.method, path))
        parts = path.strip("/").split("/")
        kind = parts[0]
        if kind not in KINDS:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "no such path"}})
        objs = self.objects[kind]
        if len(parts) == 1 and req.method == "GET":
            name = req.url.params.get("name")
            found = [o for o in objs.values() if name is None or o.get("name") == name]
            return httpx.Response(200, json={kind: found, "meta": {"pagination": {"next_page": None}}})
        if len(parts) == 1 and req.method == "POST":
            body = json.loads(req.content)
            oid = self.add(kind, body.get("labels") or {}, name=body.get("name"), rules=body.get("rules") or [])
            return httpx.Response(201, json={KINDS[kind]: objs[oid]})
        obj = objs.get(parts[1])
        if obj is None:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})
        if req.method == "GET":
            return httpx.Response(200, json={KINDS[kind]: obj})
        if req.method == "DELETE":
            objs.pop(parts[1])
            return httpx.Response(204) if kind == "ssh_keys" else httpx.Response(200, json={})
        if req.method == "PUT":
            obj["labels"] = json.loads(req.content)["labels"]
            return httpx.Response(200, json={KINDS[kind]: obj})
        if req.method == "POST" and parts[-1] == "set_rules":
            obj["rules"] = json.loads(req.content)["rules"]
            return httpx.Response(201, json={"actions": []})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "the fake has no such call"}})


def hetzner(api: FakeHetzner, home_id: str | None = HOME) -> HetznerProvider:
    p = HetznerProvider("tok", transport=httpx.MockTransport(api.handle))
    p.retry_delays = (0.0,)
    p.home_id = home_id
    return p


# ------------------------------------------------------------------ Hetzner, one call at a time


@pytest.mark.parametrize("labels", [PRODUCTION, OTHER_HOME, {}], ids=["production", "another-home", "no-labels"])
@pytest.mark.parametrize("kind,delete", [("servers", "delete_server"), ("images", "delete_image"),
                                         ("ssh_keys", "delete_ssh_key")])
async def test_hetzner_never_deletes_by_id_what_is_not_this_homes(labels, kind, delete):
    api = FakeHetzner()
    foreign = api.add(kind, labels)
    with pytest.raises(VmError) as e:
        await getattr(hetzner(api), delete)(foreign)
    assert e.value.provider_code == NOT_OURS and foreign in api.objects[kind]
    assert api.writes() == []  # read back, and no DELETE was ever sent
    said = e.value.message
    assert "does not delete it" in said and foreign in said and "Hetzner" in said and "\u2014" not in said
    assert ("another openajar home's" in said and "otherhome999" in said) if labels == OTHER_HOME else \
        "it has no agentos-home label" in said


@pytest.mark.parametrize("kind,delete", [("servers", "delete_server"), ("images", "delete_image"),
                                         ("ssh_keys", "delete_ssh_key")])
async def test_hetzner_deletes_its_own_and_a_404_is_done(kind, delete):
    api = FakeHetzner()
    mine = api.add(kind, MINE)
    p = hetzner(api)
    await getattr(p, delete)(mine)
    assert mine not in api.objects[kind] and api.writes() == [("DELETE", f"/{kind}/{mine}")]
    api.calls.clear()
    await getattr(p, delete)(mine)  # gone already: read back as none, nothing sent
    assert api.calls == [("GET", f"/{kind}/{mine}")]


async def test_hetzner_without_a_home_id_deletes_nothing():
    """A provider made outside a VmManager (no vms/home-id read): this home made nothing it could name."""
    api = FakeHetzner()
    mine = api.add("servers", MINE)
    with pytest.raises(VmError, match="this home has no id"):
        await hetzner(api, home_id=None).delete_server(mine)
    assert mine in api.objects["servers"] and api.writes() == []


async def test_hetzner_deletes_nothing_when_the_read_back_does_not_answer():
    api = FakeHetzner()
    mine = api.add("servers", MINE)
    api.down = True
    with pytest.raises(VmError) as e:
        await hetzner(api).delete_server(mine)
    assert e.value.provider_code == "unreachable" and mine in api.objects["servers"]


async def test_hetzner_writes_no_label_on_a_server_that_is_not_this_homes():
    """The warm pool relabels a machine it claims: this home's label on a production server would make every later
    check take it for this home's."""
    api = FakeHetzner()
    prod = api.add("servers", PRODUCTION)
    p = hetzner(api)
    with pytest.raises(VmError) as e:
        await p.update_labels(prod, MINE)
    assert e.value.provider_code == NOT_OURS and "change its labels" in e.value.message
    assert api.objects["servers"][prod]["labels"] == PRODUCTION and api.writes() == []
    mine = api.add("servers", MINE)
    await p.update_labels(mine, {**MINE, "agentos-lane": "dev.a"})
    assert api.objects["servers"][mine]["labels"]["agentos-lane"] == "dev.a"


async def test_hetzner_changes_no_rules_of_a_firewall_that_only_shares_the_name():
    api = FakeHetzner()
    name = f"agentos-node-1-{HOME}"
    theirs = api.add("firewalls", PRODUCTION, name=name, rules=[{"direction": "in", "protocol": "tcp", "port": "443"}])

    async def ssh_from():
        return ["198.51.100.7/32"]

    p = hetzner(api)
    with pytest.raises(VmError) as e:
        await netwall.firewall_for(p, "full", name, MINE, ssh_from)
    assert e.value.provider_code == NOT_OURS and "change its rules" in e.value.message and "vms.toml" in e.value.message
    assert api.objects["firewalls"][theirs]["rules"][0]["port"] == "443" and api.writes() == []
    api.objects["firewalls"].clear()  # once it is gone, openajar makes its own, and later keeps its rules
    fid = await netwall.firewall_for(p, "full", name, MINE, ssh_from)
    assert api.objects["firewalls"][fid]["labels"] == MINE
    assert await netwall.firewall_for(p, "full", name, MINE, ssh_from) == fid


# ------------------------------------------------------------------ Morph


@pytest.mark.parametrize("labels", [PRODUCTION, OTHER_HOME, {}], ids=["production", "another-home", "no-labels"])
async def test_morph_never_deletes_an_instance_or_a_snapshot_that_is_not_this_homes(labels):
    api = FakeMorph()
    p = morph_provider(api)
    p.home_id = HOME
    inst = api._instance(SMALL, labels, {})
    snap = api.snapshot(SMALL, labels)
    api.calls.clear()
    for call, oid in ((p.delete_server, inst["id"]), (p.delete_image, snap["id"])):
        with pytest.raises(VmError) as e:
            await call(oid)
        assert e.value.provider_code == NOT_OURS and "Morph" in e.value.message and oid in e.value.message
    assert inst["id"] in api.instances and snap["id"] in api.snapshots
    assert [c for c in api.calls if c[0] == "DELETE"] == []


async def test_morph_deletes_its_own_by_metadata():
    api = FakeMorph()
    p = morph_provider(api)
    p.home_id = HOME
    inst = api._instance(SMALL, MINE, {})
    snap = api.snapshot(SMALL, MINE)
    await p.delete_server(inst["id"])
    await p.delete_image(snap["id"])
    assert inst["id"] not in api.instances and snap["id"] not in api.snapshots


# ------------------------------------------------------------------ the node


def _record(vms: VmManager, provider_id: str) -> VmRecord:
    now = datetime.now(UTC)
    rec = VmRecord(id="vm_0123abcd", lane_id="dev/a", node="node-1", provider="hetzner", provider_id=provider_id,
                   profile="small", server_type="cx23", location="nbg1", image="ubuntu-24.04", state="ready",
                   host="203.0.113.9", created_at=now, expires_at=now + timedelta(hours=1))
    vms.store.put(rec)
    return rec


async def test_a_record_that_names_a_production_server_ends_as_lost_and_the_server_stays(tmp_path):
    """A damaged or restored record whose id now names the owner's production server: the node hands its provider
    this home's id, the read-back refuses, and the record ends as lost (never retried as a delete)."""
    api = FakeHetzner()
    prod = api.add("servers", PRODUCTION)
    vms = VmManager(tmp_path / "home", runtime=Runtime(), provider=hetzner(api, home_id=None),
                    settings=VmSettings(), node_name="node-1")
    told = []

    async def notify(title, body, *, urgent=False):
        told.append((title, body, urgent))

    vms._notify = notify
    assert vms.provider().home_id == vms.home_id  # set from vms/home-id
    rec = await vms._delete(_record(vms, prod), "its time limit")
    got = vms.store.get(rec.id)
    assert got.state == "deleted" and got.lost and "not openajar's" in (got.delete_reason or "")
    assert prod in api.objects["servers"] and api.writes() == [] and rec.id not in vms._next_delete
    assert len(told) == 1 and told[0][2] and prod in told[0][1] and "did not delete it" in told[0][1]
    mine = api.add("servers", {"agentos-home": vms.home_id, "agentos-node": "node-1"})
    other = _record(vms, mine)
    other.id = "vm_4567cdef"
    vms.store.put(other)
    assert (await vms._delete(other, "its time limit")).state == "deleted" and mine not in api.objects["servers"]
    await vms.shutdown()


async def test_morph_gets_this_homes_id_from_the_node(tmp_path):
    vms = VmManager(tmp_path / "home", runtime=Runtime(), provider=morph_provider(FakeMorph(), home_id=None),
                    settings=VmSettings(provider="morph"), node_name="node-1")
    assert vms.provider().home_id == vms.home_id
    await vms.shutdown()
