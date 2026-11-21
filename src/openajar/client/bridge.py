"""`openajar mcp-bridge <connection>`: the connection's HTTP MCP server, as a stdio MCP server.

Codex and T3 Code's Codex threads start MCP servers as commands, without our environment, so
the token cannot come from an env var. The bridge reads it from the same store Claude Code's
headersHelper reads (the login Keychain item `openajar/<name>`, or the chmod 600 file), and
forwards every message to the connection's URL with `Authorization: Bearer`.

Being a good stdio citizen (MCP spec, Transports > stdio and Lifecycle > Shutdown; Codex's
`startup_timeout_sec` and `tool_timeout_sec`):
- stdout carries only MCP messages, one JSON object per line. Everything else goes to stderr,
  which the spec lets a client capture, show or ignore (Codex keeps it in its log);
- when the client closes stdin, the bridge ends the HTTP session and exits 0. SIGTERM and
  SIGINT do the same, quietly;
- a request the node does not answer in time gets a JSON-RPC error that says so, a little
  before Codex's own tool timeout fires, and an HTTP error carries the node's own error text
  (`code: message (fix: ...)`), never just a status;
- the tunnel being down (nothing on the local port) makes it restart the tunnel's service
  once and wait for it; a 401 makes it read the token again (a new grant from `openajar
  connect`) and retry once; a 404 on the session (agentosd restarted) replays the client's
  initialize and retries once.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any

from openajar.client.mcp_http import MCP_PATH, _error_text

REQUEST_TIMEOUT = 570.0  # under Codex's tool_timeout_sec (600, written by connect)
TUNNEL_WAIT = 20.0
WORKERS = 8
PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC server errors (-32000 to -32099 are the implementation's own)
UNREACHABLE = -32001
UNAUTHORIZED = -32002
TIMEOUT = -32003
NODE_ERROR = -32004
PARSE_ERROR = -32700


def bridge_command(name: str) -> tuple[str, list[str]]:
    """What Codex runs: the `openajar` on PATH (a uv tool install puts it in ~/.local/bin), else
    this Python with `-m openajar.client`."""
    exe = shutil.which("openajar")
    if exe:
        return exe, ["mcp-bridge", name]
    return sys.executable, ["-m", "openajar.client", "mcp-bridge", name]


def log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(f"openajar mcp-bridge: {message}\n")
        sys.stderr.flush()


class Upstream(Exception):
    def __init__(self, code: int, message: str, status: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Bridge:
    """One connection's bridge. `token()` reads the store; `reopen()` restarts the tunnel (or is
    None for a node on this computer)."""

    def __init__(self, *, name: str, host: str, port: int, path: str, token: Any, reopen: Any = None,
                 timeout: float = REQUEST_TIMEOUT, out: IO[bytes] | None = None):
        self.name = name
        self.host = host
        self.port = port
        self.path = path
        self._read_token = token
        self._reopen = reopen
        self.timeout = timeout
        self.out = out or sys.stdout.buffer
        self._token: str | None = None
        self._lock = threading.Lock()
        self._session: str | None = None
        self._protocol: str | None = None
        self._initialize: dict | None = None  # the client's own, replayed when the node forgot the session
        self._reopened_at = 0.0
        self._replaying = False
        self._replay_lock = threading.Lock()
        self._closed = False

    # ---- output ----

    def emit(self, message: Any) -> None:
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with self._lock:
            if self._closed:
                return  # the client closed stdin: nobody reads an answer any more
            self.out.write(line)
            self.out.flush()

    def error(self, msg_id: Any, code: int, message: str) -> None:
        if msg_id is None or self._replaying:
            log(message)  # a notification gets no answer: the log is the only place to say it
            return
        self.emit({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": f"openajar: {message}"}})

    # ---- upstream ----

    def token(self, fresh: bool = False) -> str:
        if self._token is None or fresh:
            self._token = self._read_token()
        if not self._token:
            raise Upstream(UNAUTHORIZED, f"no token for {self.name} on this computer; run "
                                         f"`openajar connect` again (see `openajar connect --check`)")
        return self._token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token()}"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        return headers

    def _post(self, body: bytes, timeout: float) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        headers = self._headers()  # before connecting: a missing token is an answer of its own
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.connect()
        except OSError as exc:
            conn.close()
            raise Upstream(UNREACHABLE, f"nothing answers on {self.host}:{self.port} "
                                        f"({exc.strerror or exc})") from None
        try:
            conn.request("POST", self.path, body=body, headers=headers)
            return conn, conn.getresponse()
        except TimeoutError:
            conn.close()
            raise Upstream(TIMEOUT, f"the node did not answer in {timeout:.0f} seconds") from None
        except (http.client.HTTPException, OSError):
            conn.close()
            raise Upstream(UNREACHABLE, f"{self.host}:{self.port} closed the connection without an answer; "
                                        "is agentosd running on the node?") from None

    def _reopen_tunnel(self) -> bool:
        if self._reopen is None or time.monotonic() - self._reopened_at < TUNNEL_WAIT:
            return False
        self._reopened_at = time.monotonic()
        log(f"the tunnel to {self.name} is down; restarting it")
        with contextlib.suppress(Exception):
            self._reopen()
        deadline = time.monotonic() + TUNNEL_WAIT
        while time.monotonic() < deadline:
            probe = http.client.HTTPConnection(self.host, self.port, timeout=2)
            try:
                probe.connect()
                return True
            except OSError:
                time.sleep(0.5)
            finally:
                probe.close()
        return False

    def forward(self, message: dict, *, timeout: float | None = None, retry: bool = True) -> None:
        """Send one client message and write every message the node answers with. Every request
        gets an answer: an unexpected failure becomes a JSON-RPC error, never silence."""
        msg_id = message.get("id") if "method" in message else None
        try:
            self._forward(message, msg_id, timeout, retry)
        except Upstream as exc:
            self.error(msg_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001  (a hung tool call is worse than an odd error)
            self.error(msg_id, NODE_ERROR, f"the bridge failed: {type(exc).__name__}: {exc}")

    def _forward(self, message: dict, msg_id: Any, timeout: float | None, retry: bool) -> None:
        body = json.dumps(message).encode()
        sent_session = self._session
        try:
            conn, resp = self._post(body, timeout or self.timeout)
        except Upstream as exc:
            if retry and exc.code == UNREACHABLE and self._reopen_tunnel():
                return self.forward(message, timeout=timeout, retry=False)
            if exc.code == UNREACHABLE:
                exc.message += "; the tunnel did not come back, so run `openajar connect --check`"
            return self.error(msg_id, exc.code, exc.message)
        try:
            self._answer(message, msg_id, resp, retry, sent_session)
        except TimeoutError:
            self.error(msg_id, TIMEOUT, f"the node stopped answering midway (no data for "
                                        f"{timeout or self.timeout:.0f} seconds)")
        except (http.client.HTTPException, OSError) as exc:
            self.error(msg_id, UNREACHABLE, f"the answer from the node broke off ({exc})")
        finally:
            conn.close()

    def _answer(self, message: dict, msg_id: Any, resp: http.client.HTTPResponse, retry: bool,
                sent_session: str | None) -> None:
        status = resp.status
        if status == 401:
            resp.read()
            old = self._token
            if retry and self.token(fresh=True) != old:
                return self.forward(message, retry=False)
            return self.error(msg_id, UNAUTHORIZED, f"the node refused this computer's grant for {self.name} "
                                                    "(expired or revoked); run `openajar connect` again")
        if status == 404 and sent_session and retry and message.get("method") != "initialize":
            resp.read()
            with self._replay_lock:  # several calls lose the session at once: one new session for all
                if self._session == sent_session:
                    self._session = None  # agentosd restarted and forgot the session: start a new one
                    self.replay_initialize()
            return self.forward(message, retry=False)
        if status >= 400:
            why = _error_text(resp) or f"HTTP {status} {resp.reason}"
            return self.error(msg_id, NODE_ERROR, f"the node answered: {why}")
        if session := resp.getheader("mcp-session-id"):
            self._session = session
        kind = (resp.getheader("content-type") or "").split(";")[0].strip().lower()
        if status == 202 or kind not in ("application/json", "text/event-stream"):
            resp.read()
            if msg_id is not None and status != 202:
                self.error(msg_id, NODE_ERROR, f"the node answered with {kind or 'nothing'} instead of MCP")
            return
        if kind == "application/json":
            raw = resp.read()
            if raw.strip():
                try:
                    payload = json.loads(raw)
                except ValueError:
                    return self.error(msg_id, NODE_ERROR, "the node answered with JSON that does not parse")
                self._deliver(payload)
            elif msg_id is not None:
                self.error(msg_id, NODE_ERROR, "the node answered with an empty body")
            return
        data: list[str] = []
        while True:
            raw_line = resp.readline()
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if not raw_line or not line:
                if data:
                    with contextlib.suppress(ValueError):
                        self._deliver(json.loads("\n".join(data)))
                    data = []
                if not raw_line:
                    return
                continue
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))

    def _deliver(self, payload: Any) -> None:
        for item in payload if isinstance(payload, list) else [payload]:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str) and "serverInfo" in result:
                self._protocol = result["protocolVersion"]
                if self._replaying:
                    continue  # our own replayed initialize: the client already has its answer
            self.emit(item)

    def replay_initialize(self) -> None:
        if self._initialize is None:
            return
        self._replaying = True
        try:
            self.forward(self._initialize, retry=False, timeout=30)
            self.forward({"jsonrpc": "2.0", "method": "notifications/initialized"}, retry=False, timeout=30)
        finally:
            self._replaying = False

    def close(self) -> None:
        """End the HTTP session, briefly (the client is already gone)."""
        if not self._session:
            return
        conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
        try:
            conn.request("DELETE", self.path, headers=self._headers())
            conn.getresponse().read()
        except Exception:  # noqa: BLE001, S110  (best effort: the node ends idle sessions itself)
            pass
        finally:
            conn.close()
        self._session = None

    # ---- the loop ----

    def handle(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "openajar: not JSON"}})
            return
        if not isinstance(message, dict):
            # a batch, or not a message at all: MCP sends one object per line
            self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600,
                                                                "message": "openajar: one JSON-RPC object per line"}})
            return
        if message.get("method") == "initialize":
            self._initialize = message
            self._session = None
        self.forward(message)

    def serve(self, stdin: IO[bytes] | None = None) -> int:
        stdin = stdin or sys.stdin.buffer
        pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="mcp-bridge")
        try:
            for raw in iter(stdin.readline, b""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    first = json.loads(line)
                except ValueError:
                    first = None
                # initialize runs first and alone, so the session id is known before anything else goes
                if isinstance(first, dict) and first.get("method") == "initialize":
                    self.handle(line)
                    continue
                pool.submit(self.handle, line)
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        finally:
            with self._lock:
                self._closed = True  # answers still in flight are dropped, never written after EOF
            pool.shutdown(wait=False, cancel_futures=True)
            self.close()
        return 0


def run(name: str) -> int:
    """`openajar mcp-bridge NAME`. Exits 1 (with the reason on stderr) when the connection is unknown."""
    from openajar.client.errors import ClientError
    from openajar.client.ops import Client

    quit_on_signals()
    client = Client(out=log)
    try:
        conn = client.get(name)
    except ClientError as exc:
        log(f"{exc.message}" + (f"; {exc.hint}" if exc.hint else ""))
        return 1
    serve_connection(client, conn)
    return 0  # serve_connection never returns


def quit_on_signals() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: os._exit(0))  # the client is shutting us down: nothing left to say


def make_bridge(client: Any, conn: Any) -> Bridge:
    """The bridge to the connection `conn` (an openajar.client.state.Connection), its token from this
    computer's store."""
    store = client.tokens(conn.token_store)
    reopen = None
    if not conn.local:
        def reopen() -> None:
            client.service(conn.service).restart(conn.name)
    path = MCP_PATH + (f"?toolsets={','.join(conn.toolsets)}" if conn.toolsets else "")
    return Bridge(name=conn.name, host=conn.host, port=conn.local_port, path=path,
                  token=lambda: store.get(conn.name), reopen=reopen)


def serve_connection(client: Any, conn: Any, stdin: IO[bytes] | None = None) -> None:
    """Bridge stdio to `conn` until the client closes stdin, then exit the process."""
    code = make_bridge(client, conn).serve(stdin)
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)  # do not wait for calls still in flight: the client has gone
