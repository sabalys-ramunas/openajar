"""The record of every solve: one JSON line per try in solver/solves.jsonl in the node's home, kept on this
computer and shown by `openajar solver log`. Each line: when, the environment's browser, the site (its host only),
the widget's kind, the service (the entry's name and provider), the result, and the cost with where it came from
(reported by the service, or the entry's assumed cost). Never the key, the token, the page's path or its query.

A try the guards refused (the month's cap, a site off the list, a sign-up, login or payment page) is a line too,
with no cost, so the owner sees what an agent asked for.

The month's spend (calendar month, UTC) is the sum of its lines' costs per entry: the cap is checked against it,
with the next solve's assumed cost added, before anything is sent. One lock in this process makes the check and
the line one step, so two agents at once cannot both pass a cap that has room for one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("openajar.solver")

FILE = Path("solver") / "solves.jsonl"
LOCK = asyncio.Lock()

# results
PASSED = "passed"            # the token is in the page and the check no longer shows
NOT_PASSED = "not_passed"    # a token came, and the check still shows
FAILED = "failed"            # the service gave no token
REFUSED = "refused"          # a guard stopped it before anything was sent


@dataclass(frozen=True, slots=True)
class Solve:
    at: str
    browser: str
    site: str
    kind: str
    service: str        # the entry's name
    provider: str
    result: str
    cost: float = 0.0
    cost_from: str = ""  # "reported", "assumed" or "" (nothing was spent)
    note: str = ""


def record_path(home: Path) -> Path:
    return Path(home) / FILE


def write(home: Path, solve: Solve) -> None:
    """Append one line (the file 600, its folder 700, never through a symlink). A record that cannot be written is
    logged, and the solve that made it stands."""
    p = record_path(home)
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(asdict(solve), separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        log.warning("could not write the solve record in %s", record_path(home), exc_info=True)


def read(home: Path, *, month: str | None = None) -> list[Solve]:
    """The record's lines (of one month, "2026-11", when given), oldest first. Lines that do not parse are skipped."""
    p = record_path(home)
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            data: Any = json.loads(line)
            solve = Solve(**{k: data[k] for k in Solve.__dataclass_fields__ if k in data})
        except (ValueError, TypeError):
            continue
        if month is None or solve.at.startswith(month):
            out.append(solve)
    return out


def usd(amount: float) -> str:
    """A cost as it is shown: up to five decimals, no trailing zeros ("0.00145", "0.003", "0")."""
    return f"{amount:.5f}".rstrip("0").rstrip(".") or "0"


def this_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def spent(home: Path, service: str, *, month: str | None = None) -> float:
    """What `service` (an entry's name) cost this month (or `month`), in US dollars."""
    month = month or this_month()
    return round(sum(s.cost for s in read(home, month=month) if s.service == service), 6)
