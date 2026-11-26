"""Environment families across a grant's replacement: what lets this computer's agent keep its computers.

Environments, saves and title-bound logins belong to a grant family (openajar.environments.caller): the
grant's root id. A grant's expiry is signed into its root block, so it cannot be extended, and the local
agent grant (`openajar local start`, `openajar setup agents`: openajar.local.register) is replaced when it
runs out, is revoked or frozen, or its default terms change. Before 0.8.0 each replacement started a
family of its own, and the agent lost every saved and open computer (audit S7-01).

Now the replacement continues the family of the grant it replaces, across an expiry, openajar's own renewal
or replacement, and new default terms: `issue(continues=<id>)` stores that family in the new root's registry
row (`grants.env_family`), and `resolve` puts it on the Grant (`Grant.env_family`), which environments read
instead of the root id. The token's signed bytes are unchanged, and so is every check of who asked
(self-approval, a task's owner: root_id); the new family's taint ledger starts with what the old one read
(it reaches the same computers). Only the same subject continues a root grant issued by this node
(continued_family); `openajar local` is the one caller, and no API route takes it, so a remote client's grant
(openajar connect) never inherits a family. After the owner revoked the grant, or a freeze did, it is not
continued: the new grant starts a family of its own, and the owner gives the earlier computers to it with
`openajar environments adopt` after a look (openajar.local.register). Each continuation is a row of the
action log (`grant_family`).

The rest of this module finds the families 0.8.0 and before left behind: the root grants `openajar local`
issued here (LOCAL_AGENT_NOTE), and, for the current one, its subject's earlier grants that are revoked or
expired and whose family no live grant has. `openajar environments adopt` gives those computers to the
current grant (openajar.environments.adopt), and an agent that opens such a title is told so.
"""

from __future__ import annotations

import json
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.policy.grants import Grant, Subject
from openajar.policy.util import iso

# what `openajar local` writes into the agent grant it issues for this computer (openajar.local.register):
# how a grant of this computer's own agent is told from one `openajar connect` issued over the API
LOCAL_AGENT_NOTE = "openajar local: Claude Code and Codex on this computer"


class FamiliesMixin:
    """For PolicyService (its db, clock and _revoked_map)."""

    db: Any
    clock: Any

    def continued_family(self, grant_id: str, subject: Subject) -> str:
        """The family a new root grant for `subject` takes from `grant_id`, the grant it replaces.
        InvalidRequest unless that is a root agent grant this node issued for the same subject."""
        row = self.db.one("SELECT id, parent_id, source, subject, env_family FROM grants WHERE id = ?", (grant_id,))
        if row is None:
            raise InvalidRequest(f"there is no grant {grant_id} on this node to continue")
        if row["parent_id"] is not None:
            raise InvalidRequest(f"grant {grant_id} was narrowed from another one: only a root grant's family "
                                 "carries on")
        if row["source"] != "issued":
            raise InvalidRequest(f"grant {grant_id} was not issued by this node, so its family does not carry on")
        was = json.loads(row["subject"])
        same = (was.get("kind") == "agent" and subject.kind == "agent" and was.get("id") == subject.id
                and (was.get("owner") or None) == (subject.owner or None))
        if not same:
            raise InvalidRequest(f"grant {grant_id} was issued for another subject ({was.get('id')}), so its family "
                                 f"does not carry on to {subject.id}")
        return str(row["env_family"] or row["id"])

    def local_grants(self) -> list[dict]:
        """Every agent grant `openajar local` issued on this node, newest first: {id, subject, owner, family,
        state (active, revoked or expired), issued_at, ended_at}."""
        rows = self.db.all("SELECT id, subject, block, issued_at, expires_at, env_family FROM grants "
                           "WHERE parent_id IS NULL AND source = 'issued' ORDER BY issued_at DESC, rowid DESC")
        revoked: dict[str, float] = self._revoked_map()  # type: ignore[attr-defined]
        now = self.clock()
        out = []
        for r in rows:
            try:
                block, sub = json.loads(r["block"]), json.loads(r["subject"])
            except ValueError:
                continue
            if block.get("note") != LOCAL_AGENT_NOTE or sub.get("kind") != "agent":
                continue
            ended = revoked.get(r["id"])
            state = "revoked" if ended is not None else "active"
            if ended is None and r["expires_at"] is not None and r["expires_at"] < now:
                state, ended = "expired", r["expires_at"]
            out.append({"id": r["id"], "subject": sub.get("id"), "owner": sub.get("owner"),
                        "family": r["env_family"] or r["id"], "state": state, "issued_at": iso(r["issued_at"]),
                        "ended_at": iso(ended)})
        return out

    def local_agent_grant(self) -> dict | None:
        """This computer's agent grant now: the newest live one `openajar local` issued (local_grants)."""
        return next((g for g in self.local_grants() if g["state"] == "active"), None)

    def revoked_reason(self, grant_id: str) -> str | None:
        """The reason stored with the revoke that ended this grant (its own, or one of a grant it was narrowed
        from); None when it is not revoked, "" when the revoke gave none."""
        row = self.db.one("SELECT path FROM grants WHERE id = ?", (grant_id,))
        if row is None:
            return None
        for gid in [p for p in str(row["path"]).split("/") if p]:
            hit = self.db.one("SELECT reason FROM revoked WHERE grant_id = ?", (gid,))
            if hit is not None:
                return str(hit["reason"] or "")
        return None

    def earlier_families(self, subject: str, family: str, *, owner: str | None = None) -> list[dict]:
        """The families this computer's agent `subject` had under earlier grants and no longer reaches: those of
        its local grants that are revoked or expired, other than `family` and any a live one of them has. Newest
        first, one entry per family: {family, grants, state, ended_at}."""
        mine = [g for g in self.local_grants() if g["subject"] == subject and (g["owner"] or None) == (owner or None)]
        reached = {family} | {g["family"] for g in mine if g["state"] == "active"}
        out: dict[str, dict] = {}
        for g in mine:
            if g["family"] in reached:
                continue
            e = out.setdefault(g["family"], {"family": g["family"], "grants": [], "state": g["state"],
                                             "ended_at": g["ended_at"]})
            e["grants"].append(g["id"])
        return list(out.values())

    def earlier_families_of(self, grant: Any) -> list[dict]:
        """earlier_families for a call's grant, when it is a local agent grant of this node (else none: a grant
        `openajar connect` issued, another node's, a sub-agent's root is its own)."""
        if not isinstance(grant, Grant) or grant.owner_token or "issuer" in grant.extra:
            return []
        root = grant.blocks[0]
        if root.note != LOCAL_AGENT_NOTE or root.sub is None or root.sub.kind != "agent":
            return []
        return self.earlier_families(root.sub.id, grant.env_family, owner=root.sub.owner)

    def others_left(self, subject: str) -> list[dict]:
        """Families of local grants issued for another subject (a computer renamed, another agent), revoked or
        expired, which `openajar environments adopt` leaves alone: {family, grants, subject}."""
        grants = self.local_grants()
        live = {g["family"] for g in grants if g["state"] == "active"}
        out: dict[str, dict] = {}
        for g in grants:
            if g["subject"] == subject or g["state"] == "active" or g["family"] in live:
                continue
            out.setdefault(g["family"], {"family": g["family"], "grants": [], "subject": g["subject"]})["grants"].append(
                g["id"])
        return list(out.values())

    def live_family(self, family: str) -> dict | None:
        """The newest live root grant whose environments are `family`'s ({id, subject}), or None: then no agent
        reaches that family's computers."""
        rows = self.db.all("SELECT id, subject, expires_at FROM grants WHERE parent_id IS NULL AND "
                           "COALESCE(env_family, id) = ? ORDER BY issued_at DESC, rowid DESC", (family,))
        revoked = self._revoked_map()  # type: ignore[attr-defined]
        now = self.clock()
        for r in rows:
            if r["id"] in revoked or (r["expires_at"] is not None and r["expires_at"] < now):
                continue
            return {"id": r["id"], "subject": json.loads(r["subject"]).get("id")}
        return None
