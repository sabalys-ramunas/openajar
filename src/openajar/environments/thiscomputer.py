"""An environment's browser out through this computer's own connection: the reserved label `this-computer`
of environments.toml `browser_proxies` (`openajar proxy this-computer`). Free, and with openajar local the
address is the owner's home one, which sites treat as a real household's.

It needs no entry in secrets/proxies.json: the browser's forwarder (browserproxy.BrowserProxies.browser_proxy_up)
is a `DirectForwarder`, with no upstream, so it connects to the sites itself from this computer. The machine's
browser reaches it as 127.0.0.1:3132 over the SSH tunnel, as with a proxy, and the launch's check asks ipinfo.io
straight from here (browserproxy._probe with no proxy). The pick, the save and the resume treat the label like
any other.

From this computer, the forwarder could reach what the machine must never reach: this computer's own loopback
services, the owner's home network and its router. So it connects only to public addresses, whatever way a
target is written, and on top of the network's policy:

- a local name (localhost, .local, .internal, .home.arpa) is refused before any lookup;
- names are looked up here once per connection, and the connection goes to the very address that was checked
  (a name that answers a private address the second time never gets one);
- an address must be global (refused_address): not private, loopback, link-local, CGNAT, multicast, reserved
  or documentation space, also inside an IPv4-mapped, IPv4-compatible or NAT64 IPv6;
- never this computer's own public addresses or the household's prefix (own_networks): its IPv6 /56, where
  the printer and the router have global addresses of their own, and the exit address itself, which reaches
  the router from inside;
- never port 25: a home address that sends mail is listed as a spam source.

One address: a dual stack home has an IPv4 and an IPv6 exit. The probe asks over IPv4 first (system.ipv4_first)
and the forwarder follows the family the probe answered over (`follow`), taking the other family only for a
name that has no address in this one, so sites see the address the browser's WebRTC and its place claim.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from typing import Any

from openajar.env.netproxy import EgressPolicy
from openajar.environments.envproxy import Blocked, EnvForwarder

log = logging.getLogger("openajar.environments")

THIS_COMPUTER = "this-computer"
CLOSED_PORTS = frozenset({25})       # SMTP: mail from a home address gets the household on spam lists
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")
OWN_KEPT_S = 30.0                    # this computer's own addresses, looked at again after this (it moves)
OWN_V6_PREFIX = 56                   # a household's IPv6 delegation: its /64s, the printer's and the router's
OWN_V4_PREFIX = 24                   # a public IPv4 on this computer's own interface: its neighbours there
NAT64 = ipaddress.ip_network("64:ff9b::/96")
IPV6_SPECIAL = ipaddress.ip_network("::/8")  # loopback, unspecified, IPv4-compatible: no public address
PROBE_TARGETS = {socket.AF_INET: "192.0.2.1", socket.AF_INET6: "2001:db8::1"}  # never sent to: a route lookup

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def is_this_computer(label: str | None) -> bool:
    return label == THIS_COMPUTER


def exit_name(label: str) -> str:
    """The browser's exit in words: "this computer's own connection", or "the proxy lt1"."""
    return "this computer's own connection" if is_this_computer(label) else f"the proxy {label}"


def _inner(ip: Address) -> Address:
    """The IPv4 address an IPv6 one carries (IPv4-mapped or NAT64), else the address itself."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def refused_address(addr: str, own: list[Network] | tuple[Network, ...] = ()) -> str | None:
    """Why the direct forwarder must not connect to `addr` from this computer, or None for a public address
    that is not this computer's or its household's."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return "not an address"
    inner = _inner(ip)
    for x in {ip, inner}:
        if not x.is_global or x.is_multicast or x.is_unspecified or x.is_loopback or x.is_link_local:
            return "a private or local address"
        if isinstance(x, ipaddress.IPv4Address) and x.is_reserved:
            return "a private or local address"
    if isinstance(ip, ipaddress.IPv6Address) and inner is ip and ip in IPV6_SPECIAL:
        return "a private or local address"
    if any(x.version == n.version and x in n for x in {ip, inner} for n in own):
        return "this computer's own address or its home network"
    return None


_OWN: dict[int, tuple[float, str | None]] = {}


def _source_address(family: int) -> str | None:
    """The address this computer goes out from in `family` (a route lookup: a UDP connect sends nothing)."""
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect((PROBE_TARGETS[family], 9))
            return str(s.getsockname()[0]).split("%", 1)[0]
    except OSError:
        return None


def _own_address(family: int) -> str | None:
    kept = _OWN.get(family)
    if kept is None or time.monotonic() - kept[0] > OWN_KEPT_S:
        kept = (time.monotonic(), _source_address(family))
        _OWN[family] = kept
    return kept[1]


def own_networks(exits: set[str] | frozenset[str]) -> list[Network]:
    """This computer's own public addresses and its household around them, and the exits the probe found (the
    router's public side): the direct forwarder refuses them all."""
    out: list[Network] = []
    for family, prefix in ((socket.AF_INET, OWN_V4_PREFIX), (socket.AF_INET6, OWN_V6_PREFIX)):
        addr = _own_address(family)
        if addr is None:
            continue
        ip = ipaddress.ip_address(addr)
        if ip.is_global:
            out.append(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
    for e in exits:
        try:
            ip = ipaddress.ip_address(e)
        except ValueError:
            continue
        out.append(ipaddress.ip_network(ip))
        if isinstance(ip, ipaddress.IPv6Address):
            out.append(ipaddress.ip_network(f"{ip}/{OWN_V6_PREFIX}", strict=False))
    return out


def local_name(host: str) -> bool:
    return host in ("localhost", "localhost.localdomain") or host.endswith(LOCAL_SUFFIXES)


class DirectForwarder(EnvForwarder):
    """The browser's forwarder for `this-computer`: no upstream, so it connects to the sites itself, from this
    computer, and only to public addresses that are not this computer's or its household's (the module's
    docstring). The policy (the closed network's list, else browserproxy.OpenPolicy) comes first, as with a proxy."""

    def __init__(self, env_id: str, policy: EgressPolicy, blocked: Blocked, *,
                 intercept: Callable[[str, int], Any] | None = None, max_conns: int = 64):
        super().__init__(env_id, policy, blocked, intercept=intercept, upstream=None, max_conns=max_conns)
        self.family = socket.AF_INET   # the probe's family; IPv4 until a probe says
        self.exits: set[str] = set()

    def follow(self, info: dict[str, Any]) -> None:
        """The launch's check answered (browserproxy.browser_proxy_options): connect over its family from now on,
        and never to its address (the router's public side)."""
        try:
            ip = ipaddress.ip_address(str(info.get("ip") or ""))
        except ValueError:
            return
        self.family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
        self.exits.add(str(ip))

    def _own(self) -> list[Network]:
        return own_networks(self.exits)

    async def _lookup(self, host: str, port: int, family: int) -> list[str]:
        """The addresses of `host` in one family, from this computer's resolver. Raises OSError (gaierror)."""
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM),
                                       self.connect_timeout)
        return list(dict.fromkeys(str(i[4][0]) for i in infos if i[0] == family))

    async def _resolve(self, host: str, port: int) -> list[str]:
        """The public addresses of `host` it may connect to, in the probe's family first. Looked up once: the
        connection goes to one of these, never to a second answer. A local name raises PermissionError."""
        if local_name(host):
            raise PermissionError(f"{host} is a local name, which this computer never opens for the machine")
        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            addrs = await self._lookup_either(host, port)
        own = self._own()
        return [a for a in addrs if refused_address(a, own) is None]

    async def _lookup_either(self, host: str, port: int) -> list[str]:
        other = socket.AF_INET6 if self.family == socket.AF_INET else socket.AF_INET
        error: OSError | None = None
        for family in (self.family, other):
            try:
                got = await self._lookup(host, port, family)
            except OSError as e:  # gaierror: no address in this family, or no such name
                error, got = e, []
            if got:
                return got
        raise error or OSError(f"{host} has no address")

    async def _dial(self, addr: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(asyncio.open_connection(addr, port), self.connect_timeout)

    async def _open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if port in CLOSED_PORTS:
            raise PermissionError(f"port {port} (mail) is never opened from this computer's address")
        addrs = await self._resolve(host, port)
        if not addrs:
            raise PermissionError(f"{host} resolves only to private or local addresses, or to this computer's own")
        last: Exception | None = None
        for addr in addrs:
            try:
                return await self._dial(addr, port)
            except (TimeoutError, OSError) as e:
                last = e
        raise ConnectionError(f"cannot reach {host}:{port}: {last}")
