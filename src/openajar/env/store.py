"""SQLite store for env lanes, base images and install history (`$OPENAJAR_HOME/env.db`, WAL)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from openajar.core.errors import LaneExists, LaneNotFound
from openajar.core.models import utcnow
from openajar.core.sqlite import SqliteStore
from openajar.env.models import EnvLane, ImageInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS lanes (
  id TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'cold',
  quarantine_reason TEXT,
  last_used TEXT,
  service_desired TEXT NOT NULL DEFAULT 'stopped',
  service_expires_at TEXT,
  manifest TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS images (
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  info TEXT NOT NULL,
  PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS installs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lane_id TEXT NOT NULL,
  source TEXT NOT NULL,
  packages TEXT NOT NULL,
  at TEXT NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT
);
"""


class EnvStore(SqliteStore):
    def __init__(self, path: Path):
        super().__init__(path, timeout=5.0)
        self._conn().executescript(SCHEMA)
        path.chmod(0o600)

    # ---- lanes ----
    def add_lane(self, lane: EnvLane) -> None:
        try:
            self._q("INSERT INTO lanes (id, spec) VALUES (?, ?)", (lane.id, lane.model_dump_json()))
        except sqlite3.IntegrityError as e:
            raise LaneExists(f"lane {lane.id} already exists") from e

    def get_lane(self, lane_id: str) -> EnvLane:
        rows = self._q("SELECT spec FROM lanes WHERE id=?", (lane_id,))
        if not rows:
            raise LaneNotFound(f"no terminal or service lane {lane_id}")
        return EnvLane.model_validate_json(rows[0][0])

    def put_lane(self, lane: EnvLane) -> None:
        """A lane's spec, replaced (a VM lane whose machine came back on another address or port)."""
        self._q("UPDATE lanes SET spec=? WHERE id=?", (lane.model_dump_json(), lane.id))

    def has_lane(self, lane_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM lanes WHERE id=?", (lane_id,)))

    def list_lanes(self) -> list[EnvLane]:
        return [EnvLane.model_validate_json(r[0]) for r in self._q("SELECT spec FROM lanes ORDER BY id")]

    def remove_lane(self, lane_id: str) -> None:
        self._q("DELETE FROM lanes WHERE id=?", (lane_id,))

    def set_state(self, lane_id: str, state: str, reason: str | None = None) -> None:
        self._q("UPDATE lanes SET state=?, quarantine_reason=? WHERE id=?", (state, reason, lane_id))

    def get_state(self, lane_id: str) -> tuple[str, str | None]:
        rows = self._q("SELECT state, quarantine_reason FROM lanes WHERE id=?", (lane_id,))
        return (rows[0][0], rows[0][1]) if rows else ("cold", None)

    def touch(self, lane_id: str, at: datetime | None = None) -> None:
        self._q("UPDATE lanes SET last_used=? WHERE id=?", ((at or utcnow()).isoformat(), lane_id))

    def last_used(self, lane_id: str) -> datetime | None:
        rows = self._q("SELECT last_used FROM lanes WHERE id=?", (lane_id,))
        return datetime.fromisoformat(rows[0][0]) if rows and rows[0][0] else None

    def set_service_desired(self, lane_id: str, desired: str, expires_at: datetime | None = None) -> None:
        self._q("UPDATE lanes SET service_desired=?, service_expires_at=? WHERE id=?",
                (desired, expires_at.isoformat() if expires_at else None, lane_id))

    def service_desired(self, lane_id: str) -> tuple[str, datetime | None]:
        rows = self._q("SELECT service_desired, service_expires_at FROM lanes WHERE id=?", (lane_id,))
        if not rows:
            return "stopped", None
        exp = datetime.fromisoformat(rows[0][1]) if rows[0][1] else None
        return rows[0][0], exp

    def set_manifest(self, lane_id: str, manifest: dict) -> None:
        self._q("UPDATE lanes SET manifest=? WHERE id=?", (json.dumps(manifest, sort_keys=True), lane_id))

    def manifest(self, lane_id: str) -> dict:
        rows = self._q("SELECT manifest FROM lanes WHERE id=?", (lane_id,))
        return json.loads(rows[0][0]) if rows else {}

    # ---- images ----
    def add_image(self, info: ImageInfo) -> None:
        self._q("INSERT INTO images (name, version, info) VALUES (?, ?, ?)",
                (info.name, info.version, info.model_dump_json()))

    def list_images(self, name: str | None = None) -> list[ImageInfo]:
        if name:
            rows = self._q("SELECT info FROM images WHERE name=? ORDER BY version", (name,))
        else:
            rows = self._q("SELECT info FROM images ORDER BY name, version")
        return [ImageInfo.model_validate_json(r[0]) for r in rows]

    def latest_version(self, name: str) -> int:
        rows = self._q("SELECT MAX(version) FROM images WHERE name=?", (name,))
        return rows[0][0] or 0

    def get_image(self, name: str, version: int) -> ImageInfo | None:
        rows = self._q("SELECT info FROM images WHERE name=? AND version=?", (name, version))
        return ImageInfo.model_validate_json(rows[0][0]) if rows else None

    # ---- installs ----
    def record_install(self, lane_id: str, source: str, packages: list[str], ok: bool, detail: str = "") -> None:
        self._q("INSERT INTO installs (lane_id, source, packages, at, ok, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (lane_id, source, json.dumps(packages), utcnow().isoformat(), int(ok), detail[:2000]))

    def installs(self, lane_id: str) -> list[dict]:
        rows = self._q("SELECT source, packages, at, ok FROM installs WHERE lane_id=? ORDER BY id", (lane_id,))
        return [{"source": r[0], "packages": json.loads(r[1]), "at": r[2], "ok": bool(r[3])} for r in rows]
