"""SQLite store for environments (`$OPENAJAR_HOME/environments.db`, WAL). A closed environment's
record stays (what it cost, what it pushed, why it closed) until the owner prunes it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from openajar.core.errors import NotFound
from openajar.core.sqlite import SqliteStore
from openajar.environments.models import OPEN_STATES, Environment

SCHEMA = """
CREATE TABLE IF NOT EXISTS environments (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  binding TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS environments_state ON environments (state);
CREATE INDEX IF NOT EXISTS environments_binding ON environments (binding);
CREATE TABLE IF NOT EXISTS repo_requests (
  approval_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class EnvironmentStore(SqliteStore):
    """On openajar.core.sqlite: a kept connection per thread, WAL, a 5 s busy timeout as before."""

    def __init__(self, path: Path):
        super().__init__(path, timeout=5.0)
        self._conn().executescript(SCHEMA)
        cols = {r[1] for r in self._q("PRAGMA table_info(repo_requests)")}
        if "grant_id" not in cols:  # who asked: the agent may withdraw a card it raised (connect.withdraw)
            self._q("ALTER TABLE repo_requests ADD COLUMN grant_id TEXT NOT NULL DEFAULT ''")
        self.path.chmod(0o600)  # an existing file too, as the store always did

    def put(self, env: Environment) -> None:
        self._q("INSERT OR REPLACE INTO environments (id, state, binding, grant_id, title, created_at, record) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (env.id, env.state, env.binding, env.grant_id, env.title, env.created_at.isoformat(),
                 env.model_dump_json()))

    def update(self, env_id: str, change: Callable[[Environment], bool | None]) -> Environment:
        """Read, change and write one record in one write transaction, so agentosd and the CLI (two
        processes) never lose each other's writes. `change` edits the record in place; returning
        False leaves it as it was. Returns the record as stored."""
        with self._tx() as db:
            rows = db.execute("SELECT record FROM environments WHERE id = ?", (env_id,)).fetchall()
            if not rows:
                raise NotFound(f"no environment {env_id}")
            env = Environment.model_validate_json(rows[0][0])
            if change(env) is not False:
                db.execute("UPDATE environments SET state = ?, binding = ?, record = ? WHERE id = ?",
                           (env.state, env.binding, env.model_dump_json(), env.id))
                return env
            return Environment.model_validate_json(rows[0][0])

    def adopt(self, moves: dict[str, str], *, sources: set[str], family: str) -> list[Environment]:
        """Give open environments to another grant family in one write transaction
        (openajar.environments.adopt): `moves` maps each id to its title there. Each is let go (binding
        "kept:<id>", no lease), so the title takes it back at once; one no longer open or no longer in
        `sources` is left as it is. Returns the ones moved."""
        out = []
        marks = ",".join("?" for _ in OPEN_STATES)
        with self._tx() as db:
            for env_id, title in moves.items():
                rows = db.execute(f"SELECT record FROM environments WHERE id = ? AND state IN ({marks})",
                                  (env_id, *OPEN_STATES)).fetchall()
                if not rows:
                    continue
                env = Environment.model_validate_json(rows[0][0])
                if env.family not in sources:
                    continue
                env.grant_root, env.title = family, title
                env.binding, env.lease = f"kept:{env.id}", None
                db.execute("UPDATE environments SET binding = ?, title = ?, record = ? WHERE id = ?",
                           (env.binding, env.title, env.model_dump_json(), env.id))
                out.append(env)
        return out

    def has(self, env_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM environments WHERE id = ?", (env_id,)))

    def get(self, env_id: str) -> Environment:
        rows = self._q("SELECT record FROM environments WHERE id = ?", (env_id,))
        if not rows:
            raise NotFound(f"no environment {env_id}")
        return Environment.model_validate_json(rows[0][0])

    def open(self) -> list[Environment]:
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self._q(f"SELECT record FROM environments WHERE state IN ({marks}) ORDER BY created_at", OPEN_STATES)
        return [Environment.model_validate_json(r[0]) for r in rows]

    def bound(self, binding: str) -> Environment | None:
        """The open environment bound to `binding` that it opened or attached last (a session may hold
        several: each title names its own)."""
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self._q(f"SELECT record FROM environments WHERE binding = ? AND state IN ({marks}) "
                       "ORDER BY created_at DESC", (binding, *OPEN_STATES))
        envs = [Environment.model_validate_json(r[0]) for r in rows]
        return max(envs, key=lambda e: e.bound_at or e.created_at) if envs else None

    def recent(self, limit: int = 100) -> list[Environment]:
        rows = self._q("SELECT record FROM environments ORDER BY created_at DESC LIMIT ?", (limit,))
        return [Environment.model_validate_json(r[0]) for r in rows]

    # ---- the cards that ask a person to connect a repository (openajar.environments.connect) ----
    def request_put(self, approval_id: str, repo: str, reason: str, created_at: str, grant_id: str = "") -> None:
        self._q("INSERT OR REPLACE INTO repo_requests (approval_id, repo, reason, created_at, grant_id) "
                "VALUES (?, ?, ?, ?, ?)", (approval_id, repo, reason, created_at, grant_id))

    def request_shared(self, approval_id: str) -> None:
        """Another agent asked for the same repository: the card is no longer one agent's to withdraw."""
        self._q("UPDATE repo_requests SET grant_id = '*' WHERE approval_id = ?", (approval_id,))

    def requests(self) -> list[dict[str, str]]:
        rows = self._q("SELECT approval_id, repo, reason, created_at, grant_id FROM repo_requests ORDER BY created_at")
        return [{"id": r[0], "repo": r[1], "reason": r[2], "created_at": r[3], "grant_id": r[4]} for r in rows]

    def request_drop(self, approval_id: str) -> None:
        self._q("DELETE FROM repo_requests WHERE approval_id = ?", (approval_id,))
