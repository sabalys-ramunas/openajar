"""The egress forwarder: the only way out of an isolated lane.

An isolated lane has its own network namespace with nothing but loopback. The node opens a
listening socket *inside* that namespace (127.0.0.1:3128) and serves it from the host side
with this forwarder, so proxy-aware programs (HTTP_PROXY / HTTPS_PROXY) can reach allowlisted
hosts and everything else (direct TCP, UDP, DNS) has no route at all.

- CONNECT host:port for HTTPS (and any TLS), absolute-form requests for plain HTTP.
- The allowlist is enforced on the host name the client asked for. Names are resolved on
  the host, and the resolved address must be public unless the entry is an explicit IP,
  so a lane cannot reach the node's own loopback services or the private network.
- Optionally chains to an upstream proxy with credentials (the lane's identity proxy); the
  credentials never enter the lane.
- Plain HTTP is forwarded one request per connection (Connection: close, bodies only by
  Content-Length), so pipelining cannot smuggle a second request to another host.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from openajar.env.models import EgressStats

log = logging.getLogger("openajar.env.netproxy")

DEFAULT_PORTS = frozenset({80, 443})
HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer",
               "upgrade"}


def split_hostport(target: str, default_port: int = 443) -> tuple[str, int]:
    target = target.strip()
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host.lower(), port
    if target.count(":") == 1:
        host, port = target.rsplit(":", 1)
        return host.lower().rstrip("."), int(port)
    return target.lower().rstrip("."), default_port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def entry_matches(entry: str, host: str, port: int) -> bool:
    e_host, e_port = (entry.rsplit(":", 1) if entry.count(":") == 1 else (entry, None))
    if e_port is not None:
        if int(e_port) != port:
            return False
    elif port not in DEFAULT_PORTS:
        return False
    if e_host.startswith("*."):
        return host.endswith(e_host[1:])
    if e_host.startswith("."):
        return host == e_host[1:] or host.endswith(e_host)
    return host == e_host


@dataclass
class EgressPolicy:
    allowlist: list[str]
    default: str = "allow"           # what an empty allowlist means: "allow" (public hosts) or "deny"
    allow_private: bool = False      # tests only

    def check(self, host: str, port: int) -> tuple[bool, str]:
        if not host or any(c in host for c in " /\\@"):
            return False, "malformed host"
        if self.allowlist:
            if any(entry_matches(e, host, port) for e in self.allowlist):
                return True, "allowlisted"
            return False, f"{host}:{port} is not on this lane's allowlist"
        if self.default == "deny":
            return False, "this lane allows no egress (empty allowlist)"
        if port not in DEFAULT_PORTS:
            return False, f"port {port} is not allowed (only 80 and 443 without an allowlist entry)"
        return True, "open egress"

    def explicit_ip(self, host: str) -> bool:
        return any(e.split(":")[0] == host for e in self.allowlist)


@dataclass
class Upstream:
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    def auth_header(self) -> str:
        if not self.username:
            return ""
        token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode()
        return f"Proxy-Authorization: Basic {token}\r\n"


class Forwarder:
    def __init__(self, lane_id: str, policy: EgressPolicy, *, upstream: Upstream | None = None,
                 on_event: Callable[[dict], None] | None = None, max_conns: int = 64,
                 idle_timeout: float = 300.0, connect_timeout: float = 15.0, tunnel_http: bool = False):
        self.lane_id = lane_id
        self.policy = policy
        self.upstream = upstream
        # Plain http through the upstream as a CONNECT to the host's port, then the request in origin form:
        # the upstream's credentials are then never in a request whose target and headers the client chose
        # (a proxy that passed Proxy-Authorization on would hand them to that host).
        self.tunnel_http = tunnel_http
        self.on_event = on_event
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self.stats = EgressStats()
        self._sem = asyncio.Semaphore(max_conns)
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task] = set()

    async def serve(self, sock) -> asyncio.Server:
        """Serve a listening socket (typically created inside the lane's network namespace)."""
        sock.setblocking(False)
        server = await asyncio.start_server(self._client, sock=sock)
        self._servers.append(server)
        return server

    async def close(self) -> None:
        for s in self._servers:
            s.close()
        for t in list(self._tasks):
            t.cancel()
        for s in self._servers:
            try:
                await asyncio.wait_for(s.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                log.debug("forwarder for %s closed with open clients", self.lane_id)
        self._servers.clear()

    # ------------------------------------------------------------------ internals
    def _event(self, host: str, port: int, allowed: bool, reason: str) -> None:
        if allowed:
            self.stats.allowed += 1
        else:
            self.stats.denied += 1
            self.stats.last_denied = f"{host}:{port}"
        if self.on_event:
            try:
                self.on_event({"lane": self.lane_id, "host": host, "port": port, "allowed": allowed,
                               "reason": reason})
            except Exception:
                log.exception("egress event hook failed")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        try:
            if self._sem.locked():
                await self._reply(writer, 503, "too many connections from this lane")
                return
            async with self._sem:
                await self._handle(reader, writer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("forwarder error")
        finally:
            if task:
                self._tasks.discard(task)
            writer.close()

    async def _reply(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        reason = {400: "Bad Request", 403: "Forbidden", 411: "Length Required", 431: "Headers Too Large",
                  502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}.get(status, "Error")
        body = f"openajar egress: {message}\n".encode()
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nX-Agentos-Egress: denied\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except ConnectionError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except asyncio.LimitOverrunError:
            await self._reply(writer, 431, "request headers too large")
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await self._reply(writer, 400, "malformed request line")
            return
        headers = [h for h in lines[1:] if h]
        if method.upper() == "CONNECT":
            await self._connect(reader, writer, target)
        else:
            await self._http(reader, writer, method, target, version, headers)

    async def _resolve(self, host: str, port: int) -> list[str]:
        if _is_ip(host):
            addrs = [host]
        else:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), self.connect_timeout)
            addrs = list(dict.fromkeys(i[4][0] for i in infos))
        if self.policy.allow_private or self.policy.explicit_ip(host):
            return addrs
        public = [a for a in addrs if ipaddress.ip_address(a).is_global]
        return public

    async def _open(self, host: str, port: int):
        """Connect to the target (directly) or to the upstream proxy. Returns (reader, writer)."""
        if self.upstream:
            return await asyncio.wait_for(asyncio.open_connection(self.upstream.host, self.upstream.port),
                                          self.connect_timeout)
        addrs = await self._resolve(host, port)
        if not addrs:
            raise PermissionError(f"{host} resolves only to private or local addresses")
        last: Exception | None = None
        for addr in addrs:
            try:
                return await asyncio.wait_for(asyncio.open_connection(addr, port), self.connect_timeout)
            except (TimeoutError, OSError) as e:
                last = e
        raise ConnectionError(f"cannot reach {host}:{port}: {last}")

    def _unreachable(self, e: BaseException) -> str:
        """The 502's words for a connection that did not open. With an upstream the error names the upstream's
        own address, which is the owner's to know: it goes to the log here, never to the client."""
        if not self.upstream:
            return str(e)
        log.info("forwarder for %s: its upstream proxy did not answer: %s", self.lane_id, e)
        return "the upstream proxy did not answer"

    async def _tunnel(self, writer, up_r, up_w, host: str, port: int) -> bool:
        """CONNECT host:port at the upstream, with its credentials. False, and a 502 to the client, when the
        upstream refuses or does not answer."""
        assert self.upstream is not None
        hp = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        try:
            up_w.write(f"CONNECT {hp} HTTP/1.1\r\nHost: {hp}\r\n{self.upstream.auth_header()}\r\n".encode())
            await up_w.drain()
            resp = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), self.connect_timeout)
        except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
            await self._reply(writer, 502, self._unreachable(e))
            return False
        status = resp.split(b" ", 2)[1] if b" " in resp else b"000"
        if status != b"200":
            await self._reply(writer, 502, f"upstream proxy refused ({status.decode(errors='replace')})")
            return False
        return True

    async def _connect(self, reader, writer, target: str) -> None:
        try:
            host, port = split_hostport(target, 443)
        except ValueError:
            await self._reply(writer, 400, "malformed CONNECT target")
            return
        ok, why = self.policy.check(host, port)
        self._event(host, port, ok, why)
        if not ok:
            await self._reply(writer, 403, why)
            return
        try:
            up_r, up_w = await self._open(host, port)
        except PermissionError as e:
            self._event(host, port, False, str(e))
            await self._reply(writer, 403, str(e))
            return
        except (TimeoutError, OSError, ConnectionError) as e:
            await self._reply(writer, 502, self._unreachable(e))
            return
        try:
            if self.upstream and not await self._tunnel(writer, up_r, up_w, host, port):
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await self._splice(reader, writer, up_r, up_w)
        finally:
            up_w.close()

    async def _http(self, reader, writer, method: str, target: str, version: str, headers: list[str]) -> None:
        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            await self._reply(writer, 400, "only absolute http:// requests or CONNECT are accepted")
            return
        host, port = u.hostname.lower().rstrip("."), u.port or 80
        ok, why = self.policy.check(host, port)
        self._event(host, port, ok, why)
        if not ok:
            await self._reply(writer, 403, why)
            return
        length = 0
        out_headers = []
        for h in headers:
            name, _, value = h.partition(":")
            lname = name.strip().lower()
            if lname in HOP_HEADERS:
                continue
            if lname == "transfer-encoding":
                await self._reply(writer, 411, "chunked request bodies are not forwarded; send Content-Length")
                return
            if lname == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    await self._reply(writer, 400, "bad Content-Length")
                    return
            out_headers.append(h)
        if not any(h.lower().startswith("host:") for h in out_headers):
            out_headers.insert(0, f"Host: {u.netloc}")
        out_headers.append("Connection: close")
        if self.upstream and not self.tunnel_http:
            line = f"{method} {target} {version}\r\n"
            extra = self.upstream.auth_header()
        else:
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            line = f"{method} {path} {version}\r\n"
            extra = ""
        try:
            up_r, up_w = await self._open(host, port)
        except PermissionError as e:
            self._event(host, port, False, str(e))
            await self._reply(writer, 403, str(e))
            return
        except (TimeoutError, OSError, ConnectionError) as e:
            await self._reply(writer, 502, self._unreachable(e))
            return
        try:
            if self.upstream and self.tunnel_http and not await self._tunnel(writer, up_r, up_w, host, port):
                return
            up_w.write((line + "\r\n".join(out_headers) + "\r\n" + extra + "\r\n").encode("latin-1"))
            await up_w.drain()
            body = asyncio.create_task(self._copy(reader, up_w, limit=length, eof=False, out=True))
            await self._copy(up_r, writer, limit=None, eof=True, out=False)
            body.cancel()
        finally:
            up_w.close()

    async def _copy(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, *, limit: int | None,
                    eof: bool, out: bool) -> None:
        remaining = limit
        try:
            while remaining is None or remaining > 0:
                n = 65536 if remaining is None else min(65536, remaining)
                data = await asyncio.wait_for(src.read(n), self.idle_timeout)
                if not data:
                    break
                if out:
                    self.stats.bytes_out += len(data)
                else:
                    self.stats.bytes_in += len(data)
                dst.write(data)
                await dst.drain()
                if remaining is not None:
                    remaining -= len(data)
            if eof and dst.can_write_eof():
                dst.write_eof()
        except (TimeoutError, ConnectionError, OSError):
            pass

    async def _splice(self, cr, cw, ur, uw) -> None:
        a = asyncio.create_task(self._copy(cr, uw, limit=None, eof=True, out=True))
        b = asyncio.create_task(self._copy(ur, cw, limit=None, eof=True, out=False))
        done, _ = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
        if b in done:
            a.cancel()   # the far side is gone: nothing more can be delivered
            return
        # the lane half-closed: let the response finish
        try:
            await asyncio.wait_for(b, self.idle_timeout)
        except TimeoutError:
            b.cancel()
