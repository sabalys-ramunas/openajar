"""What the root command and the feature CLIs share: clean exits, the node's config, lane ids,
proxies, a call to the running agentosd, and the runtime a feature CLI opens.

It imports no command group, so `openajar.cli_groups`, `openajar.controller.cli` and `openajar.ops.cli`
read these without importing the root command (`openajar.cli`), which imports them."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
import sqlite3
import tomllib
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from openajar.core import catalog
from openajar.core.config import (
    NodeConfig,
    config_path,
    config_problems,
    home_from_env,
    load_config,
    load_proxies,
    save_config,
)
from openajar.core.errors import OpenajarError
from openajar.core.models import ProxyConfig, parse_lane_id

PROXY_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


def fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def fail_coded(code: str, message: str, fix: str | None = None) -> typer.Exit:
    """`error: <code>: <message>` and the error catalog's fix (or the one the node sent)."""
    text = catalog.cli_error(code, message)
    if fix and "\n" not in text:
        text = f"{text}\n  fix: {fix}"
    typer.secho(text, err=True, fg=typer.colors.RED)
    return typer.Exit(1)


def ensure_dir(path: Path, mode: int = 0o700, *, tighten: bool = True) -> None:
    """Create `path` with `mode`. An existing directory is tightened to `mode` only when
    `tighten` is set: openajar owns its secrets and lanes dirs, but not a home it was pointed at."""
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    if tighten or not existed:
        path.chmod(mode)


def read_config(home: Path) -> NodeConfig:
    """load_config with a clean error instead of a traceback (never echoing values)."""
    path = config_path(home)
    try:
        return load_config(home)
    except tomllib.TOMLDecodeError as exc:
        raise fail(f"{path} is not valid TOML ({exc}); fix or remove it") from None
    except ValidationError as exc:
        raise fail(f"{path} has invalid settings: {config_problems(exc)}") from None
    except OSError as exc:
        raise fail(f"cannot read {path}: {exc.strerror or exc}") from None


def initialized_config() -> NodeConfig:
    home = home_from_env()
    if not config_path(home).exists():
        raise fail(f"no node.toml in {home}; run `openajar node init` first (or set OPENAJAR_HOME)")
    return read_config(home)


def valid_lane_id(value: str) -> str:
    try:
        parse_lane_id(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    return value


def missing_module(exc: ModuleNotFoundError) -> typer.Exit:
    if (exc.name or "").startswith("openajar."):
        return fail(f"{exc.name} is not part of this build yet, so lanes cannot run here")
    return fail(f"missing Python module {exc.name}; run deploy/install-node.sh (or `uv sync`)")


def run_manager(coro_factory):
    """Run one LaneManager call from the CLI and turn openajar errors into a clean exit."""
    from openajar.node import daemon

    cfg = initialized_config()
    try:
        manager = daemon.build_lane_manager(cfg)
        return asyncio.run(coro_factory(manager))
    except ModuleNotFoundError as exc:
        raise missing_module(exc) from None
    except OpenajarError as exc:
        raise fail_coded(exc.code, exc.message) from None
    except sqlite3.DatabaseError as exc:
        raise fail(f"the lane database in {cfg.home} cannot be read ({exc}); restore it from a backup") from None


GRANT_FOR_AGENTS = "openajar access grants issue --subject claude-code --workspace '*' --ceiling commit --ttl 90d"


def connect_lines(cfg: NodeConfig, ssh_host: str) -> list[str]:
    """The by-hand way: a tunnel, and Claude Code with a grant of its own (never the node token,
    which is the owner's: a token never decides what its own grant family asked for)."""
    port = cfg.mcp_port
    tunnel = f"ssh -N -L {port}:localhost:{port} {ssh_host}"
    url = f"http://localhost:{port}/mcp"
    add = (f'claude mcp add --scope user --transport http openajar {url} '
           '--header "Authorization: Bearer <grant token>"')
    return [tunnel, add]


def init_node(home: Path, overrides: dict[str, Any] | None = None, *, new_token: bool = False) -> tuple[NodeConfig, Path]:
    """What `openajar node init` writes, without what it prints (`openajar up` runs it on a new box):
    OPENAJAR_HOME, its secrets and lanes folders, and node.toml with an MCP token. An existing
    node.toml keeps its token unless `new_token`."""
    cfg = read_config(home) if config_path(home).exists() else NodeConfig(home=home)
    changed = {key: value for key, value in (overrides or {}).items() if value is not None}
    if new_token or not cfg.mcp_token:
        changed["mcp_token"] = secrets.token_urlsafe(32)
    try:
        cfg = NodeConfig(**{**cfg.model_dump(), **changed})
    except ValidationError as exc:
        raise fail(f"invalid options: {config_problems(exc)}", code=2) from None

    ensure_dir(home, tighten=False)
    ensure_dir(cfg.secrets_dir)
    ensure_dir(home / "lanes")
    stamp_fresh_node_db(home)
    return cfg, save_config(cfg)


def stamp_fresh_node_db(home: Path) -> None:
    """A new box starts at the latest node schema: openajar.db is made by its migrations, so
    `openajar migrate status` has nothing pending. An existing one is left to `openajar migrate run`."""
    import dataclasses

    from openajar.core import migrate as migrations

    if (home / migrations.NODE.db).exists():
        return
    migrations.migrate(home, [dataclasses.replace(migrations.NODE, create=True)], backup=False)


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def parse_proxy(label: str, spec: str) -> ProxyConfig:
    """`host:port[:user[:pass]]`. The password may itself contain colons."""
    if spec.startswith("["):
        raise typer.BadParameter("IPv6 proxy hosts are not supported yet; use the proxy's IPv4 address or hostname")
    parts = spec.split(":", 3)
    if len(parts) < 2 or not parts[0]:
        raise typer.BadParameter("expected host:port:user:pass (user and pass are optional)")
    host, port_text = parts[0], parts[1]
    if _is_ipv6(host) or "[" in host or "]" in host:
        raise typer.BadParameter("IPv6 proxy hosts are not supported yet; use the proxy's IPv4 address or hostname")
    if len(host) > 253 or not PROXY_HOST_RE.fullmatch(host):
        raise typer.BadParameter("host must be an IPv4 address or a hostname")
    try:
        port = int(port_text)
    except ValueError:
        raise typer.BadParameter(f"port must be a number, got {port_text!r}") from None
    if not 0 < port < 65536:
        raise typer.BadParameter(f"port out of range: {port}")
    username = parts[2] if len(parts) > 2 and parts[2] else None
    password = parts[3] if len(parts) > 3 and parts[3] else None
    if password is not None and username is None:
        raise typer.BadParameter("a proxy password needs a username (host:port:user:pass)")
    if username and password is None:
        password = typer.prompt(f"Password for proxy {label}", hide_input=True)
    return ProxyConfig(label=label, host=host, port=port, username=username, password=password)


def check_proxy(cfg: NodeConfig, label: str | None) -> None:
    if label and label not in load_proxies(cfg):
        raise fail(f"no proxy {label!r}; add it with `openajar proxy add {label} host:port:user:pass`")


LaneIdArg = Annotated[str, typer.Argument(help="workspace/name, for example personal/reddit.",
                                          callback=valid_lane_id)]


ProxyOpt = Annotated[str | None, typer.Option(help="Proxy label from `openajar proxy add`.")]


DeviceOpt = Annotated[str | None, typer.Option(help="Device from `openajar device create`: the lane launches with "
                                                    "its fingerprint and proxy, and keeps its own profile.")]


def _first_validation_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return str(errors(include_input=False, include_url=False)[0].get("msg", exc)).removeprefix("Value error, ")
        except (IndexError, TypeError):
            pass
    return str(exc)


DAEMON_TIMEOUT = 15.0


def daemon_url(cfg: NodeConfig, path: str) -> str:
    host = cfg.mcp_host if cfg.mcp_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{cfg.mcp_port}{path}"


def lane_api_path(lane_id: str, action: str) -> str:
    workspace, name = parse_lane_id(lane_id)
    return f"/api/lanes/{workspace}/{name}/{action}"


def _send(method: str, url: str, *, body: dict | None, headers: dict, timeout: float = DAEMON_TIMEOUT):
    """One HTTP request to agentosd (tests route it to the app in-process)."""
    import httpx

    return httpx.request(method, url, json=body, timeout=timeout, headers=headers)


def operator_presence(cfg: NodeConfig) -> dict[str, str]:
    """The header that makes one request to agentosd a person's (docs/THREAT-MODEL.md): a one-time code
    this CLI writes into panel.db, which only the node's operator can write, as `openajar panel login`
    does. The node token alone is not a person, because an agent may hold it. {} when panel.db is not
    writable here (then the daemon refuses what needs a person, and says so)."""
    from openajar.api.auth import PRESENCE_HEADER, PanelStore, new_login_code

    code = new_login_code()
    try:
        PanelStore.for_home(cfg.home).add_code(code, ttl_seconds=60)
    except (OSError, sqlite3.Error):
        return {}
    return {PRESENCE_HEADER: code}


def daemon_request(cfg: NodeConfig, method: str, path: str, body: dict | None = None, *,
                   missing: str = "this agentosd does not know this route yet; update it and restart the service",
                   absent_ok: bool = False, timeout: float | None = None, person: bool = False):
    """Call the running agentosd's API with the node token; every failure becomes a clear exit.
    `absent_ok`: None when agentosd is not running or does not know the route (the caller falls
    back to the store). `person`: a route that needs a person (Presence.PERSON) gets a one-time
    code from panel.db with it (`operator_presence`)."""
    import httpx

    url = daemon_url(cfg, path)
    headers = {"Authorization": f"Bearer {cfg.mcp_token}", **(operator_presence(cfg) if person else {})}
    try:
        response = _send(method, url, body=body, headers=headers,
                         **({"timeout": timeout} if timeout else {}))
    except httpx.ConnectError:
        if absent_ok:
            return None
        raise fail(f"agentosd is not running on {daemon_url(cfg, '')}. Start it with `openajar node serve` "
                   "(or `systemctl start agentosd`), then try again.") from None
    except httpx.TimeoutException:
        raise fail(f"agentosd did not answer within {timeout or DAEMON_TIMEOUT:g}s") from None
    except httpx.HTTPError as exc:
        raise fail(f"could not reach agentosd: {type(exc).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code == 401:
        raise fail("agentosd refused the node token. Check that OPENAJAR_HOME points at this node's home.")
    error = data.get("error") if isinstance(data, dict) else None
    if absent_ok and response.status_code in (404, 405) and (
            not isinstance(error, dict) or error.get("code") in (None, "not_found")):
        return None  # an agentosd from before this route
    if isinstance(error, dict):
        fix = error.get("fix") if isinstance(error.get("fix"), str) else None
        raise fail_coded(str(error.get("code") or "error"), str(error.get("message") or ""), fix)
    if response.status_code == 404:
        raise fail(missing)
    if response.status_code >= 400 or not isinstance(data, dict | list):
        raise fail(f"agentosd answered {response.status_code}")
    return data


class _CliVault:
    """The vault for one CLI process, opened the way the daemon opens it: a key-file vault
    unseals itself on first use; a passphrase vault asks for its passphrase (on the terminal,
    so a credential piped to stdin is not read as the passphrase) the first time a secret is
    stored or read."""

    def __init__(self, vault):
        self._vault = vault

    def __getattr__(self, name: str):
        return getattr(self._vault, name)

    async def _unsealed(self) -> None:
        header = self._vault.store.header()
        if header is not None and header.mode == "passphrase" and self._vault.state() == "sealed":
            import getpass

            await self._vault.unseal(getpass.getpass("Vault passphrase: "))

    async def put_secret(self, ref, value, **kwargs):
        await self._unsealed()
        return await self._vault.put_secret(ref, value, **kwargs)

    async def get_secret(self, ref, **kwargs):
        await self._unsealed()
        return await self._vault.get_secret(ref, **kwargs)


def cli_runtime():
    """The services a feature CLI shares with the daemon: the node config, and the vault when
    this node has one (`openajar vault init`). Without a vault, crown-jewel credentials are
    refused unless the command is told to use the plain development file."""
    from openajar.core.runtime import Runtime

    config = load_config()
    runtime = Runtime(config=config)
    try:
        from openajar.vault.service import VaultService
    except ModuleNotFoundError:
        return runtime
    vault = VaultService(config.home)
    if vault.store.header() is not None:
        runtime.register("vault", _CliVault(vault))
    return runtime


def plugins_cli_runtime():
    """cli_runtime plus the brains, so `openajar plugins` can read what each brain can do. Brains
    that cannot be opened read as not available, and every job then reads as unmet."""
    runtime = cli_runtime()
    try:
        from openajar.brains.service import build

        runtime.register("brains", build(runtime))
    except ModuleNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001  (a broken brain store must not hide the plugin list)
        logging.getLogger(__name__).warning("the brains could not be opened: %s", type(exc).__name__)
    return runtime
