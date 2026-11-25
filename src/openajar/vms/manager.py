"""VmManager: disposable cloud VMs as terminal lanes (runtime service "vms").

An agent asks for a VM by profile and time limit; the node makes it (its own SSH key, a host key
it pins before the first connection, a firewall that lets in SSH from this node only), waits for
SSH, and registers a VM lane in the env manager, so `terminal_exec`, `shell_*` and service lanes work
on it unchanged. The node deletes it again:

- when its time limit passes (never longer than `max_ttl_minutes`);
- when it sits idle into the last minutes of an hour already paid for (Hetzner bills by the hour, so deleting
  earlier saves nothing and a later task could have used it);
- when the agent is done (vm_destroy), or the owner says so, or a freeze takes its lane;
- when the provider lists a server of this node that openajar has no live record of (an orphan).

The reaper lives on the node, not in the agent: a crashed session, a sleeping laptop or a compacted context never
leaves a machine billing. Deletes are retried until the provider confirms; a VM still alive ten minutes past its
limit pushes a notification.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from openajar.core.aio import maybe_await
from openajar.core.errors import InvalidRequest, LaneExists, NotFound, OpenajarError
from openajar.core.models import LaneState, parse_lane_id, utcnow
from openajar.env.models import RemoteHost, RemoteVia
from openajar.vms import capacity, following, netwall, providers, repos, ssh
from openajar.vms import keys as vmkeys
from openajar.vms.models import Size, VmError, VmFull, VmLimit, VmRecord, VmSettings, load_settings
from openajar.vms.pausing import Pausing
from openajar.vms.provider import Provider, ServerSpec, boot_error_words, by_hand_words, server_state
from openajar.vms.store import VmStore

log = logging.getLogger("openajar.vms")

OVERDUE_ALERT = timedelta(minutes=10)     # a live VM this far past its limit pushes a notification
ENV_SAVE_GRACE = timedelta(minutes=15)    # an environment's VM past its limit: the environments save it first
SNAPSHOT_WAIT_S = 1200.0  # how long a snapshot the provider still makes is waited for
SNAPSHOT_POLL_S = 5.0
ORPHAN_GRACE = timedelta(minutes=10)      # an unknown server younger than this may still be ours, being made
EXIT_IP_URLS = ("https://api.ipify.org", "https://ipinfo.io/ip")
EXIT_IP6_URLS = ("https://api6.ipify.org", "https://v6.ipinfo.io/ip")
MAX_DELETE_BACKOFF_S = 600
BOOT_POLL_S = 3.0                         # between SSH probes while a VM boots
IMAGE_BUILD_MAX = timedelta(hours=2)      # a build VM older than this is deleted by any reaper
OFFERS_TTL = timedelta(minutes=30)        # server types and prices are read again after this
LISTED_FRESH = timedelta(minutes=1)       # what has room now (listed_now) is read again after this
PREFLIGHT_WAITS_S = (2.0, 4.0, 8.0)        # between tries of a short network check before a VM is made
SPEC_KEYS = ("cores", "memory_gb", "disk_gb", "architecture", "cpu_type")  # a plan's `specs`
HOME_ID_FILE = "home-id"                  # in vms/: this OPENAJAR_HOME's own id in the provider's labels

# What `openajar vm image build` installs by default: enough to clone, build and test most projects.
DEFAULT_IMAGE_SCRIPT = """set -eu
export DEBIAN_FRONTEND=noninteractive
cloud-init status --wait >/dev/null 2>&1 || true
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git build-essential pkg-config \\
  python3 python3-venv python3-pip docker.io docker-compose-v2 jq unzip zip rsync
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y -q nodejs
systemctl enable docker
"""
# The end of every image build with the default script: no package lists, no caches, no keys, no
# machine id, so each VM booted from the snapshot is new. A provider whose gateway signs in with the
# machine's own keys (Morph: OPENAJAR_KEEP_SSH, its script_prefix) keeps them, and one without
# cloud-init has nothing of it to clean.
IMAGE_CLEANUP = """apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache
if command -v cloud-init >/dev/null 2>&1; then cloud-init clean --logs --seed; fi
if [ -z "${OPENAJAR_KEEP_SSH:-}" ]; then rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys; fi
truncate -s 0 /etc/machine-id
sync
"""


def image_script(extra: str = "") -> str:
    """The default image script, with `extra` (more to install) run before the cleanup."""
    return DEFAULT_IMAGE_SCRIPT + (extra.rstrip() + "\n" if extra else "") + IMAGE_CLEANUP


def _label_value(text: str) -> str:
    """Hetzner label values: letters, digits, -, _ and ., at most 63, alphanumeric at both ends."""
    v = re.sub(r"[^A-Za-z0-9_.-]", ".", text)[:63].strip("-_.")
    return v or "x"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def lookup_public_ips() -> list[str]:
    """This computer's public IPv4 address and, when it has one, its IPv6 address, as the internet
    sees them: each asked over its own address family. [] when nothing answered."""
    out: list[str] = []
    for local, urls in (("0.0.0.0", EXIT_IP_URLS), ("::", EXIT_IP6_URLS)):
        for url in urls:
            try:
                transport = httpx.AsyncHTTPTransport(local_address=local)
                async with httpx.AsyncClient(timeout=10, transport=transport) as c:
                    r = await c.get(url, headers={"User-Agent": "openajar-vms", "Accept": "text/plain"})
                out.append(str(ipaddress.ip_address(r.text.strip())))
                break
            except (httpx.HTTPError, ValueError, OSError):
                continue
    return out


def _give_launcher(envs: Any) -> None:
    """VM lanes run over ssh: the env manager (which vms builds on) gets the launcher from here."""
    if getattr(envs, "remote_launcher", "absent") is None:
        from openajar.vms.ssh import SshLauncher

        envs.remote_launcher = SshLauncher()

class VmManager(Pausing):
    def __init__(self, home: Path, *, runtime: Any = None, provider: Provider | None = None,
                 settings: VmSettings | None = None, node_name: str | None = None, probe=None,
                 public_ip: str | None = None, clock=utcnow, follow_ip: bool = False,
                 ip_lookup: Callable[[], Awaitable[list[str]]] | None = None):
        self.home = Path(home)
        self.runtime = runtime
        if runtime is not None and runtime.get("envs") is not None:
            _give_launcher(runtime.get("envs"))  # VM lanes that outlived a restart warm before any call here
        self.settings = settings or load_settings(self.home)
        self.dir = self.home / "vms"
        self.store = VmStore(self.home / "vms.db")
        self._provider = provider
        cfg = runtime.get("config") if runtime is not None else None
        self.node = _label_value(node_name or (cfg.name if cfg is not None else "node"))
        self._probe = probe or ssh.probe
        self._public_ip = public_ip
        self._clock = clock
        self._lock = asyncio.Lock()
        self._boots: dict[str, asyncio.Task] = {}
        self._reaper: asyncio.Task | None = None
        self._next_delete: dict[str, datetime] = {}
        self._alerted: set[str] = set()
        self._image_builds: set[str] = set()
        self._offers: tuple[datetime, list[dict]] | None = None
        self.preflight_waits: tuple[float, ...] = PREFLIGHT_WAITS_S
        self._home_id: str | None = None
        self.last_reap: dict = {}
        self._reap_lock = asyncio.Lock()  # the reaper's loop and a catch-up after a wake never overlap
        # follow_ip: SSH is let in from this computer's public address as it is now, read again for
        # every new VM and on refresh_firewall (openajar local: a laptop moves between networks)
        self.follow_ip = follow_ip
        self._ip_lookup = ip_lookup or lookup_public_ips
        self.addresses = following.AddressFollower(self, second=None if ip_lookup else following.second_ipv4)

    # ================================================================ wiring
    @property
    def token_path(self) -> Path:
        return providers.token_path(self.home, self.settings)

    @property
    def configured(self) -> bool:
        return self._provider is not None or providers.configured(self.home, self.settings)

    def provider(self) -> Provider:
        if self._provider is None:  # vms.toml's provider (openajar.vms.providers), with its secret
            self._provider = providers.make(self.home, self.settings)
        return self._provider

    @property
    def max_live(self) -> int:
        """VMs alive at once: vms.toml max_live, never below the environments' own count (environments.toml
        max_live, `openajar environments allow --max-live N`), so the owner and the agent see one limit."""
        envs = self.runtime.get("environments") if self.runtime is not None else None
        cap = getattr(getattr(envs, "settings", None), "max_live", None)
        return max(self.settings.max_live, cap if isinstance(cap, int) else 0)

    def _envs(self):
        envs = self.runtime.get("envs") if self.runtime is not None else None
        if envs is None:
            raise VmError("VM lanes need terminal lanes (openajar.env) on this node")
        _give_launcher(envs)
        return envs

    @property
    def home_id(self) -> str:
        """A random id of this OPENAJAR_HOME, made once and kept in vms/home-id. Every label the
        orphan sweep matches carries it: two homes with the same node name (node-1 is the default)
        on one cloud project or docker host never take each other's servers for orphans."""
        if self._home_id is None:
            path = self.dir / HOME_ID_FILE
            try:
                found = path.read_text().strip()
            except OSError:
                found = ""
            if not re.match(r"^[a-z0-9]{8,32}$", found):
                found = secrets.token_hex(6)
                self.dir.mkdir(parents=True, exist_ok=True)
                path.write_text(found + "\n")
            self._home_id = found
        return self._home_id

    def _labels(self, vm_id: str | None = None, lane_id: str | None = None) -> dict[str, str]:
        out = {"agentos-node": self.node, "agentos-home": self.home_id}
        if vm_id:
            out["agentos-vm"] = vm_id
        if lane_id:
            out["agentos-lane"] = _label_value(lane_id.replace("/", "."))
        return out

    def _publish(self, rec: VmRecord, what: str) -> None:
        bus = self.runtime.get("events") if self.runtime is not None else None
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.publish("vm", {"what": what, **self.brief(rec)})

    async def _notify(self, title: str, body: str, *, urgent: bool = False) -> None:
        notifier = self.runtime.get("notifier") if self.runtime is not None else None
        fn = getattr(notifier, "notify", None)
        if fn is None:
            log.warning("%s: %s", title, body)
            return
        try:
            await maybe_await(fn(title=title, body=body, level="warning", urgent=urgent, data={}))
        except Exception:
            log.exception("could not send a VM notification")

    # ================================================================ lifecycle
    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="vm-reaper")

    async def shutdown(self) -> None:
        """Stop the reaper and any boot waits. VMs are not deleted: agentosd restarts (an upgrade)
        must not kill an agent's work; the reaper resumes on the next start."""
        tasks = [t for t in (self._reaper, *self._boots.values()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._reaper = None
        self._boots.clear()

    async def _reap_loop(self) -> None:
        await asyncio.sleep(1)
        while True:
            if self.configured:
                try:
                    self.last_reap = await self.reap_once()
                except Exception as e:  # noqa: BLE001
                    log.warning("vm reaper: %s", getattr(e, "message", e))
                    self.last_reap = {"at": _iso(self._clock()), "error": str(getattr(e, "message", e))}
            await asyncio.sleep(self.settings.reap_interval_s)

    # ================================================================ create
    def _ttl(self, minutes: int | None, *, kept: bool = False) -> int:
        m = int(minutes or self.settings.default_ttl_minutes)
        if m < 5:
            raise InvalidRequest("a VM lives at least 5 minutes")
        if m > self.settings.max_ttl_minutes and not kept:
            raise VmLimit(f"at most {self.settings.max_ttl_minutes} minutes on this node (vms.toml max_ttl_minutes)")
        return m

    def month_spend(self, now: datetime | None = None) -> float:
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return round(sum((r.cost_eur(now) or 0.0) for r in self.store.since(start)), 4)

    def booked_eur(self, now: datetime | None = None, *, until: dict[str, datetime] | None = None) -> float:
        """This month's spend with every live VM counted to its time limit (or to `until[vm id]`):
        what the month can still cost if nothing is deleted early. The budget is checked against it."""
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for r in self.store.since(start):
            spent = r.cost_eur(now) or 0.0
            if r.live and r.price_hourly_eur is not None:
                end = (until or {}).get(r.id, r.expires_at)
                hours = max(1, -(-int(max(0.0, (end - r.created_at).total_seconds())) // 3600))
                spent = max(spent, hours * r.price_hourly_eur)
            total += spent
        return round(total, 4)

    async def _ssh_from(self) -> list[str]:
        if self.settings.ssh_from:
            return [str(ipaddress.ip_network(c, strict=False)) for c in self.settings.ssh_from]
        if self.follow_ip:
            return self.addresses.noted(await self._current_ips())
        if self._public_ip is None:
            for url in EXIT_IP_URLS:
                try:
                    async with httpx.AsyncClient(timeout=10) as c:
                        r = await c.get(url, headers={"User-Agent": "openajar-vms", "Accept": "text/plain"})
                    self._public_ip = str(ipaddress.ip_address(r.text.strip()))
                    break
                except (httpx.HTTPError, ValueError):
                    continue
        if self._public_ip is None:
            raise VmError("could not find this node's public IP for the VM firewall; set ssh_from in vms.toml")
        ip = ipaddress.ip_address(self._public_ip)
        return [f"{ip}/{32 if ip.version == 4 else 128}"]

    async def _current_ips(self) -> list[str]:
        """This computer's public addresses now, as /32 and /128 networks: the IPv4 one (a VM is
        reached at its IPv4 address, so it is needed) and the IPv6 one when there is one."""
        found: list[Any] = []
        for wait in (*self.preflight_waits, None):  # a laptop's network drops for a few seconds: ~15 s of tries
            try:  # never a private or reserved one: a wrong answer must not open the firewall to a range
                found = [ip for ip in map(ipaddress.ip_address, await self._ip_lookup()) if following.public(ip)]
            except (ValueError, httpx.HTTPError, OSError):
                found = []
            if any(ip.version == 4 for ip in found) or wait is None:
                break
            log.info("could not find this computer's public IPv4 address; trying again in %g s", wait)
            await asyncio.sleep(wait)
        if not any(ip.version == 4 for ip in found):
            raise VmError("could not find this computer's public IPv4 address, so the VM firewall cannot let it in "
                          f"({' and '.join(u.split('//')[1] for u in EXIT_IP_URLS)} did not answer, "
                          f"{len(self.preflight_waits) + 1} tries over about 15 s). This computer's network is down "
                          "or slow: try again in 2 minutes, or set ssh_from in vms.toml to the addresses SSH may "
                          "come from.")
        nets = {f"{ip}/{32 if ip.version == 4 else 128}" for ip in found}
        return sorted(nets, key=lambda n: (":" in n, n))

    async def refresh_firewall(self) -> list[str] | None:
        """With follow_ip: set the node's VM firewall to this computer's address now, so the tunnels
        of the VMs already running reach them again after the laptop moved to another network. None
        when there is nothing to set (no provider yet, no firewall, or a fixed ssh_from)."""
        out = await self.addresses.follow("a start or a wake", force=True)
        return out["nets"] if out is not None else None

    async def follow_address(self, why: str, *, fresh: bool = False) -> dict | None:
        """While awake: the firewall follows this computer's address when it moved (openajar.vms.following)."""
        return await self.addresses.follow(why, fresh=fresh)

    async def _types_or_none(self) -> list[dict] | None:
        """Every server type with its prices and locations (the offers' cache), None when unknown."""
        try:
            await self.offers(self.settings.location)
        except OpenajarError:
            return None
        return self._offers[1] if self._offers else None

    async def listed_now(self, server_types: list[str], location: str, *,
                         skip: set[tuple[str, str]] | frozenset = frozenset()) -> list[tuple[str, str]]:
        """capacity.listed with the provider's list, read again when older than LISTED_FRESH: which of
        `server_types` it lists as orderable now near `location`, minus `skip`. [] when it does not say."""
        if self._offers is not None and self._clock() - self._offers[0] > LISTED_FRESH:
            self._offers = None
        types = await self._types_or_none()
        return capacity.listed(types or [], await self._zones(), server_types, location, skip) if types else []

    async def _zones(self) -> dict[str, str]:
        """{location: network zone}: the provider's, else Hetzner's known ones."""
        read = getattr(self.provider(), "locations", None)
        if callable(read):
            with contextlib.suppress(OpenajarError):
                zones = await read()
                if zones:
                    return dict(zones)
        return dict(capacity.ZONES)

    async def offers(self, location: str) -> list[dict]:
        """Server types that can be ordered in `location` now, cheapest first, with `price` (EUR
        an hour, gross). Read from the provider at most every OFFERS_TTL."""
        now = self._clock()
        if self._offers is None or now - self._offers[0] > OFFERS_TTL:
            self._offers = (now, await self.provider().server_types())
        out = []
        for t in self._offers[1]:
            price = (t.get("price_hourly_eur") or {}).get(location)
            if price is None or (t.get("available") is not None and location not in t["available"]):
                continue
            out.append({**t, "price": float(price)})
        return sorted(out, key=lambda t: (t["price"], -(t.get("memory_gb") or 0), -(t.get("cores") or 0), t["name"]))

    def _size(self, profile: str | None, size: dict | None) -> tuple[str, Any]:
        """The profile asked for, with the caller's own size on top. A caller that states any size
        gets the label "custom" (a grant limited to profiles refuses it)."""
        name = profile or self.settings.default_profile
        p = self.settings.profiles.get(name)
        if p is None:
            raise InvalidRequest(f"no VM profile {name!r}; this node has {', '.join(self.settings.profiles)}")
        ask = {k: v for k, v in (size or {}).items() if v is not None}
        if not ask:
            return name, p
        try:
            want = Size(**ask)
        except ValueError as e:
            raise InvalidRequest(f"bad VM size: {e}") from None
        update = {k: getattr(want, k) for k in ask}
        if "server_type" not in ask:
            update["server_type"] = None  # a stated minimum means: pick again, cheapest that fits
        return "custom", p.model_copy(update=update)

    @staticmethod
    def _fits(t: dict, p: Any, image: dict | None) -> bool:
        arch = t.get("architecture")
        # arch None: a machine of this computer's own kind (the docker provider), so any profile fits
        if p.arch != "any" and arch is not None and arch != p.arch:
            return False
        if image and image.get("architecture") and arch is not None and arch != image["architecture"]:
            return False
        if p.cpu_type != "any" and t.get("cpu_type") != p.cpu_type:
            return False
        disk = max(p.min_disk_gb, (image or {}).get("disk_gb") or 0)
        return ((t.get("cores") or 0) >= p.min_cpu and (t.get("memory_gb") or 0) >= p.min_memory_gb
                and (t.get("disk_gb") or 0) >= disk)

    async def plan(self, lane_id: str, *, profile: str | None = None, ttl_minutes: int | None = None,
                   size: dict | None = None, kept: bool = False, image: str | None = None) -> dict:
        """What a create would make, without making it (for the approval's summary). Unless the
        profile or the caller names a server type, that is the cheapest type in the location with at least
        the asked CPU, memory and disk, plus types of every family to try if the provider is out of it
        (capacity.pick_fallbacks); a type by name the location does not sell is refused (capacity.not_sold).
        `image`: the one the VM boots, when not the profile's: no type planned has a smaller disk than it."""
        parse_lane_id(lane_id)
        label, p = self._size(profile, size)
        if image:
            p = p.model_copy(update={"image": image})
        location = p.location or self.settings.location
        ttl = self._ttl(ttl_minutes, kept=kept)
        prov = self.provider()
        place = getattr(prov, "place", None)  # a provider whose images each live in one place (openajar.vms.own:
        if callable(place):                   # a person's machines): the machine made where its image is
            location = await maybe_await(place(p.image)) or location
        out = {"lane": lane_id, "profile": label, "size": p.words(), "image": p.image, "location": location,
               "ttl_minutes": ttl, "fallbacks": []}
        if p.server_type:
            price = await prov.price_hourly(p.server_type, location)
            known = None
            with contextlib.suppress(OpenajarError):  # what it has, when the provider lists it (else unknown)
                known = next((t for t in await self.offers(location) if t["name"] == p.server_type), None)
            if known is None and (wrong := capacity.not_sold(await self._types_or_none(), p.server_type, location,
                                                             getattr(prov, "name", ""))):
                raise wrong
            specs = {k: known.get(k) for k in SPEC_KEYS} if known else None
            return {**out, "server_type": p.server_type, "price_hourly_eur": price, "specs": specs}
        image = await prov.image_info(p.image) if p.image.startswith("snapshot:") or p.image.isdigit() else None
        # a "pinned" type is rented only by name (the docker provider's sizes, which all cost nothing)
        fit = [t for t in await self.offers(location) if not t.get("pinned") and self._fits(t, p, image)]
        if not fit:
            raise VmError(f"no server type in {location} has {p.words().removeprefix('the cheapest ')}"
                          + (f" for image {p.image}" if image else "") + "; see `openajar vm types`")
        first = fit[0]
        return {**out, "server_type": first["name"], "price_hourly_eur": first["price"],
                "specs": {k: first.get(k) for k in SPEC_KEYS}, "fallbacks": capacity.pick_fallbacks(first, fit)}

    async def create(self, lane_id: str, *, profile: str | None = None, ttl_minutes: int | None = None,
                     size: dict | None = None, plan: dict | None = None, purpose: str = "",
                     created_by: str = "owner", wait_s: float | None = None, cloud_config: str = "",
                     environment: str | None = None, kept: bool = False) -> VmRecord:
        """Make a VM lane. `plan` is what plan() returned for the approval (so the VM made is the
        one approved); without it the plan is made here. `cloud_config` is more cloud-config (YAML
        keys after the node's own: write_files, runcmd) for the first boot; `environment` marks the
        VM as the machine of an agent environment (openajar.environments)."""
        plan = plan or await self.plan(lane_id, profile=profile, ttl_minutes=ttl_minutes, size=size)
        envs = self._envs()
        prov = self.provider()
        async with self._lock:
            if envs.has(lane_id) or self.store.live_for_lane(lane_id) is not None:
                raise LaneExists(f"lane {lane_id} already exists; pick another name or destroy it first")
            live = self.store.live()
            if len(live) >= self.max_live:
                raise VmFull(f"this node already has {len(live)} VMs running, and its limit is {self.max_live} at "
                             "once (vms.toml max_live); wait for one to be deleted")
            price = plan["price_hourly_eur"]
            budget = self.settings.monthly_budget_eur
            if budget and price is not None:  # the planned type must fit; a fallback that does not is not tried
                hours, booked = -(-plan["ttl_minutes"] // 60), self.booked_eur()
                worst = price * hours
                plan = {**plan, "fallbacks": capacity.affordable(price, plan["fallbacks"], hours, budget - booked)}
                if booked + worst > budget:
                    raise VmLimit(f"this VM could cost up to EUR {worst:.2f}; with EUR {booked:.2f} spent and booked "
                                  "by live VMs this month, that passes the monthly budget of "
                                  f"EUR {budget:.2f} (vms.toml monthly_budget_eur)")
            now = self._clock()
            rec = VmRecord(id=f"vm_{secrets.token_hex(5)}", lane_id=lane_id, node=self.node,
                           provider=getattr(prov, "name", "hetzner"), profile=plan["profile"],
                           server_type=plan["server_type"], location=plan["location"], image=plan["image"],
                           created_at=now, expires_at=now + timedelta(minutes=plan["ttl_minutes"]),
                           created_by=created_by, purpose=purpose[:300], price_hourly_eur=price,
                           fallbacks=list(plan["fallbacks"]), environment=environment, kept=kept,
                           network=plan.get("network") or "full")
            self.store.put(rec)
        try:
            rec = await self._make_server(rec, cloud_config)
        except Exception as e:
            rec.state, rec.error = "failed", str(getattr(e, "message", e))
            self.store.put(rec)
            vmkeys.forget(self.dir, rec.id)
            self._publish(rec, "failed")
            raise
        self._publish(rec, "created")
        task = asyncio.create_task(self._boot(rec.id), name=f"vm-boot-{rec.id}")
        self._boots[rec.id] = task
        task.add_done_callback(lambda _t, i=rec.id: self._boots.pop(i, None))
        wait = self.settings.boot_timeout_s if wait_s is None else max(0.0, wait_s)
        if wait:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        return self.store.get(rec.id)

    async def _make_server(self, rec: VmRecord, cloud_config: str = "") -> VmRecord:
        prov = self.provider()
        k = vmkeys.make(self.dir, rec.id)
        labels = {**self._labels(rec.id, rec.lane_id), **netwall.labels(rec.network)}  # docker reads its wall's mode
        fw = await netwall.firewall_for(prov, rec.network, f"agentos-{self.node}-{self.home_id}", self._labels(),
                                        self._ssh_from, on=self.settings.firewall) or None
        image = await prov.resolve_image(rec.image)
        key_id = await prov.create_ssh_key(f"agentos-{rec.id}", k.client_pub, labels)
        tries = capacity.plan_tries((rec.server_type, rec.price_hourly_eur), list(rec.fallbacks), rec.location,
                                    await self._zones(), await self._types_or_none())
        tried: list[tuple[str, str]] = []

        async def make(server_type: str, location: str) -> Any:
            return await prov.create_server(ServerSpec(
                name=f"agentos-{rec.id.replace('_', '-')}", server_type=server_type, image=image,
                location=location, user_data=vmkeys.cloud_init(k) + cloud_config, labels=labels,
                ssh_key_id=key_id, firewall_id=fw, ttl_s=self.backstop_s(rec)))

        try:
            server, server_type, location, price = await capacity.create_first(
                getattr(prov, "name", ""), tries, make, vm_id=rec.id, tried=tried)
        finally:
            with contextlib.suppress(Exception):  # the reaper removes it later if this fails
                await prov.delete_ssh_key(key_id)
        if server_type != rec.server_type:  # what was planned stays on the record (the open result says the cost)
            rec.asked = (rec.server_type, rec.price_hourly_eur)
            log.info("VM %s: %s", rec.id, capacity.fell_back_words(rec.asked, server_type, price, tried))
        rec.server_type, rec.location, rec.price_hourly_eur, rec.no_room = server_type, location, price, tried
        vmkeys.adopt(self.dir, rec.id, server)  # a provider's own login (Morph), in place of openajar's
        rec.provider_id, rec.host, rec.state = server.id, server.ipv4, "booting"
        rec.port, rec.ssh_user, rec.via = server.ssh_port, server.ssh_user, getattr(server, "via", None)
        self.store.put(rec)
        return rec

    def _remote(self, rec: VmRecord) -> RemoteHost:
        d = self.dir / rec.id
        return RemoteHost(vm_id=rec.id, host=rec.host or "", port=rec.port, user=rec.ssh_user,
                          key_path=str(d / "id_ed25519"), known_hosts=str(d / "known_hosts"), host_alias=f"agentos-{rec.id}",
                          provider=rec.provider, label=f"{rec.provider} {rec.server_type} in {rec.location}",
                          environment=rec.environment, via=RemoteVia(**rec.via) if rec.via else None)

    def remote(self, rec: VmRecord) -> RemoteHost:
        """How to reach the VM over SSH (its pinned key and host key), for a node service that holds
        a connection of its own to it (the tunnels of an agent environment)."""
        return self._remote(rec)

    async def _boot(self, vm_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.boot_timeout_s
        rec = self.store.get(vm_id)
        prov = self.provider()
        last = ""
        try:
            while loop.time() < deadline:
                rec = self.store.get(vm_id)
                if rec.state != "booting":
                    return  # destroyed while booting
                if not rec.host:
                    s = await prov.get_server(rec.provider_id or "")
                    if s is None:
                        raise VmError("the server disappeared while booting")
                    if s.ipv4:
                        rec.host, rec.port, rec.via = s.ipv4, s.ssh_port, getattr(s, "via", None)
                        self.store.put(rec)
                if rec.host:
                    ok, last = await self._probe(self._remote(rec), timeout_s=15)
                    if ok:
                        break
                await asyncio.sleep(BOOT_POLL_S)
            else:
                raise VmError(f"SSH did not answer within {self.settings.boot_timeout_s} s ({last or 'no answer'})")
            rec = self.store.get(vm_id)
            if rec.state != "booting":
                return
            self._envs().create_remote(rec.lane_id, self._remote(rec))
            rec.state, rec.ready_at = "ready", self._clock()
            self.store.put(rec)
            self._publish(rec, "ready")
            log.info("VM %s ready for lane %s at %s", rec.id, rec.lane_id, rec.host)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            rec = self.store.get(vm_id)
            rec.error = f"{boot_error_words(e)}; {await server_state(prov, rec.provider_id)}"
            self.store.put(rec)
            log.warning("VM %s failed to boot: %s", vm_id, rec.error)
            await self._delete(rec, "failed to boot", final_state="failed")

    # ================================================================ delete
    def find(self, ref: str) -> VmRecord:
        """A live VM by its lane id or VM id."""
        if ref.startswith("vm_"):
            return self.store.get(ref)
        rec = self.store.live_for_lane(ref)
        if rec is None:
            raise NotFound(f"no live VM for lane {ref}")
        return rec

    async def destroy(self, ref: str, *, reason: str = "asked") -> VmRecord:
        rec = self.find(ref)
        if not rec.live:
            return rec
        task = self._boots.get(rec.id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            rec = self.store.get(rec.id)
        return await self._delete(rec, reason)

    async def _delete(self, rec: VmRecord, reason: str, *, final_state: str = "deleted") -> VmRecord:
        if rec.state != "deleting":
            rec.state, rec.delete_reason = "deleting", reason
            self.store.put(rec)
        envs = self.runtime.get("envs") if self.runtime is not None else None
        if envs is not None and envs.has(rec.lane_id):
            lane = envs.get(rec.lane_id)
            if lane.remote is not None and lane.remote.vm_id == rec.id:
                with contextlib.suppress(Exception):
                    await envs.remove(rec.lane_id, vm_ok=True)
        try:
            if rec.provider_id:
                await self.provider().delete_server(rec.provider_id)
        except Exception as e:  # noqa: BLE001
            rec.delete_attempts += 1
            rec.error = f"delete failed: {getattr(e, 'message', e)}"
            self.store.put(rec)
            backoff = min(MAX_DELETE_BACKOFF_S, 15 * 2 ** min(rec.delete_attempts, 6))
            self._next_delete[rec.id] = self._clock() + timedelta(seconds=backoff)
            log.warning("VM %s: %s (retrying in %d s)", rec.id, rec.error, backoff)
            return rec
        rec.state = final_state  # type: ignore[assignment]
        rec.deleted_at = self._clock()
        rec.delete_reason = rec.delete_reason or reason
        self.store.put(rec)
        self._next_delete.pop(rec.id, None)
        self._alerted.discard(rec.id)
        vmkeys.forget(self.dir, rec.id)
        self._publish(rec, "deleted")
        log.info("VM %s for lane %s deleted (%s)", rec.id, rec.lane_id, reason)
        return rec

    async def extend(self, ref: str, minutes: int) -> VmRecord:
        rec = self.find(ref)
        if rec.state not in ("booting", "ready"):
            raise InvalidRequest(f"VM {rec.id} is {rec.state}")
        if minutes < 1:
            raise InvalidRequest("extend by at least one minute")
        cap = rec.created_at + timedelta(minutes=self.settings.max_ttl_minutes)
        new = min(max(rec.expires_at, self._clock()) + timedelta(minutes=minutes), cap)
        if new <= rec.expires_at:
            raise VmLimit(f"VM {rec.id} is already at the node's limit of {self.settings.max_ttl_minutes} minutes")
        return await self.extend_to(rec.id, new)

    async def extend_to(self, ref: str, until: datetime) -> VmRecord:
        """Move a live VM's time limit to `until`: never past max_ttl_minutes from its creation,
        and only while the month, with it booked that long, stays within monthly_budget_eur."""
        rec = self.find(ref)
        if rec.state not in ("booting", "ready"):
            raise InvalidRequest(f"VM {rec.id} is {rec.state}")
        if until > rec.created_at + timedelta(minutes=self.settings.max_ttl_minutes) and not rec.kept:
            raise VmLimit(f"VM {rec.id} lives at most {self.settings.max_ttl_minutes} minutes on this node "
                          "(vms.toml max_ttl_minutes)")
        budget = self.settings.monthly_budget_eur
        if budget and until > rec.expires_at:
            booked = self.booked_eur(until={rec.id: until})
            if booked > budget:
                raise VmLimit(f"with VM {rec.id} booked that long, this month's VMs could cost EUR {booked:.2f}, "
                              f"past the monthly budget of EUR {budget:.2f} (vms.toml monthly_budget_eur)")
        rec.expires_at = until
        self.store.put(rec)
        self._alerted.discard(rec.id)
        await self.push_backstop(rec)  # the provider's own TTL, where it has one (openajar.vms.pausing)
        return rec

    # ================================================================ images
    async def build_image(self, name: str, *, profile: str | None = None, script: str | None = None,
                          on_step=None) -> dict:
        """Make a snapshot to boot VMs from: a build VM from the profile's image, the script over
        SSH (as root), cleaned of its keys, snapshotted with the label agentos-image=<name>, then
        deleted. Profiles use it with image = "snapshot:<name>"."""
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,40}$", name):
            raise InvalidRequest("image names are lowercase letters, digits and dashes")
        say = on_step or (lambda _m: None)
        plan = await self.plan(f"image/{name}", profile=profile, ttl_minutes=self.settings.default_ttl_minutes)
        base = self.settings.profiles[plan["profile"]].image
        if base.startswith("snapshot:"):
            base = "ubuntu-24.04"
        prov = self.provider()
        async with self._lock:
            live = self.store.live()
            if len(live) >= self.max_live:
                raise VmFull(f"this node already has {len(live)} VMs running, and its limit is {self.max_live} at "
                             "once (vms.toml max_live); wait for one to be deleted")
            now = self._clock()
            rec = VmRecord(id=f"vm_{secrets.token_hex(5)}", lane_id=f"image/{name}", kind="image", node=self.node,
                           provider=getattr(prov, "name", "hetzner"), profile=plan["profile"],
                           server_type=plan["server_type"], location=plan["location"], image=base, created_at=now,
                           expires_at=now + IMAGE_BUILD_MAX, created_by="owner", purpose=f"image build {name}",
                           price_hourly_eur=plan["price_hourly_eur"], fallbacks=list(plan["fallbacks"]))
            self.store.put(rec)
        self._image_builds.add(rec.id)
        try:
            say(f"creating build VM {rec.id} ({rec.server_type} in {rec.location} from {base})")
            rec = await self._make_server(rec)
            remote = self._remote(rec)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.settings.boot_timeout_s
            say(f"waiting for SSH on {rec.host}")
            while True:
                if not remote.host:
                    s = await prov.get_server(rec.provider_id or "")
                    if s is not None and s.ipv4:
                        rec.host = s.ipv4
                        self.store.put(rec)
                        remote = self._remote(rec)
                ok, last = (await self._probe(remote, timeout_s=15)) if remote.host else (False, "no IP yet")
                if ok:
                    break
                if loop.time() > deadline:
                    raise VmError(f"SSH did not answer within {self.settings.boot_timeout_s} s ({last})")
                await asyncio.sleep(BOOT_POLL_S)
            say("running the setup script (several minutes)")
            prefix = getattr(prov, "script_prefix", "")  # Morph keeps the machine's SSH keys (openajar.vms.morph)
            code, output = await ssh.run(remote, prefix + (script or image_script()), timeout_s=1800)
            if code != 0:
                raise VmError(f"the setup script failed with exit code {code}: {output[-1500:]}")
            say("taking the snapshot")
            image_id = await prov.create_snapshot(rec.provider_id or "", f"openajar {name} ({self.node})",
                                                  {**self._labels(), "agentos-image": name})
            say(f"snapshot {image_id} done; deleting the build VM")
            await self._delete(rec, "image build finished")
            return {"image": name, "snapshot_id": image_id, "use": f'image = "snapshot:{name}"',
                    "build_vm": rec.id, "cost_eur": self.store.get(rec.id).cost_eur(self._clock())}
        except BaseException:
            fresh = self.store.get(rec.id)
            if fresh.live:
                with contextlib.suppress(Exception):
                    await self._delete(fresh, "image build failed", final_state="failed")
            raise
        finally:
            self._image_builds.discard(rec.id)

    # ================================================================ repos
    def repo_tokens(self) -> repos.RepoTokens:
        return repos.RepoTokens(self.runtime.get("vault") if self.runtime is not None else None)

    async def clone(self, ref: str, repo: str, *, git_ref: str | None = None, dest: str | None = None,
                    depth: int | None = None) -> dict:
        """Put a private repo on a VM: clone it on the node with the owner's stored token, then
        stream the checkout to the VM over SSH. The token never reaches the VM."""
        rec = self.find(ref)
        if rec.kind != "lane" or rec.state != "ready":
            raise InvalidRequest(f"VM {rec.id} is {rec.state}; wait until it is ready")
        name = repos.parse_repo(repo)
        git_ref = repos.check_ref(git_ref)
        dest = repos.check_dest(dest or f"/root/src/{name.short}")
        if depth is not None and not 1 <= int(depth) <= 100_000:
            raise InvalidRequest("depth is a number of commits, 1 or more")
        found = await self.repo_tokens().token_for(name, purpose=f"vm_clone {name.name} to {rec.id}")
        if found is None:
            raise NotFound(f"no token for {name.name} on this node; the owner adds one with "
                           f"`openajar vm repo add {name.host}/{name.path.split('/')[0]}/*`. A public repo needs "
                           "none: run git clone on the VM with terminal_exec")
        pattern, user, token = found
        work = self.dir / "clones" / secrets.token_hex(6)
        try:
            src, commit, size = await repos.checkout(
                name, user=user, token=token, workdir=work, ref=git_ref, depth=depth,
                timeout_s=self.settings.clone_timeout_s, max_mb=self.settings.clone_max_mb)
            await ssh.push_tree(self._remote(rec), src, dest, timeout_s=self.settings.clone_timeout_s)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log.info("VM %s: cloned %s@%s to %s (token %s)", rec.id, name.name, commit[:12], dest, pattern)
        self._touch(rec)
        return {"vm": rec.id, "lane": rec.lane_id, "repo": name.name, "ref": git_ref, "commit": commit,
                "dest": dest, "size_mb": round(size, 1), "token": pattern}

    def _touch(self, rec: VmRecord) -> None:
        """Count a clone as use of the lane, for the idle rule."""
        envs = self.runtime.get("envs") if self.runtime is not None else None
        touch = getattr(getattr(envs, "store", None), "touch", None)
        if callable(touch):
            with contextlib.suppress(Exception):
                touch(rec.lane_id, self._clock())

    # ================================================================ reaper
    def _idle(self, rec: VmRecord, now: datetime) -> bool:
        envs = self.runtime.get("envs") if self.runtime is not None else None
        last = rec.ready_at or rec.created_at
        if envs is not None and envs.has(rec.lane_id):
            if envs.state(rec.lane_id) == LaneState.ACTIVE:
                return False
            used = envs.store.last_used(rec.lane_id)
            if used is not None:
                used = used if used.tzinfo else used.replace(tzinfo=UTC)
                last = max(last, used)
        return now - last >= timedelta(minutes=self.settings.idle_minutes)

    def _saving_first(self, rec: VmRecord, now: datetime) -> bool:
        """An environment's machine past its time limit gets ENV_SAVE_GRACE more: the environments'
        reaper saves its work (or tells the owner it could not) before it deletes it. Past the grace,
        this reaper deletes it whatever happened."""
        envs = self.runtime.get("environments") if self.runtime is not None else None
        return rec.environment is not None and envs is not None and now < rec.expires_at + ENV_SAVE_GRACE

    # ================================================================ snapshots of environments
    async def snapshot(self, vm_id: str, *, description: str, labels: dict[str, str]) -> dict:
        """A disk snapshot of a live VM (an environment's save): {id, gb}, once the provider says it is
        made and lists it. The VM keeps running; the caller deletes it."""
        rec = self.store.get(vm_id)
        if not rec.live or not rec.provider_id:
            raise VmError(f"VM {vm_id} is {rec.state}; there is no machine to snapshot")
        prov = self.provider()
        clean = {k: _label_value(v) for k, v in {**self._labels(), **labels}.items()}
        try:
            image_id = await prov.create_snapshot(rec.provider_id, description[:200], clean)
        except VmError as e:
            # The call failed, but the provider may have made it anyway (Hetzner made two while its API was
            # out of reach): look for it by its save label, adopt one that is made, else raise.
            image_id = await self._adopt_snapshot(clean, e)
        info = await self._snapshot_made(image_id)
        if info is None or info.get("status") not in ("available", ""):
            raise VmError(f"the snapshot {image_id} is not listed as made ({(info or {}).get('status') or 'gone'})")
        return {"id": str(image_id), "gb": info.get("gb")}

    async def _adopt_snapshot(self, labels: dict[str, str], error: VmError) -> str:
        save = labels.get("agentos-save")
        if not save:
            raise error
        try:
            found = await self.list_images({"agentos-save": save})
        except Exception:  # noqa: BLE001 - not found is the call's own error
            raise error from None
        if not found:
            raise error
        log.warning("the snapshot call for save %s failed (%s), but the provider made %s: kept as this save's",
                    save, error.message, found[0]["id"])
        return str(found[0]["id"])

    async def _snapshot_made(self, image_id: str, *, timeout_s: float = SNAPSHOT_WAIT_S) -> dict | None:
        """The image once the provider lists it as available (one being made is waited for)."""
        prov = self.provider()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            info = await prov.get_image(image_id)
            if info is None or info.get("status") in ("available", "") or loop.time() >= deadline:
                return info
            await asyncio.sleep(SNAPSHOT_POLL_S)

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        """This home's snapshots that carry `labels` too ([] where the provider cannot list them)."""
        lister = getattr(self.provider(), "list_images", None)
        if not callable(lister):
            return []
        want = {"agentos-home": self.home_id, **{k: _label_value(v) for k, v in labels.items()}}
        return list(await lister(want))

    async def image(self, image_id: str) -> dict | None:
        return await self.provider().get_image(image_id)

    async def delete_image(self, image_id: str) -> None:
        await self.provider().delete_image(image_id)

    def _hour_ending(self, rec: VmRecord, now: datetime) -> bool:
        minute = ((now - rec.created_at).total_seconds() / 60) % 60
        return minute >= 60 - self.settings.hour_margin_minutes

    async def reap_once(self, now: datetime | None = None) -> dict:
        async with self._reap_lock:
            return await self._reap(now)

    async def _reap(self, now: datetime | None) -> dict:
        now = now or self._clock()
        out: dict[str, Any] = {"at": now.isoformat(), "deleted": [], "orphans": [], "gone": [], "overdue": []}
        for rec in self.store.live():
            if rec.state == "deleting":
                if self._next_delete.get(rec.id, now) <= now:
                    r = await self._delete(rec, rec.delete_reason or "retry")
                    if r.state == "deleted":
                        out["deleted"].append(rec.id)
            elif rec.kind == "image":
                if now - rec.created_at > IMAGE_BUILD_MAX and rec.id not in self._image_builds:
                    await self._delete(rec, "image build ran too long or was abandoned", final_state="failed")
                    out["deleted"].append(rec.id)
            elif rec.state in ("creating", "booting") and rec.id not in self._boots:
                if now - rec.created_at > timedelta(seconds=self.settings.boot_timeout_s + 120):
                    await self._delete(rec, "never finished booting", final_state="failed")
                    out["deleted"].append(rec.id)
                elif rec.state == "booting" and rec.provider_id:
                    task = asyncio.create_task(self._boot(rec.id), name=f"vm-boot-{rec.id}")
                    self._boots[rec.id] = task
                    task.add_done_callback(lambda _t, i=rec.id: self._boots.pop(i, None))
            elif rec.state == "ready":
                if now >= rec.expires_at and self._saving_first(rec, now):
                    pass  # openajar.environments saves it first, then deletes it
                elif now >= rec.expires_at:
                    await self._delete(rec, "time limit")
                    out["deleted"].append(rec.id)
                elif rec.environment is None and self._idle(rec, now) and self._hour_ending(rec, now):
                    # an environment's VM is closed for idleness by openajar.environments, which knows unsaved work
                    await self._delete(rec, f"idle {self.settings.idle_minutes}+ min at the end of a paid hour")
                    out["deleted"].append(rec.id)
            fresh = self.store.get(rec.id)
            late = OVERDUE_ALERT + (ENV_SAVE_GRACE if fresh.environment is not None else timedelta(0))
            if fresh.live and now - fresh.expires_at > late:
                out["overdue"].append(fresh.id)
                if fresh.id not in self._alerted:
                    self._alerted.add(fresh.id)
                    await self._notify(f"VM {fresh.id} is still running past its time limit",
                                       f"Lane {fresh.lane_id} ({fresh.server_type}, {fresh.host or 'no IP'}) should "
                                       f"have been deleted at {fresh.expires_at:%H:%M} UTC: {fresh.error or 'no error'}. "
                                       + by_hand_words(fresh), urgent=True)
        await self._reconcile(now, out)
        return out

    async def _reconcile(self, now: datetime, out: dict) -> None:
        """Compare the provider's servers of this node with the records, both ways."""
        prov = self.provider()
        servers = await prov.list_servers(self._labels())
        by_id = {s.id: s for s in servers}
        live = {r.provider_id: r for r in self.store.live() if r.provider_id}
        creating = any(r.state == "creating" for r in self.store.live())
        for s in servers:
            if s.id in live:
                continue
            vm_id = s.labels.get("agentos-vm", "")
            with contextlib.suppress(NotFound):
                rec = self.store.get(vm_id) if vm_id else None
                if rec is not None and (rec.live or rec.state == "paused"):  # a paused one is a save's
                    continue
            created = _parse_time(s.created)
            if creating and (created is None or now - created < ORPHAN_GRACE):
                continue  # maybe the server a create is making right now
            log.warning("VM reaper: deleting server %s (%s): no live openajar record", s.id, s.name)
            try:
                await prov.delete_server(s.id)
                out["orphans"].append(s.id)
            except Exception as e:  # noqa: BLE001
                log.warning("VM reaper: could not delete orphan %s: %s", s.id, getattr(e, "message", e))
        for pid, rec in live.items():
            if rec.state == "ready" and pid not in by_id:
                rec.delete_reason = "deleted outside openajar"
                await self._delete(rec, "deleted outside openajar")
                out["gone"].append(rec.id)
        if not creating:
            for kid, _name in await prov.list_ssh_keys(self._labels()):
                with contextlib.suppress(Exception):
                    await prov.delete_ssh_key(kid)
            sweep = getattr(prov, "sweep", None)  # the docker provider: networks no machine uses any more
            if callable(sweep):
                with contextlib.suppress(Exception):
                    await sweep(self._labels())

    # ================================================================ read
    def brief(self, rec: VmRecord) -> dict:
        now = self._clock()
        left = (rec.expires_at - now).total_seconds() if rec.live else None
        return {"id": rec.id, "lane": rec.lane_id, "state": rec.state, "profile": rec.profile,
                "server_type": rec.server_type, "location": rec.location, "image": rec.image, "host": rec.host,
                "created_at": _iso(rec.created_at), "ready_at": _iso(rec.ready_at),
                "expires_at": _iso(rec.expires_at), "minutes_left": None if left is None else max(0, int(left // 60)),
                "deleted_at": _iso(rec.deleted_at), "delete_reason": rec.delete_reason, "purpose": rec.purpose,
                "created_by": rec.created_by, "price_hourly_eur": rec.price_hourly_eur,
                "cost_eur_so_far": rec.cost_eur(now), "error": rec.error,
                **({"fell_back": capacity.fell_back_words(rec.asked, rec.server_type, rec.price_hourly_eur, rec.no_room)}
                   if rec.asked else {})}

    def list(self, *, include_past: bool = False, limit: int = 30) -> list[dict]:
        recs = self.store.recent(limit) if include_past else self.store.live()
        return [self.brief(r) for r in recs]

    def report(self) -> dict:
        return {"configured": self.configured, "provider": self.settings.provider, "live": len(self.store.live()),
                "max_live": self.max_live, "month_spend_eur": self.month_spend(),
                "monthly_budget_eur": self.settings.monthly_budget_eur, "last_reap": self.last_reap,
                "profiles": {n: p.model_dump() for n, p in self.settings.profiles.items()}}


def _parse_time(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
