"""An environment's browser through the owner's proxy (environments.toml `browser_proxies`). A mixin of
EnvironmentManager.

A browser that goes out from a cloud machine's datacenter address is stopped by many sites. The owner adds
static residential proxies (`openajar proxy add lt1 host:port:user:pass`), and each environment whose browser
runs on its machine (browser = "vm") exits through one of them:

- Which one (`assign_browser_proxy`, at open): the label its title (else its repository) had last time when
  no open environment uses it, else the label of browser_proxies that the fewest open environments use,
  ties in list order. A resume keeps the label its save had, so the sites its sign-ins know see the same
  address; when that label is gone it picks again, and the resume's `missing` says so. The label is the
  identity: a save also keeps the exit IP, and a resume whose label now exits elsewhere (`proxy add --yes`
  pointed it at another host) says so too (`exit_moved`). An agent never chooses one and never sees more
  than the label, the exit IP and its country.
- The credentials stay on the node (`browser_proxy_up`, as the environment's network comes up): a forwarder
  of the browser's own (envproxy.EnvForwarder, with the owner's proxy as its upstream) on this computer's
  loopback, which the tunnel carries to the machine's 127.0.0.1:3132 (network.BROWSER_PROXY_PORT_ON_VM).
  The browser gets only Firefox prefs that point at it (network.vm_browser_prefs). Its rules are the
  network's list on a closed network, else every public host on any port. Nothing else on the machine is
  pointed at it: apt, npm and the shell keep their own way out.
- Fail closed (`browser_proxy_options`, before each launch): the node asks ipinfo.io through the proxy, and
  when that fails the launch fails with the label and what to run. Once it runs, Firefox never falls back
  from a manual proxy and the forwarder answers 502 when the proxy refuses: the browser never goes out from
  the machine's own address.
- Where the exit is: its timezone, languages and IP go into Camoufox's config, so the page's clock, its
  languages and WebRTC agree with the address sites see. The browser hands out no position (vmharden drops
  the geolocation keys: an untouched Firefox has none to give). Camoufox's own GeoIP stays off on the machine.

An environment whose browser runs on the node (browser = "node") is not covered: that browser's traffic is
the tunnel's SOCKS proxy (or envproxy.SplitSocks), which does not chain to an upstream.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openajar.core.errors import EngineError
from openajar.core.models import ProxyConfig
from openajar.core.system import redact
from openajar.env.netproxy import EgressPolicy
from openajar.environments.exitplace import (
    EXIT_LANGUAGE,
    accept_language,
    clock_mismatches,
    exit_languages,
    exit_position,
    exit_timezone,
    known_zone,
    language_mismatches,
    locale_config,
    parse_languages,
    place_mismatches,
    with_language,
)
from openajar.environments.models import Environment

log = logging.getLogger("openajar.environments")

PROBE_TIMEOUT_S = 8.0     # the exit check before a launch, through the owner's proxy
BROWSER_MAX_CONNS = 256   # a browser keeps many connections open (the allowlist proxy's 64 is for programs)
RECENT_KEPT = 200         # how far back a new environment looks for the label its title had (last_browser_proxy)
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def pick(pool: list[str], known: dict[str, Any] | set[str], in_use: list[str]) -> str | None:
    """The label of `pool` (that `known` still has) used by the fewest of `in_use`; ties in pool order."""
    choices = [label for label in pool if label in known]
    if not choices:
        return None
    return min(choices, key=in_use.count)  # min keeps the first of equals: the pool's order


class OpenPolicy(EgressPolicy):
    """The browser's own traffic on a full network: every public host on any port (a site may use any),
    never a local name or a private, loopback or link-local address. Through an upstream the forwarder
    resolves no name itself, so this refuses what it can see: such names and literal addresses."""

    def __init__(self) -> None:
        super().__init__(allowlist=[], default="allow")

    def check(self, host: str, port: int) -> tuple[bool, str]:
        if not host or any(c in host for c in " /\\@"):
            return False, "malformed host"
        if not 0 < port < 65536:
            return False, f"port {port} is out of range"
        if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
            return False, f"{host} is a local name"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True, "open egress"
        if not ip.is_global:
            return False, f"{host} is a private or local address"
        return True, "open egress"


def location_config(info: dict[str, Any]) -> dict[str, Any]:
    """Camoufox config keys (camoufox 0.5.9: timezone, geolocation:latitude, :longitude and :accuracy,
    webrtc:ipv4 or :ipv6, the keys its own GeoIP sets) for the exit the probe described. A timezone or
    position it left out (or a timezone that is no real zone) comes from the exit's country
    (openajar.environments.exitplace); without a country it stays unset."""
    out: dict[str, Any] = {}
    tz, source = exit_timezone(info)
    if tz:
        out["timezone"] = tz
    if source != "probe" and info.get("timezone"):
        log.info("the proxy's exit has a timezone that is no zone this knows (%s): %s from its country",
                 str(info["timezone"])[:60], tz or "none")
    position = exit_position(info)
    if position is not None:
        out["geolocation:latitude"], out["geolocation:longitude"], out["geolocation:accuracy"] = position
    try:
        ip = ipaddress.ip_address(str(info.get("ip") or ""))
    except ValueError:
        return out
    out[f"webrtc:ipv{ip.version}"] = str(ip)
    return out


def browser_languages(info: dict[str, Any], language: str = EXIT_LANGUAGE) -> list[str] | None:
    """The locale a launch gives Camoufox (navigator.languages in order, the first also Intl's and the
    browser's own): the exit country's (exitplace.exit_languages) when `language` is "exit", else the
    owner's fixed tags. None: the lane's own locale stays (an exit with no country this knows)."""
    fixed = parse_languages(language)
    return with_language(fixed) if fixed else exit_languages(info)


def exit_options(info: dict[str, Any], language: str = EXIT_LANGUAGE) -> dict[str, Any]:
    """Everything a launch takes from the exit (EXIT_OPTION_KEYS): Camoufox config keys under "config" and
    the locale at the top level, which Camoufox turns into locale:language, :region, :script and :all,
    navigator.languages, Accept-Language, Intl's default locale, the browser's own language and its voices."""
    out: dict[str, Any] = {"config": location_config(info)}
    langs = browser_languages(info, language)
    if langs:
        out["locale"] = langs
        out["config"].update(locale_config(langs))
        out["config"]["headers.Accept-Language"] = accept_language(langs)  # Firefox's own q values
    return out


# What exit_options sets: a fingerprint pinned for an environment keeps none of these, so they always follow
# the exit of this launch (Camoufox also derives locale:* and voices from "locale").
EXIT_CONFIG_KEYS = ("timezone", "geolocation:latitude", "geolocation:longitude", "geolocation:accuracy",
                    "webrtc:ipv4", "webrtc:ipv6", "headers.Accept-Language", "locale:language", "locale:region", "locale:script",
                    "locale:all", "voices")
EXIT_OPTION_KEYS = ("locale",)
EXIT_PREFS = ("intl.locale.requested",)  # the browser's own language, which Camoufox sets from the locale


def claims_words(info: dict[str, Any], language: str = EXIT_LANGUAGE) -> str:
    """One line on what the browser claims at this exit (`openajar proxy test`), and where the exit is: the
    browser itself hands out no position (vmharden)."""
    got = exit_options(info, language)
    config, langs = got["config"], got.get("locale")
    where = ("near {:.2f},{:.2f}".format(config["geolocation:latitude"], config["geolocation:longitude"])
             if "geolocation:latitude" in config else "no position")
    return (f"browser: {config.get('timezone') or 'no timezone'}, {', '.join(langs) if langs else 'the lane locale'}"
            f" (exit {where})")


def exit_mismatches(seen: dict[str, Any], info: dict[str, Any], language: str = EXIT_LANGUAGE) -> list[str]:
    """What a page saw that does not agree with the exit, in plain words ([] when all agree). `seen`, as a
    page reads it: timezone (Intl), offset_minutes (new Date().getTimezoneOffset()), languages
    (navigator.languages), accept_language (the header a server got), intl_locale (Intl's default locale),
    latitude and longitude (geolocation, when it gave one), ips (the addresses in WebRTC candidates)."""
    want = exit_options(info, language)
    out = clock_mismatches(seen, want["config"].get("timezone"))
    out += language_mismatches(seen, want.get("locale"))
    out += place_mismatches(seen, want["config"], str(info.get("ip") or ""))
    return out


def _thin(info: dict[str, Any]) -> bool:
    return not (info.get("country") and info.get("loc") and known_zone(str(info.get("timezone") or "")))


_MORE: dict[str, tuple[float, dict[str, str]]] = {}  # exit IP -> (when, the second source's answer)
MORE_KEPT_S = 6 * 3600


def _probe(proxy: ProxyConfig, timeout: float) -> dict[str, str]:
    """ipinfo.io through the proxy (the check before each launch). When it leaves the country, position or
    timezone out, or answers with an error although the proxy carried the request, the second source
    (Host.proxy_exit_geo, geojs.io) fills the gaps; its answer is kept for the exit IP a while, so a relaunch
    asks only ipinfo. The proxy's own failure (no connection, 407) raises at once."""
    import time

    from openajar.core.system import Host

    host = Host()
    first: OSError | None = None
    try:
        info = host.proxy_exit_info(proxy, timeout=timeout)
    except OSError as e:
        if not str(e).startswith(("HTTP ", "no exit IP")):
            raise
        first, info = e, {}
    if not _thin(info):
        return info
    kept = _MORE.get(info.get("ip", ""))
    if kept is not None and time.monotonic() - kept[0] < MORE_KEPT_S:
        return _merged(info, kept[1])
    try:
        more = host.proxy_exit_geo(proxy, timeout=timeout)
    except OSError:
        if first is not None:
            raise first from None
        return info
    if info and (more.get("ip") != info.get("ip") or more.get("country", info.get("country")) != info.get("country")):
        return info  # another exit answered (a rotating proxy) or another place: not this exit's
    _MORE[more["ip"]] = (time.monotonic(), more)
    return _merged(info, more)


def _merged(info: dict[str, str], more: dict[str, str]) -> dict[str, str]:
    """ipinfo's answer with what it lacked from the second source (a timezone that is no zone counts as lacking)."""
    out = {**more, **{k: v for k, v in info.items() if v}}
    if not known_zone(str(info.get("timezone") or "")) and more.get("timezone"):
        out["timezone"] = more["timezone"]
    return out


def gone_words(label: str) -> str:
    return (f"The browser's proxy {label} is not set up for this environment (it was removed from this node's "
            "proxies, or was missing when the environment came up), so the browser did not start: it never goes "
            "out without its proxy. Close this environment and open it again, and it takes one of the proxies "
            f"there are. The owner adds {label} again with `openajar proxy add {label} host:port:user:pass`.")


def failed_words(label: str, why: str) -> str:
    return (f"The browser's proxy {label} did not work: {why}. The browser did not start, since it never goes out "
            f"without its proxy. Tell the owner to run `openajar proxy test {label}`.")


class BrowserProxies:
    """Each environment's browser proxy (EnvironmentManager's self.home, self.settings, self.store, self._nets)."""

    home: Any
    settings: Any
    store: Any
    _nets: dict[str, Any]
    proxy_probe: Callable[[ProxyConfig, float], dict[str, str]] | None = None  # tests set one

    def owner_proxies(self) -> dict[str, ProxyConfig]:
        """The owner's proxies by label (secrets/proxies.json); none when the file cannot be read."""
        from openajar.core.config import NodeConfig, load_proxies

        try:
            return load_proxies(NodeConfig(home=self.home))
        except (OSError, ValueError) as e:
            log.warning("could not read this node's proxies: %s", e)
            return {}

    def browser_proxy_pool(self) -> list[str]:
        """browser_proxies as environments.toml says now: `openajar proxy add` and `remove` change it while
        the node runs, and the next open follows. A file that does not load keeps what the node started with."""
        from openajar.environments.models import SETTINGS_FILE, load_settings

        if not (Path(self.home) / SETTINGS_FILE).exists():
            return list(self.settings.browser_proxies)
        try:
            return list(load_settings(self.home).browser_proxies)
        except (OSError, ValueError) as e:
            log.warning("environments.toml does not load (%s); the browser proxies stay as the node started", e)
            return list(self.settings.browser_proxies)

    def last_browser_proxy(self, env: Environment) -> str | None:
        """The label the newest earlier environment of env's family with its title (else its repository) exited
        through: a new environment of the same work goes on from the same address when it can."""
        from openajar.environments.picking import same_title

        earlier = [e for e in self.store.recent(RECENT_KEPT) if e.id != env.id and e.browser_proxy
                   and e.family == env.family]
        for match in (lambda e: same_title(e.title, env.title), lambda e: bool(env.repo) and e.repo == env.repo):
            found = next((e for e in earlier if match(e)), None)  # recent() is newest first
            if found is not None:
                return found.browser_proxy
        return None

    def assign_browser_proxy(self, env: Environment) -> None:
        """At open (create(), under its lock): the label env's browser exits through. A resume brings its
        save's label in `resumed` (Saving.resuming); it is taken out here, and kept when it is still in the
        pool and in proxies.json, else a new pick and a note for the resume's `missing`. A new environment
        takes the label its title (else its repository) had last time when no open environment uses it
        (last_browser_proxy), else the one the fewest open environments use."""
        resumed = dict(env.resumed) if env.resumed is not None else None
        saved = resumed.pop("browser_proxy", None) if resumed is not None else None
        label = None
        pool = self.browser_proxy_pool() if env.browser == "vm" else []
        if pool:
            known = self.owner_proxies()
            in_use = [e.browser_proxy for e in self.store.open() if e.browser_proxy and e.id != env.id]
            last = self.last_browser_proxy(env) if saved is None else None
            if saved in pool and saved in known:
                label = saved
            elif last in pool and last in known and last not in in_use:
                label = last
            else:
                label = pick(pool, known, in_use)
        if saved and label != saved and resumed is not None:
            now = f"now exits through the proxy {label}" if label else "now goes out from the machine itself"
            resumed["browser_proxy_note"] = (f"the browser's proxy {saved} is no longer one this node's environments "
                                             f"use, so the browser {now}: a new address, so sites may ask to sign "
                                             "in again")
        env.browser_proxy = label
        if resumed is not None:
            env.resumed = resumed

    async def browser_proxy_up(self, env: Environment, node: Any) -> None:
        """Start env's browser forwarder on this computer's loopback (Network._net_up), when it has a proxy.
        A label that is not in proxies.json starts nothing, so the launch fails closed (browser_proxy_options)."""
        if env.browser != "vm" or not env.browser_proxy:
            return
        proxy = self.owner_proxies().get(env.browser_proxy)
        if proxy is None:
            log.warning("environment %s: its browser's proxy %s is not in this node's proxies; its browser will not "
                        "start", env.id, env.browser_proxy)
            return
        from openajar.env.netproxy import Upstream
        from openajar.environments.envproxy import EnvForwarder

        closed = node.forwarder  # a closed network: the browser keeps to its list, and bound hosts to the swap
        policy = closed.policy if closed is not None else OpenPolicy()
        fwd = EnvForwarder(env.id, policy, node.blocked, intercept=getattr(closed, "intercept", None),
                           upstream=Upstream(proxy.host, proxy.port, proxy.username, proxy.password),
                           max_conns=BROWSER_MAX_CONNS)
        server = await asyncio.start_server(fwd._client, "127.0.0.1", 0)
        node.servers.append(server)
        node.browser_forwarder, node.browser_upstream = fwd, proxy
        node.browser_proxy_port = int(server.sockets[0].getsockname()[1])

    async def browser_proxy_options(self, env: Any) -> dict[str, Any]:
        """What a launch of env's browser on its machine adds to Camoufox's options: {} without a proxy, else
        what it takes from the exit (exit_options: its place as config, its country's languages as the locale,
        or the owner's fixed browser_language). Asks ipinfo.io through the proxy first, and raises EngineError (the
        label and what to run) when it cannot: the browser then does not start."""
        label = getattr(env, "browser_proxy", None)
        if getattr(env, "browser", None) != "vm" or not label:
            return {}
        node = self._nets.get(env.id)
        proxy = getattr(node, "browser_upstream", None)
        if node is None or proxy is None:
            raise EngineError(gone_words(label))
        probe = self.proxy_probe or _probe
        try:
            # ipinfo, and the second source only when its answer is thin (_probe): two requests at most
            info = await asyncio.wait_for(asyncio.to_thread(probe, proxy, PROBE_TIMEOUT_S), 2 * PROBE_TIMEOUT_S + 2)
        except (OSError, TimeoutError) as e:
            why = redact(str(e) or "no answer in time", proxy.username, proxy.password)
            log.warning("environment %s: its browser's proxy %s failed its check: %s", env.id, label, why)
            raise EngineError(failed_words(label, why)) from None
        before = (getattr(node, "browser_exit", None) or {}).get("ip")
        node.browser_exit = info
        self.exit_moved(env, label, before or self.saved_exit_ip(env), info.get("ip"))
        return exit_options(info, self.browser_language())

    def browser_language(self) -> str:
        """environments.toml browser_language as it says now (like browser_proxy_pool): "exit" or fixed tags."""
        from openajar.environments.models import SETTINGS_FILE, load_settings

        if (Path(self.home) / SETTINGS_FILE).exists():
            try:
                return str(load_settings(self.home).browser_language)
            except (OSError, ValueError) as e:
                log.warning("environments.toml does not load (%s); the browser's language stays as the node started", e)
        return str(getattr(self.settings, "browser_language", EXIT_LANGUAGE))

    def browser_exit_ip(self, env: Environment) -> str | None:
        """The exit IP the last launch of env's browser found, for its save (SaveManifest.browser_exit_ip)."""
        if not env.browser_proxy:
            return None
        return (getattr(self._nets.get(env.id), "browser_exit", None) or {}).get("ip")

    def saved_exit_ip(self, env: Any) -> str | None:
        """The exit IP the save env resumes had, when it resumes one."""
        save_id = getattr(env, "restore_from", None)
        if not save_id:
            return None
        try:
            m, _ = self.saves().find(save_id)  # type: ignore[attr-defined]  (Saving, on the same manager)
        except Exception:  # noqa: BLE001 - a save forgotten meanwhile: nothing to compare with
            return None
        return m.browser_exit_ip if m.browser_proxy == env.browser_proxy else None

    def exit_moved(self, env: Any, label: str, was: str | None, now: str | None) -> None:
        """The label's proxy now exits from another address than the environment's browser had (`openajar proxy
        add <label> ... --yes` pointed it elsewhere, or the provider moved it): the sites its sign-ins know
        see a new IP. Logged, and said in the resume's `missing` for the agent."""
        if not was or not now or was == now:
            return
        log.warning("environment %s: its browser's proxy %s now exits from %s, it was %s", env.id, label, now, was)
        words = (f"the browser's proxy {label} now exits from {now}, and the browser had {was} before: a new "
                 "address, so sites may ask to sign in again")

        def say(e: Environment) -> bool:
            r = dict(e.resumed or {})
            if words in (r.get("missing") or []):
                return False
            r["missing"] = [*(r.get("missing") or []), words]
            e.resumed = r
            return True

        if getattr(env, "resumed", None) is not None:
            self.store.update(env.id, say)

    def browser_proxy_view(self, env: Environment) -> dict[str, Any] | None:
        """environment()'s network.browser_proxy: the label, and the exit the last launch found. Never the
        proxy's host, port or credentials."""
        if not env.browser_proxy:
            return None
        got = getattr(self._nets.get(env.id), "browser_exit", None) or {}
        return {"label": env.browser_proxy, "exit_ip": got.get("ip"), "exit_country": got.get("country")}
