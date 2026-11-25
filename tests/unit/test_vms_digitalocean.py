"""The DigitalOcean provider (openajar.vms.digitalocean, experimental) against a fake of DigitalOcean's API:
an in-memory account behind a mock transport that answers the calls the provider makes, in the shapes of
DigitalOcean's API reference. No test here reaches DigitalOcean; docs/ENVIRONMENTS.md says what was tried
against the real API."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest
from typer.testing import CliRunner

from openajar.core.models import utcnow
from openajar.core.runtime import Runtime
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher
from openajar.vms import netwall, providers
from openajar.vms.digitalocean import (
    EXPERIMENTAL,
    DigitalOceanProvider,
    firewall_name,
    firewall_tag,
    labels_of,
    tag_of,
)
from openajar.vms.manager import VmManager
from openajar.vms.models import VmError, VmSettings
from openajar.vms.provider import ServerSpec, capable

TOKEN = "dop_v1_" + "t" * 20  # made up, put together here
SIZES = [
    {"slug": "s-1vcpu-512mb-10gb", "memory": 512, "vcpus": 1, "disk": 10, "price_hourly": 0.00595,
     "regions": ["fra1", "ams3", "nyc3"], "available": True, "description": "Basic"},
    {"slug": "s-2vcpu-4gb", "memory": 4096, "vcpus": 2, "disk": 80, "price_hourly": 0.03571,
     "regions": ["fra1", "ams3", "nyc3"], "available": True, "description": "Basic"},
    {"slug": "s-4vcpu-8gb", "memory": 8192, "vcpus": 4, "disk": 160, "price_hourly": 0.07143,
     "regions": ["fra1", "nyc3"], "available": True, "description": "Basic"},
    {"slug": "c-4", "memory": 8192, "vcpus": 4, "disk": 50, "price_hourly": 0.125,
     "regions": ["fra1"], "available": True, "description": "CPU-Optimized"},
    {"slug": "gpu-h100x1-80gb", "memory": 245760, "vcpus": 20, "disk": 720, "price_hourly": 3.39,
     "regions": ["nyc3"], "available": True, "description": "GPU", "gpu_info": {"count": 1}},
]


class FakeDigitalOcean:
    """One DigitalOcean team: Droplets, tags, firewalls, SSH keys, images and actions."""

    def __init__(self):
        self.droplets: dict[int, dict] = {}
        self.firewalls: dict[str, dict] = {}
        self.keys: dict[int, dict] = {}
        self.images: dict[int, dict] = {}
        self.tags: set[str] = set()
        self.actions: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: list[httpx.Response] = []     # answered before anything else, once each
        self.slow_actions = 0                    # polls an action stays in progress
        self._n = 1000

    def _id(self) -> int:
        self._n += 1
        return self._n

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        path = req.url.path.removeprefix("/v2")
        self.calls.append((req.method, path))
        if self.fail:
            return self.fail.pop(0)
        body = json.loads(req.content) if req.content else {}
        for pattern, fn in self.ROUTES:
            m = re.fullmatch(pattern, path)
            if m and fn.__name__.startswith(req.method.lower() + "_"):
                return fn(self, req, body, *m.groups())
        return httpx.Response(404, json={"id": "not_found", "message": "The resource you requested could not be found."})

    @staticmethod
    def _gone():
        return httpx.Response(404, json={"id": "not_found", "message": "The resource you requested could not be found."})

    def get_account(self, req, body):
        return httpx.Response(200, json={"account": {"droplet_limit": 25, "status": "active", "email": "o@example.test"}})

    def get_sizes(self, req, body):
        return httpx.Response(200, json={"sizes": SIZES, "links": {}, "meta": {"total": len(SIZES)}})

    def get_regions(self, req, body):
        return httpx.Response(200, json={"regions": [{"slug": s, "name": s, "available": True, "sizes": [],
                                                      "features": []} for s in ("fra1", "ams3", "nyc3")]})

    def post_tags(self, req, body):
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-_" for c in body["name"]):
            return httpx.Response(422, json={"id": "unprocessable_entity", "message": "Name is invalid"})
        self.tags.add(body["name"])
        return httpx.Response(201, json={"tag": {"name": body["name"]}})

    def delete_tag(self, req, body, tag):
        self.tags.discard(tag)
        for thing in (*self.droplets.values(), *self.images.values()):
            if tag in thing["tags"]:
                thing["tags"].remove(tag)
        return httpx.Response(204)

    def post_tag_resources(self, req, body, tag):
        if tag not in self.tags:
            return self._gone()
        for r in body["resources"]:
            assert r["resource_type"] == "image"
            self.images[int(r["resource_id"])]["tags"].append(tag)
        return httpx.Response(204)

    def post_droplets(self, req, body):
        size = next((s for s in SIZES if s["slug"] == body["size"]), None)
        if size is None or body["region"] not in size["regions"]:
            return httpx.Response(422, json={"id": "unprocessable_entity",
                                             "message": "Size is not available in this region."})
        if isinstance(body["image"], int):
            img = self.images.get(body["image"])
            if img is None or body["region"] not in img["regions"]:
                return httpx.Response(422, json={"id": "unprocessable_entity", "message": "Image is not available in the region."})
        for key in body.get("ssh_keys") or []:
            assert key in self.keys
        self.tags.update(body.get("tags") or [])
        did = self._id()
        self.droplets[did] = {"id": did, "name": body["name"], "status": "new", "size_slug": body["size"],
                              "created_at": utcnow().isoformat(), "tags": list(body.get("tags") or []),
                              "networks": {"v4": [], "v6": []}, "region": {"slug": body["region"]},
                              "image": body["image"], "user_data": body.get("user_data"), "snapshot_ids": []}
        return httpx.Response(202, json={"droplet": self._shown(did), "links": {}})

    def _shown(self, did: int) -> dict:
        return {k: v for k, v in self.droplets[did].items() if k != "user_data"}

    def boot(self, did: int | str) -> None:
        d = self.droplets[int(did)]
        d["status"] = "active"
        d["networks"]["v4"] = [{"ip_address": "10.19.0.5", "type": "private"},
                               {"ip_address": f"203.0.113.{int(did) % 200 + 1}", "type": "public"}]

    def get_droplets(self, req, body):
        tag = req.url.params.get("tag_name")
        found = [self._shown(i) for i, d in self.droplets.items()
                 if tag is None or tag.lower() in [t.lower() for t in d["tags"]]]
        return httpx.Response(200, json={"droplets": found, "links": {}, "meta": {"total": len(found)}})

    def get_droplet(self, req, body, did):
        return httpx.Response(200, json={"droplet": self._shown(int(did))}) if int(did) in self.droplets else self._gone()

    def delete_droplet(self, req, body, did):
        return httpx.Response(204) if self.droplets.pop(int(did), None) is not None else self._gone()

    def post_droplet_actions(self, req, body, did):
        assert body["type"] == "snapshot"
        d = self.droplets[int(did)]
        iid, aid = self._id(), self._id()
        self.images[iid] = {"id": iid, "name": body["name"], "type": "snapshot", "status": "available",
                            "min_disk_size": 80, "size_gigabytes": 2.36, "regions": [d["region"]["slug"]],
                            "created_at": utcnow().isoformat(), "tags": [], "droplet": int(did)}
        d["snapshot_ids"].append(iid)
        self.actions[aid] = {"id": aid, "status": "in-progress", "type": "snapshot", "left": self.slow_actions}
        return httpx.Response(201, json={"action": {"id": aid, "status": "in-progress", "type": "snapshot"}})

    def get_action(self, req, body, aid):
        a = self.actions[int(aid)]
        if a["left"] > 0:
            a["left"] -= 1
        else:
            a["status"] = "completed"
        return httpx.Response(200, json={"action": {k: a[k] for k in ("id", "status", "type")}})

    def get_droplet_snapshots(self, req, body, did):
        return httpx.Response(200, json={"snapshots": [i for i in self.images.values() if i.get("droplet") == int(did)]})

    def get_images(self, req, body):
        assert req.url.params.get("private") == "true"
        return httpx.Response(200, json={"images": list(self.images.values()), "links": {}})

    def get_image(self, req, body, iid):
        if not iid.isdigit():  # a slug: a public image
            return httpx.Response(200, json={"image": {"id": 1, "slug": iid, "status": "available", "regions": ["fra1"]}})
        return httpx.Response(200, json={"image": self.images[int(iid)]}) if int(iid) in self.images else self._gone()

    def delete_image(self, req, body, iid):
        return httpx.Response(204) if self.images.pop(int(iid), None) is not None else self._gone()

    def get_firewalls(self, req, body):
        return httpx.Response(200, json={"firewalls": list(self.firewalls.values()), "links": {}})

    def _check_firewall(self, body) -> httpx.Response | None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]+", body["name"]):
            return httpx.Response(422, json={"id": "unprocessable_entity", "message": "Name is invalid"})
        if any(t not in self.tags for t in body.get("tags") or []):
            return httpx.Response(422, json={"id": "unprocessable_entity", "message": "Tag does not exist"})
        for r in body["inbound_rules"] + body["outbound_rules"]:
            assert r["protocol"] in ("tcp", "udp", "icmp") and isinstance(r["ports"], str)
        return None

    def post_firewalls(self, req, body):
        bad = self._check_firewall(body)
        if bad is not None:
            return bad
        fid = f"fw-{self._id()}"
        self.firewalls[fid] = {"id": fid, "status": "succeeded", "droplet_ids": [], **body}
        return httpx.Response(202, json={"firewall": self.firewalls[fid]})

    def get_firewall(self, req, body, fid):
        return httpx.Response(200, json={"firewall": self.firewalls[fid]}) if fid in self.firewalls else self._gone()

    def put_firewall(self, req, body, fid):
        bad = self._check_firewall(body)
        if bad is not None:
            return bad
        self.firewalls[fid] = {"id": fid, "status": "succeeded", "droplet_ids": [], **body}
        return httpx.Response(200, json={"firewall": self.firewalls[fid]})

    def post_keys(self, req, body):
        kid = self._id()
        self.keys[kid] = {"id": kid, "name": body["name"], "public_key": body["public_key"], "fingerprint": "aa:bb"}
        return httpx.Response(201, json={"ssh_key": self.keys[kid]})

    def get_keys(self, req, body):
        return httpx.Response(200, json={"ssh_keys": list(self.keys.values()), "links": {}})

    def delete_key(self, req, body, kid):
        return httpx.Response(204) if self.keys.pop(int(kid), None) is not None else self._gone()

    ROUTES = (
        (r"/account", get_account), (r"/sizes", get_sizes), (r"/regions", get_regions), (r"/tags", post_tags),
        (r"/tags/([^/]+)/resources", post_tag_resources), (r"/tags/([^/]+)", delete_tag), (r"/droplets", post_droplets), (r"/droplets", get_droplets),
        (r"/droplets/(\d+)", get_droplet), (r"/droplets/(\d+)", delete_droplet),
        (r"/droplets/(\d+)/actions", post_droplet_actions), (r"/actions/(\d+)", get_action),
        (r"/droplets/(\d+)/snapshots", get_droplet_snapshots), (r"/images", get_images),
        (r"/images/([^/]+)", get_image), (r"/images/([^/]+)", delete_image), (r"/firewalls", get_firewalls),
        (r"/firewalls", post_firewalls), (r"/firewalls/([^/]+)", get_firewall), (r"/firewalls/([^/]+)", put_firewall),
        (r"/account/keys", post_keys), (r"/account/keys", get_keys), (r"/account/keys/(\d+)", delete_key),
    )


@pytest.fixture
def do():
    return FakeDigitalOcean()


def provider(do: FakeDigitalOcean, **kw) -> DigitalOceanProvider:
    p = DigitalOceanProvider(TOKEN, transport=httpx.MockTransport(do.handler), **kw)
    p.retry_delays, p.action_poll_s = (0.0, 0.0), 0.0
    return p


async def ok_probe(remote, *, timeout_s=15):
    return True, ""


@pytest.fixture
async def node(tmp_path, do):
    home = tmp_path / "home"
    envs = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    runtime = Runtime(envs=envs)
    settings = VmSettings(provider="digitalocean", ssh_from=["198.51.100.7/32"], monthly_budget_eur=20.0)
    prov = provider(do)
    vms = VmManager(home, runtime=runtime, provider=prov, settings=settings, node_name="node-1", probe=ok_probe)
    runtime.register("vms", vms)
    yield vms, envs, do, prov
    await vms.shutdown()
    await envs.shutdown()


# ---------------------------------------------------------------- labels as tags

def test_a_label_is_a_tag_and_reads_back_exactly():
    labels = {"agentos-node": "node-1", "agentos-home": "ab12cd34ef56", "agentos-vm": "vm_0a1b2c3d4e",
              "agentos-lane": "env.e7k2-term", "agentos-network": "limited"}
    tags = [tag_of(k, v) for k, v in labels.items()]
    assert "agentos-lane:env:e7k2-term" in tags and "agentos-vm:vm_0a1b2c3d4e" in tags
    assert all(re.fullmatch(r"[A-Za-z0-9:_-]{1,255}", t) for t in tags)  # DigitalOcean's rule for a tag
    # the person's own tags and the firewall's tag are no labels
    assert labels_of([*tags, "production", "fw-agentos-node-1-ab12"]) == labels
    assert labels_of(["production"]) == {}


def test_firewall_names_and_tags_fit_digitaloceans_rules():
    assert firewall_name("agentos-my_node-ab12") == "agentos-my-node-ab12"
    assert re.fullmatch(r"[A-Za-z0-9:_-]+", firewall_tag("agentos-my.node-ab12-closed"))
    assert firewall_tag("a") != firewall_tag("a-closed")


def test_it_declares_what_it_can_do_and_that_it_is_experimental(tmp_path):
    assert DigitalOceanProvider.experimental is True and "experimental" in EXPERIMENTAL
    for name in ("firewall", "snapshot", "network_policy", "cloud_init"):
        assert capable(DigitalOceanProvider, name)
    assert not capable(DigitalOceanProvider, "pause")
    assert netwall.cannot_close(DigitalOceanProvider) is None  # it has a closed firewall of its own
    settings = VmSettings(provider="digitalocean")
    assert settings.location == "fra1" and settings.profiles["small"].min_memory_gb == 4
    assert VmSettings(provider="digitalocean", location="nyc3").location == "nyc3"
    assert not providers.configured(tmp_path, settings)
    with pytest.raises(VmError, match="vm setup --provider digitalocean"):
        providers.make(tmp_path, settings)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "digitalocean.token").write_text(TOKEN + "\n")
    made = providers.make(tmp_path, settings)
    assert made.name == "digitalocean" and "digitalocean" not in providers.FREE and TOKEN not in repr(made)


# ---------------------------------------------------------------- the client

async def test_sizes_become_server_types_in_eur_without_gpus(do):
    p = provider(do, eur_per_usd=0.9)
    types = {t["name"]: t for t in await p.server_types()}
    assert set(types) == {"s-1vcpu-512mb-10gb", "s-2vcpu-4gb", "s-4vcpu-8gb", "c-4"}
    small = types["s-2vcpu-4gb"]
    assert (small["cores"], small["memory_gb"], small["disk_gb"], small["architecture"]) == (2, 4.0, 80, "x86")
    assert small["cpu_type"] == "shared" and types["c-4"]["cpu_type"] == "dedicated" and small["estimate"] is True
    assert small["price_hourly_eur"] == {"fra1": 0.03214, "ams3": 0.03214, "nyc3": 0.03214}
    assert types["c-4"]["available"] == ["fra1"]
    assert await p.price_hourly("s-2vcpu-4gb", "fra1") == 0.03214
    with pytest.raises(VmError, match="not offered in ams3"):
        await p.price_hourly("c-4", "ams3")
    assert await p.locations() == {"fra1": "fra1", "ams3": "ams3", "nyc3": "nyc3"}


async def test_errors_carry_digitaloceans_words_and_never_the_token(do):
    p = provider(do)
    do.fail.append(httpx.Response(401, json={"id": "Unauthorized", "message": "Unable to authenticate you"}))
    with pytest.raises(VmError, match="vm setup --provider digitalocean") as e:
        await p.check()
    assert TOKEN not in e.value.message
    do.fail.append(httpx.Response(403, json={"id": "Forbidden", "message": "You are not authorized"}))
    with pytest.raises(VmError, match="lacks a scope"):
        await p.delete_server("1")
    await p.delete_server("404")  # already gone: fine
    assert await p.get_server("404") is None and await p.get_image("404") is None


async def test_a_busy_or_unreachable_api_is_tried_again_and_a_create_is_never_repeated(do):
    p = provider(do)
    do.fail += [httpx.Response(429, json={"id": "too_many_requests", "message": "API rate limit exceeded."}),
                httpx.Response(503, text="upstream")]
    await p.check()  # a read: after the rate limit and the 503
    do.fail.append(httpx.Response(503, text="upstream"))
    with pytest.raises(VmError):  # a create that may have reached DigitalOcean is not sent twice
        await p.create_server(ServerSpec(name="n", server_type="s-2vcpu-4gb", image="ubuntu-24-04-x64",
                                         location="fra1", user_data="", labels={}))
    assert [c for c in do.calls if c == ("POST", "/droplets")] == [("POST", "/droplets")]

    def refuse(req):
        raise httpx.ConnectError("no route")

    down = DigitalOceanProvider(TOKEN, transport=httpx.MockTransport(refuse))
    down.retry_delays = (0.0,)
    with pytest.raises(VmError) as e:
        await down.check()
    assert e.value.provider_code == "unreachable"


async def test_no_room_for_a_size_is_said_as_no_room(do):
    p = provider(do)
    with pytest.raises(VmError) as e:
        await p.create_server(ServerSpec(name="n", server_type="c-4", image="ubuntu-24-04-x64", location="ams3",
                                         user_data="", labels={}))
    assert e.value.provider_code == "resource_unavailable"


# ---------------------------------------------------------------- the lifecycle, through the VM manager

async def test_a_machine_is_a_droplet_with_its_labels_key_and_firewall(node):
    vms, envs, do, prov = node
    plan = await vms.plan("dev/a", ttl_minutes=60)
    # the default profile asks for 2 CPU and 4 GB: never DigitalOcean's 512 MB Droplet
    assert (plan["server_type"], plan["location"]) == ("s-2vcpu-4gb", "fra1")
    assert plan["price_hourly_eur"] == pytest.approx(0.03571 * 0.86, abs=1e-5)
    rec = await vms.create("dev/a", plan=plan, wait_s=0)
    assert rec.state == "booting" and rec.host is None  # no address at creation
    did = int(rec.provider_id)
    made = do.droplets[did]
    assert made["image"] == "ubuntu-24-04-x64" and made["size_slug"] == "s-2vcpu-4gb"
    assert "ssh_pwauth: false" in made["user_data"]  # the node's cloud-config: keys only, its pinned host key
    labels = labels_of(made["tags"])
    assert labels["agentos-vm"] == rec.id and labels["agentos-home"] == vms.home_id and labels["agentos-lane"] == "dev.a"
    # the firewall covers the Droplet by a tag it carries from its creation
    (fw,) = do.firewalls.values()
    assert fw["tags"] == [firewall_tag(f"agentos-node-1-{vms.home_id}")] and fw["tags"][0] in made["tags"]
    assert fw["inbound_rules"] == [
        {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["198.51.100.7/32"]}},
        {"protocol": "icmp", "ports": "0", "sources": {"addresses": ["198.51.100.7/32"]}}]
    assert {(r["protocol"], r["ports"]) for r in fw["outbound_rules"]} == {("tcp", "0"), ("udp", "0"), ("icmp", "0")}
    assert all(r["destinations"]["addresses"] == ["0.0.0.0/0", "::/0"] for r in fw["outbound_rules"])
    assert do.keys == {}  # the throwaway login key is gone once the Droplet exists
    # it boots: the node reads its public address and the lane is ready
    do.boot(did)
    await vms._boots[rec.id]
    rec = vms.store.get(rec.id)
    assert rec.state == "ready" and rec.host == f"203.0.113.{did % 200 + 1}" and envs.has("dev/a")
    assert [s.id for s in await prov.list_servers({"agentos-home": vms.home_id, "agentos-vm": rec.id})] == [str(did)]
    assert await prov.list_servers({"agentos-home": vms.home_id, "agentos-vm": "vm_other"}) == []
    await vms.destroy("dev/a")
    assert do.droplets == {} and vms.store.get(rec.id).state == "deleted"
    # the tags that named this one machine go with it; the node's, the home's and the firewall's stay
    assert do.tags == {tag_of("agentos-node", "node-1"), tag_of("agentos-home", vms.home_id), fw["tags"][0]}


async def test_the_firewall_is_made_once_and_follows_a_new_address(node):
    vms, _, do, prov = node
    name = f"agentos-node-1-{vms.home_id}"
    first = await prov.ensure_firewall(name, {}, ["198.51.100.7/32"])
    assert await prov.ensure_firewall(name, {}, ["198.51.100.7/32"]) == first and len(do.firewalls) == 1
    assert ("PUT", f"/firewalls/{first}") not in do.calls  # the same rules: nothing is written again
    await netwall.refresh(prov, name, {}, ["203.0.113.99/32"])  # this computer's address moved
    assert do.firewalls[first]["inbound_rules"][0]["sources"]["addresses"] == ["203.0.113.99/32"]
    assert len(do.firewalls) == 2  # and its closed twin
    # a process that did not make the firewall still finds the tag it covers
    fresh = provider(do)
    assert await fresh._firewall_tag(first) == firewall_tag(name)


async def test_a_closed_network_is_a_firewall_with_one_way_out(node):
    vms, _, do, _prov = node
    plan = {**await vms.plan("dev/closed", ttl_minutes=60), "network": "none"}
    rec = await vms.create("dev/closed", plan=plan, wait_s=0)
    (fw,) = do.firewalls.values()
    assert fw["name"].endswith("-closed") and fw["tags"][0] in do.droplets[int(rec.provider_id)]["tags"]
    assert fw["outbound_rules"] == [{"protocol": "icmp", "ports": "0", "destinations": {"addresses": ["198.51.100.7/32"]}}]
    assert labels_of(do.droplets[int(rec.provider_id)]["tags"])["agentos-network"] == "none"
    await vms.destroy("dev/closed")


async def test_a_save_is_a_snapshot_with_its_labels_and_a_machine_boots_from_it(node):
    vms, _, do, _prov = node
    rec = await vms.create("dev/a", ttl_minutes=60, wait_s=0)
    do.boot(rec.provider_id)
    await vms._boots[rec.id]
    do.slow_actions = 2  # DigitalOcean works on it for a while: the action is polled
    snap = await vms.snapshot(rec.id, description="openajar save s1", labels={"agentos-save": "s1"})
    image = do.images[int(snap["id"])]
    assert snap["gb"] == 2.36 and image["name"] == f"openajar-s1-{rec.provider_id}"
    assert labels_of(image["tags"]) == {"agentos-save": "s1", "agentos-node": "node-1", "agentos-home": vms.home_id}
    assert [i["id"] for i in await vms.list_images({"agentos-save": "s1"})] == [snap["id"]]
    assert await vms.list_images({"agentos-save": "other"}) == []
    await vms.destroy("dev/a")
    # resume: a Droplet from the snapshot, with a disk at least as big as the one it was taken from
    plan = await vms.plan("dev/back", image=snap["id"])
    assert plan["server_type"] == "s-2vcpu-4gb" and plan["image"] == snap["id"]
    back = await vms.create("dev/back", plan=plan, wait_s=0)
    assert do.droplets[int(back.provider_id)]["image"] == int(snap["id"])
    await vms.destroy("dev/back")
    await vms.delete_image(snap["id"])
    assert do.images == {} and await vms.image(snap["id"]) is None
    assert not [t for t in do.tags if t.startswith(("agentos-save:", "agentos-vm:", "agentos-lane:"))]


async def test_a_snapshot_is_resumed_in_its_own_region(node):
    """A snapshot lives where its Droplet was: after the node's location changed, a save still resumes there."""
    vms, _, do, prov = node
    rec = await vms.create("dev/a", ttl_minutes=60, wait_s=0)
    snap = await vms.snapshot(rec.id, description="s", labels={"agentos-save": "s2"})
    await vms.destroy("dev/a")
    prov.location = vms.settings.location = "nyc3"
    assert (await vms.plan("dev/new"))["location"] == "nyc3"
    plan = await vms.plan("dev/back", image=snap["id"])
    assert plan["location"] == "fra1"
    back = await vms.create("dev/back", plan=plan, wait_s=0)
    assert do.droplets[int(back.provider_id)]["region"]["slug"] == "fra1"


async def test_a_named_snapshot_is_found_by_its_label(node):
    vms, _, _do, prov = node
    with pytest.raises(VmError, match="vm image build dev"):
        await prov.resolve_image("snapshot:dev")
    rec = await vms.create("dev/a", ttl_minutes=60, wait_s=0)
    made = await prov.create_snapshot(rec.provider_id, "openajar dev", {"agentos-image": "dev"})
    assert await prov.resolve_image("snapshot:dev") == made
    assert await prov.image_info("snapshot:dev") == {"architecture": "x86", "disk_gb": 80.0}
    assert await prov.resolve_image("ubuntu-24.04") == "ubuntu-24-04-x64" and await prov.image_info("ubuntu-24.04") is None


async def test_the_reaper_deletes_only_this_homes_droplets_and_keys(node):
    vms, _, do, prov = node
    rec = await vms.create("dev/a", ttl_minutes=60, wait_s=0)
    do.boot(rec.provider_id)
    await vms._boots[rec.id]
    theirs = provider(do)
    await theirs.create_server(ServerSpec(name="blog", server_type="s-2vcpu-4gb", image="ubuntu-24-04-x64",
                                          location="fra1", user_data="", labels={}))
    await theirs.create_server(ServerSpec(name="agentos-vm-x", server_type="s-2vcpu-4gb", image="ubuntu-24-04-x64",
                                          location="fra1", user_data="",
                                          labels={"agentos-node": "node-1", "agentos-home": "anotherhome"}))
    lost = await theirs.create_server(ServerSpec(
        name="agentos-vm-lost", server_type="s-2vcpu-4gb", image="ubuntu-24-04-x64", location="fra1", user_data="",
        labels={"agentos-node": "node-1", "agentos-home": vms.home_id, "agentos-vm": "vm_lost"}))
    mine = await prov.create_ssh_key("agentos-vm_left", "ssh-ed25519 " + "A" * 68, {"agentos-home": vms.home_id})
    await theirs.create_ssh_key("laptop", "ssh-ed25519 " + "B" * 68, {})
    await theirs.create_ssh_key("agentos-vm_x", "ssh-ed25519 " + "C" * 68, {"agentos-home": "anotherhome"})
    out = await vms.reap_once(utcnow() + timedelta(minutes=30))
    assert out["orphans"] == [lost.id]
    assert sorted(d["name"] for d in do.droplets.values()) == sorted(
        ["blog", "agentos-vm-x", f"agentos-{rec.id.replace('_', '-')}"])
    assert int(mine) not in do.keys and sorted(k["name"] for k in do.keys.values()) == ["agentos-vm_x@anotherhome", "laptop"]


async def test_a_droplet_deleted_outside_openajar_is_found_gone(node):
    vms, _, do, _prov = node
    rec = await vms.create("dev/a", ttl_minutes=60, wait_s=0)
    do.boot(rec.provider_id)
    await vms._boots[rec.id]
    do.droplets.clear()
    out = await vms.reap_once()
    assert out["gone"] == [rec.id] and vms.store.get(rec.id).delete_reason == "deleted outside openajar"


# ---------------------------------------------------------------- the CLI says it is experimental

def test_vm_setup_keeps_the_token_and_says_it_is_experimental(tmp_path, monkeypatch, do):
    from openajar.vms import cli as vms_cli
    from openajar.vms import digitalocean

    real = digitalocean.DigitalOceanProvider
    monkeypatch.setattr(digitalocean, "DigitalOceanProvider",
                        lambda token, **kw: real(token, transport=httpx.MockTransport(do.handler), **kw))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    r = CliRunner().invoke(vms_cli.app, ["--home", str(home), "setup", "--provider", "digitalocean", "--token-stdin"],
                           input=TOKEN + "\n")
    assert r.exit_code == 0, r.output
    assert "experimental" in r.output and "not yet against the real one" in r.output
    assert 'provider = "digitalocean"' in r.output and "fra1" in r.output and TOKEN not in r.output
    path = home / "secrets" / "digitalocean.token"
    assert path.read_text().strip() == TOKEN and oct(path.stat().st_mode & 0o777) == "0o600"
    assert 'provider = "digitalocean"' in (home / "vms.toml").read_text()
    assert "—" not in r.output
    bad = CliRunner().invoke(vms_cli.app, ["--home", str(home), "setup", "--provider", "aws", "--token-stdin"], input="x\n")
    assert bad.exit_code != 0 and "digitalocean" in bad.output
