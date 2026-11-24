"""Where a browser proxy's exit is, as a browser there would show it: its timezone, its position and the
languages a browser in that country is set to (openajar.environments.browserproxy).

Pure functions over the exit-info answer (Host.proxy_exit_info: ip, country, region, city, timezone, loc) and
exitplaces.json, which ships with openajar so a node needs neither Camoufox nor a GeoIP database:

- "languages": each country's language, from CLDR's territoryInfo (the table Camoufox's own GeoIP draws a
  language from by speaker share): the official language most people speak, or English where English is an
  official language at least 15% of the people use (India, Pakistan, Nigeria, the Philippines...), since the
  browsers there are mostly English. A few countries follow the region (LANGUAGE_BY_REGION): Quebec is French,
  Wallonia and Brussels French, Romandy French, Ticino Italian.
- "zones": each country's IANA zones with their coordinates (tzdata's zone.tab), its most populous zone first:
  the timezone and position when the answer left them out.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import zoneinfo
from pathlib import Path
from typing import Any

PLACES_FILE = Path(__file__).with_name("exitplaces.json")
EXIT_LANGUAGE = "exit"  # environments.toml browser_language: follow the exit's country
TAG_RE = re.compile(r"[a-z]{2,3}(-[A-Z]{2}|-\d{3})?")  # language, or language-country (no script: Camoufox drops it)
COUNTRY_RE = re.compile(r"[A-Z]{2}")
CITY_ACCURACY_M = (1500, 6000)    # an IP-based position of a city: a few km, as a network location answers
COUNTRY_ACCURACY_M = 50_000       # only the country is known: its main zone's city, far less sure

# (country, words in ipinfo's region name) -> language: where the country's language follows the region
LANGUAGE_BY_REGION: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "CA": ((("quebec", "québec"), "fr"),),
    "BE": ((("wallon", "wallonia", "brussels", "bruxelles"), "fr"),),
    "CH": ((("geneva", "genève", "geneve", "vaud", "neuch", "jura", "valais", "fribourg"), "fr"),
           (("ticino",), "it")),
}


@functools.cache
def places() -> dict[str, Any]:
    return dict(json.loads(PLACES_FILE.read_text()))


def country_of(info: dict[str, Any]) -> str | None:
    cc = str(info.get("country") or "").strip().upper()
    return cc if COUNTRY_RE.fullmatch(cc) else None


def country_language(country: str | None, region: str | None = None) -> str | None:
    """The language a browser in `country` (and its `region`, as ipinfo names it) is most likely set to."""
    if not country:
        return None
    where = (region or "").lower()
    for words, lang in LANGUAGE_BY_REGION.get(country, ()):
        if any(w in where for w in words):
            return lang
    got = places()["languages"].get(country)
    return str(got) if got else None


def exit_languages(info: dict[str, Any]) -> list[str] | None:
    """navigator.languages for a browser at the exit: the country's language with the country, the language
    alone, then English (en-US, en) after it, the shape Firefox's localized builds and most browsers set up
    in the country have (lt-LT, lt, en-US, en). An English country: en-<country>, en. None when the answer
    has no country this knows."""
    country = country_of(info)
    lang = country_language(country, str(info.get("region") or ""))
    if not country or not lang:
        return None
    tags = [f"{lang}-{country}", lang] + ([] if lang == "en" else ["en-US", "en"])
    return list(dict.fromkeys(tags))


def parse_languages(value: str) -> list[str]:
    """environments.toml browser_language: "exit", or language tags in order, the first with its country
    ("en-US" or "de-DE, de, en"). Raises ValueError."""
    text = str(value).strip()
    if text == EXIT_LANGUAGE:
        return []
    tags = [t.strip() for t in text.split(",") if t.strip()]
    bad = [t for t in tags if not TAG_RE.fullmatch(t)]
    if not tags or bad:
        raise ValueError(f'browser_language is "exit" or language tags like "en-US" or "de-DE, de, en": {value!r}')
    if "-" not in tags[0] or not re.search(r"-([A-Z]{2}|\d{3})$", tags[0]):
        raise ValueError(f"browser_language's first tag names its country too (en-US, not en): {value!r}")
    return list(dict.fromkeys(tags))


def accept_language(tags: list[str]) -> str:
    """The Accept-Language header Firefox sends for these languages (nsHttpHandler's PrepareAcceptLanguages:
    q falls by 1/n from 1, one decimal below 10 tags): en-US, en is "en-US,en;q=0.5"; lt-LT, lt, en-US, en is
    "lt-LT,lt;q=0.8,en-US;q=0.5,en;q=0.3". Camoufox's own header has Chrome's steps (0.9, 0.8...)."""
    n = len(tags)
    parts = []
    for i, tag in enumerate(tags):
        q = 1.0 - i / n
        if i == 0:
            parts.append(tag)
        elif n < 10:
            parts.append(f"{tag};q=0.{int(q * 10 + 0.5)}")
        else:
            parts.append(f"{tag};q=0.{int(q * 100 + 0.5):02d}")
    return ",".join(parts)


def with_language(tags: list[str]) -> list[str]:
    """One tag with its country (en-US): Firefox also lists its language (en-US, en), as it does by itself."""
    if len(tags) == 1 and "-" in tags[0]:
        return [tags[0], tags[0].split("-")[0]]
    return tags


def locale_config(tags: list[str]) -> dict[str, str]:
    """The locale:* config keys Camoufox's handle_locales makes of these tags (camoufox 0.5.9): the first one's
    language, country and script (its Suppress-Script, when the language has one), and all of them, joined.
    Set by the node too, so they win over a pinned fingerprint's (vmpin), as the exit's place does."""
    lang, _, rest = tags[0].partition("-")
    region = rest.rsplit("-", 1)[-1]
    out = {"locale:language": lang, "locale:region": region}
    script = places()["scripts"].get(lang)
    if script:
        out["locale:script"] = str(script)
    if len(tags) > 1:
        out["locale:all"] = ", ".join(tags)
    return out


def known_zone(name: str) -> bool:
    """A real IANA zone name: one this computer knows, or one of zone.tab (a node's tz database may be older)."""
    if not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", name):
        return False
    if any(name == z[0] for zones in places()["zones"].values() for z in zones):
        return True
    try:
        zoneinfo.ZoneInfo(name)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return False
    return "/" in name  # zoneinfo also knows "UTC", "EST5EDT"...: none is where a person is


def parse_loc(info: dict[str, Any]) -> tuple[float, float] | None:
    lat, _, lon = str(info.get("loc") or "").partition(",")
    try:
        latitude, longitude = float(lat), float(lon)
    except ValueError:
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if -90 <= latitude <= 90 and -180 <= longitude <= 180 and (latitude, longitude) != (0.0, 0.0):
        return latitude, longitude
    return None


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = (tuple(map(math.radians, p)) for p in (a, b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _country_zones(country: str | None) -> list[list[Any]]:
    return list(places()["zones"].get(country or "", []))


def exit_timezone(info: dict[str, Any]) -> tuple[str | None, str]:
    """The exit's timezone and where it came from: "probe" (the answer's own), "nearest" (the country's zone
    nearest the answer's position), "country" (the country's main zone) or "none"."""
    tz = str(info.get("timezone") or "").strip()
    if known_zone(tz):
        return tz, "probe"
    zones = _country_zones(country_of(info))
    if not zones:
        return None, "none"
    here = parse_loc(info)
    if here is not None and len(zones) > 1:
        return str(min(zones, key=lambda z: km_between(here, (z[1], z[2])))[0]), "nearest"
    return str(zones[0][0]), "country"


def exit_position(info: dict[str, Any]) -> tuple[float, float, float] | None:
    """(latitude, longitude, accuracy in metres): the answer's position, a city's accuracy drawn from the exit
    IP (the same every launch on that IP); without one the main zone's city of its country, far less sure."""
    here = parse_loc(info)
    if here is not None:
        low, high = CITY_ACCURACY_M
        seed = int.from_bytes(hashlib.sha256(str(info.get("ip") or "").encode()).digest()[:4], "big")
        return here[0], here[1], float(low + seed % (high - low))
    zones = _country_zones(country_of(info))
    if not zones:
        return None
    return float(zones[0][1]), float(zones[0][2]), float(COUNTRY_ACCURACY_M)


def clock_mismatches(seen: dict[str, Any], tz: str | None) -> list[str]:
    """The page's timezone and its clock's offset against the exit's zone."""
    out: list[str] = []
    if not tz:
        return out
    got = seen.get("timezone")
    if got is not None and got != tz:
        out.append(f"the page's timezone is {got}, the exit's is {tz}")
    offset = seen.get("offset_minutes")
    if offset is not None:
        from datetime import datetime

        now = datetime.now(zoneinfo.ZoneInfo(tz)).utcoffset()
        want = -int(now.total_seconds() // 60) if now is not None else 0
        if int(offset) != want:
            out.append(f"the page's clock is {-int(offset)} minutes off UTC, {tz} is {-want} now")
    return out


def accept_tags(header: str) -> list[str]:
    """The language tags of an Accept-Language header, in its order (q values dropped)."""
    return [part.split(";")[0].strip() for part in str(header).split(",") if part.split(";")[0].strip()]


def language_mismatches(seen: dict[str, Any], langs: list[str] | None) -> list[str]:
    """navigator.languages, the Accept-Language header and Intl's default locale against the locale the
    launch gave, and against each other."""
    out: list[str] = []
    page = [str(x) for x in seen.get("languages") or []]
    if langs and page and page[:len(langs)] != langs:  # one tag ("en-US"): Firefox adds its language (en)
        out.append(f"the page's languages are {', '.join(page)}, the exit's are {', '.join(langs)}")
    header = seen.get("accept_language")
    if header is not None and page and accept_tags(header) != page:
        out.append(f"the Accept-Language header ({header}) does not list the page's languages ({', '.join(page)})")
    elif header is not None and langs and accept_tags(header) != langs:
        out.append(f"the Accept-Language header ({header}) is not the exit's languages ({', '.join(langs)})")
    elif header is not None and page and header.replace(" ", "") != accept_language(page):
        out.append(f"the Accept-Language header ({header}) is not the one Firefox sends ({accept_language(page)})")
    intl = seen.get("intl_locale")
    first = langs[0] if langs else (page[0] if page else None)
    if intl is not None and first and str(intl).lower() != first.lower():
        out.append(f"Intl's locale is {intl}, the browser's first language is {first}")
    return out


def place_mismatches(seen: dict[str, Any], config: dict[str, Any], exit_ip: str) -> list[str]:
    """The page's geolocation against the exit's position, and the public addresses WebRTC shows."""
    import ipaddress

    out: list[str] = []
    lat, lon = seen.get("latitude"), seen.get("longitude")
    if lat is not None and lon is not None and "geolocation:latitude" in config:
        want = (float(config["geolocation:latitude"]), float(config["geolocation:longitude"]))
        far = km_between((float(lat), float(lon)), want)
        if far > max(50.0, float(config.get("geolocation:accuracy") or 0) / 500):
            out.append(f"the page's position is {far:.0f} km from the exit's")
    for addr in seen.get("ips") or []:
        try:
            ip = ipaddress.ip_address(str(addr))
        except ValueError:
            continue  # an mDNS name (...local) hides the address, as Firefox does
        if ip.is_global and str(ip) != exit_ip:
            out.append(f"WebRTC shows {ip}, not the exit {exit_ip}")
    return out
