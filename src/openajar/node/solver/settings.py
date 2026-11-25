"""The owner's solving services, as `openajar solver add` stores them: secrets/solvers.json in the node's home
(chmod 600, in the 700 secrets folder, written as proxies.json is). Nothing here is ever sent to an environment's
machine, a page, an agent, a log or a trace: the key leaves this computer only in the request to the owner's own
service (openajar.node.solver.services).

No file, or an empty one, is the default: no solver, and openajar solves nothing. There is no bundled or shared key.
Each entry is one service: its provider, its key, and its address or command when it needs one. It answers for any
site a browser is on.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from openajar.core.fs import write_private

FILE = "solvers.json"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,40}")
Provider = Literal["capsolver", "2captcha", "anticaptcha", "command", "webhook"]
PROVIDERS: tuple[str, ...] = ("capsolver", "2captcha", "anticaptcha", "command", "webhook")
BUILT_IN: frozenset[str] = frozenset({"capsolver", "2captcha", "anticaptcha"})

_HOME: Path | None = None
_CACHE: dict[Path, tuple[float, list[SolverEntry]]] = {}
_LOCK = threading.Lock()


class SolverEntry(BaseModel):
    """One solving service. `key` is the secret; every other field may be shown."""

    name: str
    provider: Provider
    key: str | None = None
    endpoint: str | None = None      # a built-in's API address (its own by default), or the webhook's URL
    command: str | None = None       # the command hook's command line (no shell)
    added: str = ""

    def public(self) -> dict[str, Any]:
        """Everything but the key (and the webhook's address, which may carry one in its query)."""
        out = self.model_dump(exclude={"key"})
        if out.get("endpoint"):
            out["endpoint"] = safe_url(out["endpoint"])
        out["has_key"] = bool(self.key)
        return out


def safe_url(url: str) -> str:
    """A URL as it may be shown: scheme, host, port and path, no user, password, query or fragment."""
    try:
        parts = urlsplit(url)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}"
    except ValueError:
        return "(an address that does not parse)"


def bind_home(home: Path | None) -> None:
    """The node's home, where the composed runtime reads solvers.json from (openajar.node.daemon.node_runtime)."""
    global _HOME
    _HOME = Path(home) if home is not None else None


def home() -> Path | None:
    return _HOME


def path(home_dir: Path) -> Path:
    return Path(home_dir) / "secrets" / FILE


def load(home_dir: Path) -> list[SolverEntry]:
    """The entries in the order they were added. A malformed file raises ValueError naming the entry and the bad
    fields, never their values (a key could be among them)."""
    p = path(home_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except ValueError:
        raise ValueError(f"{p} is not valid JSON") from None
    if not isinstance(raw, list):
        raise ValueError(f"{p} must be a list of solver entries")  # noqa: TRY004 (callers treat any malformed file as ValueError)
    out: list[SolverEntry] = []
    for i, entry in enumerate(raw):
        name = entry.get("name") if isinstance(entry, dict) else None
        try:
            out.append(SolverEntry(**entry) if isinstance(entry, dict) else SolverEntry.model_validate(None))
        except (ValidationError, TypeError) as e:
            fields = sorted({".".join(str(x) for x in err["loc"]) for err in e.errors()}) \
                if isinstance(e, ValidationError) else ["unexpected fields"]
            raise ValueError(f"solver {name or i!r} in {p} is invalid: check {', '.join(fields) or 'the entry'}") from None
    return out


def save(home_dir: Path, entries: list[SolverEntry]) -> None:
    secrets = Path(home_dir) / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    secrets.chmod(0o700)
    data = [e.model_dump(exclude_none=True) for e in entries]
    write_private(path(home_dir), json.dumps(data, indent=2))
    with _LOCK:
        _CACHE.pop(path(home_dir), None)


def current(home_dir: Path | None = None) -> list[SolverEntry]:
    """The entries of the node's home (or `home_dir`), read again when the file changes; [] when there is none or
    it does not load (a solve then says so, and nothing is sent anywhere)."""
    where = home_dir if home_dir is not None else _HOME
    if where is None:
        return []
    p = path(where)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    with _LOCK:
        cached = _CACHE.get(p)
        if cached is not None and cached[0] == mtime:
            return list(cached[1])
    try:
        entries = load(where)
    except (OSError, ValueError):
        return []
    with _LOCK:
        _CACHE[p] = (mtime, entries)
    return list(entries)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
