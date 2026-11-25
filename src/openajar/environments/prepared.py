"""The node's prepared machines: one provider image per repository, with its setup already run
(openajar.environments.prepbuild makes them, openajar.environments.preparing boots from them).

The store is one JSON file, `$OPENAJAR_HOME/environments/prepared.json` (0600), read and written under
a file lock, so agentosd and the CLI (`openajar environments prepared forget`) never lose each other's
writes. It also keeps the image the newest open used, which the warm pool boots its machines from.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from openajar.core.fs import write_private_json

STORE_FILE = ("environments", "prepared.json")
PreparedState = Literal["building", "ready", "failed"]


class Prepared(BaseModel):
    key: str                                  # repoconfig.prepared_key
    repo: str                                 # github.com/me/shop
    state: PreparedState = "building"
    image: str | None = None                  # the provider's image id (a docker image name on docker)
    base_image: str = ""                      # the image the build started from
    provider: str = ""
    ref: str | None = None                    # what was asked for (None: the default branch)
    commit: str | None = None                 # the commit the build checked out
    started_at: datetime
    built_at: datetime | None = None
    last_used_at: datetime | None = None
    gb: float | None = None                   # the image's size, as the provider bills it
    setup_s: float | None = None              # how long setup took on the build machine
    build_s: float | None = None              # the whole build, the machine's rent and snapshot included
    build_vm: str | None = None               # the build machine (openajar.vms id) while it runs
    build_cost_eur: float | None = None
    step: str = ""                            # what the build does now, for `openajar environments prepare`
    error: str | None = None                  # a failed build: what failed, with the end of its log
    uses: int = 0                             # opens that booted from it

    def last_used(self) -> datetime:
        return self.last_used_at or self.built_at or self.started_at


class PreparedStore:
    def __init__(self, home: Path):
        self.path = Path(home).joinpath(*STORE_FILE)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"machines": []}
        return data if isinstance(data, dict) else {"machines": []}

    def _write(self, data: dict[str, Any]) -> None:
        write_private_json(self.path, data)

    def all(self) -> list[Prepared]:
        out = []
        for row in self._read().get("machines") or []:
            with contextlib.suppress(ValueError, TypeError):
                out.append(Prepared.model_validate(row))
        return out

    def get(self, key: str) -> Prepared | None:
        return next((p for p in self.all() if p.key == key), None)

    def of_repo(self, repo: str) -> list[Prepared]:
        """The repository's prepared machines, newest first."""
        return sorted((p for p in self.all() if p.repo == repo), key=lambda p: p.started_at, reverse=True)

    def put(self, rec: Prepared) -> Prepared:
        with self._locked():
            data = self._read()
            rows = [r for r in data.get("machines") or [] if r.get("key") != rec.key]
            data["machines"] = [*rows, json.loads(rec.model_dump_json())]
            self._write(data)
        return rec

    def update(self, key: str, change: Callable[[Prepared], bool | None]) -> Prepared | None:
        """Change one record in place under the lock; False from `change` leaves it. None when it is gone."""
        with self._locked():
            data = self._read()
            rows = data.get("machines") or []
            for i, row in enumerate(rows):
                if row.get("key") != key:
                    continue
                rec = Prepared.model_validate(row)
                if change(rec) is not False:
                    rows[i] = json.loads(rec.model_dump_json())
                    data["machines"] = rows
                    self._write(data)
                return rec
        return None

    def remove(self, key: str) -> None:
        with self._locked():
            data = self._read()
            data["machines"] = [r for r in data.get("machines") or [] if r.get("key") != key]
            self._write(data)

    # ---- the image the newest open booted, for the warm pool (openajar.environments.warmpool) ----
    def last_image(self) -> str | None:
        v = self._read().get("last_image")
        return v if isinstance(v, str) and v else None

    def set_last_image(self, image: str) -> None:
        with self._locked():
            data = self._read()
            if data.get("last_image") != image:
                data["last_image"] = image
                self._write(data)


def eur_month(rec: Prepared, eur_per_gb: float) -> float:
    """What its image costs a month: nothing on docker or a person's own machine (an image in their own
    Docker), else its size at the rate."""
    from openajar.vms.providers import FREE

    if rec.provider in FREE or not rec.gb:
        return 0.0
    return round(rec.gb * eur_per_gb, 4)


def age_words(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def row(rec: Prepared, eur_per_gb: float, now: datetime) -> dict[str, Any]:
    """One prepared machine for `openajar environments prepared`, report() and the API."""
    from openajar.core import clock

    out: dict[str, Any] = {"repo": rec.repo, "state": rec.state, "commit": (rec.commit or "")[:12] or None,
                           "ref": rec.ref, "built_at": clock.iso_or_none(rec.built_at, z=True),
                           "last_used_at": clock.iso_or_none(rec.last_used_at, z=True), "uses": rec.uses,
                           "gb": rec.gb, "cost_eur_month": eur_month(rec, eur_per_gb), "key": rec.key[:12]}
    if rec.built_at is not None:
        out["age"] = age_words((now - rec.built_at).total_seconds())
    if rec.setup_s is not None:
        out["setup_s"] = round(rec.setup_s, 1)
    if rec.state == "building":
        out["step"] = rec.step
    if rec.error:
        out["error"] = rec.error
    return out
