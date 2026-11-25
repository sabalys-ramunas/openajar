"""The log of solves: one JSON line per request to a service, in solver/solves.jsonl in the node's home, shown by
`openajar solver log` and summed by `openajar solver list`. Each line: when, the environment's browser, the site
(its host only), the widget's kind, the service (the entry's name and provider), the result, the cost the service
reported (none when it reports none), and a short note. Never the key, the token, the page's path or its query.

It is a log, nothing more: a line that cannot be written or read is a warning in the node's log, and the solve goes
on as if it had been written.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("openajar.solver")

FILE = Path("solver") / "solves.jsonl"

# results
PASSED = "passed"            # the token is in the page and the check no longer shows
NOT_PASSED = "not_passed"    # a token came, and the check still shows (or the page moved on)
FAILED = "failed"            # the service gave no token
CANCELLED = "cancelled"      # the call was cut off after the request went out


@dataclass(frozen=True, slots=True)
class Solve:
    at: str
    browser: str
    site: str
    kind: str
    service: str        # the entry's name
    provider: str
    result: str
    cost: float | None = None  # US dollars, as the service reported it
    note: str = ""


def record_path(home: Path) -> Path:
    return Path(home) / FILE


def usd(amount: float) -> str:
    """A cost as it is shown: up to five decimals, no trailing zeros ("0.00145", "0.003", "0")."""
    return f"{amount:.5f}".rstrip("0").rstrip(".") or "0"


def this_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def write(home: Path | None, solve: Solve) -> None:
    """Append one line (the file 600, its folder 700, never through a symlink). Never raises."""
    if home is None:
        return
    p = record_path(home)
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(asdict(solve), separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        log.warning("could not write the solve log %s", p, exc_info=True)


def read(home: Path, *, month: str | None = None) -> list[Solve]:
    """The log's lines (of one month, "2026-11", when given), oldest first. A line that does not read is skipped, and
    a log that does not read is empty, each with a warning. Never raises."""
    p = record_path(home)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        log.warning("could not read the solve log %s", p, exc_info=True)
        return []
    out: list[Solve] = []
    for n, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        solve = _line(line)
        if solve is None:
            log.warning("line %d of the solve log %s does not read; skipped", n, p)
        elif month is None or solve.at.startswith(month):
            out.append(solve)
    return out


def _line(raw: bytes) -> Solve | None:
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = {k: data.get(k) or "" for k in ("at", "browser", "site", "kind", "service", "provider", "result", "note")}
    cost = data.get("cost")
    if not all(isinstance(v, str) for v in text.values()):
        return None
    if isinstance(cost, bool) or not isinstance(cost, int | float | type(None)):
        cost = None
    return Solve(**text, cost=None if cost is None else float(cost))


def spent(solves: list[Solve]) -> float:
    """What these solves cost, as their services reported it (a solve that reported nothing adds nothing)."""
    return round(sum(s.cost for s in solves if s.cost is not None), 6)
