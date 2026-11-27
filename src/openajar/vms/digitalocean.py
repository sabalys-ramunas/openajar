"""DigitalOcean API client (https://docs.digitalocean.com/reference/api): only the calls openajar.vms
makes. EXPERIMENTAL: written from DigitalOcean's API reference and tested against a fake of that API
(tests/unit/test_vms_digitalocean.py); docs/ENVIRONMENTS.md says what was tried against the real one.

A machine is a Droplet. The token is read from `$OPENAJAR_HOME/secrets/digitalocean.token` (0600) by the
caller and passed in; this module never logs, prints or stores it. Every error is a VmError with
DigitalOcean's own id and message, never the request headers.

What differs from Hetzner, and how it is bridged:

- Labels. DigitalOcean has tags (one string each: letters, digits, colons, dashes, underscores), no
  key=value labels. A label is the tag `<key>:<value>`, with each `.` of the value written `:` (openajar's
  label values hold letters, digits, `-`, `_` and `.`, and its keys no colon, so it reads back exactly).
  Tags do not tell capitals apart, so labels are matched without them.
- The firewall. A Droplet is not created with a firewall: a DigitalOcean firewall covers the Droplets
  that carry one of its tags. The node's firewall has a tag of its own (its name), and each Droplet is
  created with that tag. A DigitalOcean firewall drops everything it has no rule for, in both directions,
  so the node's firewall lets SSH and ping in from the node and everything out; the closed one (a
  machine whose network is limited or none, openajar.vms.netwall) lets only ping to the node out, DNS
  included.
- Snapshots. A snapshot is an action on the Droplet; its id is read from the Droplet's snapshots once the
  action completed (by its name, which is unique), and its labels are tags on the image. It is taken
  while the Droplet runs (the environment's save has stopped its services and synced its disk first),
  and it lives in the Droplet's region: a machine from it is made in that region (`place`).
- Tags are things of their own at DigitalOcean and outlive what carried them, so the tags that name one
  machine or one save are deleted with it (`OWN_KEYS`).
- SSH keys have no labels: the key's name carries this home's id, which is how leftovers are found.
- Prices are in USD; openajar shows EUR at `digitalocean_eur_per_usd` (vms.toml), marked as estimates.
  DigitalOcean bills by the second; openajar counts started hours, so its figure is the most it costs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from openajar.vms.models import VmError
from openajar.vms.provider import Server, ServerSpec, check_ours

log = logging.getLogger("openajar.vms")

API_URL = "https://api.digitalocean.com/v2"
# Said wherever the owner chooses or sees this provider: setup, the CLI, `environments setup`.
EXPERIMENTAL = ("DigitalOcean is experimental in this version of openajar: it was written from DigitalOcean's API "
                "reference and tested against a fake of that API, not yet against the real one. After your first "
                "environments there, look at the DigitalOcean control panel for a Droplet or a snapshot left behind.")
HOURLY = "about EUR 0.03 an hour for a small one, estimated"
TIMEOUT_S = 30.0
PER_PAGE = 200
MAX_PAGES = 20
# As for Hetzner: waits between tries of a call the API refused for now (429, a 5xx on a read) or that
# never reached it. A read timeout is retried only on a read or a delete, which are safe to repeat.
RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
BUSY_CODES = frozenset({"rate_limit_exceeded", "locked", "conflict"})
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
SAFE_METHODS = frozenset({"GET", "DELETE"})
UNREACHABLE = "unreachable"
ACTION_POLL_S = 5.0
IMAGES = {"ubuntu-24.04": "ubuntu-24-04-x64", "ubuntu-22.04": "ubuntu-22-04-x64", "debian-12": "debian-12-x64"}
STATUSES = {"new": "initializing", "active": "running", "off": "off", "archive": "deleted"}
ANYWHERE = ["0.0.0.0/0", "::/0"]
ALL_PORTS = "0"  # DigitalOcean's word for every port of a protocol (and what an ICMP rule carries)
# Labels that name one machine or one save: their tags are deleted with it, so the account does not fill
# with a dead tag for every machine openajar ever made. The others (the node, the home) are shared and stay.
OWN_KEYS = ("agentos-vm", "agentos-lane", "agentos-save")
SHARED = ("s-",)  # Basic Droplets share their CPUs; every other family (c-, g-, m-, so-, ...) has dedicated ones
_TAG_BAD = re.compile(r"[^A-Za-z0-9:_-]")
_NAME_BAD = re.compile(r"[^A-Za-z0-9.-]")


def tag_of(key: str, value: str) -> str:
    """The tag that stands for the label key=value."""
    return _TAG_BAD.sub("-", f"{key}:{str(value).replace('.', ':')}")[:255]


def labels_of(tags: list[str] | None) -> dict[str, str]:
    """The labels a resource's tags stand for (a tag without a colon is not one of openajar's)."""
    out = {}
    for tag in tags or []:
        key, sep, value = str(tag).partition(":")
        if sep and key and value:
            out[key] = value.replace(":", ".")
    return out


def _has(tags: list[str] | None, want: dict[str, str]) -> bool:
    have = {str(t).lower() for t in tags or []}
    return all(tag_of(k, v).lower() in have for k, v in want.items())


def firewall_name(name: str) -> str:
    """A DigitalOcean firewall's name: letters, digits, dots and dashes."""
    return _NAME_BAD.sub("-", name).strip("-.") or "openajar"


def firewall_tag(name: str) -> str:
    """The tag a firewall covers: every Droplet of the node carries it from its creation."""
    return _TAG_BAD.sub("-", f"fw-{name}")[:255]


def _rules(ssh_from: list[str], *, closed: bool) -> tuple[list[dict], list[dict]]:
    inbound = [{"protocol": "tcp", "ports": "22", "sources": {"addresses": list(ssh_from)}},
               {"protocol": "icmp", "ports": ALL_PORTS, "sources": {"addresses": list(ssh_from)}}]
    if closed:  # one rule out (ping to the node): DigitalOcean drops every other packet out
        return inbound, [{"protocol": "icmp", "ports": ALL_PORTS, "destinations": {"addresses": list(ssh_from)}}]
    return inbound, [{"protocol": p, "ports": ALL_PORTS, "destinations": {"addresses": list(ANYWHERE)}}
                     for p in ("tcp", "udp", "icmp")]


def _rule_key(r: dict) -> tuple:
    ports = str(r.get("ports") or ALL_PORTS)
    ports = ALL_PORTS if ports in ("all", "1-65535") or r.get("protocol") == "icmp" else ports
    ends = r.get("sources") or r.get("destinations") or {}
    return (r.get("protocol"), ports, tuple(sorted(ends.get("addresses") or [])))


def _same_rules(have: list[dict] | None, want: list[dict]) -> bool:
    return sorted(_rule_key(r) for r in have or []) == sorted(_rule_key(r) for r in want)


def _server(d: dict) -> Server:
    ipv4 = next((n.get("ip_address") for n in ((d.get("networks") or {}).get("v4") or [])
                 if n.get("type") == "public"), None)
    status = str(d.get("status") or "")
    return Server(id=str(d["id"]), name=d.get("name", ""), status=STATUSES.get(status, status), ipv4=ipv4,
                  labels=labels_of(d.get("tags")), server_type=str(d.get("size_slug") or ""),
                  created=d.get("created_at", ""))


def _image(img: dict, image_id: str = "") -> dict:
    size = img.get("size_gigabytes")
    status = str(img.get("status") or "").lower()
    return {"id": str(img.get("id", image_id)), "status": "creating" if status in ("new", "pending") else status,
            "created": img.get("created_at") or "", "gb": float(size) if size is not None else None,
            "labels": labels_of(img.get("tags"))}


class DigitalOceanProvider:
    name = "digitalocean"
    firewall = True
    experimental = True  # said wherever the owner chooses or sees it (setup, the CLI, the docs)
    capabilities = frozenset({"firewall", "snapshot", "network_policy", "cloud_init"})
    retry_delays: tuple[float, ...] = RETRY_DELAYS_S
    action_poll_s: float = ACTION_POLL_S
    # this openajar home's id (vms/home-id; VmManager.provider sets it): a Droplet or a snapshot is deleted by its
    # id only once DigitalOcean reads back this home's tag on it (openajar.vms.provider.check_ours)
    home_id: str | None = None

    def __init__(self, token: str, *, api_url: str = API_URL, eur_per_usd: float = 0.86, location: str = "fra1",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not token or not token.strip():
            raise VmError("no DigitalOcean API token: run `openajar vm setup --provider digitalocean` and paste one")
        self._token = token.strip()
        self._api = api_url.rstrip("/")
        self.eur_per_usd = eur_per_usd
        self.location = location
        self._transport = transport
        self._fw_tags: dict[str, str] = {}

    def __repr__(self) -> str:  # never the token
        return f"DigitalOceanProvider({self._api})"

    # ------------------------------------------------------------------ http
    async def _call(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
                    ok_404: bool = False) -> dict | None:
        """One API call, tried again with backoff while DigitalOcean is busy or out of reach."""
        delays = list(self.retry_delays)
        while True:
            try:
                return await self._call_once(method, path, params=params, body=body, ok_404=ok_404)
            except VmError as e:
                if not delays or not self._retryable(method, e):
                    raise
                wait = delays.pop(0)
                log.info("DigitalOcean %s %s: %s; trying again in %g s", method, path.split("?")[0], e.message, wait)
                await asyncio.sleep(wait)

    @staticmethod
    def _retryable(method: str, e: VmError) -> bool:
        why = getattr(e, "retry", None)
        if why == "not_sent" or e.provider_code in BUSY_CODES:
            return True
        return method in SAFE_METHODS and why in ("no_answer", "server_error")

    async def _call_once(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
                         ok_404: bool = False) -> dict | None:
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": "openajar-vms"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as c:
                r = await c.request(method, self._api + path, params=params, json=body, headers=headers)
        except httpx.HTTPError as e:
            err = VmError(f"DigitalOcean API unreachable ({type(e).__name__})", provider_code=UNREACHABLE)
            err.retry = "not_sent" if isinstance(e, NOT_SENT) else "no_answer"  # type: ignore[attr-defined]
            raise err from None
        if r.status_code == 404 and ok_404:
            return None
        if r.status_code >= 400:
            try:
                said = r.json() or {}
            except ValueError:
                said = {}
            code = str(said.get("id") or said.get("error") or f"http_{r.status_code}")
            msg = str(said.get("message") or said.get("messages") or r.reason_phrase)
            if r.status_code == 401:
                msg = ("the token was refused (revoked, expired, or not a DigitalOcean API token); run `openajar vm "
                       "setup --provider digitalocean` again")
            elif r.status_code == 403:
                msg += " (the token lacks a scope: it needs to read and write Droplets, images, firewalls, SSH keys and tags)"
            err = VmError(f"DigitalOcean: {code}: {msg}", provider_code=code)
            if r.status_code == 429:
                err.provider_code = "rate_limit_exceeded"
            elif r.status_code >= 500:
                err.retry = "server_error"  # type: ignore[attr-defined]
            elif r.status_code == 422 and "not available" in msg.lower():
                err.provider_code = "resource_unavailable"  # the size has no room in the region now
            raise err
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    async def _paged(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._call("GET", path, params={**(params or {}), "page": page, "per_page": PER_PAGE}) or {}
            got = data.get(key) or []
            out += got
            if len(got) < PER_PAGE or not (((data.get("links") or {}).get("pages")) or {}).get("next"):
                break
        return out

    async def _wait_action(self, action: dict | None, timeout_s: float = 600.0) -> None:
        if not action or action.get("status") in (None, "completed"):
            return
        aid = action["id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            a = ((await self._call("GET", f"/actions/{aid}")) or {}).get("action") or {}
            if a.get("status") == "completed":
                return
            if a.get("status") == "errored":
                raise VmError(f"DigitalOcean: the {a.get('type') or 'action'} failed", provider_code="action_failed")
            await asyncio.sleep(self.action_poll_s)
        raise VmError(f"DigitalOcean: action {aid} did not finish in {int(timeout_s)} s")

    # ------------------------------------------------------------------ provider
    async def check(self) -> None:
        data = await self._call("GET", "/account") or {}
        status = str((data.get("account") or {}).get("status") or "active")
        if status == "locked":
            raise VmError("DigitalOcean: this account is locked; see its message in the DigitalOcean control panel")

    async def _sizes(self) -> list[dict]:
        return [s for s in await self._paged("/sizes", "sizes") if not s.get("gpu_info")
                and not str(s.get("slug") or "").startswith("gpu-")]

    async def locations(self) -> dict[str, str]:
        """{region: region}: each region is a zone of its own, because a snapshot lives in one region."""
        return {str(r.get("slug")): str(r.get("slug")) for r in await self._paged("/regions", "regions")
                if r.get("slug") and r.get("available", True)}

    def _eur(self, usd: Any) -> float | None:
        try:
            return round(float(usd) * self.eur_per_usd, 5)
        except (TypeError, ValueError):
            return None

    async def server_types(self) -> list[dict]:
        out = []
        for s in await self._sizes():
            price = self._eur(s.get("price_hourly"))
            if price is None or not s.get("slug"):
                continue
            regions = [str(r) for r in s.get("regions") or []]
            out.append({"name": s["slug"], "cores": s.get("vcpus"), "memory_gb": round((s.get("memory") or 0) / 1024, 2),
                        "disk_gb": s.get("disk"), "architecture": "x86", "estimate": True,
                        "cpu_type": "shared" if str(s["slug"]).startswith(SHARED) else "dedicated",
                        "price_hourly_eur": dict.fromkeys(regions, price),
                        "available": sorted(regions) if s.get("available", True) else []})
        return out

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        for s in await self._sizes():
            if s.get("slug") == server_type:
                if location not in (s.get("regions") or []):
                    raise VmError(f"size {server_type} is not offered in {location}; see `openajar vm types`")
                return self._eur(s.get("price_hourly"))
        raise VmError(f"DigitalOcean has no size {server_type!r}; see `openajar vm types`")

    async def create_server(self, spec: ServerSpec) -> Server:
        tags = [tag_of(k, v) for k, v in sorted(spec.labels.items())]
        if spec.firewall_id:
            tags.append(await self._firewall_tag(spec.firewall_id))
        body: dict[str, Any] = {
            "name": spec.name, "region": spec.location, "size": spec.server_type,
            "image": int(spec.image) if str(spec.image).isdigit() else spec.image,
            "user_data": spec.user_data, "tags": tags, "ipv6": True, "monitoring": False,
            "with_droplet_agent": False,  # nothing of DigitalOcean's runs on the machine
        }
        if spec.ssh_key_id:
            body["ssh_keys"] = [int(spec.ssh_key_id) if str(spec.ssh_key_id).isdigit() else spec.ssh_key_id]
        data = await self._call("POST", "/droplets", body=body) or {}
        return _server(data["droplet"])  # no address yet: the node reads it again while the machine boots

    async def get_server(self, server_id: str) -> Server | None:
        data = await self._call("GET", f"/droplets/{server_id}", ok_404=True)
        return None if data is None else _server(data["droplet"])

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        """The Droplets that carry every label. DigitalOcean filters by one tag: the rest is matched here."""
        first = "agentos-home" if "agentos-home" in labels else next(iter(sorted(labels)), None)
        params = {"tag_name": tag_of(first, labels[first])} if first else {}
        return [_server(d) for d in await self._paged("/droplets", "droplets", params) if _has(d.get("tags"), labels)]

    async def delete_server(self, server_id: str) -> None:
        """Delete a Droplet of this home: read back first, a 404 is done, and one without this home's tag is
        refused (the account may hold the owner's other Droplets; never a DELETE by a bare id)."""
        data = await self._call("GET", f"/droplets/{server_id}", ok_404=True)
        if data is None:
            return  # DigitalOcean says there is none: nothing to delete
        tags = (data.get("droplet") or {}).get("tags")
        check_ours(labels_of(tags), self.home_id, f"DigitalOcean: droplet {server_id}")
        await self._call("DELETE", f"/droplets/{server_id}", ok_404=True)
        await self._drop_tags(tags)

    async def start_server(self, server_id: str) -> Server | None:
        """Power on a Droplet that is off (an agent's poweroff, a host event): its disk and address as they were.
        The server once DigitalOcean says it is active, else None."""
        data = await self._call("POST", f"/droplets/{server_id}/actions", body={"type": "power_on"}) or {}
        await self._wait_action(data.get("action"), timeout_s=300)
        server = await self.get_server(server_id)
        return server if server is not None and server.status == "running" else None

    async def _drop_tags(self, tags: list[str] | None) -> None:
        """Delete the tags that named this one machine or save (OWN_KEYS). Never a reason to fail a delete."""
        for tag in tags or []:
            if str(tag).partition(":")[0] in OWN_KEYS:
                try:
                    await self._call("DELETE", f"/tags/{tag}", ok_404=True)
                except VmError as e:
                    log.info("DigitalOcean: the tag %s was not deleted: %s", tag, e.message)

    # ------------------------------------------------------------------ firewall
    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        return await self._ensure_rules(name, *_rules(ssh_from, closed=False))

    async def ensure_closed_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        """The firewall of a machine whose network is closed (openajar.vms.netwall): the same way in, and
        one rule out (ping to the node), so DigitalOcean drops every other outbound packet, DNS included."""
        return await self._ensure_rules(name, *_rules(ssh_from, closed=True))

    async def _ensure_rules(self, name: str, inbound: list[dict], outbound: list[dict]) -> str:
        name, tag = firewall_name(name), firewall_tag(name)
        body = {"name": name, "inbound_rules": inbound, "outbound_rules": outbound, "tags": [tag]}
        found = next((f for f in await self._paged("/firewalls", "firewalls") if f.get("name") == name), None)
        await self._ensure_tag(tag)  # a firewall can only name a tag that exists
        if found is None:
            fid = str(((await self._call("POST", "/firewalls", body=body)) or {})["firewall"]["id"])
        else:
            fid = str(found["id"])
            if not (_same_rules(found.get("inbound_rules"), inbound) and _same_rules(found.get("outbound_rules"), outbound)
                    and tag in (found.get("tags") or [])):
                await self._call("PUT", f"/firewalls/{fid}", body=body)  # replaces the rules in one call
        self._fw_tags[fid] = tag
        return fid

    async def _firewall_tag(self, firewall_id: str) -> str:
        """The tag a firewall covers (read from DigitalOcean when this process did not make it)."""
        if firewall_id not in self._fw_tags:
            fw = ((await self._call("GET", f"/firewalls/{firewall_id}")) or {}).get("firewall") or {}
            tags = [t for t in fw.get("tags") or [] if str(t).startswith("fw-")]
            if not tags:
                raise VmError(f"DigitalOcean: firewall {firewall_id} covers no tag, so it would not cover the machine")
            self._fw_tags[firewall_id] = str(tags[0])
        return self._fw_tags[firewall_id]

    async def _ensure_tag(self, tag: str) -> None:
        try:
            await self._call("POST", "/tags", body={"name": tag})
        except VmError as e:
            if e.provider_code not in ("unprocessable_entity", "conflict") or "already" not in e.message.lower():
                raise

    # ------------------------------------------------------------------ ssh keys
    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        home = labels.get("agentos-home") or ""
        data = await self._call("POST", "/account/keys", body={"name": f"{name}@{home}" if home else name,
                                                               "public_key": public_key}) or {}
        return str(data["ssh_key"]["id"])

    async def delete_ssh_key(self, key_id: str) -> None:
        await self._call("DELETE", f"/account/keys/{key_id}", ok_404=True)

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        """This home's leftover keys, by the home id in their names (a key has no tags)."""
        home = labels.get("agentos-home")
        if not home:
            return []
        keys = await self._paged("/account/keys", "ssh_keys")
        return [(str(k["id"]), k.get("name", "")) for k in keys
                if str(k.get("name", "")).startswith("agentos-") and str(k.get("name", "")).endswith(f"@{home}")]

    # ------------------------------------------------------------------ images
    async def _private_images(self) -> list[dict]:
        return [i for i in await self._paged("/images", "images", {"private": "true"}) if i.get("type") == "snapshot"]

    async def resolve_image(self, image: str) -> str:
        if not image.startswith("snapshot:"):
            return IMAGES.get(image, image)
        name = image.split(":", 1)[1]
        imgs = [i for i in await self._private_images() if _has(i.get("tags"), {"agentos-image": name})
                and str(i.get("status") or "available").lower() == "available"]
        if not imgs:
            raise VmError(f"no snapshot named {name}; build it with `openajar vm image build {name}`")
        return str(max(imgs, key=lambda i: str(i.get("created_at") or ""))["id"])

    async def image_info(self, image: str) -> dict | None:
        """A snapshot's disk (a Droplet from it needs at least that much); None for a system image."""
        if not image.startswith("snapshot:") and not image.isdigit():
            return None
        img_id = image if image.isdigit() else await self.resolve_image(image)
        img = ((await self._call("GET", f"/images/{img_id}")) or {}).get("image") or {}
        size = img.get("min_disk_size")
        return {"architecture": "x86", "disk_gb": float(size) if size is not None else None}

    async def place(self, image: str | None = None) -> str | None:
        """The region a machine from this image is made in: a snapshot lives in the region of the Droplet it
        was taken from. None: the node's own location (a system image, or a snapshot that is there too)."""
        if not image or not str(image).isdigit():
            return None
        try:
            img = ((await self._call("GET", f"/images/{image}", ok_404=True)) or {}).get("image") or {}
        except VmError:
            return None
        regions = [str(r) for r in img.get("regions") or []]
        return None if not regions or self.location in regions else regions[0]

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        mark = labels.get("agentos-save") or labels.get("agentos-prepared") or labels.get("agentos-image") or "snapshot"
        name = f"openajar-{mark}-{server_id}"[:100]
        data = await self._call("POST", f"/droplets/{server_id}/actions", body={"type": "snapshot", "name": name}) or {}
        await self._wait_action(data.get("action"), timeout_s=1200)
        snaps = ((await self._call("GET", f"/droplets/{server_id}/snapshots", params={"per_page": PER_PAGE})) or {}
                 ).get("snapshots") or []
        made = [s for s in snaps if s.get("name") == name]
        if not made:
            raise VmError(f"DigitalOcean: the snapshot {name} is not listed for droplet {server_id}")
        image_id = str(max(made, key=lambda s: str(s.get("created_at") or ""))["id"])
        for k, v in sorted(labels.items()):  # its labels, as tags on the image
            tag = tag_of(k, v)
            await self._ensure_tag(tag)
            await self._call("POST", f"/tags/{tag}/resources",
                             body={"resources": [{"resource_id": image_id, "resource_type": "image"}]})
        return image_id

    async def get_image(self, image_id: str) -> dict | None:
        """A snapshot by its id: status and gb (the size DigitalOcean bills by). None when it is gone."""
        data = await self._call("GET", f"/images/{image_id}", ok_404=True)
        if data is None:
            return None
        img = _image(data.get("image") or {}, image_id)
        return None if img["status"] in ("deleted", "retired") else img

    async def delete_image(self, image_id: str) -> None:
        """Delete a snapshot of this home (a save's, a prepared machine's): read back first, a 404 is done, and
        one without this home's tag is refused (never a DELETE by a bare id)."""
        data = await self._call("GET", f"/images/{image_id}", ok_404=True)
        if data is None:
            return  # DigitalOcean says there is none: nothing to delete
        tags = (data.get("image") or {}).get("tags")
        check_ours(labels_of(tags), self.home_id, f"DigitalOcean: snapshot {image_id}")
        await self._call("DELETE", f"/images/{image_id}", ok_404=True)
        await self._drop_tags(tags)

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        """The snapshots that carry every label (a save looks for its own after a create call that failed)."""
        return [_image(i) for i in await self._private_images() if _has(i.get("tags"), labels)
                and str(i.get("status") or "").lower() not in ("deleted", "retired")]
