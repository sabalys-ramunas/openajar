"""The policy half of a freeze (openajar.freeze): revoke the grants whose whole reach is the frozen scope.

A node (or mesh) freeze revokes every agent grant, one with no lane scope too. A lane or workspace
freeze revokes only the grants that cannot reach anything outside it: a chain is inside the scope
when one of its blocks names only lanes in it (the lane itself for a lane freeze, the workspace or
its lanes for a workspace freeze). A wider grant (a workspace grant during a lane freeze, `*`) is
kept: it keeps working elsewhere, and the freeze gate (openajar.freeze, `check_call`) refuses every
call it makes inside the frozen scope until the owner unfreezes. A person's grant (a member, the
owner) is never revoked: people keep taking over and looking. Revoking a grant revokes everything
narrowed from it (child grants, a brain's task grants, turn grants), so those are listed as
`cascades_to`. Standing approvals that could cover the scope, or that belong to a revoked grant,
are revoked too.

Grants a trusted controller issued are not in the local grant table. Each chain the node admitted
is remembered by openajar.policy.issuers, and the highest block of it whose reach is inside the
scope is revoked there (a revocation by block id ends every token narrowed from it). A root seen
before chains were remembered is revoked whatever the scope, toward frozen.

Nothing is ever restored by an unfreeze: a revoked agent must be granted again, and the grant `openajar local`
issues it then starts without the revoked one's computers (saves, open environments, title-bound logins) until
the owner gives them with `openajar environments adopt` (openajar.policy.families).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openajar.core import globs
from openajar.policy.errors import Conflict
from openajar.policy.grants import GrantBlock

EXEMPT_TOOLS = frozenset({"browser_freeze", "browser_control", "whoami"})  # callable on a frozen lane

log = logging.getLogger("openajar.policy")

# How a freeze's revocations start their stored reason ("freeze of lane biz-a/social: frozen by owner:
# ..."), so a later call with such a grant is told it was frozen (frozen_message), not to sign in.
FREEZE_MARK = "freeze of "


def revoke_reason(reach: Reach, why: str) -> str:
    """The reason a freeze stores on each grant it revokes."""
    if reach.everything:
        what = "the whole node"
    elif reach.lanes:
        what = ("lane " if len(reach.lanes) == 1 else "lanes ") + ", ".join(reach.lanes)
    else:
        what = ("workspace " if len(reach.workspaces) == 1 else "workspaces ") + ", ".join(reach.workspaces)
    return f"{FREEZE_MARK}{what}: {why}"[:300]


def frozen_message(reason: str | None) -> str | None:
    """What an agent whose grant a freeze revoked reads, from the stored reason; None for a
    revocation that was not a freeze's."""
    if not reason or not reason.startswith(FREEZE_MARK):
        return None
    what, _, why = reason[len(FREEZE_MARK):].partition(": ")
    why = f" ({why})" if why else ""
    return (f"Your grant was revoked by a freeze of {what}{why}, so this node takes no calls from it. Stop and "
            "tell the user. Only the owner unfreezes, and after that they must give you a new grant: do not "
            "look for another token.")


class Reach:
    """A frozen scope, as lane and workspace sets (or everything)."""

    def __init__(self, *, lanes: Iterable[str] = (), workspaces: Iterable[str] = (), everything: bool = False):
        self.lanes = sorted(set(lanes))
        self.workspaces = sorted(set(workspaces))
        self.everything = everything

    def pattern(self, pattern: str) -> bool:
        """Could a lane pattern match a lane in this scope?"""
        if self.everything:
            return True
        ws = pattern.split("/", 1)[0] if "/" in pattern else pattern
        if any(_safe(globs.segment_match, ws, w) for w in self.workspaces):
            return True
        return any(_safe(globs.lane_match, pattern, lane) for lane in self.lanes)

    def workspace(self, pattern: str) -> bool:
        """Could a workspace pattern cover a lane in this scope?"""
        if self.everything:
            return True
        return any(_safe(globs.segment_match, pattern, w) for w in self.workspaces) or \
            any(_safe(globs.segment_match, pattern, lane.split("/", 1)[0]) for lane in self.lanes)

    def grant(self, blocks: list[GrantBlock]) -> bool:
        return self.patterns([b.lane_patterns(authority=i == 0) for i, b in enumerate(blocks)])

    def within(self, pattern: str) -> bool:
        """Can a lane pattern match only lanes in this scope?"""
        if self.everything:
            return True
        return any(_inside(pattern, f"{w}/*") for w in self.workspaces) or \
            any(_inside(pattern, lane) for lane in self.lanes)

    def contains(self, pats: list[str] | None) -> bool:
        """Is one block's reach (its lane patterns; None: not narrowed, []: no lane) inside this scope?"""
        if self.everything:
            return True
        return bool(pats) and all(self.within(p) for p in pats)

    def holds(self, blocks: list[GrantBlock]) -> bool:
        """Is a chain's whole reach inside this scope? A node freeze holds every chain. Otherwise one
        block whose lanes are all in the scope is enough: a chain reaches no more than any block of it."""
        if self.everything:
            return True
        return any(self.contains(b.lane_patterns(authority=i == 0)) for i, b in enumerate(blocks))

    def patterns(self, chain: list[list[str] | None]) -> bool:
        """Does a chain reach the scope, given each block's lane patterns (None: not narrowed)?"""
        if self.everything:
            return True  # a node freeze revokes every agent grant, one with no lane scope too
        for pats in chain:
            if pats is None:
                continue
            if not any(self.pattern(p) for p in pats):
                return False
        return True

    def describe(self) -> dict[str, Any]:
        return {"everything": self.everything, "workspaces": self.workspaces, "lanes": self.lanes}


def _safe(fn: Any, pattern: str, value: str) -> bool:
    try:
        return bool(fn(pattern, value))
    except Exception:  # noqa: BLE001  (a stored pattern this version cannot parse: assume it reaches)
        return True


def _inside(pattern: str, scope: str) -> bool:
    try:
        return bool(globs.lane_pattern_within(pattern, scope))
    except Exception:  # noqa: BLE001  (a pattern this version cannot parse: not provably inside; the gate still holds)
        return False


def revoke_reaching(policy: Any, reach: Reach, *, by: str, reason: str) -> dict[str, Any]:
    """Revoke, in one sweep, every live agent grant whose whole reach is inside `reach` (every one
    for a node freeze), and the standing approvals over it. Never raises for one grant: a failure is listed under `errors` (the freeze
    holds either way; the lane gate refuses agents whatever the grants say)."""
    out: dict[str, Any] = {"grants": [], "cascades_to": [], "standing": [], "errors": []}
    now = policy.clock()
    try:
        rows = policy.db.all("SELECT * FROM grants ORDER BY depth, issued_at")
        dead = set(policy._revoked_map())
    except Exception as exc:  # noqa: BLE001  (a broken database: report it, the freeze still holds)
        out["errors"].append(f"the grant list could not be read ({type(exc).__name__})")
        return out
    by_id = {r["id"]: r for r in rows}
    cascaded: set[str] = set()
    for row in rows:
        subject = _swept_subject(row, by_id, dead, reach, now)
        if subject is None:
            continue
        try:
            result = policy.revoke(row["id"], by=by, reason=reason)
        except Conflict:
            dead.add(row["id"])
            continue
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"grant {row['id']} could not be revoked ({type(exc).__name__})")
            continue
        dead.add(row["id"])
        out["grants"].append({"id": row["id"], "subject": f"{subject.get('kind', '?')}:{subject.get('id', '?')}",
                              "note": _note(row)})
        cascaded.update(result.get("cascades_to") or [])
    out["cascades_to"] = sorted(cascaded)
    _revoke_foreign(policy, reach, out, now)
    revoked_ids = {g["id"] for g in out["grants"]} | cascaded
    _revoke_standing_over(policy, reach, revoked_ids, out, by=by)
    with contextlib.suppress(Exception):
        policy.bus.publish("grant", {"state": "frozen", "grants": sorted(revoked_ids), "standing": out["standing"]})
    return out


def _swept_subject(row: Any, by_id: dict[str, Any], dead: set[str], reach: Reach, now: float) -> dict | None:
    """The subject of a grant row the sweep revokes, or None when it keeps it: already revoked, run
    out (a site key's is swept still), a person's, or reaching beyond the scope."""
    path = [p for p in row["path"].split("/") if p]
    if any(p in dead for p in path):
        return None
    try:
        subject = json.loads(row["subject"])
        root_kind = json.loads(by_id[path[0]]["subject"]).get("kind") if path and path[0] in by_id else None
    except (ValueError, KeyError, TypeError):
        subject, root_kind = {}, None
    # a site key's next call replaces a grant that ran out, so it must be swept too, or it outlives this
    if row["expires_at"] is not None and row["expires_at"] < now and not str(subject.get("id")).startswith("site-key:"):
        return None
    if root_kind == "human" or subject.get("kind") == "human":
        return None  # a person's grant: people keep taking over and looking
    try:
        blocks = [_block(by_id[p]) for p in path]
    except Exception:  # noqa: BLE001  (a chain this version cannot read: revoke it to be safe)
        blocks = None
    if blocks is not None and not reach.holds(blocks):
        return None  # it reaches beyond the scope: kept, and refused inside it by the freeze gate
    return subject


def _revoke_standing_over(policy: Any, reach: Reach, revoked_ids: set[str], out: dict[str, Any], *, by: str) -> None:
    """Revoke the standing approvals of a revoked grant, or over the frozen scope."""
    approvals = getattr(policy, "approvals", None)
    if approvals is None:
        return
    try:
        standing = approvals.list_standing(active_only=True)
    except Exception as exc:  # noqa: BLE001
        standing = []
        out["errors"].append(f"standing approvals could not be read ({type(exc).__name__})")
    for sa in standing:
        covers = (sa.grant_id in revoked_ids or any(reach.workspace(w) for w in sa.workspaces)
                  or any(reach.pattern(p) for p in sa.lanes))
        if not covers:
            continue
        try:
            approvals.revoke_standing(sa.id, by=by)
            out["standing"].append(sa.id)
        except Conflict:
            continue
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"standing approval {sa.id} could not be revoked ({type(exc).__name__})")


def _revoke_foreign(policy: Any, reach: Reach, out: dict[str, Any], now: float) -> None:
    """Revoke, through the trusted issuers, every foreign chain whose whole reach is inside the scope."""
    from openajar.policy.issuers import _issuers

    trusted = _issuers(policy)
    if trusted is None:
        return
    try:
        chains = trusted.chains()
        dead = set(trusted.revoked)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"the controller's grants could not be read ({type(exc).__name__})")
        return
    ids: list[str] = []
    for ch in chains:
        if ch["human"]:
            continue  # a person's grant: people keep taking over and looking
        blocks = ch["blocks"]
        if blocks is None:  # seen before this node remembered chains: revoke it, toward frozen
            target: dict | None = {"id": ch["root_id"]}
        else:
            try:
                ids_in = {str(b["id"]) for b in blocks} | {str(b["rid"]) for b in blocks if b.get("rid")}
                expired = any(b.get("exp") is not None and float(b["exp"]) < now for b in blocks)
                # the highest block whose reach is inside the scope: revoking it ends every token below it
                held = next((b for b in blocks if reach.contains(b.get("pats"))), None)
                target = None if held is None else (held if held.get("id") else {"id": ch["leaf_id"]})
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                ids_in, expired, target = set(), False, {"id": ch["leaf_id"]}  # a record this version cannot read
            if ids_in & dead or expired:
                continue
        if target is None or target["id"] in dead:
            continue
        ids += [str(target["id"])] + ([str(target["rid"])] if target.get("rid") else [])
        dead.add(str(target["id"]))
        out["grants"].append({"id": str(target["id"]), "subject": ch["subject"], "note": f"issued by {ch['issuer']}"})
    if ids:
        try:
            trusted.revoke(ids, issuer="freeze")
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"the controller's grants could not be revoked here ({type(exc).__name__})")


def _block(row: Any) -> GrantBlock:
    from openajar.policy.registry import _stored_block

    return _stored_block(row)


def _note(row: Any) -> str | None:
    try:
        note = json.loads(row["block"]).get("note")
    except (ValueError, TypeError):
        return None
    return str(note)[:120] if note else None


def check_new_grant(policy: Any, block: GrantBlock) -> None:
    """Refuse a new agent grant the sweep would have revoked (`frozen`): during a node freeze every
    one, during a lane or workspace freeze one whose whole reach is inside the frozen scope. Such a
    grant would start to work the moment the owner unfreezes, which the owner did not decide. A
    wider grant is made, and the freeze gate refuses its calls inside the scope, as it does for the
    wide grants the sweep kept. The freezes come from the running node's freeze service, or, for
    a policy opened without one (the node's CLI), from the freeze records on disk."""
    for rec in _freezes(policy):
        scope, target = getattr(rec, "scope", None), str(getattr(rec, "target", "") or "")
        if scope == "lane":
            reach = Reach(lanes=[target])
        elif scope == "workspace":
            reach = Reach(workspaces=[target])
        else:
            reach = Reach(everything=True)
        if not reach.holds([block]):
            continue
        from openajar.core.errors import Frozen

        where = "the whole mesh" if scope == "mesh" else ("this node" if scope == "node" else f"{scope} {target}")
        why = f" ({rec.reason})" if getattr(rec, "reason", None) else ""
        if scope in ("node", "mesh"):
            raise Frozen(f"{where} is frozen{why}, so no new agent grant is made: one made now would work as "
                         "soon as it is unfrozen. Unfreeze first, then grant agents again.")
        raise Frozen(f"{where} is frozen{why}, and this grant reaches only that, so it is not made: it would "
                     "work there as soon as it is unfrozen. Unfreeze first, or make a grant that also reaches "
                     "browsers or terminals outside it (it takes no calls in the frozen scope until the unfreeze).")


def _freezes(policy: Any) -> list[Any]:
    freeze = policy.runtime.get("freeze") if getattr(policy, "runtime", None) is not None else None
    if freeze is not None and callable(getattr(freeze, "records", None)):
        return list(freeze.records())
    home = getattr(policy, "home", None)
    if home is None or not (Path(home) / "freeze").is_dir():
        return []
    from openajar.policy.freeze_store import FreezeStore

    try:
        records, _error = FreezeStore(Path(home)).load()
    except Exception:  # a store this process cannot read: the daemon's sweep still holds
        log.warning("the freeze records could not be read; a new grant is not checked against them", exc_info=True)
        return []
    return list(records.values())


def check_child_lanes(policy: Any, lanes: Iterable[str]) -> None:
    """Refuse a new child grant for a frozen lane (mint_child_token)."""
    freeze = policy.runtime.get("freeze") if policy.runtime is not None else None
    if freeze is None:
        return
    for lane in lanes:
        freeze.check_call(lane)


def frozen_scope(policy: Any, ctx: Any, *, lane_id: str | None = None, workspace: str | None = None) -> Any:
    """The freeze record that holds an agent's call off this lane or workspace, noted on the context
    (`frozen_hits`), or None. A person in person is not held; nor is a tool the freeze exempts."""
    if getattr(ctx, "in_person", False):
        return None
    freeze = policy.runtime.get("freeze") if policy.runtime is not None else None
    if freeze is None or not callable(getattr(freeze, "frozen_for", None)):
        return None
    if getattr(ctx, "tool", None) in EXEMPT_TOOLS:
        return None
    ws = workspace if isinstance(workspace, str) and workspace else None
    lane = lane_id if isinstance(lane_id, str) and lane_id else None
    rec = freeze.frozen_for(lane, ws)
    if rec is not None:
        hits = getattr(ctx, "frozen_hits", None)
        if isinstance(hits, list) and len(hits) < 50:
            hits.append((lane or ws or "", rec))
    return rec


def frozen_note(freeze: Any, hits: list[tuple[str, Any]]) -> str:
    """What a listing says when a freeze left rows out: which scopes, and what to do."""
    seen: dict[str, Any] = {}
    for _what, rec in hits:
        where = "the whole mesh" if rec.scope == "mesh" else (
            "this node" if rec.scope == "node" else f"{rec.scope} {rec.target}")
        seen.setdefault(where, rec)
    names = ", ".join(seen)
    return (f"Left out because {names} {'is' if len(seen) == 1 else 'are'} frozen: what is in "
            f"{'it' if len(seen) == 1 else 'them'} is not shown or changed until the owner unfreezes. Do not "
            "try to reach it another way; tell your user.")


def check_call(policy: Any, ctx: Any, lane_id: str | None) -> None:
    """The freeze gate at authorization: an agent's call on a frozen scope is refused. A person in
    person (the panel) is not stopped; they take over and look."""
    if getattr(ctx, "in_person", False):
        return
    freeze = policy.runtime.get("freeze") if policy.runtime is not None else None
    if freeze is None:
        return
    freeze.check_call(lane_id, tool=getattr(ctx, "tool", None))
