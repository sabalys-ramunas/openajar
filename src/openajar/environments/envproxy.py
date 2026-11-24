"""The node side of an environment's closed network: its allowlist proxy, the record of what it
refused, and the browser's split SOCKS proxy.

- `EnvForwarder`: the isolated lanes' forwarder (openajar.env.netproxy) with this environment's list
  (openajar.environments.network.allowlist). A refusal says plainly what to do, as an HTTP 403 for a
  plain request and for a CONNECT alike. A CONNECT to a host a secret is bound to is not tunnelled
  blind: the node's swap (openajar.environments.swap) ends its TLS and puts the secret in.
- `Blocked`: the hosts it refused, each with a count and when last, kept for environment(), the web
  app and the pull request. Never asked about: a refusal is the answer.
- `SplitSocks`: the browser lane's proxy when the browser runs on the node (browser = "node"). The
  usual SOCKS tunnel leaves from the machine, whose own connections out are closed now, so this one
  splits: localhost and the machine's loopback go through the machine's SOCKS (the dev server), and
  everything else goes out from the node through the same list. Sites then see the node's address.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import struct
from collections.abc import Callable
from datetime import datetime
from typing import Any

from openajar.env.netproxy import EgressPolicy, Forwarder, Upstream, split_hostport
from openajar.environments.network import BLOCKED_KEPT, refusal

log = logging.getLogger("openajar.environments")

PLACEHOLDER = "agentos-secret-"


class EnvPolicy(EgressPolicy):
    """An empty list allows nothing (network none with no bound secret), and a refusal names the fix."""

    def __init__(self, allowlist: list[str], *, allow_private: bool = False):
        super().__init__(allowlist=list(allowlist), default="deny", allow_private=allow_private)

    def check(self, host: str, port: int) -> tuple[bool, str]:
        ok, why = super().check(host, port)
        if ok or why == "malformed host":
            return ok, why
        return False, refusal(host if port in (80, 443) else f"{host}:{port}")


class Blocked:
    """What the environment's network refused: host -> {count, last, why}, at most BLOCKED_KEPT hosts
    (the oldest go first). `on_change` lets the owner keep it with the environment's record."""

    def __init__(self, rows: list[dict] | None = None, *, clock: Callable[[], datetime],
                 on_change: Callable[[list[dict]], None] | None = None):
        self.clock = clock
        self.on_change = on_change
        self.rows: dict[str, dict] = {r["host"]: dict(r) for r in rows or [] if r.get("host")}

    def note(self, host: str, why: str = "") -> None:
        row = self.rows.pop(host, None) or {"host": host, "count": 0}
        row["count"] = int(row.get("count") or 0) + 1
        row["last"] = self.clock().isoformat().replace("+00:00", "Z")
        if why:
            row["why"] = why
        self.rows[host] = row
        while len(self.rows) > BLOCKED_KEPT:
            self.rows.pop(next(iter(self.rows)))
        if self.on_change is not None:
            try:
                self.on_change(self.list())
            except Exception:
                log.exception("could not keep the environment's blocked hosts")

    def list(self) -> list[dict]:
        return sorted(self.rows.values(), key=lambda r: r.get("last") or "", reverse=True)


class EnvForwarder(Forwarder):
    """netproxy's forwarder for one environment: its list, its refusals recorded, and CONNECT to a bound
    host handed to the swap (`intercept(host) -> handler or None`). With `upstream` (the browser's own
    forwarder, openajar.environments.browserproxy) what it lets through goes out by the owner's proxy."""

    def __init__(self, env_id: str, policy: EgressPolicy, blocked: Blocked, *,
                 intercept: Callable[[str, int], Any] | None = None, upstream: Upstream | None = None,
                 max_conns: int = 64):
        # with an upstream, plain http goes through a CONNECT too: the owner's credentials never ride in a
        # request whose target the machine chose
        super().__init__(f"env/{env_id}", policy, on_event=self._seen, upstream=upstream, max_conns=max_conns,
                         tunnel_http=upstream is not None)
        self.blocked = blocked
        self.intercept = intercept

    async def _reply(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        if status != 403:
            await super()._reply(writer, status, message)
            return
        body = f"{message}\n".encode()  # the refusal in plain words, for a CONNECT as for a plain request
        # and in the reason phrase: Python's and Go's clients print it for a refused CONNECT, curl only the code
        reason = "".join(c for c in f"Forbidden: {message}" if " " <= c <= "~")[:240]
        writer.write(f"HTTP/1.1 403 {reason}\r\n".encode() + b"Content-Type: text/plain\r\nX-Agentos-Egress: denied\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        with contextlib.suppress(ConnectionError):
            await writer.drain()

    def _seen(self, ev: dict) -> None:
        if not ev.get("allowed"):
            reason = str(ev.get("reason") or "")
            why = "" if "network list" in reason else reason[:120]
            self.blocked.note(str(ev.get("host")) + ("" if ev.get("port") in (80, 443) else f":{ev.get('port')}"), why)

    async def _connect(self, reader, writer, target: str) -> None:
        try:
            host, port = split_hostport(target, 443)
        except ValueError:
            await self._reply(writer, 400, "malformed CONNECT target")
            return
        handler = self.intercept(host, port) if self.intercept is not None else None
        if handler is None:
            await super()._connect(reader, writer, target)
            return
        self._event(host, port, True, "a bound secret's host: the node's swap")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await handler(reader, writer, expect=host)

    async def _http(self, reader, writer, method: str, target: str, version: str, headers: list[str]) -> None:
        if PLACEHOLDER in target or any(PLACEHOLDER in h for h in headers):
            # plain http never gets a swap (the swap is TLS only): its placeholder goes out as it is
            host = split_hostport(target.split("//", 1)[-1].split("/", 1)[0], 80)[0]
            self.blocked.note(host, "a secret's placeholder in plain http: sent as it is, never swapped")
        await super()._http(reader, writer, method, target, version, headers)


# ---------------------------------------------------------------- the browser's split SOCKS

def _local(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


class SplitSocks:
    """SOCKS5 on 127.0.0.1:<port> for the environment's browser on the node (no authentication, CONNECT
    only). The machine's loopback goes through its own SOCKS (`machine_socks`, a Unix socket); every
    other host through `policy` from the node, its refusals into `blocked`. `allow(peer_port, port)`
    keeps it to the node's own user, as the tunnel's guard does."""

    def __init__(self, port: int, *, machine_socks: Callable[[], str | None], policy: EnvPolicy, blocked: Blocked,
                 allow: Callable[[int, int], bool], forwarder: Forwarder):
        self.port = port
        self.machine_socks = machine_socks
        self.policy = policy
        self.blocked = blocked
        self.allow = allow
        self.forwarder = forwarder   # its resolve and connect: public addresses only
        self.server: asyncio.Server | None = None

    @property
    def serving(self) -> bool:
        return self.server is not None and self.server.is_serving()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", self.port)

    async def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        try:
            if not self.allow(int(peer[1]), self.port):
                return
            host, port = await asyncio.wait_for(self._greet(reader, writer), 15)
            if not host:
                return
            up = await self._open(host, port)
            if up is None:
                writer.write(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")  # not allowed by the rules
                await writer.drain()
                return
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await _pipe_both(reader, writer, *up)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _greet(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int]:
        ver, n = await reader.readexactly(2)
        methods = await reader.readexactly(n)
        if ver != 5 or 0 not in methods:
            writer.write(b"\x05\xff")
            return "", 0
        writer.write(b"\x05\x00")
        _ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif atyp == 3:
            (size,) = await reader.readexactly(1)
            host = (await reader.readexactly(size)).decode("idna").lower().rstrip(".")
        else:
            return "", 0
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        if cmd != 1:  # CONNECT only
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return "", 0
        return host, port

    async def _open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        if _local(host):
            return await self._through_machine(host, port)
        ok, why = self.policy.check(host, port)
        if not ok:
            self.blocked.note(host if port in (80, 443) else f"{host}:{port}", "" if "network list" in why else why)
            return None
        try:
            return await self.forwarder._open(host, port)
        except PermissionError:
            self.blocked.note(host, "resolves only to private or local addresses")
            return None
        except (OSError, TimeoutError, ConnectionError):
            return None

    async def _through_machine(self, host: str, port: int):
        path = self.machine_socks()
        if not path:
            return None
        r, w = await asyncio.open_unix_connection(path)
        w.write(b"\x05\x01\x00")
        await w.drain()
        if await r.readexactly(2) != b"\x05\x00":
            w.close()
            return None
        name = host.encode()
        w.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack("!H", port))
        await w.drain()
        head = await r.readexactly(4)
        if head[1] != 0:
            w.close()
            return None
        skip = {1: 4, 4: 16}.get(head[3])
        if skip is None:
            skip = (await r.readexactly(1))[0]
        await r.readexactly(skip + 2)
        return r, w


async def _pipe_both(cr, cw, ur, uw) -> None:
    async def pipe(src, dst):
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(OSError, RuntimeError):
                if dst.can_write_eof():
                    dst.write_eof()

    try:
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    finally:
        uw.close()
