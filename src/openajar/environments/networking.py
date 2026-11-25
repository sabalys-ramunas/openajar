"""The node side of an environment's network and app secrets. A mixin of EnvironmentManager.

At open (`network_for_open`, `check_network_plan`): the level, narrowed from the node's default only,
and refused when the provider cannot close a machine's network (openajar.vms.netwall) or when `none`
could not even clone the repository. The level goes into the plan, so the machine is rented closed.

While it runs (`net_forwards`, called as the tunnels start): per environment, on this computer's
loopback, the allowlist proxy (openajar.environments.envproxy) and, with bound secrets, the swap
(openajar.environments.swap). The tunnel forwards them into the machine as 127.0.0.1:3130 and :3131
(`-R`, like the git gateway). For the browser on the node, a split SOCKS proxy. For a browser on the
machine with an owner's proxy, its own forwarder to that proxy, as 127.0.0.1:3132 (browserproxy).

After the tunnels (`net_setup`, one step of the bring-up): the machine's side, set by the node over
SSH (openajar.environments.network.vm_script): the proxy variables, apt's and Docker's proxy, the
placeholders, the environment's CA and the relay for bound hosts.

At close (`net_down`): the servers stop and the CA's key, the placeholders and the rest of the
environment's folder on the node are deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.environments import network as net
from openajar.environments.models import WORKSPACE, Environment

log = logging.getLogger("openajar.environments")

NET_DIR = "environments-net"     # under OPENAJAR_HOME: a folder per environment, the node's user only
VALUE_TTL_S = 60.0               # a value read from the vault is used this long before it is read again
BLOCKED_SAVE_S = 5.0             # the refused hosts reach the record at most this often


class _VaultTrace:
    """The context the vault traces a use into: a log line with the entry and host, never a value."""

    def __init__(self, env_id: str):
        self.env_id = env_id

    def trace(self, kind: str, **fields: Any) -> None:
        log.info("environment %s: %s %s for %s", self.env_id, kind, fields.get("entry"), fields.get("host"))


@dataclass
class NetNode:
    """What runs on the node for one environment's network."""
    level: str
    folder: Path
    blocked: Any = None                       # envproxy.Blocked
    forwarder: Any = None                     # envproxy.EnvForwarder
    servers: list[asyncio.Server] = field(default_factory=list)
    proxy_port: int | None = None
    swap: Any = None                          # swap.SwapProxy
    swap_port: int | None = None
    socks: Any = None                         # envproxy.SplitSocks
    browser_forwarder: Any = None             # the browser's own forwarder to the owner's proxy (browserproxy)
    browser_proxy_port: int | None = None
    browser_upstream: Any = None              # that proxy (a ProxyConfig: it stays on the node)
    browser_direct: bool = False              # no proxy: it exits through this computer (thiscomputer)
    browser_exit: dict | None = None          # where the last launch's check found the exit (ip, country, ...)
    values: dict[str, tuple[float, Any]] = field(default_factory=dict)
    save_at: float = 0.0
    save_pending: bool = False

    def forwards(self) -> tuple[tuple[int, int], ...]:
        out = []
        if self.proxy_port:
            out.append((net.PROXY_PORT_ON_VM, self.proxy_port))
        if self.swap_port:
            out.append((net.SWAP_PORT_ON_VM, self.swap_port))
        if self.browser_proxy_port:
            out.append((net.BROWSER_PROXY_PORT_ON_VM, self.browser_proxy_port))
        return tuple(out)


class Network:
    """The network of each environment (EnvironmentManager's self.store, self.settings, self._get)."""

    _nets: dict[str, NetNode]
    _tunnels: dict[str, Any]

    # ================================================================ open
    def network_for_open(self, asked: str | None, repo: str | None) -> str:
        """The level an environment opens with, after the checks that rent nothing."""
        level = net.narrowed(self.settings.network, asked)
        if level == "full":
            return level
        from openajar.vms import netwall

        vms = self.vms()
        why = netwall.cannot_close(vms.provider(), firewall_on=vms.settings.firewall)
        if why:
            raise net.NetworkNotAllowed(f"network {level}: {why}")
        if level == "none" and repo and not self._declared(repo):
            raise net.NetworkNotAllowed(
                f"network none reaches only the git gateway, and {repo} is not connected to it, so it could not be "
                f"cloned. Open it with network=\"limited\", or tell the user: `openajar environments repo add {repo}` "
                "connects it")
        return level

    def check_network_plan(self, level: str, plan: dict) -> dict:
        """The plan rents the machine closed. A cloud machine closed from its first boot cannot install its
        tools, so it needs the prepared image."""
        from openajar.environments.machine import BASE_IMAGE

        if level != "full" and plan.get("image") == BASE_IMAGE and not getattr(self.vms().provider(),
                                                                                "closes_network", False):
            raise net.NetworkNotAllowed(
                f"network {level} needs the prepared image (a machine closed from its first boot cannot install "
                "its tools). Open with network full, or tell the user: `openajar vm image build dev` makes it")
        plan["network"] = level
        return plan

    def _declared(self, repo: str) -> bool:
        git = self._get("git")
        if git is None:
            return False
        from openajar.git.wire import parse_remote

        with contextlib.suppress(Exception):
            return git.remotes.find(parse_remote(repo), [WORKSPACE]) is not None
        return False

    # ================================================================ the node's side
    def _net_folder(self, env_id: str) -> Path:
        return Path(self.home) / NET_DIR / env_id

    def _state(self, env: Environment) -> dict:
        """The environment's placeholders and CA, made once and kept in its folder (0700), so a restart of
        the node keeps what the machine already holds."""
        folder = self._net_folder(env.id)
        path = folder / "state.json"
        if path.exists():
            with contextlib.suppress(ValueError, OSError):
                return dict(json.loads(path.read_text()))
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        return {"placeholders": {}}

    def _save_state(self, env: Environment, state: dict) -> None:
        from openajar.core.config import write_private

        write_private(self._net_folder(env.id) / "state.json", json.dumps(state))

    def _ca(self, env: Environment) -> Any:
        from openajar.environments.swap import EnvCA

        folder = self._net_folder(env.id)
        cert, key = folder / "ca.pem", folder / "ca.key"
        if cert.exists() and key.exists():
            return EnvCA(cert.read_bytes(), key.read_bytes(), workdir=folder)
        from openajar.core.config import write_private

        ca = EnvCA.make(env.id, workdir=folder)
        write_private(key, ca.key_pem().decode())
        cert.write_bytes(ca.cert_pem)
        return ca

    async def _bindings(self, env: Environment) -> list[Any]:
        """The secrets bound for this environment, each with its placeholder (made once)."""
        from openajar.environments import appsecrets
        from openajar.environments.swap import Binding

        try:
            bound = await appsecrets.bound_for(self._get("vault"), env.repo)
        except Exception as e:  # noqa: BLE001 - the environment works without them; the log says why
            log.warning("environment %s: could not read the secrets bound to environments: %s", env.id,
                        getattr(e, "message", e))
            return []
        if env.state == "ready":  # the node restarted: the machine has the placeholders it was given then
            bound = [b for b in bound if b.var in env.secrets]
        if not bound:
            return []
        state = self._state(env)
        ph = state.setdefault("placeholders", {})
        for b in bound:
            ph.setdefault(b.var, "agentos-secret-" + secrets.token_hex(16))
        self._save_state(env, state)
        return [Binding(name=b.var, hosts=b.hosts, placeholder=ph[b.var], ref=b.ref) for b in bound]

    async def net_forwards(self, env: Environment) -> tuple[tuple[int, int], ...]:
        """Start (once) what runs on the node for the environment's network, and the `-R` forwards its
        tunnel carries: (port on the machine, port on this computer)."""
        node = self._nets.get(env.id)
        if node is None:
            node = await self._net_up(env)
        return node.forwards()

    async def _net_up(self, env: Environment) -> NetNode:
        from openajar.environments.envproxy import Blocked, EnvForwarder, EnvPolicy

        bindings = await self._bindings(env)
        node = NetNode(level=env.network, folder=self._net_folder(env.id))
        node.blocked = Blocked(env.blocked, clock=self._clock, on_change=lambda rows, i=env.id: self._blocked_changed(i))
        hosts = sorted({h for b in bindings for h in b.hosts})
        if bindings:
            from openajar.environments.swap import SwapProxy

            node.swap = SwapProxy(env.id, self._ca(env), bindings, self._resolver(env, node),
                                  on_event=lambda kind, host, detail, n=node: self._swap_event(n, kind, host, detail))
            server = await asyncio.start_server(node.swap.handle_relay, "127.0.0.1", 0)
            node.servers.append(server)
            node.swap_port = int(server.sockets[0].getsockname()[1])
        if env.network != "full":
            from openajar.git.wire import parse_remote

            repo_host = None
            with contextlib.suppress(Exception):
                repo_host = parse_remote(env.repo).host if env.repo else None
            allow = net.allowlist(env.network, allow_hosts=self.settings.allow_hosts, repo_host=repo_host,
                                  secret_hosts=hosts)
            swap = node.swap

            def intercept(host: str, port: int) -> Any:
                return swap.serve_tls if swap is not None and port == 443 and host in swap.hosts else None

            node.forwarder = EnvForwarder(env.id, EnvPolicy(allow), node.blocked, intercept=intercept)
            server = await asyncio.start_server(node.forwarder._client, "127.0.0.1", 0)
            node.servers.append(server)
            node.proxy_port = int(server.sockets[0].getsockname()[1])
            if env.browser == "node":  # the browser's traffic: the machine's own loopback, else this list
                from openajar.environments.envproxy import SplitSocks
                from openajar.environments.tunnels import free_port, peer_is_us

                def machine_socks(i: str = env.id) -> str | None:
                    tunnel = self._tunnels.get(i)
                    return tunnel.socks_path if tunnel is not None else None

                node.socks = SplitSocks(free_port(), machine_socks=machine_socks, policy=node.forwarder.policy,
                                        blocked=node.blocked, allow=peer_is_us, forwarder=node.forwarder)
                await node.socks.start()
        await self.browser_proxy_up(env, node)  # type: ignore[attr-defined]  (BrowserProxies, on the same manager)

        def noted(e: Environment) -> None:
            e.secrets = [b.name for b in bindings]

        self.store.update(env.id, noted)
        self._nets[env.id] = node
        return node

    def _resolver(self, env: Environment, node: NetNode) -> Any:
        async def resolve(binding: Any, host: str) -> Any:
            now = time.monotonic()
            got = node.values.get(binding.name)
            if got is not None and now - got[0] < VALUE_TTL_S:
                return got[1]
            vault = self._get("vault")
            if vault is None:
                raise ConnectionError("this node has no vault")
            value = await vault.resolve_app_secret(_VaultTrace(env.id), ref=binding.ref, lane_id=env.terminal_lane,
                                                   url=f"https://{host}/")
            node.values[binding.name] = (now, value)
            return value

        return resolve

    def _swap_event(self, node: NetNode, kind: str, host: str, detail: str) -> None:
        if kind == "placeholder_elsewhere":
            node.blocked.note(host, f"the placeholder of {detail} was sent here: never swapped")
        elif kind == "tls_refused":
            log.info("swap: a TLS connection for %s was refused (%s)", host, detail)

    def _blocked_changed(self, env_id: str) -> None:
        """Keep the refused hosts with the record, at most every BLOCKED_SAVE_S (a flood is one write)."""
        node = self._nets.get(env_id)
        if node is None:
            return
        wait = node.save_at + BLOCKED_SAVE_S - time.monotonic()
        if wait <= 0:
            self._save_blocked(env_id)
        elif not node.save_pending:
            node.save_pending = True
            asyncio.get_running_loop().call_later(wait, self._save_blocked, env_id)

    def _save_blocked(self, env_id: str) -> None:
        node = self._nets.get(env_id)
        if node is None:
            return
        node.save_pending, node.save_at = False, time.monotonic()
        rows = node.blocked.list()

        def kept(e: Environment) -> None:
            e.blocked = rows

        with contextlib.suppress(Exception):
            self.store.update(env_id, kept)

    def browser_socks_port(self, env_id: str, tunnel: Any) -> int:
        """The port the environment's browser on the node uses: the tunnel's SOCKS, or in a closed network
        the split one (envproxy.SplitSocks)."""
        node = self._nets.get(env_id)
        if node is not None and node.socks is not None:
            return int(node.socks.port)
        return int(tunnel.socks_port)

    async def net_setup(self, env: Environment) -> None:
        """The machine's side, over SSH (one step of the bring-up, after the tunnels)."""
        node = self._nets.get(env.id)
        if node is None or (env.network == "full" and node.swap is None and not env.restore_from):
            return  # nothing to set; a resumed disk may hold an earlier one's settings, which this clears
        swap = node.swap
        variables = {b.name: b.placeholder for b in swap.bindings} if swap is not None else {}
        script = net.vm_script(env.network, variables=variables,
                               ca_pem=swap.ca.cert_pem.decode() if swap is not None else None,
                               secret_hosts=sorted(swap.hosts) if swap is not None else [])
        from openajar.core.errors import OpenajarError
        from openajar.environments.errors import EnvironmentFailed

        try:
            res = await self.envs().exec(env.terminal_lane, "sh -s", stdin=script, timeout_s=120)
        except OpenajarError as e:
            raise EnvironmentFailed(f"setting up the machine's network failed: {e.message}") from None
        if res.exit_code != 0:
            raise EnvironmentFailed("setting up the machine's network failed: "
                                    f"{(res.stderr or res.stdout or '').strip()[-300:]}")

    async def net_down(self, env_id: str, *, forget: bool = True) -> None:
        """Stop the environment's servers; with `forget` (its close) delete its folder: the CA's key too."""
        node = self._nets.get(env_id)
        if node is not None:
            self._save_blocked(env_id)
            self._nets.pop(env_id, None)
            for server in node.servers:
                server.close()
            if node.socks is not None:
                await node.socks.stop()
            if node.swap is not None:
                await node.swap.close()
            if node.forwarder is not None:
                await node.forwarder.close()
            if node.browser_forwarder is not None:
                await node.browser_forwarder.close()
        if forget:
            shutil.rmtree(self._net_folder(env_id), ignore_errors=True)

    async def net_shutdown(self) -> None:
        for env_id in list(self._nets):
            await self.net_down(env_id, forget=False)

    # ================================================================ read
    def blocked_since(self, env: Environment, since: Any) -> list[str]:
        """The hosts the environment's proxy refused at `since` or later (a datetime), newest first: what
        a command's result names as network.blocked_now."""
        node = self._nets.get(env.id)
        if env.network == "full" or node is None or node.blocked is None or since is None:
            return []
        out = []
        for row in node.blocked.list():
            with contextlib.suppress(TypeError, ValueError):
                if datetime.fromisoformat(str(row.get("last"))) >= since:
                    out.append(row["host"])
        return out

    def network_view(self, env: Environment) -> dict:
        """`network` of environment() and the web API: the level, the allowed list in short, what was
        refused, the names of the bound secrets (never a value), and the browser's proxy (its label and exit)."""
        node = self._nets.get(env.id)
        blocked = node.blocked.list() if node is not None and node.blocked is not None else list(env.blocked)
        allow = list(node.forwarder.policy.allowlist) if node is not None and node.forwarder is not None else []
        swap_hosts = sorted(node.swap.hosts) if node is not None and node.swap is not None else []
        out = {"policy": env.network, "allowed": net.short_list(env.network, allow, swap_hosts),
               "blocked": blocked[:20], "secrets": list(env.secrets)}
        proxy = self.browser_proxy_view(env)  # type: ignore[attr-defined]  (BrowserProxies, on the same manager)
        if proxy is not None:
            out["browser_proxy"] = proxy
        if env.network != "full" and blocked:
            out["fix"] = f"the owner allows a host with `openajar environments allow --host {blocked[0]['host']}`"
        return out
