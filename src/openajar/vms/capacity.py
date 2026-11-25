"""Where a VM is made when the provider has no room for the planned server type right now.

The planned type is tried first in the planned location, then in the other locations of the same
network zone (nbg1, fsn1 and hel1 are all eu-central): an image and a snapshot can be used in every
location, and the firewall and the SSH key are project-wide. Only then the plan's fallback types,
each in the same order. A size is "any type with at least this much": the fallbacks come from every
family the provider sells (cx, cpx, ccx, ...), cheapest first (pick_fallbacks), because the types of
one family share the provider's room and run out together. The owner's monthly budget bounds them
(affordable), not a price factor. The plan never holds a type whose disk is smaller than the image's
(the planner reads the image's disk size), and a type the provider refuses for its disk is skipped in
every location. When every try fails, NoRoom says what was tried and what to do next.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.vms.models import VmError, provider_name

log = logging.getLogger("openajar.vms")

# The provider is out of this type here right now: the next location or type may have room.
RETRY_CODES = frozenset({"resource_unavailable", "placement_error", "unavailable"})
# Hetzner's words when an image does not fit a type's disk (service_error): that type fails everywhere.
DISK_WORDS = "disk is bigger than server type disk"
# Hetzner's network zones, for a provider that cannot list its locations right now.
ZONES = {"nbg1": "eu-central", "fsn1": "eu-central", "hel1": "eu-central", "ash": "us-east", "hil": "us-west",
         "sin": "ap-southeast"}
RETRY_WAIT_WORDS = "try again in 5 minutes"
FALLBACKS = 5     # other types tried when the provider is out of the cheapest
PER_FAMILY = 2    # of them, at most this many of one family (cx, cpx, ccx, ...)
DEARER = 1.5      # a machine that costs this many times the planned price or more says so (fell_back_words)


class NoRoom(VmError):
    """Every server type and location tried had no room now. `tried` is [(server type, location)]."""

    def __init__(self, message: str, tried: list[tuple[str, str]]):
        super().__init__(message, provider_code="resource_unavailable")
        self.tried = tried


class NotSold(InvalidRequest):
    """A server type asked for by name that the provider lists, but does not sell in the location now.
    `words` is what is wrong and what is sold instead, without the closing "Nothing was rented";
    `has` is the type's (CPU, memory GB)."""

    def __init__(self, words: str, has: tuple[float, float]):
        super().__init__(f"{words}. Nothing was rented")
        self.words, self.has = words, has


def family(server_type: str) -> str:
    """cx43: cx, cpx42: cpx, ccx33: ccx (the letters before the digits)."""
    m = re.match(r"[a-z]+", server_type or "")
    return m.group(0) if m else server_type


def pick_fallbacks(first: dict, fit: list[dict], n: int = FALLBACKS) -> list[tuple[str, float | None]]:
    """(type, EUR an hour) tried after `first` when it has no room. `fit`: every type that has enough,
    cheapest first. The cheapest of each family comes in, then the next of each, at most PER_FAMILY of a
    family and `n` in all, tried in price order: a family that is out of room (cx43 and cx53 together)
    does not use up the tries another family would have had room for."""
    rest = [t for t in fit if t["name"] != first["name"]]
    count = {family(first["name"]): 1}
    picked: list[dict] = []
    for rnd in range(PER_FAMILY):
        for t in rest:
            f = family(t["name"])
            if t in picked or count.get(f, 0) > rnd or len(picked) >= n:
                continue
            picked.append(t)
            count[f] = count.get(f, 0) + 1
    picked.sort(key=lambda t: rest.index(t))
    return [(t["name"], t["price"]) for t in picked]


def affordable(price: float, fallbacks: list[tuple[str, float | None]], hours: int,
               room_eur: float) -> list[tuple[str, float | None]]:
    """The fallbacks whose whole time limit still fits in `room_eur` (what the month's budget has
    left). The planned type itself must fit; a dearer fallback that does not is never tried."""
    return [(n, q) for n, q in fallbacks if (q if q is not None else price) * hours <= room_eur]


def _gb(x: float | None) -> str:
    return f"{x:g}" if x is not None else "?"


def not_sold(types: list[dict] | None, server_type: str, location: str, provider: str = "") -> NotSold | None:
    """A server type asked for by name that the provider lists but does not sell in `location` now
    (Hetzner sells cpx41 only outside eu-central, and answers "unsupported location" to a create): the
    error in openajar's words, with where it is sold and what is sold here with as much. None when it is
    sold there, or the provider's list does not say."""
    t = next((t for t in types or [] if t.get("name") == server_type), None)
    if t is None or offered(types, server_type, location)[0]:
        return None
    where = t.get("available")
    sold = [loc for loc in (where if where is not None else (t.get("price_hourly_eur") or {}))
            if loc != location and (where is not None or offered(types, server_type, loc)[0])]
    cores, memory = t.get("cores") or 0, t.get("memory_gb") or 0
    here = []
    for o in types or []:
        ok, price = offered(types, str(o.get("name")), location)
        if (ok and price is not None and (o.get("cores") or 0) >= cores and (o.get("memory_gb") or 0) >= memory
                and o.get("architecture") == t.get("architecture")):
            here.append((o.get("cores") or 0, o.get("memory_gb") or 0, price, o))
    here.sort(key=lambda h: (h[0], h[1], h[2]))
    name = provider_name(provider)
    words = f"{name} does not sell {server_type} in {location} now"
    words += f" (it sells it in {_places(sold)})" if sold else ""
    if here:
        alts = ", ".join(f"{o['name']} ({c:g} CPU, {_gb(m)} GB, EUR {p:.4f} an hour)" for c, m, p, o in here[:2])
        words += f". Sold in {location} with at least its {cores:g} CPU and {_gb(memory)} GB: {alts}; ask for one of those"
    return NotSold(words, (cores, memory))


def fell_back_words(asked: tuple[str, float | None], server_type: str, price: float | None,
                    tried: list[tuple[str, str]]) -> str:
    """'No room for cx43 or cx53, so this is cpx42, with as much, at EUR 0.1350 an hour (4.4 times the
    EUR 0.0304 planned)': what a machine of a fallback type says, so the agent and the owner see the cost."""
    gone = list(dict.fromkeys(t for t, _lc in tried)) or [asked[0]]
    words = f"No room for {_places(gone)} now, so this is {server_type}, with at least as much"
    if price is None:
        return words
    words += f", at EUR {price:.4f} an hour"
    if asked[1] and price >= asked[1] * DEARER:
        words += f" ({price / asked[1]:.1f} times the EUR {asked[1]:.4f} planned for {asked[0]})"
    return words


def zone_locations(location: str, zones: dict[str, str]) -> list[str]:
    """`location` first, then the other locations of its network zone, in the order the provider lists them."""
    zone = zones.get(location)
    return [location, *[loc for loc, z in zones.items() if zone is not None and z == zone and loc != location]]


def offered(types: list[dict] | None, server_type: str, location: str) -> tuple[bool, float | None]:
    """Is `server_type` sold in `location` now (by the provider's list), and its price there. Unknown
    (no list): not offered, so only the planned location is tried."""
    for t in types or []:
        if t.get("name") != server_type:
            continue
        price = (t.get("price_hourly_eur") or {}).get(location)
        where = t.get("available")
        return price is not None and (where is None or location in where), price
    return False, None


def listed(types: list[dict], zones: dict[str, str], server_types: list[str], location: str,
           skip: set[tuple[str, str]] | frozenset = frozenset()) -> list[tuple[str, str]]:
    """(server type, location) of `server_types` in the network zone of `location` that the provider's
    list (`types`, its `available` locations) says can be ordered right now, minus `skip` (the ones that
    just had no room). A type whose `available` is None (the provider does not say) is never listed."""
    out = []
    for name in server_types:
        where = next((t.get("available") for t in types if t.get("name") == name), None)
        if where is not None:
            out += [(name, loc) for loc in zone_locations(location, zones) if loc in where and (name, loc) not in skip]
    return out


def plan_tries(first: tuple[str, float | None], fallbacks: list[tuple[str, float | None]], location: str,
               zones: dict[str, str], types: list[dict] | None) -> list[tuple[str, str, float | None]]:
    """(server type, location, EUR an hour) in the order they are tried: each type in the planned
    location, then in the other locations of its zone that sell it now."""
    out: list[tuple[str, str, float | None]] = []
    for server_type, price in [first, *fallbacks]:
        out.append((server_type, location, price))
        for loc in zone_locations(location, zones)[1:]:
            ok, there = offered(types, server_type, loc)
            if ok:
                out.append((server_type, loc, there if there is not None else price))
    return out


def _places(locations: list[str]) -> str:
    return locations[0] if len(locations) == 1 else f"{', '.join(locations[:-1])} or {locations[-1]}"


def no_room_words(provider: str, tried: list[tuple[str, str]]) -> str:
    """"Hetzner has no room for cx43 or cx53 in nbg1, fsn1 or hel1 now, nor for cpx42 in nbg1; try again in 5
    minutes"."""
    by_type: dict[str, list[str]] = {}
    for server_type, loc in tried:
        by_type.setdefault(server_type, [])
        if loc not in by_type[server_type]:
            by_type[server_type].append(loc)
    by_places: dict[tuple[str, ...], list[str]] = {}  # types that had no room in the same places, said once
    for t, locs in by_type.items():
        by_places.setdefault(tuple(locs), []).append(t)
    parts = [f"{_places(types)} in {_places(list(locs))}" for locs, types in by_places.items()]
    name = provider_name(provider)
    head = f"{name} has no room for {parts[0]} now"
    if len(parts) > 1:
        head += ", nor for " + ", ".join(parts[1:])
    return f"{head}; {RETRY_WAIT_WORDS}"


async def create_first(provider: str, tries: list[tuple[str, str, float | None]],
                       create: Callable[[str, str], Awaitable[Any]], *, vm_id: str = "",
                       tried: list[tuple[str, str]] | None = None) -> tuple[Any, str, str, float | None]:
    """Create the server with the first (type, location) that has room: (server, type, location, price).
    Raises NoRoom when none had room, or the provider's own error for anything else. `tried` gets each
    (type, location) that had no room, also when one after them had."""
    tried = [] if tried is None else tried
    skip: set[str] = set()
    last: VmError | None = None
    for i, (server_type, loc, price) in enumerate(tries):
        if server_type in skip:
            continue
        try:
            return await create(server_type, loc), server_type, loc, price
        except VmError as e:
            if DISK_WORDS in e.message:
                skip.add(server_type)  # the image does not fit this type anywhere
            elif e.provider_code not in RETRY_CODES:
                raise
            tried.append((server_type, loc))
            last = e
            nxt = next(((t, lc) for t, lc, _p in tries[i + 1:] if t not in skip), None)
            if nxt is not None:
                log.info("VM %s: %s has no room in %s (%s), trying %s in %s", vm_id, server_type, loc,
                         e.provider_code or e.message[:80], *nxt)
    if not tried:
        raise VmError("no server type to try")
    if last is not None and DISK_WORDS in last.message and all(t in skip for t, _lc in tried):
        raise VmError(f"{provider_name(provider)}: the image does not fit the disk of "
                      f"{', '.join(dict.fromkeys(t for t, _lc in tried))}; ask for a bigger size",
                      provider_code="image_too_big")
    raise NoRoom(no_room_words(provider, tried), tried)
