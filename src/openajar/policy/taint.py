"""Taint: what a grant family has read, and where its untrusted content came from.

The ledger (TaintLedger, tables `taint`, `taint_origins` and `taint_turns` in policy.db) and the
PolicyService methods that start, inherit, clear and show it (a mixin of openajar.policy.service).
The authorization order reads it in two steps there: the lethal-trifecta overlay (step 7, the
family's live labels) and the untrusted-content downgrade (step 7b, the call's context's origins).
Kept apart from the decision so the decision reads as the numbered order, and the ledger as one
store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import Principal
from openajar.policy.approval_models import follows_words
from openajar.policy.errors import Denied
from openajar.policy.grants import OWNER_GRANT_ID, Grant
from openajar.policy.labels import DEAD_FAMILIES, EXPIRY_GRACE_S, EverLedger  # memory: the `ever` label view
from openajar.policy.store import DB
from openajar.policy.traces import TraceEvent
from openajar.policy.util import iso

TAINT_LABELS = {"untrusted", "private", "owner"}
_ORIGIN_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_ORIGINS = 50  # per context; past it the oldest go (the downgrade needs only one)


def family_key(g: Grant) -> str:
    """The taint ledger key of a grant family: its root grant. Every grant narrowed from one root
    shares one ledger, so reconnecting, a new MCP session or a child grant never starts clean,
    and a child inherits what its parent read (SEC-4)."""
    return f"family:{g.root_id}"


def turn_key(grant_id: str) -> str:
    """The taint context of a turn token (PolicyService.mint_turn_token) and the grants under it."""
    return f"turn:{grant_id}"


def origin_kind(kind: str | None, source: str | None) -> str:
    """A safe origin kind: the one given, else read from the source (`browser_snapshot` is a web page)."""
    if kind and _ORIGIN_KIND_RE.match(kind):
        return kind
    src = (source or "").lower()
    if src.startswith(("browser_", "web")):
        return "web_page"
    if src.startswith("memory"):
        return "memory_note"
    if src.startswith("connector"):
        return "connector_output"
    if src.startswith("event"):
        return "event"
    return "untrusted_content"


class TaintLedger:
    """Per grant family labels (see family_key): untrusted (web, terminal, model output), private
    (lane data, secrets used), owner (input from the owner). Persisted; a label expires `ttl_s`
    after it was last added (policy.session_ttl_s, 12 h by default).

    Untrusted origins (table `taint_origins`): where each context's untrusted content came from,
    kind and host, never the text. A context is the family (`family:<root>`), or a turn
    (`turn:<grant id>`, mint_turn_token) and the grants narrowed under it. Origins do not expire on a
    clock: they end with the context (its root grant or turn grant expires or is revoked) or when the
    owner clears them in person. Every origin lands in the family's context too, so a turn's reads
    taint the family it runs in; the family's earlier reads never reach a new turn."""

    def __init__(self, db: DB, *, ttl_s: int, clock: Callable[[], float]):
        self.db = db
        self.ttl_s = ttl_s
        self.clock = clock
        self.ever_ledger = EverLedger(db, clock=clock)  # memory: labels kept for the root grant's life

    def add(self, session: str, labels: Iterable[str], source: str | None = None, *, context: str | None = None,
            kind: str | None = None, host: str | None = None) -> None:
        """Label the family `session`. An untrusted label also records its origin (`kind`, `host`)
        in the family's context and in `context` (a turn) when it is another one."""
        now = self.clock()
        labels = list(labels)
        self.ever_ledger.add(session, labels, source)  # memory: every add also lands in `ever`
        for label in labels:
            if label not in TAINT_LABELS:
                continue
            self.db.execute("INSERT INTO taint (session, label, source, at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(session, label) DO UPDATE SET at = excluded.at, source = excluded.source",
                            (session, label, (source or "")[:200], now))
        if "untrusted" in labels:
            for ctx_key in dict.fromkeys([session, context or session]):
                self.add_origin(ctx_key, session, kind=origin_kind(kind, source), host=host, source=source)

    def add_origin(self, context: str, family: str, *, kind: str, host: str | None = None,
                   source: str | None = None) -> None:
        now = self.clock()
        host = (host or "").lower()[:253]
        with self.db.tx() as conn:
            conn.execute("INSERT INTO taint_origins (context, family, kind, host, source, first_at, last_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(context, kind, host) DO UPDATE SET "
                         "last_at = excluded.last_at, source = excluded.source",
                         (context, family, kind, host, (source or "")[:200], now, now))
            conn.execute("DELETE FROM taint_origins WHERE context = ? AND rowid NOT IN (SELECT rowid FROM "
                         "taint_origins WHERE context = ? ORDER BY last_at DESC LIMIT ?)",
                         (context, context, MAX_ORIGINS))

    def origins(self, context: str) -> list[dict]:
        """The untrusted origins of a context, newest first: [{kind, host, source, since, last}]."""
        rows = self.db.all("SELECT kind, host, source, first_at, last_at FROM taint_origins WHERE context = ? "
                           "ORDER BY last_at DESC, kind, host", (context,))
        return [{"kind": r["kind"], "host": r["host"] or None, "source": r["source"] or None,
                 "since": r["first_at"], "last": r["last_at"]} for r in rows]

    def family_origins(self, family: str) -> list[dict]:
        """The untrusted origins of a family and every turn in it, one per kind and host, newest first."""
        rows = self.db.all("SELECT kind, host, MAX(source) AS source, MIN(first_at) AS first_at, MAX(last_at) AS "
                           "last_at FROM taint_origins WHERE family = ? GROUP BY kind, host "
                           "ORDER BY last_at DESC, kind, host", (family,))
        return [{"kind": r["kind"], "host": r["host"] or None, "source": r["source"] or None,
                 "since": r["first_at"], "last": r["last_at"]} for r in rows]

    def context_of(self, g: Any) -> str:
        """The taint context of a grant: the deepest turn in its chain, else its family."""
        chain = list(getattr(g, "chain_ids", []) or [])
        if len(chain) > 0 and not getattr(g, "owner_token", False):
            rows = self.db.all(f"SELECT grant_id FROM taint_turns WHERE grant_id IN ({','.join('?' * len(chain))})",
                               chain)
            turns = {r["grant_id"] for r in rows}
            for gid in reversed(chain):
                if gid in turns:
                    return turn_key(gid)
        return family_key(g)

    def start_turn(self, grant_id: str, family: str, seed: Iterable[str] = ()) -> None:
        self.db.execute("INSERT INTO taint_turns (grant_id, family, at, seed) VALUES (?, ?, ?, ?)",
                        (grant_id, family, self.clock(), json.dumps(sorted(set(seed)))))

    def inherit(self, src_family: str, src_context: str, dst_family: str, source: str) -> None:
        """A fresh root narrowed from the owner token starts with what the owner token's family holds:
        its live labels, its `ever` labels and its context's untrusted origins."""
        with self.db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO taint (session, label, source, at) SELECT ?, label, source, at "
                         "FROM taint WHERE session = ?", (dst_family, src_family))
            conn.execute("INSERT OR IGNORE INTO taint_origins (context, family, kind, host, source, first_at, "
                         "last_at) SELECT ?, ?, kind, host, source, first_at, last_at FROM taint_origins "
                         "WHERE context = ?", (dst_family, dst_family, src_context))
        self.ever_ledger.add(dst_family, self.ever(src_family), source)

    def clear_family(self, family: str) -> int:
        """The owner in person: forget the family's live labels and every origin of the family and its
        turns. The `ever` view stays (memory trust keeps what was read)."""
        with self.db.tx() as conn:
            conn.execute("DELETE FROM taint WHERE session = ?", (family,))
            return conn.execute("DELETE FROM taint_origins WHERE family = ?", (family,)).rowcount

    def get(self, session: str) -> set[str]:
        rows = self.db.all("SELECT label FROM taint WHERE session = ? AND at > ?",
                           (session, self.clock() - self.ttl_s))
        return {r["label"] for r in rows}

    def ever(self, session: str) -> set[str]:
        """memory: every label the family has seen since its root grant was issued (labels.py)."""
        return self.ever_ledger.get(session)

    def clear(self, session: str) -> None:
        self.db.execute("DELETE FROM taint WHERE session = ?", (session,))

    def purge(self) -> int:
        self.ever_ledger.purge()  # memory: drop `ever` rows of expired or revoked root grants
        cutoff = self.clock() - EXPIRY_GRACE_S
        # origins end with their context: the family's root grant, or the turn grant
        self.db.execute(f"DELETE FROM taint_origins WHERE family IN ({DEAD_FAMILIES})", (cutoff,))
        dead_turns = ("SELECT t.grant_id FROM taint_turns t LEFT JOIN grants g ON g.id = t.grant_id WHERE g.id IS NULL "
                      "OR (g.expires_at IS NOT NULL AND g.expires_at <= ?) OR g.id IN (SELECT grant_id FROM revoked)")
        self.db.execute(f"DELETE FROM taint_origins WHERE context IN (SELECT 'turn:' || grant_id FROM taint_turns "
                        f"WHERE grant_id IN ({dead_turns}))", (cutoff,))
        self.db.execute(f"DELETE FROM taint_turns WHERE grant_id IN ({dead_turns})", (cutoff,))
        return self.db.execute("DELETE FROM taint WHERE at <= ?", (self.clock() - self.ttl_s,)).rowcount


class TaintMixin:
    """The taint methods of PolicyService (the attributes it reads, `taint`, `db`, `cfg`, `traces`,
    are set by PolicyService.__init__)."""

    def _inherit_taint(self, parent: Grant, child: Grant) -> None:
        """What `parent`'s family read goes to `child`'s: a grant narrowed from it, or a local agent grant that
        continues its family (openajar.policy.families), which reaches the same computers."""
        if family_key(child) != family_key(parent):
            how = "continues" if len(child.chain_ids) == 1 else "narrowed from"
            self.taint.inherit(family_key(parent), self.taint.context_of(parent), family_key(child),
                               source=f"{how} {parent.root_id}")

    def mint_turn_token(self, token: str, *, seed: Iterable[str] = (), origin: dict | None = None,
                        by: str = "agents", **narrow: Any) -> tuple[Grant, str]:
        """A turn token: a grant narrowed from `token` (attenuate's fields in `narrow`) that starts a
        clean taint context. Node services call it (the agents service, once per turn); no tool or API
        does, so a holder cannot clear its own context. `seed` is what the turn starts with: the taint
        of the events that woke it and of the CLI session it continues; for an untrusted seed,
        `origin` ({kind, host}) says where it came from. Grants narrowed from the turn token share
        its context; everything the turn reads also taints the family it runs in."""
        seed = set(seed)
        bad = seed - TAINT_LABELS
        if bad:
            raise InvalidRequest(f"unknown taint labels: {sorted(bad)}")
        g, tok = self.attenuate(token, by=by, **narrow)
        self.taint.start_turn(g.id, family_key(g), seed)
        if seed:
            o = origin or {}
            self.taint.add(family_key(g), seed, f"turn {g.id}", context=turn_key(g.id),
                           kind=o.get("kind") or "turn_seed", host=o.get("host"))
        return g, tok

    def clear_taint(self, grant_id: str, *, by: str = "owner") -> dict:
        """The owner in person (the CLI on the node, the signed-in panel) starts the grant's family
        clean: its live labels and every untrusted origin of the family and its turns go. The `ever`
        view stays, so memory still knows what the family read. Never reachable by an agent."""
        if grant_id == OWNER_GRANT_ID:
            fam = family_key(self.owner_grant())
        else:
            row = self.db.one("SELECT root_id FROM grants WHERE id = ?", (grant_id,))
            if row is None:
                raise NotFound(f"no grant {grant_id}")
            fam = f"family:{row['root_id']}"
        before = self.taint.family_origins(fam)
        removed = self.taint.clear_family(fam)
        self.traces.write(TraceEvent(kind="taint_cleared", status="ok", principal=by, principal_kind="human",
                                     grant_id=grant_id, summary=f"cleared {fam} in person",
                                     args={"origins": removed, "after_untrusted": follows_words(before)}))
        return {"grant_id": grant_id, "family": fam, "origins_cleared": removed}

    def taint_for(self, principal: Principal | dict | None) -> set[str]:
        """Every live taint label of the principal's grant family (see family_key)."""
        gid = principal.get("grant_id") if isinstance(principal, dict) else getattr(principal, "grant_id", None)
        if not gid or not isinstance(gid, str):
            return set()
        if gid == OWNER_GRANT_ID:
            return self.taint.get(family_key(self.owner_grant()))
        row = self.db.one("SELECT root_id FROM grants WHERE id = ?", (gid,))
        return self.taint.get(f"family:{row['root_id']}") if row is not None else set()

    def lane_taint(self, lane_id: str, *, roots: Iterable[str] | None = None) -> dict:
        """The taint state of every live grant family that reaches `lane_id` (taint lives on grant
        families, not lanes): who read untrusted content, from where (the family's own context and
        every turn token's in it), and what waits for a person. `roots`: only these families (the
        caller's own, for anyone but the owner); None: all of them."""
        families: dict[str, dict] = {}
        only = set(roots) if roots is not None else None
        rows = [{"id": OWNER_GRANT_ID, "root_id": OWNER_GRANT_ID}] + self.list_grants()
        for r in rows:
            root = r["root_id"]
            if only is not None and root not in only:
                continue
            try:
                g = self.owner_grant() if r["id"] == OWNER_GRANT_ID else self.grant_by_id(r["id"])
            except Denied:
                continue
            if g.subject.kind == "human" or not g.lane_allowed(lane_id):
                continue
            fam = families.setdefault(root, {"grant_id": root, "grants": []})
            fam["grants"].append({"id": g.id, "label": self._grant_label(g)})
            if "state" not in fam:
                fam["state"] = self.taint_state(g, context=family_key(g), whole_family=True)
        out = []
        for fam in families.values():
            st = fam.pop("state")
            if st["untrusted"] or st["labels"]:
                out.append({**fam, **st})
        out.sort(key=lambda f: f["since"] or "", reverse=True)
        return {"lane": lane_id, "tainted": any(f["untrusted"] for f in out), "families": out,
                "waits_for_a_person": list(self.cfg.taint.downgrade)}

    def taint_state(self, g: Grant, *, context: str | None = None, whole_family: bool = False) -> dict:
        """What grant_info and `openajar grants taint` show: the context, its untrusted origins (kind and
        host), the family's live labels, the effects that wait for a person now, and what clears it.
        `whole_family`: the origins of the family and of every turn in it (a lane's taint)."""
        ctx_key = context or self.taint.context_of(g)
        found = self.taint.family_origins(family_key(g)) if whole_family else self.taint.origins(ctx_key)
        waits = list(self.cfg.taint.downgrade) if found else []
        return {
            "context": "turn" if ctx_key.startswith("turn:") else "family",
            "labels": sorted(self.taint.get(family_key(g))),
            "untrusted": bool(found),
            "since": iso(min(o["since"] for o in found)) if found else None,
            "follows": [{"kind": o["kind"], "host": o["host"], "last": iso(o["last"])} for o in found[:10]],
            "after_untrusted": follows_words(found),
            "waits_for_a_person": waits,
            "clean_context": "a new turn token, a fresh grant, or the owner clearing it in person "
                             "(`openajar grants taint <grant> --clear`)",
        }
