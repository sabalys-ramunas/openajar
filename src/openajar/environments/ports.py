"""An environment's port on the person's own computer (environment_port), as E2B and Daytona give a
preview link.

The node already holds one SSH connection to the machine, with a SOCKS proxy whose connections leave
from the machine (openajar.environments.tunnels). A forward is a listener on 127.0.0.1:<free port> of
the node that opens each connection through that proxy to 127.0.0.1:<port> on the machine, so it
rides the same connection and needs no new SSH session, key or firewall rule. Only the node's own
user may use it (the tunnels' Guard check), and it closes with the environment.

Where the person is:

- a laptop node (openajar local): the node is the person's computer, so `url` is http://127.0.0.1:<p>/;
- a server node: the forward is on the server, and the person reaches it with the same `ssh -N -L`
  line browser_view gives (`tunnel`), then opens `url` on their own computer.

The local port is a free one by default. `same` asks for the VM's own number (environment_port's
same_numbers): a page at localhost:5173 whose code calls its API at localhost:3000 then reaches the VM's
3000 too, as it does on the VM. Its url says localhost, so the page's origin is the one the app expects.
A number already taken here (another program, another environment's link) is never shared: that one gets
a free port and `why` says that calls to localhost:<port> will not reach the VM. Not the default: on a
laptop it would hold the person's own usual dev ports (3000, 5173) while an environment is open, and a
server node holds every environment's forwards, so the numbers would collide.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import socket
import struct
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.environments.errors import EnvironmentNotReady
from openajar.environments.tunnels import Guard, _pipe, free_port, peer_is_us

log = logging.getLogger("openajar.environments")

_KEY = "_port_forwards"
SOCKS_TIMEOUT_S = 10.0
MAX_FORWARDS = 8        # per environment
RESERVED = {22, 3129}   # the machine's ssh and the git gateway's reverse tunnel


async def socks_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int) -> None:
    """SOCKS5, no auth, CONNECT 127.0.0.1:<port> (on the machine, through ssh -D). Raises OSError."""
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    if await reader.readexactly(2) != b"\x05\x00":
        raise OSError("the environment's proxy refused the handshake")
    writer.write(b"\x05\x01\x00\x01" + bytes([127, 0, 0, 1]) + struct.pack(">H", port))
    await writer.drain()
    head = await reader.readexactly(4)
    if head[1] != 0:
        raise OSError(f"nothing answers on port {port} of the machine (SOCKS reply {head[1]})")
    tail = {1: 4, 4: 16}.get(head[3])
    if tail is None:
        tail = (await reader.readexactly(1))[0]
    await reader.readexactly(tail + 2)


class PortForward(Guard):
    """127.0.0.1:<local> on the node, relayed through the environment's SOCKS socket to <port> on its
    machine. `target()` is the tunnel's socket now (the tunnel may have been started again). `on_use()`,
    for each connection that reached the machine, notes that a person uses the link and returns what
    ends that (openajar.environments.watching), so the reaper never closes it under their open page."""

    def __init__(self, local: int, port: int, target: Any, *, allow: Any = peer_is_us, on_use: Any = None):
        super().__init__(local, "", allow=allow)
        self.remote_port = port
        self.socks = target
        self.on_use = on_use

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        if not self.allow(int(peer[1]), self.port):
            self.refused += 1
            writer.close()
            return
        path = self.socks()
        if not path:
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_unix_connection(path), SOCKS_TIMEOUT_S)
        except (OSError, TimeoutError):
            writer.close()
            return
        try:
            await asyncio.wait_for(socks_connect(up_r, up_w, self.remote_port), SOCKS_TIMEOUT_S)
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            for w in (up_w, writer):
                w.close()
            return
        stop = None
        if self.on_use is not None:
            with contextlib.suppress(Exception):
                stop = self.on_use()
        try:
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        finally:
            for w in (up_w, writer):
                w.close()
            if callable(stop):
                stop()


def _forwards(mgr: Any) -> dict[str, dict[int, PortForward]]:
    return mgr.__dict__.setdefault(_KEY, {})


def on_laptop(mgr: Any) -> bool:
    """openajar local: the node is the person's computer (openajar.local.node sets `on_laptop`)."""
    return bool(getattr(mgr, "on_laptop", False))


def _ssh_host(mgr: Any) -> str:
    try:
        from openajar.node.tools.lanes import ssh_host

        return ssh_host(getattr(getattr(mgr, "runtime", None), "config", None))
    except Exception:  # noqa: BLE001 - a hint with a placeholder rather than none
        return "<your node>"


def tunnel_line(mgr: Any, local_ports: list[int]) -> str:
    """The person's `ssh -N -L` line for these ports of a server node (one -L each)."""
    return "ssh -N " + " ".join(f"-L {p}:127.0.0.1:{p}" for p in local_ports) + f" {_ssh_host(mgr)}"


def view(mgr: Any, env_id: str, port: int, fwd: PortForward) -> dict:
    """What environment_port returns and what environment()'s ports show for a forwarded port."""
    host = "localhost" if fwd.port == port else "127.0.0.1"  # the VM's own number: the origin the app expects
    out: dict = {"port": port, "local_port": fwd.port, "url": f"http://{host}:{fwd.port}/"}
    if not on_laptop(mgr):
        out["tunnel"] = tunnel_line(mgr, [fwd.port])
    return out


def link_for(mgr: Any, env_id: str, port: int) -> dict | None:
    fwd = _forwards(mgr).get(env_id, {}).get(port)
    if fwd is None or not fwd.serving:
        return None
    return view(mgr, env_id, port, fwd)


def with_links(mgr: Any, env_id: str, ports: list[dict]) -> list[dict]:
    """environment()'s ports, each forwarded one with its `link` (and `tunnel` on a server node)."""
    out = []
    for p in ports:
        got = link_for(mgr, env_id, int(p.get("port") or 0))
        if got is not None:
            p = {**p, "link": got["url"], **({"tunnel": got["tunnel"]} if "tunnel" in got else {})}
        out.append(p)
    return out


def check_port(port: Any) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or port in RESERVED:
        raise InvalidRequest("port is the machine's port a server listens on, 1 to 65535 (not 22 or 3129), "
                             "like 3000 or 5173")
    return port


def taken_here(mgr: Any, port: int) -> str | None:
    """Why `port` of this computer's loopback cannot be the VM's (None: it is free). The probe binds
    127.0.0.1 and ::1 without SO_REUSEADDR: a forward's own bind sets it, and on macOS it would then
    succeed beside a program listening on 0.0.0.0, and take that program's localhost from it."""
    if any(f.port == port for fwds in _forwards(mgr).values() for f in fwds.values() if f.serving):
        return "another environment's link"
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # no IPv6 on this computer
        with s:
            if family == socket.AF_INET6:
                with contextlib.suppress(OSError):
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                s.bind((host, port))
            except OSError as e:
                if e.errno == errno.EACCES:
                    return "a port only root may open"
                if e.errno == errno.EADDRINUSE:
                    return "taken by another program"
    return None


def not_same_words(mgr: Any, port: int, local: int, why: str) -> str:
    where = "this computer" if on_laptop(mgr) else "the node"
    return (f"port {port} on {where} is {why}, so it is {local} here: a page calling localhost:{port} does not "
            f"reach the VM. Free port {port} and call again, or have the page call that server through its own "
            "origin (a dev-server proxy, like Vite's server.proxy)")


def _relink_app(mgr: Any, env: Any, port: int, link: dict) -> None:
    """The app's link (preparing.after_ready) follows a forward that moved to the same number."""
    if int((env.app or {}).get("port") or 0) != port or not (env.app or {}).get("link"):
        return
    got = {"link": link["url"], **({"tunnel": link["tunnel"]} if "tunnel" in link else {})}
    with contextlib.suppress(Exception):
        mgr.store.update(env.id, lambda e: setattr(e, "app", {**(e.app or {}), **got}))


async def forward(mgr: Any, env: Any, port: int, *, same: bool = False) -> dict:
    """Forward the machine's `port` to a free port here (the same one again when it is forwarded), or
    with `same` to that number here when it is free: then `same` is true, else false with `why`."""
    check_port(port)
    if env.state != "ready":
        raise EnvironmentNotReady(f"environment {env.id} is {env.state}; call environment() until it is ready")
    tunnel = mgr.tunnel_of(env.id)
    if tunnel is None or not tunnel.alive:
        raise EnvironmentNotReady(f"environment {env.id}'s connection to its machine is down; the node starts it "
                                  "again within a minute, then call environment_port again")
    mine = _forwards(mgr).setdefault(env.id, {})
    old = mine.get(port)
    old = old if old is not None and old.serving else None
    if old is not None and (not same or old.port == port):
        return {**view(mgr, env.id, port, old), **({"same": True} if same else {})}
    if old is None and len(mine) >= MAX_FORWARDS:
        raise InvalidRequest(f"environment {env.id} forwards {MAX_FORWARDS} ports already "
                             f"({', '.join(str(p) for p in sorted(mine))}); use one of those")
    why = taken_here(mgr, port) if same else None
    if old is not None and why:  # its link stays as it is
        return {**view(mgr, env.id, port, old), "same": False, "why": not_same_words(mgr, port, old.port, why)}
    fwd = await _start(mgr, env, port, port if same and not why else free_port())
    if same and fwd.port != port:
        why = why or "taken by another program"  # it was taken between the check and the bind
    mine[port] = fwd
    if old is not None:  # moved to the same number: the free port's link ends
        await old.stop()
    log.info("environment %s: port %d on its machine is 127.0.0.1:%d here", env.id, port, fwd.port)
    out = view(mgr, env.id, port, fwd)
    if old is not None:
        _relink_app(mgr, env, port, out)
    if same:
        out["same"] = not why
        if why:
            out["why"] = not_same_words(mgr, port, fwd.port, why)
    return out


async def _start(mgr: Any, env: Any, port: int, local: int) -> PortForward:
    watch = getattr(mgr, "watch_lane", None)

    def make(n: int) -> PortForward:
        return PortForward(n, port, lambda: getattr(mgr.tunnel_of(env.id), "socks_path", None),
                           on_use=(lambda: watch(env.terminal_lane, "port")) if callable(watch) else None)

    fwd = make(local)
    try:
        await fwd.start()
    except OSError:
        if local != port:
            raise
        fwd = make(free_port())
        await fwd.start()
    return fwd


async def share(mgr: Any, env: Any, port: int | None, ports: list[int] | None, same: bool) -> dict:
    """environment_port's result: `port` alone in the shape it always had (with the other ports the VM
    listens on, which a page may call too), or `ports` as `forwards` with one `tunnel` line."""
    wanted = list(dict.fromkeys([*([port] if port is not None else []), *(ports or [])]))
    if not wanted:
        raise InvalidRequest("give port=3000, or ports=[5173, 3000] for a page that calls another port")
    if len(wanted) > MAX_FORWARDS:
        raise InvalidRequest(f"at most {MAX_FORWARDS} ports in one environment")
    for p in wanted:
        check_port(p)
    got = [await forward(mgr, env, p, same=same) for p in wanted]
    listening = {p.get("port") for p in await mgr.ports(env)}
    for g in got:
        g["listening"] = g["port"] in listening
    idle = [str(g["port"]) for g in got if not g["listening"]]
    moved = [str(g["port"]) for g in got if g.get("same") is False]
    tail = (f". Nothing listens on port {', '.join(idle)} yet: start the server (terminal_job_start, with --host "
            "0.0.0.0 or 127.0.0.1) and the link works once it does" if idle else "")
    tail += f". Port {', '.join(moved)} could not keep its number here: why says what breaks" if moved else ""
    if ports is None:
        out = got[0]
        first = f"run `{out['tunnel']}` on their computer, then " if "tunnel" in out else ""
        same_here = {int(f.port) for f in _forwards(mgr).get(env.id, {}).values() if f.serving and f.port == f.remote_port}
        others = sorted(p for p in listening if isinstance(p, int) and p != out["port"] and p not in same_here)
        if others:
            out["also_listening"] = others
            tail += (f". The VM also listens on {', '.join(map(str, others))}: if the page calls localhost:<one of "
                     f"those>, forward them together: environment_port(ports=[{out['port']}, {others[0]}], "
                     "same_numbers=true)")
        out["next"] = f"give the human the url: they {first}open {out['url']} in their own browser{tail}"
        return out
    out = {"forwards": [{k: v for k, v in g.items() if k != "tunnel"} for g in got]}
    first = ""
    if not on_laptop(mgr):
        out["tunnel"] = tunnel_line(mgr, [g["local_port"] for g in got])
        busy = " (if ssh says an address is in use, that port is taken there)" if same else ""
        first = f"run `{out['tunnel']}` on their computer{busy}, then "
    out["next"] = f"give the human the urls: they {first}open the page's url in their own browser{tail}"
    return out


async def close_all(mgr: Any, env_id: str) -> None:
    """The environment's forwards end with it."""
    for fwd in (_forwards(mgr).pop(env_id, None) or {}).values():
        with contextlib.suppress(Exception):
            await fwd.stop()


async def ended(mgr: Any, env_id: str) -> None:
    """A closed environment: its forwards stop, and the sign-ins and the pinned device the node held for
    its browser go (a save holds its own copy)."""
    from openajar.environments import signins, vmpin

    await close_all(mgr, env_id)
    signins.drop(mgr, env_id)
    with contextlib.suppress(OSError):
        vmpin.drop(mgr, env_id)
