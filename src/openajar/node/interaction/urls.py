"""Where a lane may go: URL and ref checks, private targets, origins and challenge URLs.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

from openajar.core.errors import InvalidRequest
from openajar.node.sites import is_challenge_frame
from openajar.node.snapshot import REF_RE, is_check_ref

ALLOWED_SCHEMES = ("http", "https")


def normalize_ref(ref: str) -> str:
    """Accept `e12`, `ref=e12` or `[ref=e12]` (and a check's `c1`, interaction.checks); reject anything else."""
    value = (ref or "").strip().strip("[]").strip().removeprefix("ref=")
    if not REF_RE.match(value) and not is_check_ref(value):
        raise InvalidRequest(f"not a snapshot ref: {ref!r} (refs look like e12)")
    return value


def _ip_of(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a browser would use for a literal host, including legacy spellings that
    browsers accept for IPv4 (2130706433, 0x7f.1, 0177.0.0.1, 127.1)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def is_private_target(host: str) -> bool:
    """The node itself or a private network: loopback, private, link-local, unspecified,
    reserved, multicast, or a *.localhost name."""
    host = (host or "").strip().lower().rstrip(".")
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    ip = _ip_of(host)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved \
        or ip.is_multicast


# Browser lanes whose every request leaves from an agent environment's VM (openajar.environments: a
# SOCKS tunnel, DNS included, localhost not bypassed). For them localhost and private addresses are
# the VM's, not the node's, so a page there may open them. Only the environments service adds to it.
VM_PROXIED_LANES: set[str] = set()


def through_vm(page: Any) -> bool:
    """Is this page in a browser lane whose traffic leaves from an environment's VM?"""
    from openajar.node.egress import guard_for

    lane = getattr(guard_for(page), "lane_id", None)
    return isinstance(lane, str) and lane in VM_PROXIED_LANES


def check_url(url: str, *, allow_private: bool = False) -> str:
    """Only web pages: http(s) and about:blank. No file:, data:, javascript: or browser internals,
    and (unless allowed, for local test fixtures) nothing on the node or its private network."""
    value = (url or "").strip()
    if value == "about:blank":
        return value
    parts = urlsplit(value)
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc:
        raise InvalidRequest(f"only http and https URLs can be opened: {url!r}")
    if not allow_private and is_private_target(parts.hostname or ""):
        raise InvalidRequest(
            f"browsers cannot open addresses on the node or its private network: {parts.hostname}")
    return value


def is_challenge_url(url: str) -> bool:
    """Frames that only a challenge page or a solvable captcha widget loads (sites.VENDORS)."""
    return is_challenge_frame(url)


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or port is None:
        return None
    return parts.scheme, parts.hostname.lower(), port


def same_origin(a: str, b: str) -> bool:
    """Both are web addresses of one origin (scheme, host and port)."""
    origin = _origin(a)
    return origin is not None and origin == _origin(b)
