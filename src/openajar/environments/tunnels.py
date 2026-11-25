"""The two tunnels of an environment, carried by one ssh connection from the node to its VM:

- `-D <socket>` on the node: a SOCKS proxy whose connections leave from the VM, on a Unix socket in
  a folder only the node's user can open. The environment's browser lane and studio's review
  browser use it, with DNS through the tunnel and localhost not bypassed, so http://localhost:3000
  in that browser is port 3000 on the VM.
- `-R 127.0.0.1:3129:127.0.0.1:<gateway port>` on the VM: the git gateway's listener for this VM
  alone (openajar.git.service.vm_listener), so git on the VM clones and pushes the environment's
  repository through the node (the credential never reaches the VM; its token works only here).

With the browser on the machine (environments.toml browser = "vm", openajar.environments.vmbrowser) the
same connection carries three more, each from a Unix socket in that folder:

- `-L <folder>/vnc:/run/agentos-browser/vnc.sock`: the machine's VNC server, for the live view (the node's
  viewer relay opens it as it opens a local display's socket);
- `-L <folder>/browser:127.0.0.1:47613`: the browser server on the machine's loopback, which the node
  drives with Playwright through a guard like the SOCKS one (`browser_port`);
- `-L <folder>/desk:/run/agentos-browser/desk.sock`: the desktop's helper (desktop/desk.py), which shows
  the agent's commands in a terminal window and brings the window the agent uses to the front. Only the
  node opens it (`desk_path`); nothing on this computer listens on TCP for it.

An environment with a closed network or app secrets (openajar.environments.networking) gets a third
connection with the node's ports for it, `-R 127.0.0.1:3130:...` (the allowlist proxy) and
`-R 127.0.0.1:3131:...` (the secret swap), so the machine's package downloads never wait behind the
browser or a push (`net_forwards`, set before `start`).

Browsers speak SOCKS over TCP only, so the node relays 127.0.0.1:<port> to that socket (`Guard`).
Who may use the port:

- Not a machine. Its own firewall refuses this computer's addresses (the docker provider), and a
  cloud VM reaches the node only through this ssh connection.
- Not another user of this computer, on Linux: the guard looks up who opened each connection (the
  socket's owner in /proc/net/tcp) and closes any that is not the node's own user. macOS has no
  such lookup for TCP, so there another account on the same Mac could use the port.

The connection uses the VM's own key and pinned host key (openajar.vms.ssh), and forwards nothing
else: no agent, no X11, no other port. If it drops, the node starts it again (openajar.environments.relink):
the dead tunnel is stopped first, so its guards give up their ports and its folder its sockets, and the new
one takes the same ports. A `-R` the machine still holds for a session it has not seen end (this computer's
address changed, so the old connection never said goodbye, and sshd keeps it until TCP gives up, hours
later) fails the new connection; the node then ends the sshd sessions that listen on its own reverse ports
on the machine (`free_remote_command`) and tries once more.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from openajar.env.models import RemoteHost
from openajar.vms.ssh import ssh_base

log = logging.getLogger("openajar.environments")

GATEWAY_PORT_ON_VM = 3129  # as inside an isolated lane (openajar.git.service.LANE_PORT)
VM_BROWSER_PORT = 47613    # the browser server on the machine's loopback (vmbrowser_ctl.py)
VM_VNC_SOCKET = "/run/agentos-browser/vnc.sock"  # its VNC server (vmbrowser_ctl.py)
VM_DESK_SOCKET = "/run/agentos-browser/desk.sock"  # its desktop's helper (desktop/desk.py), when the desktop runs
START_WAIT_S = 10.0
FREE_TIMEOUT_S = 20.0
REMOTE_TAKEN = "remote port forwarding failed"  # ssh's words when the machine refused a -R (ExitOnForwardFailure)
RELAY_CHUNK = 65536
PROC_TCP = ("/proc/net/tcp", "/proc/net/tcp6")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def tunnel_argv(remote: RemoteHost, *, socks_path: str, gateway_port: int | None,
                forwards: tuple[tuple[str, str], ...] = ()) -> list[str]:
    """ssh_base with forwarding allowed for exactly these (and each `-L local:remote` of `forwards`), and
    no remote command."""
    base = ssh_base(remote)
    i = base.index("ClearAllForwardings=yes")
    base = base[: i - 1] + base[i + 1:]          # drop "-o ClearAllForwardings=yes"
    target = base[-2:]                           # "-T", "user@host"
    out = base[:-2] + ["-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
                       "-o", "StreamLocalBindMask=0177", "-N", "-D", socks_path]
    if gateway_port:
        out += ["-R", f"127.0.0.1:{GATEWAY_PORT_ON_VM}:127.0.0.1:{gateway_port}"]
    for local, remote_end in forwards:
        out += ["-L", f"{local}:{remote_end}"]
    return out + target


def git_tunnel_argv(remote: RemoteHost, *, gateway_port: int) -> list[str]:
    """The git gateway's `-R` alone, on an ssh connection of its own. On one connection with the browser
    and its live view, a push from the VM waited behind a stuck browser's traffic (the 2026-11-15
    test: a push took 123 s and a branch delete 107 s, both while the browser on the machine hung)."""
    base = ssh_base(remote)
    i = base.index("ClearAllForwardings=yes")
    base = base[: i - 1] + base[i + 1:]
    return base[:-2] + ["-o", "ExitOnForwardFailure=yes", "-N",
                        "-R", f"127.0.0.1:{GATEWAY_PORT_ON_VM}:127.0.0.1:{gateway_port}"] + base[-2:]


def net_tunnel_argv(remote: RemoteHost, forwards: tuple[tuple[int, int], ...]) -> list[str]:
    """The network's `-R` forwards (port on the machine, port on this computer), on a connection of their own."""
    base = ssh_base(remote)
    i = base.index("ClearAllForwardings=yes")
    base = base[: i - 1] + base[i + 1:]
    out = base[:-2] + ["-o", "ExitOnForwardFailure=yes", "-N"]
    for on_vm, here in forwards:
        out += ["-R", f"127.0.0.1:{int(on_vm)}:127.0.0.1:{int(here)}"]
    return out + base[-2:]


def free_remote_command(ports: tuple[int, ...]) -> str:
    """The command that ends the sshd sessions on the machine that listen on these loopback ports (the node's own
    reverse ports: 3129 to 3132), so a new tunnel can take them. Only sshd's processes: a program of the agent's
    that listens there is left alone, and the new -R then says so."""
    which = " or ".join(f"sport = :{int(p)}" for p in sorted(set(ports)))
    return (f"ss -Htlnp '( {which} )' | grep -oE '\"sshd(-session)?\",pid=[0-9]+' | grep -oE '[0-9]+$' "
            "| sort -u | xargs -r kill")


class _RemoteTaken(OSError):
    """The machine refused one of the tunnel's -R forwards: something there listens on its port already."""


# ---------------------------------------------------------------- who opened a connection

def _hex_port(addr: str) -> int:
    return int(addr.rsplit(":", 1)[1], 16)


def tcp_owner(table: str, *, local_port: int, remote_port: int) -> int | None:
    """The uid that owns the loopback TCP socket local_port -> remote_port, from the text of
    /proc/net/tcp (or tcp6), else None. The client's side of a connection to our listener has
    local = the peer's port and remote = our port."""
    for line in table.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            if _hex_port(parts[1]) == local_port and _hex_port(parts[2]) == remote_port:
                return int(parts[7])
        except ValueError:
            continue
    return None


def peer_is_us(peer_port: int, our_port: int) -> bool:
    """Linux: is the other end of this loopback connection a socket of this process's user? Other
    systems: no way to tell for TCP, so yes (see the module's docstring)."""
    if not sys.platform.startswith("linux"):
        return True
    for path in PROC_TCP:
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        uid = tcp_owner(text, local_port=peer_port, remote_port=our_port)
        if uid is not None:
            return uid == os.getuid()
    return False


class Guard:
    """127.0.0.1:<port> relayed to a Unix socket, for this computer's user only (`allow`)."""

    def __init__(self, port: int, target: str, *, allow: Callable[[int, int], bool] = peer_is_us):
        self.port = port
        self.target = target
        self.allow = allow
        self.server: asyncio.Server | None = None
        self.refused = 0

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
                await asyncio.wait_for(server.wait_closed(), timeout=2)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        if not self.allow(int(peer[1]), self.port):
            self.refused += 1
            log.warning("environment tunnel on port %s: refused a connection from another user", self.port)
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.open_unix_connection(self.target)
        except OSError:
            writer.close()
            return
        try:
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        finally:
            for w in (up_w, writer):
                w.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(RELAY_CHUNK):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


async def _answers(path: str) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout=1.0)
    except (OSError, TimeoutError):
        return False
    w.close()
    with contextlib.suppress(Exception):
        await w.wait_closed()
    return True


class Tunnel:
    """One ssh process for SOCKS (and a "vm" browser's three forwards), another for the git gateway's
    `-R`, and the guard in front of the SOCKS socket. `start` returns once the SOCKS port answers."""

    def __init__(self, remote: RemoteHost, *, gateway_port: int | None, socks_port: int | None = None,
                 vm_browser: bool = False, browser_port: int | None = None, split_git: bool = True):
        self.remote = remote
        self.gateway_port = gateway_port
        self.socks_port = socks_port or free_port()
        # the browser on the machine (vm_browser): its live view and its server, forwarded too
        self.vm_browser = vm_browser
        self.browser_port = (browser_port or free_port()) if vm_browser else None
        self.proc: asyncio.subprocess.Process | None = None
        self.git_proc: asyncio.subprocess.Process | None = None  # the gateway's -R, on its own connection
        self.net_forwards: tuple[tuple[int, int], ...] = ()  # the network's -R (openajar.environments.networking)
        self.net_proc: asyncio.subprocess.Process | None = None
        self.split_git = split_git
        self.guard: Guard | None = None
        self.browser_guard: Guard | None = None
        self.dir: str | None = None
        self.last_error = ""
        # called once when one of its ssh processes exits by itself (the link dropped), not on stop()
        self.on_exit: Callable[[], None] | None = None
        self._watch: asyncio.Task | None = None
        # a -R the machine refused after start() returned (its -D answered first): the next tunnel frees it first
        self.remote_taken = False
        self.free_first = False

    @property
    def alive(self) -> bool:
        up = self.proc is not None and self.proc.returncode is None and self.guard is not None and self.guard.serving
        git = not self._git_apart() or (self.git_proc is not None and self.git_proc.returncode is None)
        net = not self.net_forwards or (self.net_proc is not None and self.net_proc.returncode is None)
        return up and git and net and (not self.vm_browser or (self.browser_guard is not None and self.browser_guard.serving))

    def _git_apart(self) -> bool:
        return bool(self.gateway_port) and self.split_git

    @property
    def socks_path(self) -> str | None:
        """The SOCKS proxy's Unix socket on this computer (openajar.environments.ports forwards through it)."""
        return os.path.join(self.dir, "socks") if self.dir else None

    @property
    def desk_path(self) -> str | None:
        """The machine's desktop helper (desk.sock), as a Unix socket on this computer (vm_browser only)."""
        return os.path.join(self.dir, "desk") if self.vm_browser and self.dir else None

    @property
    def vnc_path(self) -> str | None:
        """The machine's x11vnc, as a Unix socket on this computer (vm_browser only)."""
        return os.path.join(self.dir, "vnc") if self.vm_browser and self.dir else None

    def _forwards(self) -> tuple[tuple[str, str], ...]:
        if not self.vm_browser or not self.dir:
            return ()
        return ((os.path.join(self.dir, "vnc"), VM_VNC_SOCKET),
                (os.path.join(self.dir, "browser"), f"127.0.0.1:{VM_BROWSER_PORT}"),
                (os.path.join(self.dir, "desk"), VM_DESK_SOCKET))

    def _remote_ports(self) -> tuple[int, ...]:
        return ((GATEWAY_PORT_ON_VM,) if self.gateway_port else ()) + tuple(on_vm for on_vm, _ in self.net_forwards)

    async def start(self) -> None:
        if self.alive:
            return
        if self.free_first:
            await self._free_remote()
        try:
            await self._start_once()
        except _RemoteTaken as e:
            log.info("the tunnel to %s: the machine still holds a reverse port of an earlier connection (%s); "
                     "ending that session there and trying again", self.remote.host_alias, e)
            await self._free_remote()
            await self._start_once()

    async def _free_remote(self) -> None:
        ports = self._remote_ports()
        if not ports:
            return
        proc = await self._ssh([*ssh_base(self.remote), free_remote_command(ports)])
        try:
            await asyncio.wait_for(proc.wait(), FREE_TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def _start_once(self) -> None:
        await self.stop()
        # a short path (a Unix socket's is at most 104 bytes on macOS), in a folder only we can open
        self.dir = tempfile.mkdtemp(prefix="agentos-env-")
        sock = os.path.join(self.dir, "socks")
        argv = tunnel_argv(self.remote, socks_path=sock, gateway_port=None if self._git_apart() else self.gateway_port,
                           forwards=self._forwards())
        self.proc = await self._ssh(argv)
        if self._git_apart():
            assert self.gateway_port is not None
            self.git_proc = await self._ssh(git_tunnel_argv(self.remote, gateway_port=self.gateway_port))
        if self.net_forwards:
            self.net_proc = await self._ssh(net_tunnel_argv(self.remote, self.net_forwards))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_WAIT_S
        while loop.time() < deadline:
            for proc in (self.proc, self.git_proc, self.net_proc):
                if proc is not None and proc.returncode is not None:
                    err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
                    self.last_error = err[-300:] or f"ssh exited {proc.returncode}"
                    await self.stop()
                    kind = _RemoteTaken if REMOTE_TAKEN in err else OSError
                    raise kind(f"the tunnel to the environment's machine did not start: {self.last_error}")
            if await _answers(sock):
                self.guard = Guard(self.socks_port, sock)
                if self.vm_browser and self.browser_port:
                    self.browser_guard = Guard(self.browser_port, os.path.join(self.dir, "browser"))
                for guard in (self.guard, self.browser_guard):
                    if guard is None:
                        continue
                    try:
                        await guard.start()
                    except OSError as e:
                        await self.stop()
                        raise OSError(f"the tunnel's port {guard.port} on this computer is taken: {e}") from None
                self._watch = asyncio.create_task(self._watch_exit(), name=f"tunnel-{self.remote.host_alias}")
                return
            await asyncio.sleep(0.2)
        await self.stop()
        raise OSError("the tunnel to the environment's machine did not answer within "
                      f"{START_WAIT_S:g} s")

    async def _watch_exit(self) -> None:
        """Wait for any of its ssh processes to end; one that ends by itself is the link dropping (on_exit)."""
        procs = [p for p in (self.proc, self.git_proc, self.net_proc) if p is not None]
        waits = [asyncio.ensure_future(p.wait()) for p in procs]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
        self._watch = None
        for proc in procs:
            if proc.returncode is not None and proc.stderr is not None:
                with contextlib.suppress(Exception):
                    err = (await asyncio.wait_for(proc.stderr.read(), 2)).decode(errors="replace").strip()
                    self.last_error = err[-300:] or self.last_error
                    self.remote_taken = self.remote_taken or REMOTE_TAKEN in err
        if self.on_exit is not None:
            try:
                self.on_exit()
            except Exception:
                log.exception("the tunnel's drop handler failed")

    @staticmethod
    async def _ssh(argv: list[str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})

    async def stop(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()  # stopped on purpose: no drop to tell
        for guard in (self.guard, self.browser_guard):
            if guard is not None:
                await guard.stop()
        self.guard = self.browser_guard = None
        procs = [p for p in (self.proc, self.git_proc, self.net_proc) if p is not None]
        self.proc = self.git_proc = self.net_proc = None
        for proc in procs:
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        folder, self.dir = self.dir, None
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
