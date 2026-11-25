"""The record of every solve: JSON lines in solver/solves.jsonl in the node's home, kept on this computer and shown by
`openajar solver log`. Each try: when, the environment's browser, the site (its host only), the widget's kind, the
service (the entry's name and provider), the result, and the cost with where it came from. Never the key, the
token, the page's path or its query.

The cap holds through a reservation, made before anything is sent (`reserve`):

1. under an exclusive lock on solver/.lock (fcntl, so the node and `openajar solver test --solve` in another process
   take turns), the record is read, the month's spend of that service is summed, and a line `pending` at the entry's
   assumed cost is appended and synced to disk, only when the spend plus that cost stays within the cap;
2. the lock is let go, and the request goes to the service;
3. a second line with the same id settles it (`settle`): passed, not passed, failed or cancelled, with the cost the
   service reported, the assumed cost when it may have charged and did not say, or 0 when it answered with an error
   (the services charge nothing for those).

A pending line nothing settled (the node stopped, the call was cut off and the settle could not be written) counts at
its assumed cost. So the spend never reads lower than what was sent, and two agents at once cannot both pass a cap
with room for one. What the cap cannot bound: a service that charges more than the assumed cost for a solve it was
already sent (that solve's difference; the spend then shows it and the next solve is refused).

The record fails closed. A record that cannot be read (missing permission, a symlink, bytes that are not UTF-8) or a
line in it that is not a solve as written here (a cost that is not a number from 0 to 1000, a time that is not one)
makes every reservation refuse, with where the problem is, until the owner fixes the file. A record that cannot be
written refuses too: nothing is sent unless its reservation is on disk, whole and synced. What follows the record's
last newline is a write that never finished (a full disk, the power), so its try was never sent: it spends nothing
and the next write removes it. Every write holds the record's lock, and a lock nobody lets go refuses the try after
LOCK_WAIT_S.

A try the guards refused (the month's cap, a site off the list, a sign-up, login or payment page) is a line too, with
no cost, so the owner sees what an agent asked for. That line is best effort: it spends nothing.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("openajar.solver")

DIR = "solver"
FILE = Path(DIR) / "solves.jsonl"
LOCK_FILE = Path(DIR) / ".lock"
MAX_COST = 1000.0
LOCK_WAIT_S = 10.0  # for the lock, then the try is refused: a holder that hangs never hangs the tool with it
NOTE_LOCK_WAIT_S = 2.0  # for a line that sends nothing (a settle, a refused try), written from the node's own loop

# results
PENDING = "pending"          # reserved, sent or about to be: counts at the assumed cost until settled
PASSED = "passed"            # the token is in the page and the check no longer shows
NOT_PASSED = "not_passed"    # a token came, and the check still shows (or the page moved on)
FAILED = "failed"            # the service gave no token
CANCELLED = "cancelled"      # the call was cut off after the request went out
REFUSED = "refused"          # a guard stopped it before anything was sent

_AT = re.compile(r"\d{4}-\d{2}-\d{2}T")


class LedgerError(Exception):
    """The record cannot be read or written, so nothing is sent. The message says where."""


class CapReached(Exception):
    def __init__(self, spent: float):
        super().__init__(f"{spent}")
        self.spent = spent


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
    id: str = ""


def record_path(home: Path) -> Path:
    return Path(home) / FILE


def usd(amount: float) -> str:
    """A cost as it is shown: up to five decimals, no trailing zeros ("0.00145", "0.003", "0")."""
    return f"{amount:.5f}".rstrip("0").rstrip(".") or "0"


def this_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


# ---- reading ---------------------------------------------------------------------------------------------------

def _folder(home: Path) -> Path:
    folder = Path(home) / DIR
    try:
        st = os.lstat(folder)
    except FileNotFoundError:
        return folder
    except OSError as e:
        raise LedgerError(f"the solve record's folder {folder} cannot be read ({e.strerror})") from None
    if not stat.S_ISDIR(st.st_mode):
        raise LedgerError(f"the solve record's folder {folder} is not a folder (a link or a file)")
    return folder


def _raw(home: Path) -> bytes:
    p = record_path(home)
    _folder(home)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return b""
    except OSError as e:
        raise LedgerError(f"the solve record {p} cannot be read ({e.strerror or e})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LedgerError(f"the solve record {p} is not a plain file")
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _line(raw: bytes, n: int, where: Path) -> Solve:
    bad = LedgerError(f"line {n} of the solve record {where} is not a solve openajar wrote: fix or delete that line "
                      "to solve again")
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise bad from None
    if not isinstance(data, dict):
        raise bad
    text = {k: data.get(k, "") for k in ("at", "browser", "site", "kind", "service", "provider", "result",
                                          "cost_from", "note", "id")}
    if not all(isinstance(v, str) for v in text.values()) or not _AT.match(text["at"]):
        raise bad
    cost = data.get("cost", 0.0)
    if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost) or \
            not 0 <= cost <= MAX_COST:
        raise bad
    return Solve(**text, cost=float(cost))


def read_all(home: Path) -> list[Solve]:
    """Every try, oldest first, as its latest line says (a settled reservation is one try). Raises LedgerError when
    the record or a line in it cannot be read."""
    where = record_path(home)
    tries: dict[str, Solve] = {}
    lines = _raw(home).split(b"\n")
    # what follows the last newline is a write that never finished (a full disk, the power): its try was never
    # sent, since a request goes out only after its line is whole and synced, so it spends nothing. The next
    # write removes it (_append)
    for n, raw in enumerate(lines[:-1], start=1):
        if not raw.strip():
            continue
        solve = _line(raw, n, where)
        key = solve.id or f"line-{n}"
        first = tries.get(key)
        tries[key] = replace(solve, at=first.at) if first is not None else solve  # a settle keeps its month
    return list(tries.values())


def read(home: Path, *, month: str | None = None) -> list[Solve]:
    """The tries (of one month, "2026-11", when given), oldest first. Raises LedgerError like read_all."""
    return [s for s in read_all(home) if month is None or s.at.startswith(month)]


def spent(home: Path, service: str, *, month: str | None = None) -> float:
    """What `service` (an entry's name) cost this month (or `month`), in US dollars: a pending try at its assumed
    cost. Raises LedgerError when the record cannot be read: a spend is never guessed lower."""
    month = month or this_month()
    return round(sum(s.cost for s in read(home, month=month) if s.service == service), 6)


# ---- writing ---------------------------------------------------------------------------------------------------

def _append(home: Path, solve: Solve) -> None:
    p = record_path(home)
    try:
        folder = _folder(home)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _drop_unfinished(fd)
            data = (json.dumps(asdict(solve), separators=(",", ":")) + "\n").encode()
            while data:  # a short write (a disk nearly full) goes on, or raises: never half a line taken as done
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise LedgerError(f"the solve record {p} cannot be written ({e.strerror or e})") from None


def _drop_unfinished(fd: int) -> None:
    """Cut what follows the record's last newline: a line whose write never finished (read_all says why it spent
    nothing), so the next line does not join it."""
    size = os.fstat(fd).st_size
    if not size:
        return
    back = min(size, 1 << 16)
    tail = os.pread(fd, back, size - back)
    if tail.endswith(b"\n"):
        return
    keep = size - back + tail.rfind(b"\n") + 1  # nothing kept of it when the tail holds no newline at all
    log.warning("the solve record ends in an unfinished line (%d bytes); removing it", size - keep)
    os.ftruncate(fd, keep)


@contextlib.contextmanager
def _locked(home: Path, wait_s: float = LOCK_WAIT_S) -> Iterator[None]:
    folder = _folder(home)
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(Path(home) / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise LedgerError(f"the solve record's lock in {folder} cannot be taken ({e.strerror or e})") from None
    try:
        until = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise LedgerError(f"the solve record's lock in {folder} is held by another openajar process "
                                      f"that does not let it go (waited {wait_s:.0f} s)") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing lets the lock go


def reserve(home: Path, solve: Solve, *, assumed: float, cap: float) -> Solve:
    """Before anything is sent: the pending line for `solve` at `assumed` cost, on disk, when the month's spend of its
    service plus `assumed` stays within `cap`. Returns the pending line (settle it). Raises CapReached, or LedgerError
    when the record cannot be read or written. Blocking (a file lock): call it in a thread from async code."""
    with _locked(home):
        now = spent(home, solve.service, month=solve.at[:7])
        if now + assumed > cap + 1e-9:
            raise CapReached(now)
        pending = replace(solve, result=PENDING, cost=round(assumed, 6), cost_from="assumed",
                          id=solve.id or secrets.token_hex(8))
        _append(home, pending)
    return pending


def settle(pending: Solve, home: Path, *, result: str, cost: float, cost_from: str, note: str = "") -> None:
    """The try's outcome, as a line with the reservation's id. When it cannot be written the pending line stands, at
    the assumed cost (logged)."""
    try:
        with _locked(home, NOTE_LOCK_WAIT_S):  # every write is under the lock: none meets another's half line
            _append(home, replace(pending, result=result, cost=round(max(0.0, min(cost, MAX_COST)), 6),
                                  cost_from=cost_from, note=note[:300]))
    except LedgerError:
        log.warning("could not settle solve %s; it counts at its assumed cost", pending.id, exc_info=True)


def write(home: Path, solve: Solve) -> None:
    """A line that spends nothing (a refused try). Best effort: one that cannot be written is logged."""
    try:
        with _locked(home, NOTE_LOCK_WAIT_S):
            _append(home, solve)
    except LedgerError:
        log.warning("could not write the solve record in %s", record_path(home), exc_info=True)
