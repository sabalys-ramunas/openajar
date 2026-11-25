"""An environment's SOCKS tunnel on the node: ssh listens on a Unix socket only the node's user can
open, and the TCP port browsers need is a relay that serves the node's own user only."""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import sys

import pytest

from openajar.env.models import RemoteHost
from openajar.environments import tunnels
from openajar.environments.tunnels import Guard, free_port, tcp_owner, tunnel_argv

REMOTE = RemoteHost(vm_id="vm_1", host="127.0.0.1", port=2222, key_path="/k/id", known_hosts="/k/kh",
                    host_alias="openajar-vm_1", provider="docker", label="docker")

# /proc/net/tcp: our listener on 127.0.0.1:0x9C40 (40000), a client of uid 1000 from port 0xC350
# (50000), and one of uid 1001 from port 0xC351 (50001)
TABLE = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:9C40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1
   1: 0100007F:C350 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 2 1
   2: 0100007F:C351 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1001        0 3 1
"""


def test_ssh_serves_socks_on_a_unix_socket_not_a_tcp_port():
    argv = tunnel_argv(REMOTE, socks_path="/tmp/agentos-env-x/socks", gateway_port=4000)
    d = argv.index("-D")
    assert argv[d + 1] == "/tmp/agentos-env-x/socks"
    assert "StreamLocalBindMask=0177" in argv and "ClearAllForwardings=yes" not in argv
    assert "127.0.0.1:3129:127.0.0.1:4000" in argv and argv[-1].endswith("127.0.0.1")


def test_the_owner_of_a_loopback_connection_is_read_from_proc_net_tcp():
    assert tcp_owner(TABLE, local_port=50000, remote_port=40000) == 1000
    assert tcp_owner(TABLE, local_port=50001, remote_port=40000) == 1001
    assert tcp_owner(TABLE, local_port=50002, remote_port=40000) is None


async def _echo_socket(path: str) -> asyncio.Server:
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(b"from the vm: " + data)
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(echo, path)


async def test_the_guard_relays_for_the_nodes_user_and_refuses_anyone_else(tmp_path):
    import tempfile

    folder = tempfile.mkdtemp(prefix="openajar-t-")
    path = os.path.join(folder, "s")
    upstream = await _echo_socket(path)
    seen: list[int] = []

    def only_first(peer_port: int, our_port: int) -> bool:
        seen.append(peer_port)
        return len(seen) == 1

    port = free_port()
    guard = Guard(port, path, allow=only_first)
    await guard.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"hello")
        await w.drain()
        assert await asyncio.wait_for(r.read(100), 5) == b"from the vm: hello"
        w.close()
        r, w = await asyncio.open_connection("127.0.0.1", port)  # "another user"
        try:
            w.write(b"hello")
            got = await asyncio.wait_for(r.read(100), 5)
        except ConnectionResetError:
            got = b""
        assert got == b"" and guard.refused == 1
        w.close()
    finally:
        await guard.stop()
        upstream.close()
        os.unlink(path)
        os.rmdir(folder)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the owner of a TCP socket is read on Linux only")
async def test_on_linux_the_guard_knows_our_own_connections():
    port = free_port()
    seen: list[bool] = []

    async def note(reader, writer):
        peer = writer.get_extra_info("peername")
        seen.append(tunnels.peer_is_us(peer[1], port))
        writer.close()

    server = await asyncio.start_server(note, "127.0.0.1", port)
    _r, w = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.2)
    w.close()
    server.close()
    assert seen == [True]


def test_the_socket_folder_is_the_node_users_alone(tmp_path):
    import tempfile

    folder = tempfile.mkdtemp(prefix="agentos-env-")
    try:
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        assert len(os.path.join(folder, "socks")) < 100  # a Unix socket path fits (104 bytes on macOS)
    finally:
        os.rmdir(folder)


FAKE_SSH = '''#!{py}
import os, socket, sys, time
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(" ".join(args) + "\\n")
if "-D" in args:
    s = socket.socket(socket.AF_UNIX)
    s.bind(args[args.index("-D") + 1])
    s.listen()
time.sleep(60)
'''


async def test_the_git_gateway_gets_an_ssh_connection_of_its_own(tmp_path, monkeypatch):
    """A push from the VM never waits behind the browser's traffic on a shared connection."""
    from openajar.environments.tunnels import Tunnel, git_tunnel_argv
    from openajar.vms import ssh as vssh

    log = tmp_path / "ssh.log"
    fake = tmp_path / "ssh"
    fake.write_text(FAKE_SSH.format(py=sys.executable, log=str(log)))
    fake.chmod(0o755)
    monkeypatch.setattr(vssh, "SSH", str(fake))
    t = Tunnel(REMOTE, gateway_port=4000)
    await t.start()
    try:
        assert t.alive and t.git_proc is not None and t.git_proc.pid != t.proc.pid
        lines = log.read_text().splitlines()
        socks = next(line for line in lines if "-D" in line)
        git = next(line for line in lines if "-R" in line)
        assert "-R" not in socks and "-D" not in git and "127.0.0.1:3129:127.0.0.1:4000" in git
        t.git_proc.kill()
        await t.git_proc.wait()
        assert not t.alive  # the reaper starts both again
    finally:
        await t.stop()
    argv = git_tunnel_argv(REMOTE, gateway_port=4000)
    assert argv[argv.index("-R") + 1] == "127.0.0.1:3129:127.0.0.1:4000" and "ClearAllForwardings=yes" not in argv


# ---------------------------------------------------------------- a restart after a drop

# An ssh that serves the tunnel's sockets, or (with REFUSE_R holding a port) fails its -R as an sshd does whose
# old session still listens there, until the node's freeing command (ss ... | kill) ran.
FAKE_SSH_R = '''#!{py}
import os, socket, sys, time
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(" ".join(args) + "\\n")
held = {held!r}
if args and args[-1].startswith("ss "):
    if os.path.exists(held):
        os.remove(held)
    sys.exit(0)
if "-R" in args and os.path.exists(held):
    port = open(held).read().strip()
    if any(a.startswith("127.0.0.1:" + port + ":") for a in args):
        if os.path.exists(held + ".late"):
            time.sleep(0.4)  # refused after the -D answered
        sys.stderr.write("Error: remote port forwarding failed for listen port " + port + "\\n")
        sys.exit(255)
if "-D" in args and os.path.exists(held + ".slow"):
    time.sleep(0.6)  # the machine is far: the -D answers after the -R was refused
kept = []  # every listener held open (a socket object dropped would close it)
for i, a in enumerate(args):
    if a in ("-D", "-L"):
        s = socket.socket(socket.AF_UNIX)
        s.bind(args[i + 1].split(":", 1)[0])
        s.listen()
        kept.append(s)
time.sleep(60)
'''


def _fake_r(tmp_path, monkeypatch, held: str | None = None, *, when: str = "slow"):
    """`when`: "slow" (the -D answers after the -R was refused) or "late" (the -R is refused after it answered)."""
    from openajar.vms import ssh as vssh

    log, flag = tmp_path / "ssh.log", tmp_path / "held"
    if held:
        flag.write_text(held)
        (tmp_path / f"held.{when}").write_text("")
    fake = tmp_path / "ssh"
    fake.write_text(FAKE_SSH_R.format(py=sys.executable, log=str(log), held=str(flag)))
    fake.chmod(0o755)
    monkeypatch.setattr(vssh, "SSH", str(fake))
    return log


async def test_a_dead_tunnel_stopped_frees_its_ports_and_sockets_for_the_next(tmp_path, monkeypatch):
    """The reaper's restart reused the dead tunnel's ports while its guards still held them: "the tunnel's port
    ... is taken", four times in a row (the 2026-11-24 sites run). Stopped first, the next one gets them."""
    from openajar.environments.tunnels import Tunnel

    _fake_r(tmp_path, monkeypatch)
    old = Tunnel(REMOTE, gateway_port=4000, vm_browser=True)
    await old.start()
    folder = old.dir
    try:
        old.proc.kill()  # the link dropped: ssh is gone, the guards on this computer still listen
        await old.proc.wait()
        assert not old.alive and old.guard.serving and old.browser_guard.serving
        new = Tunnel(REMOTE, gateway_port=4000, vm_browser=True, socks_port=old.socks_port,
                     browser_port=old.browser_port)
        with pytest.raises(OSError, match="is taken"):
            await new.start()
        await old.stop()
        assert old.git_proc is None and not os.path.exists(folder)  # its other ssh reaped, its sockets gone
        await new.start()
        assert new.alive and (new.socks_port, new.browser_port) == (old.socks_port, old.browser_port)
        assert os.path.exists(new.vnc_path) and os.path.exists(os.path.join(new.dir, "browser"))
    finally:
        await old.stop()
        with contextlib.suppress(NameError):
            await new.stop()


async def test_a_reverse_port_an_old_session_holds_on_the_machine_is_freed_once(tmp_path, monkeypatch):
    """After this computer's address changed, the machine's sshd keeps the old session (and its -R listeners: the
    git gateway's 3129, the browser proxy's 3132) until TCP gives up, hours later, so the new -R fails. The node
    ends the sshd sessions that listen on its own reverse ports, and tries once more."""
    from openajar.environments.tunnels import Tunnel, free_remote_command

    log = _fake_r(tmp_path, monkeypatch, held="3132")
    t = Tunnel(REMOTE, gateway_port=4000)
    t.net_forwards = ((3130, 5001), (3132, 5002))
    try:
        await t.start()
        assert t.alive
        lines = log.read_text().splitlines()
        [freeing] = [line for line in lines if line.endswith(free_remote_command((3129, 3130, 3132)))]
        assert "-R" not in freeing.split() and "-D" not in freeing.split()
        assert sum("3132:127.0.0.1:5002" in line for line in lines) == 2  # failed, freed, then up
    finally:
        await t.stop()
    cmd = free_remote_command((3129, 3132))
    assert "sport = :3129 or sport = :3132" in cmd and '"sshd(-session)?",pid=' in cmd and "kill" in cmd


async def test_a_reverse_port_still_taken_after_the_freeing_says_so(tmp_path, monkeypatch):
    from openajar.environments.tunnels import Tunnel

    _fake_r(tmp_path, monkeypatch, held="3132")
    monkeypatch.setattr(tunnels, "free_remote_command", lambda ports: "true")  # nothing freed it
    t = Tunnel(REMOTE, gateway_port=None)
    t.net_forwards = ((3132, 5002),)
    with pytest.raises(OSError, match="remote port forwarding failed for listen port 3132"):
        await t.start()
    assert t.proc is None and t.dir is None


async def test_a_tunnel_says_once_when_its_ssh_ends_by_itself_and_never_when_stopped(tmp_path, monkeypatch):
    """The drop is the signal the node acts on (openajar.environments.relink), at once rather than on the reaper's
    next pass a minute later."""
    from openajar.environments.tunnels import Tunnel

    _fake_r(tmp_path, monkeypatch)
    dropped: list[str] = []
    t = Tunnel(REMOTE, gateway_port=4000)
    t.on_exit = lambda: dropped.append("down")
    await t.start()
    try:
        t.git_proc.kill()
        for _ in range(50):
            if dropped:
                break
            await asyncio.sleep(0.05)
        assert dropped == ["down"] and not t.alive
    finally:
        await t.stop()
    await t.start()
    await t.stop()
    await asyncio.sleep(0.1)
    assert dropped == ["down"]  # a stop is no drop


async def test_a_reverse_port_refused_after_the_start_is_freed_by_the_next_tunnel(tmp_path, monkeypatch):
    """The -D can answer before the machine refuses a -R on another connection: start() returns, the -R's ssh ends,
    and the drop says why. The next tunnel (BringUp._start_tunnel: free_first) frees the port before it starts."""
    from openajar.environments.tunnels import Tunnel, free_remote_command

    log = _fake_r(tmp_path, monkeypatch, held="3132", when="late")
    dropped: list[str] = []
    t = Tunnel(REMOTE, gateway_port=None)
    t.net_forwards = ((3132, 5002),)
    t.on_exit = lambda: dropped.append("down")
    await t.start()
    for _ in range(60):
        if dropped:
            break
        await asyncio.sleep(0.05)
    assert dropped == ["down"] and t.remote_taken and "listen port 3132" in t.last_error
    await t.stop()
    again = Tunnel(REMOTE, gateway_port=None)
    again.net_forwards, again.free_first = t.net_forwards, t.remote_taken
    try:
        await again.start()
        await asyncio.sleep(0.6)
        assert again.alive
        assert log.read_text().splitlines()[-3].endswith(free_remote_command((3132,)))
    finally:
        await again.stop()
