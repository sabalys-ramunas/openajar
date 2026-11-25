"""Where an environment's machine is, for its browser when it has no proxy (browser = "vm", no browser_proxies).

A browser with a proxy (or this-computer) claims the exit's place: its timezone and WebRTC address follow the
exit (openajar.environments.browserproxy). One with no proxy goes out from its machine's own address, and claimed
UTC from wherever that machine is: a datacenter in Germany said UTC, which fingerprint pages flag ("privacy
settings: yes", the 2026-11-24 sites run). Now its launch claims the machine's own place:

- the timezone, from ipinfo.io's answer for the machine's public address, asked from this computer (one request,
  kept for that address while the node runs); when it does not answer, the provider's location of the server
  (Hetzner's nbg1 is in Germany: Europe/Berlin), which the node knows without asking anyone;
- the WebRTC address: the machine's public address, which is what sites see.

The languages stay as they are (the lane's en-US; browser_language applies to a proxied browser only): changing
every owner's page language in a patch release is not wanted. The position stays unset, as for a proxy (vmharden
drops it). These keys are per launch and never pinned (openajar.environments.vmpin.location_key), so a resume
on a machine elsewhere claims its new place. A machine with no public address (the docker provider's, on this
computer's loopback) or one whose place cannot be learned launches as before, and the log says so: the launch
never waits on this for more than LOOKUP_TIMEOUT_S, and never fails for it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from typing import Any

from openajar.vms.following import public

log = logging.getLogger("openajar.environments")

LOOKUP_TIMEOUT_S = 6.0
# Hetzner's locations: where each is, for when the lookup does not answer (vms.capacity.ZONES has the same names)
PROVIDER_PLACES = {"nbg1": ("Europe/Berlin", "DE"), "fsn1": ("Europe/Berlin", "DE"), "hel1": ("Europe/Helsinki", "FI"),
                   "ash": ("America/New_York", "US"), "hil": ("America/Los_Angeles", "US"),
                   "sin": ("Asia/Singapore", "SG")}
_KNOWN: dict[str, dict[str, str]] = {}  # machine address -> ipinfo's answer, while the node runs


def _public(host: str | None) -> str | None:
    try:
        ip = ipaddress.ip_address(str(host or ""))
    except ValueError:
        return None
    return str(ip) if public(ip) else None


def _lookable(ip: str) -> bool:
    """Worth asking ipinfo about: a global address (not one of the documentation ranges a test machine has)."""
    return ipaddress.ip_address(ip).is_global


def ipinfo_of(ip: str, timeout: float = LOOKUP_TIMEOUT_S) -> dict[str, str]:
    """ipinfo.io's answer for another address than this computer's (the same keys as Host.proxy_exit_info).
    Raises OSError."""
    from openajar.core.system import EXIT_INFO_KEYS, Host

    text = Host()._through_proxy(None, f"https://ipinfo.io/{ip}/json", timeout=timeout, accept="application/json")
    try:
        data = json.loads(text)
    except ValueError:
        raise OSError("no answer from ipinfo.io") from None
    if not isinstance(data, dict) or str(data.get("ip") or "") != ip:
        raise OSError("ipinfo.io answered for another address")
    return {**{k: str(data[k]) for k in EXIT_INFO_KEYS if isinstance(data.get(k), str) and data[k]}, "ip": ip}


async def machine_place(svc: Any, env: Any, *, lookup: Any = None) -> dict[str, str] | None:
    """The machine's place, in ipinfo's keys ({"ip", "timezone", "country", ...}), or None when it has no public
    address or nothing could say where it is."""
    vms = svc._get("vms") if callable(getattr(svc, "_get", None)) else None
    try:
        rec = vms.store.get(env.vm_id) if vms is not None and getattr(env, "vm_id", None) else None
    except Exception:  # noqa: BLE001 - its record is gone: nothing to place
        rec = None
    ip = _public(getattr(rec, "host", None))
    if ip is None:
        return None
    if ip in _KNOWN:
        return _KNOWN[ip]
    try:
        if not _lookable(ip):
            raise OSError("an address no lookup service places (a documentation range)")
        info = await asyncio.wait_for(asyncio.to_thread(lookup or ipinfo_of, ip), LOOKUP_TIMEOUT_S + 1)
        _KNOWN[ip] = info
        return info
    except (OSError, TimeoutError) as e:
        place = PROVIDER_PLACES.get(str(getattr(rec, "location", "") or ""))
        if place is None:
            log.info("environment %s: where its machine is could not be learned (%s); its browser claims no "
                     "timezone", env.id, str(e)[:120] or "no answer")
            return None
        log.info("environment %s: ipinfo.io did not place its machine (%s); its browser takes the timezone of "
                 "its location, %s", env.id, str(e)[:120] or "no answer", rec.location)
        return {"ip": ip, "timezone": place[0], "country": place[1]}


async def place_options(svc: Any, env: Any) -> dict[str, Any]:
    """What a launch of a browser with no proxy adds to Camoufox's options: the machine's timezone and WebRTC
    address under "config" (browserproxy.location_config, no locale), or {} when its place is not known."""
    if getattr(env, "browser_proxy", None):
        return {}
    try:
        info = await machine_place(svc, env)
    except Exception:  # never a reason for the browser not to start (logged with its trace)
        log.exception("environment %s: where its machine is could not be read", getattr(env, "id", "?"))
        return {}
    if not info:
        return {}
    from openajar.environments.browserproxy import location_config

    config = location_config(info)
    for key in [k for k in config if k.startswith("geolocation:")]:
        config.pop(key)  # no position, as for a proxy (vmharden drops it anyway)
    return {"config": config} if config else {}
