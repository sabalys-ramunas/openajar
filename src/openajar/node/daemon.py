"""agentosd: the composition root and the long-running node process.

Real implementations are imported lazily, inside `_build_lane_manager` and
`_build_interaction`, so unit tests (and the CLI's non-lane commands) load on a
Mac without Camoufox, Playwright or Xvfb. Those two functions are the only
place that knows the concrete modules; they call each module's own factory
(`openajar.node.lanes.build_lane_manager`, `openajar.node.interaction.create_interaction`).

The MCP surface is composed here too: the node's own tools (openajar.node.tools) plus
every feature package found in FEATURE_TOOLS (openajar.mcp.providers), each through one wrapper. Until a policy
service is registered on the runtime that wrapper is allow-all (the owner); once one is,
openajar.policy.wrap decides every call, filters tools/list per token and vouches for
grant tokens.

Optional features are plugin-gated: openajar.plugins says which are on, and only their services
(build_plugins) and tools (openajar.mcp.providers._plugin_tools) load. FEATURE_TOOLS and FEATURE_SERVICES always load.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import os
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openajar.core.config import NodeConfig, check_mcp_bind
from openajar.core.runtime import Runtime
from openajar.core.tools import TOOL_CATALOG, ToolSpec
from openajar.mcp.providers import Provider, tool_providers
from openajar.node.interfaces import Interaction, LaneManager
from openajar.node.tools.lanes import ssh_host as _ssh_host

if TYPE_CHECKING:
    from openajar.mcp.http import TokenBinder, TokenCheck
    from openajar.mcp.registry import VisibleFilter, Wrap

log = logging.getLogger(__name__)

SHUTDOWN_GRACE = 5.0  # seconds uvicorn waits for open MCP streams before closing them
# How long an idle keep-alive connection stays open. uvicorn's default (5 s) closed pooled connections
# while agents thought between calls, and a client that reused one at that moment got a bare reset
# (Bun's fetch in Claude Code: "ECONNRESET: The socket connection was closed unexpectedly", 4 times in
# the 2026-11-15 test with three chats). The client should close an idle connection first, never the
# node: a long idle on localhost costs nothing.
KEEP_ALIVE_S = 620
LANES_SHUTDOWN_TIMEOUT = 20.0  # systemd TimeoutStopSec is 30; leave room for the force kill
SHUTDOWN_DEADLINE = 28.0  # seconds from the first signal to a hard exit, logged; systemd kills at 30
HARD_EXIT_CODE = 3  # a forced stop: not a crash (1), not a usage error (2)

POLICY_WRAP = "openajar.policy.wrap"  # wrap_tool, bind_request_token, visible_tools

# Feature services, built in this order by each module's `build(runtime)` (which registers them
# on the runtime), started after the lanes and stopped before them in reverse order. The event
# bus comes first, so every later service can publish to the panel. Policy is required: once it
# is installed, a node whose policy cannot build or start refuses to run rather than serve every
# call as the owner.
FEATURE_SERVICES: tuple[str, ...] = (
    "openajar.api.events",
    "openajar.policy.service",
    "openajar.freeze.service",  # right after policy: its gate is closed before anything else is built
    "openajar.vault.service",
    "openajar.git.service",  # the git gateway: lanes and brains reach declared remotes through it (docs/GIT.md)
    "openajar.workspaces.service",
    "openajar.memory.service",  # workspace memory (after policy: trust and labels come from it)
    "openajar.runner.service",
    "openajar.brains.service",
    "openajar.env.service",
    "openajar.vms.service",  # after env: a VM lane is a terminal lane; its reaper deletes VMs past their time
    "openajar.environments.service",  # after vms and git: an environment is a VM, a browser lane and the gateway
    "openajar.profiles.service",  # each account's profile and voice, and the learned-field sync
    "openajar.events.service",  # after the runner and brains: it delivers to them, and hooks their ends
    "openajar.webhooks.service",  # after the event queue: it listens on the bus, and sends what a receiver asked for
    "openajar.agenda.service",  # planned one-off actions, ticked by the runner's scheduler (after events: triggers)
    "openajar.ops.service",  # health checks look at everything above
    "openajar.keepalive.service",  # read-only visits to idle lanes (off until the owner turns it on)
    "openajar.node.expose.service",  # the owner's dev servers in a lane, a lane's port on the laptop
    "openajar.studio.browser.service",  # the review browser pool (not a lane): starts an engine on first use
    "openajar.node.anchors",  # audit checkpoint heads, sent off the node (off with no target)
    "openajar.node.channel.service",  # last: controller work arrives only once everything above runs
)
REQUIRED_SERVICES: frozenset[str] = frozenset({"openajar.policy.service", "openajar.freeze.service"})
SERVICES_KEY = "feature_services"  # runtime entry: [(module, service), ...] in start order
SERVICE_STOP_TIMEOUT = 5.0  # per service; lanes still get LANES_SHUTDOWN_TIMEOUT afterwards
SERVICE_STOP_TIMEOUTS: dict[str, float] = {"openajar.env.service": 10.0}  # stops supervised services

# Plugin-gated features: the modules are named by the manifests in openajar.plugins.entries, and
# nothing a plugin does can stop the node. These two are the registry itself.
PLUGINS_SERVICE = "openajar.plugins.service"
PLUGIN_START_TIMEOUT = 30.0  # a plugin's service that hangs is a plugin that failed to start
LAST_SERVICE = "openajar.node.channel.service"  # stays last: controller work arrives once all else runs


def _build_lane_manager(config: NodeConfig) -> LaneManager:
    """The lanes module's own wiring: SQLite store, Camoufox engine, Xvfb/x11vnc/noVNC displays."""
    from openajar.node.lanes import build_lane_manager as lanes_factory

    return lanes_factory(config)


def _build_interaction(config: NodeConfig) -> Interaction:
    """Playwright interaction with human-like input."""
    from openajar.node.interaction import create_interaction

    return create_interaction()


def build_lane_manager(config: NodeConfig) -> LaneManager:
    """A LaneManager for the CLI: not started, so it never sweeps the daemon's browsers."""
    return _build_lane_manager(config)


def node_runtime(config: NodeConfig, lanes: LaneManager, interaction: Interaction | None, **services: Any) -> Runtime:
    """A Runtime with the node's own services (config, lanes, interaction, the facts an approval card
    shows, the tools it serves, what keeps the page after an agent's commit) plus any others. No
    `interaction` (openajar local: no browser here) leaves it off, so the browser tools are not served."""
    from openajar.audit.captures import after_commit
    from openajar.mcp.providers import ProviderCatalog
    from openajar.node.cardfacts import NodeCardFacts
    from openajar.node.solver import settings as solver_settings
    from openajar.node.tools.browser import AFTER_COMMIT

    if interaction is not None:
        services["interaction"] = interaction
    solver_settings.bind_home(config.home)  # the owner's solving services, if any (openajar.node.solver)
    runtime = Runtime(config=config, lanes=lanes, card_facts=NodeCardFacts(), **services)
    if not runtime.has(AFTER_COMMIT):
        runtime.register(AFTER_COMMIT, after_commit)
    if not runtime.has(TOOL_CATALOG):
        runtime.register(TOOL_CATALOG, ProviderCatalog(runtime))
    return runtime


def build_runtime(config: NodeConfig, services: Sequence[str] = FEATURE_SERVICES) -> Runtime:
    lanes, interaction = _build_lane_manager(config), _build_interaction(config)
    watch = getattr(lanes, "watch", None)  # every signal an interaction reads reaches the lane manager (holds)
    runtime = node_runtime(config, lanes, watch(interaction) if callable(watch) else interaction)
    _register_flow_driver(runtime)
    build_services(runtime, services)
    build_plugins(runtime)
    _install_flow_take_over(runtime)
    _install_memory_cards(runtime)
    return runtime


def _register_flow_driver(runtime: Runtime) -> None:
    """Flows replay on real browsers through the node's own lanes (openajar.node.flow_driver), and
    the vault types secrets into real pages through the guarded typist (openajar.node.typist)."""
    from openajar.node.files import NodeFiles
    from openajar.node.flow_driver import driver_factory
    from openajar.node.typist import typist_factory

    runtime.register("files", NodeFiles(runtime.require("config")))  # flows upload by inbox file id
    runtime.register("flow_driver", driver_factory(runtime))
    runtime.register("typist", typist_factory(runtime))


def _install_flow_take_over(runtime: Runtime) -> None:
    """A take over in the middle of a flow run pauses the run instead of failing it."""
    runner = runtime.get("runner")
    if runner is None:
        return
    from openajar.node.flow_lanes import install_take_over_pause

    install_take_over_pause(runner)


def _install_memory_cards(runtime: Runtime) -> None:
    """The memory watchdog (a step of the lane manager's reaper loop) puts its warnings in Needs you."""
    from openajar.node.memwatch import install

    install(runtime)


def _installed(name: str) -> Any | None:
    """The module, or None when its package is not part of this build. A module that is
    present but needs something missing is an error, not "not installed"."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name and (name == exc.name or name.startswith(exc.name + ".")):
            return None
        raise


def build_services(runtime: Runtime, modules: Sequence[str] = FEATURE_SERVICES) -> list[tuple[str, Any]]:
    """Build every installed feature service, in order, and remember them for start and stop."""
    built: list[tuple[str, Any]] = runtime.get(SERVICES_KEY) or []
    for name in modules:
        required = name in REQUIRED_SERVICES
        try:
            module = _installed(name)
            if module is None:
                if required:  # never skipped: without the policy every MCP call would run as the owner
                    raise ModuleNotFoundError(f"{name} is not installed", name=name)
                log.info("%s is not installed", name)
                continue
            service = module.build(runtime)
            service_name = getattr(module, "SERVICE_NAME", None)
            if service_name and not runtime.has(service_name):
                runtime.register(service_name, service)  # builds that leave registering to the caller
        except Exception as exc:
            if required:
                raise RuntimeError(f"{name} failed to build ({type(exc).__name__}: {exc}); refusing to "
                                   "start without it") from exc
            log.exception("%s failed to build; it is not available on this node", name)
            continue
        log.info("built %s", name)
        built.append((name, service))
    runtime.register(SERVICES_KEY, built)
    return built


def build_plugins(runtime: Runtime) -> None:
    """The registry, then the services of every plugin that is on and has what it needs. Runs after
    the feature services, so its checks see them. A failure here is that plugin's alone."""
    try:
        module = _installed(PLUGINS_SERVICE)
        if module is None:
            return
        registry = module.build(runtime)
        plugins = registry.to_load()
    except Exception:
        log.exception("the plugin registry failed to build; no plugin is loaded")
        return
    for plugin in plugins:
        modules = [name for name in plugin.services if name not in FEATURE_SERVICES]
        built = build_services(runtime, modules)
        missing = [name for name in modules if name not in {entry[0] for entry in built}]
        if missing:
            built[:] = [entry for entry in built if entry[0] not in modules]  # all of a plugin, or none of it
            registry.mark_failed(plugin.id, f"{missing[0]} did not build")
        registry.confirm(plugin)
    built = runtime.get(SERVICES_KEY) or []
    built.sort(key=lambda entry: entry[0] == LAST_SERVICE)  # stable: only the last service moves


async def start_services(runtime: Runtime) -> None:
    registry: Any = runtime.get("plugins")
    plugin_of = registry.service_owners() if registry is not None else {}
    for name, service in runtime.get(SERVICES_KEY) or []:
        start = getattr(service, "start", None)
        if start is None:
            continue
        plugin = plugin_of.get(name)
        if plugin and registry.has_failed(plugin):
            continue  # its tools clashed or did not load: nothing of it runs
        try:
            await (asyncio.wait_for(start(), PLUGIN_START_TIMEOUT) if plugin else start())
        except Exception as exc:
            if name in REQUIRED_SERVICES:
                raise RuntimeError(f"{name} failed to start ({type(exc).__name__}); refusing to serve") from exc
            log.exception("%s failed to start", name)
            if plugin:
                registry.mark_failed(plugin, f"{name} failed to start ({type(exc).__name__})")


async def stop_services(runtime: Runtime) -> None:
    """Stop in reverse start order, each within SERVICE_STOP_TIMEOUT; a stuck one is abandoned."""
    for name, service in reversed(runtime.get(SERVICES_KEY) or []):
        stop = getattr(service, "stop", None) or getattr(service, "shutdown", None)
        timeout = SERVICE_STOP_TIMEOUTS.get(name, SERVICE_STOP_TIMEOUT)
        if stop is not None:
            try:
                await asyncio.wait_for(stop(), timeout)
            except TimeoutError:
                log.error("%s did not stop within %ss", name, timeout)
            except Exception:
                log.exception("%s failed to stop", name)
        close = getattr(service, "close", None)
        if callable(close) and not inspect.iscoroutinefunction(close):
            try:
                close()
            except Exception:
                log.exception("%s failed to close", name)


def ssh_host(config: NodeConfig | None = None) -> str:
    """The SSH alias printed in tunnel hints: OPENAJAR_SSH_HOST wins, then node.toml."""
    return _ssh_host(config)


@dataclass
class McpHooks:
    """The policy seam of the MCP layer; the defaults are M1's allow-all."""
    wrap: Wrap | None = None                # None: openajar.mcp.registry.allow_all_wrap
    visible: VisibleFilter | None = None    # (token, specs) -> specs; None: no filter
    bind_token: TokenBinder | None = None   # extra per-request token binder (a context manager)
    accept_token: TokenCheck | None = None  # (token) -> bool, for tokens other than the node's own
    explain_token: Any = None               # (token) -> (code, message) | None: a refusal to say (frozen)


def _positional_count(fn: Callable[..., Any]) -> int:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 2
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return sum(1 for p in params if p.kind in kinds)


def mcp_hooks(runtime: Runtime, module: str | None = None) -> McpHooks:
    """Allow-all as the owner while no `policy` service is registered. With one, the policy
    package's wrapper, binder and filter are used; if they cannot be loaded the node refuses
    to start rather than fall back to allow-all."""
    if not runtime.has("policy"):
        log.info("no policy service: every MCP call runs as the owner")
        return McpHooks()
    module = module or POLICY_WRAP
    policy = importlib.import_module(module)
    visible_tools = policy.visible_tools
    if _positional_count(visible_tools) >= 3:
        def visible(token: str | None, specs: list[ToolSpec]) -> list[ToolSpec]:
            return visible_tools(token, specs, runtime)
    else:
        visible = visible_tools
    accept = getattr(policy, "accept_token", None)
    if accept is None and callable(getattr(runtime.policy, "resolve", None)):
        service = runtime.policy

        def accept(token: str) -> bool:
            try:
                service.resolve(token)
            except Exception:  # noqa: BLE001  (a token that does not resolve is simply refused)
                return False
            return True
    log.info("MCP calls go through %s", module)
    explain = getattr(runtime.policy, "refusal", None) if runtime.has("policy") else None
    return McpHooks(wrap=policy.wrap_tool, visible=visible, bind_token=policy.bind_request_token,
                    accept_token=accept, explain_token=explain if callable(explain) else None)


def node_panel(runtime: Runtime) -> Any | None:
    """The panel API and the built panel (openajar.api.build_api), or None without that package."""
    try:
        from openajar.api import ApiSettings, build_api
    except ModuleNotFoundError as exc:
        if exc.name and "openajar.api".startswith(exc.name):
            log.info("openajar.api is not installed; no panel")
            return None
        raise
    return build_api(runtime, ApiSettings())


def build_node_app(
    config: NodeConfig,
    runtime: Runtime,
    *,
    providers: Sequence[Provider] | None = None,
    api_app: Any = None,
    viewer_app: Any = None,
    panel: bool = False,
    instructions: str | None = None,
) -> Any:
    """The node's HTTP app: MCP with every provider's tools, plus the viewer when given. The
    panel: `api_app` at /api when given; with `panel=True` it (built here when not given) also
    serves the page shell at / (every path MCP, /api and /view do not claim). `instructions` is
    what the MCP server tells an agent when it connects (the node's own by default)."""
    from openajar.mcp.http import build_app
    from openajar.mcp.registry import allow_all_wrap, build_mcp
    from openajar.node.tools.recipes import INSTRUCTIONS

    if not runtime.has("config"):
        runtime.register("config", config)
    default_viewer, control_api = _control_apps(config, runtime)
    viewer_app = viewer_app or default_viewer
    hooks = mcp_hooks(runtime)
    mcp = build_mcp(runtime, tool_providers() if providers is None else providers, hooks.wrap or allow_all_wrap,
                    visible=hooks.visible, instructions=instructions or INSTRUCTIONS, toolsets=config.mcp_toolsets)
    runtime.register("mcp", mcp)  # the controller channel runs invokes through this same server
    runtime.register("mcp_hooks", hooks)
    _watch_tools(runtime, mcp)
    if panel and api_app is None:
        api_app = node_panel(runtime)  # carries the lane control routes behind its own auth
    if api_app is None:
        api_app = control_api  # no panel: the owner's lane control API behind the node token
    return build_app(runtime, mcp, api_app=api_app, viewer_app=viewer_app,
                     root_app=api_app if panel else None, bind_token=hooks.bind_token,
                     accept_token=hooks.accept_token, explain_token=hooks.explain_token)


def _watch_tools(runtime: Runtime, mcp: Any) -> None:
    """Tools built from stored flows and connectors follow the store while agentosd runs
    (openajar.mcp.reload): polled, poked by the ops socket, and at once on an in-process save."""
    from openajar.mcp.reload import SERVICE_NAME, ToolWatch

    watch = ToolWatch(runtime, mcp)
    runtime.register(SERVICE_NAME, watch)
    flows = runtime.get("flows")
    on_change = getattr(flows, "on_change", None)
    if callable(on_change):
        on_change(lambda *_: watch.poke())


def _control_apps(config: NodeConfig, runtime: Runtime) -> tuple[Any, Any]:
    """The signed-link viewer (/view, /viewer-ws, /viewer-assets: each link carries its own auth)
    and, until the panel API is mounted, the owner's lane control API at /api behind the node
    token. Both need a lane manager with take over and hand back."""
    lanes = runtime.get("lanes")
    if lanes is None or not callable(getattr(lanes, "take_control", None)):
        return None, None
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from openajar.mcp.http import BearerAuth
    from openajar.node.viewer_app import build_viewer_app
    from openajar.node.viewer_routes import control_api_routes
    from openajar.node.viewer_tokens import ViewTokens

    tokens = runtime.get("viewer_links")
    if tokens is None:
        tokens = ViewTokens.for_config(config)
        runtime.register("viewer_links", tokens)
    from openajar.core import live as live_sockets
    from openajar.node.login_assist import service_for

    viewer = build_viewer_app(lanes, config, traces=runtime.get("traces"), tokens=tokens,
                              login=lambda: service_for(runtime), live=live_sockets.of(runtime),
                              freeze=lambda: runtime.get("freeze"), environments=lambda: runtime.get("environments"))
    api = BearerAuth(Starlette(routes=[Mount("/api", routes=control_api_routes(lanes, tokens,
                                                                              traces=runtime.get("traces")))]),
                     config.mcp_token)
    return viewer, api


_ALSO_SCRUBBED: list[str] = []  # exact values every later install keeps (`openajar up`: the controller's tokens)


def install_log_scrubbers(config: NodeConfig, runtime: Runtime | None = None, *, also: Sequence[str] = ()) -> None:
    """What every log line goes through before it is written (openajar.core.logs): the node
    token and the request's bearer token as exact values, the vault's values (once a vault is on
    the runtime), proxy credentials, and the policy's secret shapes (grant tokens, bearer and
    cookie headers, JWTs, long random strings). Replaces what was registered before, except the
    exact values given in `also`, which every later call keeps (serve() calls this again)."""
    from openajar.core import logs
    from openajar.core.tools import current_request_token
    from openajar.policy.redactor import Redactor

    logs.clear_scrubbers()
    node_token = config.mcp_token or ""
    _ALSO_SCRUBBED.extend(v for v in also if v and v not in _ALSO_SCRUBBED)
    pinned = tuple(_ALSO_SCRUBBED)

    def exact(text: str) -> str:
        for value in (node_token, current_request_token() or "", *pinned):
            if len(value) >= 6:
                text = text.replace(value, "[redacted]")
        return text

    logs.add_scrubber(exact)
    vault = runtime.get("vault") if runtime is not None else None
    if vault is not None and callable(getattr(vault, "scrub", None)):
        logs.add_scrubber(vault.scrub)
    logs.add_scrubber(Redactor(config, cache_seconds=30).text)
    try:
        from openajar.policy.redact import redact_text
    except ModuleNotFoundError:
        return
    logs.add_scrubber(lambda text: redact_text(text, max_len=0))


class _Uvicorn:
    """Imported lazily so `openajar version` and friends start fast."""

    @staticmethod
    def server(app, config: NodeConfig):
        import uvicorn

        class Server(uvicorn.Server):
            # uvicorn re-raises SIGTERM after a graceful exit, which would kill the process
            # before the lanes close. serve() owns the signals instead.
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        return Server(uvicorn.Config(
            app,
            host=config.mcp_host,
            port=config.mcp_port,
            log_level="info",
            lifespan="on",
            timeout_graceful_shutdown=SHUTDOWN_GRACE,
            timeout_keep_alive=KEEP_ALIVE_S,
            access_log=False,
            log_config=None,  # uvicorn's lines go to the root logger: ids, JSON and scrubbing
        ))


def _exit_hard(reason: str) -> None:
    """End the process at once, with no more cleanup: the lanes and stores are left as they are.
    Only for a shutdown that cannot finish (a third signal, or the deadline), so it logs why."""
    log.error("exiting now without a clean shutdown: %s", reason)
    logging.shutdown()  # flush the log before the process goes
    os._exit(HARD_EXIT_CODE)


async def serve(
    config: NodeConfig,
    runtime: Runtime | None = None,
    *,
    stop: asyncio.Event | None = None,
    on_started: Callable[[int], None] | None = None,
    providers: Sequence[Provider] | None = None,
    api_app: Any = None,
    viewer_app: Any = None,
    panel: bool = True,
    instructions: str | None = None,
) -> None:
    """Start the lane manager, serve MCP (and the panel) until SIGTERM/SIGINT (or `stop`), then
    close every lane.

    The signal handlers stay installed until the lanes have closed, so a signal during the
    shutdown never reaches the default handler, which would kill the process. A second signal
    during the shutdown is logged once and skips the graceful wait for open MCP streams; the
    lanes still close. A third signal exits at once, and so does a shutdown still running after
    SHUTDOWN_DEADLINE seconds. Both log why.
    `on_started` receives the bound port (useful with mcp_port=0 in tests).
    """
    if not config.mcp_token:
        raise RuntimeError(f"no mcp_token in {config.home / 'node.toml'}; run `openajar node init` first")
    check_mcp_bind(config)

    runtime = runtime or build_runtime(config)
    install_log_scrubbers(config, runtime)
    app = build_node_app(config, runtime, providers=providers, api_app=api_app, viewer_app=viewer_app,
                         panel=panel, instructions=instructions)
    lanes: LaneManager = runtime.lanes
    server = _Uvicorn.server(app, config)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    deadline = threading.Timer(SHUTDOWN_DEADLINE, _exit_hard,
                               args=(f"the shutdown did not finish within {SHUTDOWN_DEADLINE:g}s",))
    deadline.daemon = True  # a timer on its own thread: it still fires if the loop is blocked
    signals_during_shutdown = 0

    def request_stop(signame: str) -> None:
        nonlocal signals_during_shutdown
        if not stop.is_set():
            log.info("%s: shutting down", signame)
            stop.set()
            return
        signals_during_shutdown += 1
        if signals_during_shutdown == 1:
            log.warning("%s: shutdown is already in progress; not waiting for open MCP streams", signame)
            server.force_exit = True
        else:
            _exit_hard(f"{signame} again during shutdown")

    async def watch_stop() -> None:
        await stop.wait()
        server.should_exit = True
        deadline.start()

    async def announce() -> None:
        while not server.started:
            await asyncio.sleep(0.05)
        port = bound_port(server) or config.mcp_port
        log.info("MCP on http://%s:%s/mcp%s", config.mcp_host, port,
                 f", the panel on http://{config.mcp_host}:{port}/" if panel else "")
        if on_started is not None:
            on_started(port)

    handled: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, request_stop, sig.name)
            handled.append(sig)

    tasks: list[asyncio.Task] = []
    try:
        log.info("starting lanes (home %s)", config.home)
        await lanes.start()
        await start_services(runtime)
        tool_watch = runtime.get("tool_reload")
        if tool_watch is not None:
            await tool_watch.start()
        tasks = [asyncio.create_task(watch_stop()), asyncio.create_task(announce())]
        if not stop.is_set():
            await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        try:
            tool_watch = runtime.get("tool_reload")
            if tool_watch is not None:
                await tool_watch.stop()
            await stop_services(runtime)
            log.info("closing lanes")
            try:
                await asyncio.wait_for(lanes.shutdown(), LANES_SHUTDOWN_TIMEOUT)
            except TimeoutError:
                log.error("lanes did not close within %ss", LANES_SHUTDOWN_TIMEOUT)
            log.info("stopped")
        finally:
            # Last, so a signal during the shutdown above is handled and never kills the process.
            deadline.cancel()
            for sig in handled:
                loop.remove_signal_handler(sig)


def bound_port(server) -> int | None:
    for srv in getattr(server, "servers", []) or []:
        for sock in srv.sockets:
            return sock.getsockname()[1]
    return None
