"""Fakes for the client tests: a fake system on PATH and a real agentosd on loopback.

No tests live here. `FakeSystem` writes fake `ssh`, `security`, `launchctl`, `systemctl`
and `claude` executables into a temp dir that becomes the whole PATH, so nothing can
reach the real Keychain, launchd or Claude Code. Each fake records its argv to
calls.jsonl and keeps its state in JSON files:

- ssh: `sh -s` runs the received script with /bin/sh against a temp node home (the real
  remote script, with /var/lib/agentos pointed at the temp dir); `-N -L l:localhost:r`
  is a real TCP forward from 127.0.0.1:l to 127.0.0.1:r, so the tunnel can be tested.
- security: a JSON keychain; `-i` reads commands from stdin like the real one.
- launchctl / systemctl: start the job's program in the background, but only if it is
  one of these fakes; `print` / `is-active` report it.
- claude: `mcp add-json | remove | get`, the user scope in the profile's .claude.json (CLAUDE_CONFIG_DIR's,
  else ~/.claude.json), as the real one keeps it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from fakes import ControlFakeLaneManager, FakeInteraction, FakeLaneManager

from openajar.core.config import NodeConfig, save_config
from openajar.core.models import LaneSpec
from openajar.node.daemon import build_services, node_runtime, serve

TOOLS = ("ssh", "security", "launchctl", "systemctl", "claude", "rsync")

FAKE = r'''
import json, os, plistlib, shlex, signal, socket, subprocess, sys, threading, time

STATE = os.environ["OPENAJAR_FAKE_STATE"]
TOOL = os.path.basename(sys.argv[0])
ARGS = sys.argv[1:]
BIN = os.path.dirname(os.path.abspath(sys.argv[0]))


def path(name):
    return os.path.join(STATE, name)


def load(name, default):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(name, value):
    with open(path(name) + ".tmp", "w") as f:
        json.dump(value, f, indent=1)
    os.replace(path(name) + ".tmp", path(name))


def record(**extra):
    with open(path("calls.jsonl"), "a") as f:
        f.write(json.dumps({"tool": TOOL, "argv": ARGS, "pid": os.getpid(), **extra}) + "\n")


def die(message, code):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    sys.exit(code)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(100):
        if not alive(pid):
            return
        time.sleep(0.03)


def spawn(argv, log):
    if os.path.dirname(os.path.abspath(argv[0])) != BIN:
        die(f"fake {TOOL} refuses to start {argv[0]}: it is not a fake", 99)
    out = open(log, "a") if log else subprocess.DEVNULL
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    with open(path("pids"), "a") as f:
        f.write(f"{proc.pid}\n")
    return proc.pid


# ---- ssh ----

def parse_ssh(args):
    opts, forwards, flags, i = {}, [], "", 0
    with_value = set("bcDEeFIiJLlmOoPpQRSWw")
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a[1] in with_value:
            value = a[2:] or args[i + 1]
            i += 1 if a[2:] else 2
            if a[1] == "o":
                key, _, val = value.partition("=")
                opts[key] = val
            elif a[1] == "L":
                forwards.append(value)
        else:
            flags += a[1:]
            i += 1
    return opts, forwards, flags, args[i], args[i + 1:]


def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def forward(client, port):
    try:
        upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError:
        sys.stderr.write("channel 2: open failed: connect failed: Connection refused\n")
        sys.stderr.flush()
        client.close()
        return
    upstream.settimeout(None)
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
    pipe(client, upstream)


def accept_loop(server, port):
    while True:
        client, _ = server.accept()
        threading.Thread(target=forward, args=(client, port), daemon=True).start()


def tunnel(forwards):
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    for spec in forwards:
        local, _host, remote = spec.rsplit(":", 2)
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", int(local)))
        except OSError:
            die(f"bind [127.0.0.1]:{local}: Address already in use\n"
                f"channel_setup_fwd_listener_tcpip: cannot listen to port: {local}\n"
                "Could not request local forwarding.", 255)
        server.listen(16)
        threading.Thread(target=accept_loop, args=(server, int(remote)), daemon=True).start()
    while True:
        time.sleep(3600)


def ssh():
    opts, forwards, flags, host, command = parse_ssh(ARGS)
    node = load("nodes.json", {}).get(host)
    record(stdin=None)
    if node is None:
        die(f"ssh: Could not resolve hostname {host}: nodename nor servname provided, or not known", 255)
    if node.get("down"):
        die(f"ssh: connect to host {host} port 22: Operation timed out", 255)
    if node.get("refuse_login"):
        die(f"{host}: Permission denied (publickey).", 255)
    if "N" in flags:
        tunnel(forwards)
    if command == ["sh", "-s"]:
        script = sys.stdin.read()
        if "# openajar-prepare-source" in script:
            with open(path("installs.jsonl"), "a") as f:
                f.write(json.dumps({"host": host, "script": script, "step": "prepare"}) + "\n")
            sys.exit(0)
        if "# openajar-install-node" in script:
            with open(path("installs.jsonl"), "a") as f:
                f.write(json.dumps({"host": host, "script": script}) + "\n")
            if node.get("install_fails"):
                die("E: Unable to locate package xvfb", 1)
            import shutil as _shutil
            _shutil.copy(os.path.join(node["install_from"], "node.toml"), os.path.join(node["home"], "node.toml"))
            print("==> Done. openajar is at /opt/agentos/.venv/bin/openajar")
            sys.exit(0)
        script = script.replace("/var/lib/agentos", node["home"])
        for real, fake in (node.get("paths") or {}).items():
            script = script.replace(real, fake)
        env = {"PATH": path("remote-bin"), "LC_ALL": "C", "HOME": node["home"],
               "FAKE_UNAME_S": node.get("kernel", "Linux"), "FAKE_UNAME_M": node.get("arch", "x86_64"),
               "FAKE_SUDO": "ok" if node.get("sudo", True) else "no"}
        proc = subprocess.run(["/bin/sh", "-s"], input=script, capture_output=True, text=True, env=env)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)
    die("fake ssh: unexpected command " + " ".join(command), 2)


# ---- security ----

VALUE_OPTS = {"add-generic-password": set("acCDGjlrsTw"), "find-generic-password": set("acCDGjlrs"),
              "delete-generic-password": set("acCDGjlrs")}


def run_security(argv):
    cmd, rest = argv[0], argv[1:]
    opts, flags, i = {}, set(), 0
    while i < len(rest):
        letter = rest[i][1]
        if letter in VALUE_OPTS[cmd]:
            opts[letter] = rest[i + 1]
            i += 2
        else:
            flags.add(letter)
            i += 1
    if os.path.exists(path("keychain.denied")):
        sys.stderr.write("security: SecKeychainItemCreateFromContent (<default>): "
                         "User interaction is not allowed.\n")
        return 51
    items = load("keychain.json", {})
    key = f"{opts.get('s')}/{opts.get('a')}"
    if cmd == "add-generic-password":
        if key in items and "U" not in flags:
            sys.stderr.write("security: SecKeychainItemCreateFromContent (<default>): "
                             "The specified item already exists in the keychain.\n")
            return 45
        items[key] = opts["w"]
        save("keychain.json", items)
        return 0
    if key not in items:
        sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n")
        return 44
    if cmd == "find-generic-password":
        print(items[key] if "w" in flags else f'attributes:\n    "acct"<blob>="{opts.get("a")}"')
        return 0
    del items[key]
    save("keychain.json", items)
    print("password has been deleted.")
    return 0


def security():
    if ARGS[:1] == ["-i"]:
        text = sys.stdin.read()
        with open(path("security-stdin.txt"), "a") as f:
            f.write(text)
        record(stdin_lines=len(text.splitlines()))
        code = 0
        for line in text.splitlines():
            if line.strip():
                code = run_security(shlex.split(line)) or code
        sys.exit(code)
    record()
    sys.exit(run_security(ARGS))


# ---- launchctl ----

def launchctl():
    record()
    jobs = load("launchd.json", {})
    sub = ARGS[0]
    if sub == "bootstrap":
        with open(ARGS[2], "rb") as f:
            plist = plistlib.load(f)
        label = plist["Label"]
        if label in jobs and alive(jobs[label]["pid"]):
            die("Bootstrap failed: 5: Input/output error", 5)
        jobs[label] = {"plist": ARGS[2], "pid": spawn(plist["ProgramArguments"], plist.get("StandardErrorPath"))}
        save("launchd.json", jobs)
        return
    target = ARGS[-1]
    label = target.split("/", 2)[-1]
    job = jobs.get(label)
    if sub == "bootout":
        if not job:
            die("Boot-out failed: 3: No such process", 3)
        kill(job["pid"])
        del jobs[label]
        save("launchd.json", jobs)
        return
    if sub == "print":
        if not job:
            die(f'Could not find service "{label}" in domain for user gui: {os.getuid()}', 113)
        running = alive(job["pid"])
        print(f"{target} = {{\n\tactive count = 1\n\tpath = {job['plist']}\n\ttype = LaunchAgent\n"
              f"\tstate = {'running' if running else 'not running'}\n"
              + (f"\tpid = {job['pid']}\n" if running else "\tlast exit code = 255\n") + "}")
        return
    if sub == "kickstart":
        if not job:
            die(f'Could not find service "{label}" in domain for user gui: {os.getuid()}', 113)
        kill(job["pid"])
        with open(job["plist"], "rb") as f:
            plist = plistlib.load(f)
        job["pid"] = spawn(plist["ProgramArguments"], plist.get("StandardErrorPath"))
        save("launchd.json", jobs)
        return
    die(f"fake launchctl: unsupported {sub}", 64)


# ---- systemctl ----

def systemctl():
    record()
    args = [a for a in ARGS if a != "--user"]
    units = load("systemd.json", {})
    unit_dir = os.path.join(os.environ["HOME"], ".config", "systemd", "user")
    sub = args[0]
    if sub == "daemon-reload":
        return
    now = "--now" in args
    for name in [a for a in args[1:] if not a.startswith("-")]:
        unit = units.setdefault(name, {"enabled": False, "pid": None})
        file = os.path.join(unit_dir, name)
        if sub in ("enable", "restart", "start") and not os.path.exists(file):
            die(f"Failed to {sub} unit: Unit file {name} does not exist.", 5)
        if sub in ("disable", "stop", "restart") and unit["pid"]:
            if sub != "disable" or now:
                kill(unit["pid"])
                unit["pid"] = None
        if sub == "disable" and not os.path.exists(file):
            die(f"Failed to disable unit: Unit file {name} does not exist.", 1)
        if sub == "enable":
            unit["enabled"] = True
        if sub == "disable":
            unit["enabled"] = False
        if sub in ("restart", "start") or (sub == "enable" and now):
            with open(file) as f:
                line = next(x for x in f.read().splitlines() if x.startswith("ExecStart="))
            argv = shlex.split(line[len("ExecStart="):].replace("%%", "%").replace("$$", "$"))
            unit["pid"] = spawn(argv, path(name + ".log"))
        if sub == "is-active":
            active = bool(unit["pid"]) and alive(unit["pid"])
            save("systemd.json", units)
            print("active" if active else "inactive")
            sys.exit(0 if active else 3)
    save("systemd.json", units)


# ---- claude ----

def profile_file():
    """Where the real claude keeps user-scope servers: $CLAUDE_CONFIG_DIR/.claude.json, else ~/.claude.json."""
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ["HOME"], ".claude.json")


def load_claude():
    """Every scope's servers as "scope:name": the user scope from the profile's file, the others from state."""
    servers = {k: v for k, v in load("claude.json", {}).items() if not k.startswith("user:")}
    try:
        with open(profile_file()) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    servers.update({f"user:{name}": entry for name, entry in (data.get("mcpServers") or {}).items()})
    return servers


def save_claude(servers):
    save("claude.json", {k: v for k, v in servers.items() if not k.startswith("user:")})
    try:
        with open(profile_file()) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"firstStartTime": "2026-01-01T00:00:00Z"}
    data["mcpServers"] = {k.split(":", 1)[1]: v for k, v in servers.items() if k.startswith("user:")}
    os.makedirs(os.path.dirname(profile_file()), exist_ok=True)
    with open(profile_file(), "w") as f:
        json.dump(data, f, indent=1)


def claude():
    record(cwd=os.getcwd(), profile=os.environ.get("CLAUDE_CONFIG_DIR"))
    servers = load_claude()
    if ARGS[:1] != ["mcp"]:
        die("fake claude: only `claude mcp` is faked", 2)
    sub, rest = ARGS[1], ARGS[2:]
    scope, positional, i = "local", [], 0
    while i < len(rest):
        if rest[i] in ("-s", "--scope"):
            scope = rest[i + 1]
            i += 2
        else:
            positional.append(rest[i])
            i += 1
    key = f"{scope}:{positional[0]}"
    if sub == "add-json":
        if os.path.exists(path("claude.fail")):
            die("Error: Invalid configuration: headersHelper is not supported", 1)
        if key in servers:
            die(f"MCP server {positional[0]} already exists in {scope} config", 1)
        servers[key] = json.loads(positional[1])
        save_claude(servers)
        print(f"Added {servers[key].get('type')} MCP server {positional[0]} to {scope} config")
        return
    if sub == "get":
        found = [v for k, v in servers.items() if k.split(":", 1)[1] == positional[0]]
        if not found:
            die(f"No MCP server found with name: {positional[0]}", 1)
        print(f"{positional[0]}:\n  Scope: User config\n  Type: {found[0].get('type')}\n  URL: {found[0].get('url')}")
        return
    if sub == "remove":
        if key not in servers:
            die(f"No {scope}-scoped MCP server found with name: {positional[0]}", 1)
        del servers[key]
        save_claude(servers)
        print(f"Removed MCP server {positional[0]} from {scope} config")
        return
    die(f"fake claude: unsupported mcp {sub}", 2)


def rsync():
    record()  # the checkout "arrives": nothing is copied


{"ssh": ssh, "security": security, "launchctl": launchctl, "systemctl": systemctl, "claude": claude,
 "rsync": rsync}[TOOL]()
'''


class FakeSystem:
    def __init__(self, root: Path, tools: tuple[str, ...] = TOOLS):
        self.root = root
        self.bin = root / "bin"
        self.state = root / "state"
        self.bin.mkdir(parents=True)
        self.state.mkdir()
        (self.state / "remote-bin").mkdir()
        for tool in ("cat", "sed", "id", "test", "true"):
            real = next(p for p in (Path("/bin") / tool, Path("/usr/bin") / tool) if p.exists())
            (self.state / "remote-bin" / tool).symlink_to(real)
        remote = self.state / "remote-bin"
        (remote / "uname").write_text('#!/bin/sh\ncase "$1" in -m) echo "$FAKE_UNAME_M" ;; *) echo "$FAKE_UNAME_S" ;; '
                                      'esac\n')
        (remote / "sudo").write_text('#!/bin/sh\n[ "$FAKE_SUDO" = ok ] || { echo "sudo: a password is required" >&2; '
                                     'exit 1; }\n[ "$1" = -n ] && shift\nexec "$@"\n')
        for tool in ("uname", "sudo"):
            (remote / tool).chmod(0o755)
        for tool in tools:
            self.add_tool(tool)

    def add_tool(self, tool: str) -> Path:
        path = self.bin / tool
        path.write_text(f"#!{sys.executable}\n{FAKE}")
        path.chmod(0o755)
        return path

    def remove_tool(self, tool: str) -> None:
        (self.bin / tool).unlink()

    def env(self) -> dict[str, str]:
        return {"PATH": str(self.bin), "OPENAJAR_FAKE_STATE": str(self.state)}

    # ---- nodes ----

    def add_node(self, host: str, home: Path, **flags: bool) -> None:
        nodes = self._load("nodes.json", {})
        nodes[host] = {"home": str(home), **flags}
        self._save("nodes.json", nodes)

    def add_bare_host(self, host: str, home: Path, install_from: Path, *, os_release: str = "ubuntu 24.04",
                      **flags) -> None:
        """A host with no openajar: `connect --install` "installs" by copying install_from/node.toml in."""
        home.mkdir(parents=True, exist_ok=True)
        os_id, _, version = os_release.partition(" ")
        release = self.root / f"os-release-{host}"
        release.write_text(f'ID={os_id}\nVERSION_ID="{version}"\n')
        systemd = self.root / f"systemd-{host}"
        systemd.mkdir(exist_ok=True)
        paths = {"/etc/os-release": str(release), "/run/systemd/system": str(systemd if flags.pop("systemd", True)
                                                                             else self.root / "nowhere"),
                 "/etc/systemd/system/agentosd.service": str(self.root / "no-unit"),
                 "/opt/agentos": str(self.root / "no-opt")}
        nodes = self._load("nodes.json", {})
        nodes[host] = {"home": str(home), "install_from": str(install_from), "paths": paths, **flags}
        self._save("nodes.json", nodes)

    def installs(self) -> list[dict]:
        """The install scripts that ran (a checkout's preparation step is in `steps`)."""
        return [s for s in self.steps() if s.get("step") != "prepare"]

    def steps(self) -> list[dict]:
        path = self.state / "installs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def set_node(self, host: str, **flags: bool) -> None:
        nodes = self._load("nodes.json", {})
        nodes[host].update(flags)
        self._save("nodes.json", nodes)

    def deny_keychain(self, denied: bool = True) -> None:
        flag = self.state / "keychain.denied"
        if denied:
            flag.touch()
        elif flag.exists():
            flag.unlink()

    # ---- what happened ----

    def _load(self, name: str, default):
        path = self.state / name
        return json.loads(path.read_text()) if path.exists() else default

    def _save(self, name: str, value) -> None:
        (self.state / name).write_text(json.dumps(value))

    def calls(self, tool: str | None = None) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text().splitlines()]
        return [c for c in calls if tool is None or c["tool"] == tool]

    def argv(self, tool: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls(tool)]

    def keychain(self) -> dict[str, str]:
        return self._load("keychain.json", {})

    def claude_servers(self, profile: Path | None = None) -> dict[str, dict]:
        """What the fake claude holds, as "scope:name": the user scope from the profile's .claude.json
        (`profile` is its CLAUDE_CONFIG_DIR; default: ~/.claude.json), the other scopes from its state."""
        out = {k: v for k, v in self._load("claude.json", {}).items() if not k.startswith("user:")}
        path = (profile or Path(os.environ["HOME"])) / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        out.update({f"user:{name}": entry for name, entry in (data.get("mcpServers") or {}).items()})
        return out

    def set_claude_servers(self, servers: dict[str, dict], profile: Path | None = None) -> None:
        """Put user-scope entries ("user:name") in a profile's .claude.json, as Claude Code would."""
        path = (profile or Path(os.environ["HOME"])) / ".claude.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text()) if path.exists() else {}
        data["mcpServers"] = {k.split(":", 1)[1]: v for k, v in servers.items() if k.startswith("user:")}
        path.write_text(json.dumps(data))

    def launchd_jobs(self) -> dict[str, dict]:
        return self._load("launchd.json", {})

    def security_stdin(self) -> str:
        path = self.state / "security-stdin.txt"
        return path.read_text() if path.exists() else ""

    def everything_visible(self) -> str:
        """Every argv any fake saw, as one string (what `ps` could have shown)."""
        return "\n".join(" ".join(c["argv"]) for c in self.calls())

    def cleanup(self) -> None:
        path = self.state / "pids"
        if not path.exists():
            return
        pids = [int(line) for line in path.read_text().split()]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        wait_until(lambda: not any(_alive(pid) for pid in pids), timeout=3)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class NodeServer:
    """agentosd with a FakeLaneManager on 127.0.0.1, and its node.toml for the fake ssh to read.
    With `policy` (the default) the real policy service runs, as on a real node: grants, approvals
    and the panel's grant routes; `runtime` is the node's runtime once it started."""

    def __init__(self, home: Path, token: str, *, name: str = "node-test", port: int = 0, control: bool = False,
                 policy: bool = True, setup=None):
        self.setup = setup  # setup(runtime): more services before it serves (the env manager, say)
        self.home = home
        self.control = control  # a manager with take over, so browser_view returns signed links
        self.policy = policy
        self.runtime = None
        self.token = token
        self.name = name
        self.port = port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def write_toml(self, *, port: int | None = None, token: str | None = None) -> None:
        cfg = NodeConfig(home=self.home, name=self.name, mcp_port=port or self.port, mcp_token=token or self.token)
        save_config(cfg)

    def start(self) -> NodeServer:
        ready = threading.Event()
        config = NodeConfig(home=self.home / "run", name=self.name, mcp_port=self.port, mcp_token=self.token)

        async def main() -> None:
            manager = (ControlFakeLaneManager if self.control else FakeLaneManager)(config)
            await manager.create(LaneSpec(id="personal/reddit"))
            await manager.create(LaneSpec(id="biz-a/instagram"))
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()

            def started(port: int) -> None:
                self.port = port
                ready.set()

            runtime = node_runtime(config, manager, FakeInteraction())
            if self.policy:
                build_services(runtime, ["openajar.policy.service"])
            if self.setup is not None:
                self.setup(runtime)
            self.runtime = runtime
            await serve(config, runtime, stop=self._stop, on_started=started)

        self._thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
        self._thread.start()
        assert ready.wait(15), "agentosd did not start"
        self.write_toml()
        return self

    def on_loop(self, coro, timeout: float = 30.0):
        """Run a coroutine on the server's own event loop (its services live there)."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stop(self) -> None:
        if self._loop and self._stop and self._thread and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(15)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()
