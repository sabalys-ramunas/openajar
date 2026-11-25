"""The VM firewall follows this computer's address while it is awake (openajar local: VmManager follow_ip).

With openajar local the node is the owner's laptop, and the provider's firewall (Hetzner) lets SSH into each
machine from this computer's public address only. That address moves: a laptop wakes on another network, and
while it is awake a phone hotspot, a VPN or the provider's own NAT can hand it another (the 2026-11-24 sites
run: three changes in an hour, awake). Each move left every environment's tunnels dead, since the firewall still
named the old address. `follow` sets the firewall to the address now, and is asked:

- at the service's start and after a sleep (openajar.local.wake, as before);
- when a tunnel to a machine drops (openajar.environments.relink): the signal that matters;
- once a minute while VMs run (openajar.local.wake): the backstop, for a move nothing noticed yet.

Looking is cheap: one HTTPS request for the IPv4 address and one for the IPv6 one, and the provider is asked only
when the answer differs from what the firewall names. Looks closer than SHARED_S share one answer, so a burst of
dropped tunnels asks once.

The answer comes from a third-party "what is my IP" service, and it opens a firewall, so it is bounded:

- The rule names this one address only: a /32 (and a /128 for IPv6), for SSH and ping. The new address replaces
  the old one in the same call (Hetzner's set_rules), so the firewall never names more than one IPv4 address. The
  old one is not kept until the new link is up: it no longer belongs to this computer (the next customer of the
  network may hold it), and the tunnels that used it are dead already.
- Never a private, loopback, link-local, CGNAT, multicast or reserved address (`public`): a wrong answer cannot
  open the firewall to a range, or to an address that means "anyone near the machine".
- A change needs a second source not to disagree: when the address differs from what the firewall names, a
  second service is asked (SECOND_V4_URLS) and the firewall changes only when it gives the same address or no
  answer. Two services that disagree change nothing, and the log says so.
- Both are asked over HTTPS from this computer's own connection, and SSH on the machine takes the node's key
  only (with its host key pinned), so a lie opens port 22 to one address that still needs that key.

A fixed `ssh_from` in vms.toml turns all of this off: the firewall then names what the owner wrote.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger("openajar.vms")

FOLLOW_EVERY_S = 60.0   # the backstop's look while VMs run (openajar.local.wake)
SHARED_S = 10.0         # looks closer than this share one answer
SECOND_V4_URLS = ("https://ipinfo.io/ip", "https://api.ipify.org")  # the confirming look: the other service first
NOT_PUBLIC = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96",
    "fc00::/7", "fe80::/10", "ff00::/8"))


def public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """May a firewall rule name this address as this computer's? Not a private, loopback, link-local, CGNAT,
    multicast or reserved one, nor IPv4 written as IPv6."""
    return not any(ip in net for net in NOT_PUBLIC if net.version == ip.version)


async def second_ipv4() -> str | None:
    """This computer's IPv4 address from another service than the first look's, or None with no answer."""
    for url in SECOND_V4_URLS:
        try:
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
            async with httpx.AsyncClient(timeout=10, transport=transport) as c:
                r = await c.get(url, headers={"User-Agent": "openajar-vms", "Accept": "text/plain"})
            return str(ipaddress.ip_address(r.text.strip()))
        except (httpx.HTTPError, ValueError, OSError):
            continue
    return None


def _v4(nets: list[str]) -> str | None:
    return next((n.split("/")[0] for n in nets if ":" not in n), None)


class AddressFollower:
    """What the node's firewall names now (`applied`), and `follow`, which sets it to this computer's address."""

    def __init__(self, vms: Any, *, second: Callable[[], Awaitable[str | None]] | None = second_ipv4,
                 clock: Callable[[], float] = time.monotonic):
        """`second`: the confirming look; None asks the node's own lookup again (a node given its own, as in tests)."""
        self.vms = vms
        self._second = second or self._first_again
        self._clock = clock
        self._lock = asyncio.Lock()
        self.applied: list[str] | None = None
        self._looked_at: float | None = None
        self.changes = 0
        self.last_disagreed: str | None = None

    def noted(self, nets: list[str]) -> list[str]:
        """What a new machine's firewall is made with (VmManager._ssh_from): the address it names from now on."""
        self.applied, self._looked_at = list(nets), self._clock()
        return nets

    def applies(self) -> bool:
        """Does this node's firewall follow this computer (follow_ip, a provider with firewalls, no fixed ssh_from)?"""
        vms = self.vms
        if not (vms.follow_ip and vms.configured and vms.settings.firewall and not vms.settings.ssh_from):
            return False
        return bool(getattr(vms.provider(), "firewall", True))

    async def follow(self, why: str, *, force: bool = False, fresh: bool = False) -> dict | None:
        """Set the firewall to this computer's address now, when it moved (`force`: ask the provider even when it
        did not: a start, a wake; `fresh`: look again even right after a look). {"changed", "nets"} (changed: it named another address before), or
        {"changed": False, "disagreed": ...} when the second source said otherwise; None when it does not apply.
        Raises VmError when no address could be found (the network is down)."""
        if not self.applies():
            return None
        async with self._lock:
            now = self._clock()
            if (not force and not fresh and self.applied is not None and self._looked_at is not None
                    and now - self._looked_at < SHARED_S):
                return {"changed": False, "nets": list(self.applied)}
            nets = await self.vms._current_ips()
            self._looked_at = self._clock()
            moved = self.applied is not None and nets != self.applied
            if not moved and not force and self.applied is not None:
                return {"changed": False, "nets": nets}
            if moved and (said := await self._disagrees(nets)) is not None:
                self.last_disagreed = said
                log.warning("this computer's address looks changed (%s), but a second service gives another one, "
                            "so the VM firewall stays as it is", why)
                return {"changed": False, "nets": list(self.applied or []), "disagreed": said}
            from openajar.vms import netwall

            vms = self.vms
            await netwall.refresh(vms.provider(), f"agentos-{vms.node}-{vms.home_id}", vms._labels(), nets)
            self.applied = nets
            if moved:
                self.changes += 1
                log.warning("this computer's address changed (%s): the VM firewall now lets SSH in from the new "
                            "address only", why)
            return {"changed": moved, "nets": nets}

    async def _first_again(self) -> str | None:
        found = [a for a in await self.vms._ip_lookup() if ":" not in a]
        return found[0] if found else None

    async def _disagrees(self, nets: list[str]) -> str | None:
        """The second source's IPv4 address when it answers with another one than `nets` has, else None."""
        mine = _v4(nets)
        try:
            other = await self._second()
        except Exception:  # noqa: BLE001 - no second answer: the first stands, as at a start
            return None
        if other is None or mine is None or other == mine:
            return None
        return other
