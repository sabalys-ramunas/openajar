"""The grant registry: the node's signing keys, issuing and narrowing grants, resolving a
presented token to its grant, and revoking.

Part of PolicyService (openajar.policy.service), as a mixin: `resolve` and `check_live` are steps 1
and 2 of the authorization order there. Every grant this node signs or observes has a row in
policy.db's `grants` table, bound to the exact bytes of its block, and every revocation a row in
`revoked`; `check_live` reads both on every call, so a revoked or expired grant stops at its next
call. A member's grants live here too (`member_revocation` revokes them all at once), but are made by
openajar.policy.members.
"""

from __future__ import annotations

import contextlib
import contextvars
import hmac
import json
import logging
import re
from collections.abc import Iterable, Iterator
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import Effect, Principal, parse_lane_id
from openajar.policy import freeze as freeze_gate
from openajar.policy import issuers, memory_rules, tokens
from openajar.policy.ceiling import ControllerCeiling
from openajar.policy.errors import Conflict, Denied, GrantFrozen, TokenInvalid
from openajar.policy.explain import explain
from openajar.policy.grants import (
    BUDGET_LIMITS,
    LADDER,
    Budget,
    Grant,
    GrantBlock,
    LaneCap,
    Specials,
    Subject,
    _remaining_depth,
    blocks_from_token,
    child_subject,
    coerce_effect,
    owner_block,
    parse_block,
    subject_fields,
    subject_problems,
    widening_problems,
)
from openajar.policy.keys import DEFAULT_OVERLAP_S
from openajar.policy.members import MEMBER_CALL_MAX_TTL_S, MEMBER_ID_RE, member_revocation
from openajar.policy.util import iso, new_id

log = logging.getLogger("openajar.policy")


class _ChannelMark:
    """The grant token of the channel `invoke` being run (channel_call). A holder, not the token
    itself: a task created during the call copies the context, and so the holder, and channel_call
    empties it when the call ends, so that task no longer resolves the member's token."""

    __slots__ = ("token",)

    def __init__(self, token: str | None):
        self.token = token


# Only a token equal to the mark may be a controller's member call; every other path leaves it unset.
_channel_token: contextvars.ContextVar[_ChannelMark | None] = contextvars.ContextVar("openajar_channel_token",
                                                                                     default=None)


def bare_token(token: str | None) -> str:
    """A presented token as resolve reads it: no surrounding space, no `Bearer ` scheme."""
    token = str(token or "").strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    return token


# subject ids (exact, or a prefix ending in ":") only the node's own issuers give: `issue(_reserved=True)`
RESERVED_SUBJECTS = ("site-key:", "owner", "owner-child")


class GrantRegistryMixin:
    """Keys, grants and revocation of PolicyService (the attributes it reads, `db`, `cfg`, `ring`,
    `bus`, `clock`, are set by PolicyService.__init__)."""

    # ================================================================ keys and identity
    @property
    def key(self) -> Any:
        """The active grant signing key (it changes when the owner rotates it)."""
        return self.ring.active

    @property
    def public_key(self) -> Any:
        return self.ring.active.public_key()

    @property
    def kid(self) -> str:
        return self.ring.active_kid

    def _local_id(self) -> str:
        """This node's own issuer id, made once and kept in policy.db: `iss` of the grants it
        signs and an audience it accepts. Copying the key to another node does not copy it."""
        row = self.db.one("SELECT value FROM meta WHERE key = 'local_id'")
        if row is None:
            self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('local_id', ?)", (new_id("local"),))
            row = self.db.one("SELECT value FROM meta WHERE key = 'local_id'")
        return row["value"]

    @property
    def issuer(self) -> str:
        return f"node:{self.local_id}"

    def audiences(self) -> set[str]:
        """The `aud` values naming this node: its own issuer id, and once enrolled its controller
        node id and its org."""
        out = {self.issuer}
        cfg = self.node_config
        if cfg is not None and getattr(cfg, "node_id", ""):
            out.add(f"node:{cfg.node_id}")
        if cfg is not None and getattr(cfg, "org_id", ""):
            out.add(f"org:{cfg.org_id}")
        return out

    def rotate_key(self, *, overlap_s: int = DEFAULT_OVERLAP_S, by: str = "owner") -> dict:
        """Make a fresh grant key active. Grants signed with the old key keep working for
        `overlap_s` seconds, then fail with retired_key; version 1 grants stop at once."""
        v1 = self.db.one("SELECT COUNT(*) AS n FROM grants WHERE v = 1 AND (expires_at IS NULL OR expires_at > ?) "
                         "AND id NOT IN (SELECT grant_id FROM revoked)", (self.clock(),))["n"]
        out = self.ring.rotate(overlap_s=overlap_s)
        log.warning("grant key rotated by %s: %s replaces %s (accepted until %s)", by, out["kid"], out["previous"],
                    iso(out["retire_at"]))
        self.bus.publish("grant", {"state": "key_rotated", "kid": out["kid"]})
        return {**out, "retire_at": iso(out["retire_at"]), "v1_grants_ended": v1}

    def retire_key(self, kid: str, *, at: float | None = None) -> dict:
        try:
            out = self.ring.retire(kid, at=at)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        return {**out, "retire_at": iso(out["retire_at"])}

    def signing_keys(self) -> list[dict]:
        return [{**e, "retire_at": iso(e["retire_at"]), **({"rotated_at": iso(e["rotated_at"])}
                                                           if e.get("rotated_at") else {})}
                for e in self.ring.entries()]

    def v1_accepted(self, *, foreign: bool = False) -> bool:
        """Version 1 tokens verify during the migration window: policy.accept_v1_tokens, and for
        this node's own grants only until its key is first rotated."""
        return self.cfg.accept_v1_tokens and (foreign or not self.ring.rotated)

    # ================================================================ grants
    def issue(self, *, subject: Subject | dict, workspaces: list[str] | None = None, lanes: list[str] | None = None,
              domains: list[str] | None = None, ceiling: Effect | str = Effect.READ,
              specials: Specials | dict | None = None, budget: Budget | dict | None = None,
              ttl_s: int | None = None, not_before: float | None = None, depth: int | None = None,
              fanout: int | None = None, pins: dict[str, str] | None = None, egress: list[str] | None = None,
              admin: bool = False, note: str | None = None, by: str = "owner",
              audience: list[str] | None = None, owner: str | None = None, within: Grant | None = None,
              continues: str | None = None, _member: bool = False, _reserved: bool = False) -> tuple[Grant, str]:
        """Mint a root grant (token v2, for `audience`: this node by default). The token is
        returned once; only its hash is stored. `owner` is the member an agent grant belongs to
        (its `sub.owner`: the person behind the agent, for routing and four-eyes). A person's
        grant (subject kind human) is made only by issue_member_grant. `within`: the grant of the
        caller who asks (an admin grant on the panel API): the new grant is never wider than it, in
        lanes, domains, egress, ceiling, specials, budget, expiry, admin or depth, and is refused
        with the fields named otherwise. The owner token, and a person, issue without it.
        `continues`: the root grant this one replaces for the same subject, whose environments, saves
        and title-bound logins it keeps reaching (openajar.policy.families; only `openajar local`
        passes it, and no API route does)."""
        now = int(self.clock())
        if within is not None and within.owner_token:
            within = None
        if within is not None and depth is None:
            left = _remaining_depth(within)  # a delegation the caller has not got is not one it can give
            depth = self.cfg.default_depth if left is None else max(0, min(self.cfg.default_depth, left - 1))
        ttl = int(ttl_s or self.cfg.default_grant_ttl_s)
        if ttl <= 0 or ttl > self.cfg.max_grant_ttl_s:
            raise InvalidRequest(f"ttl must be between 1 second and {self.cfg.max_grant_ttl_s // 86400} days")
        sub = _root_subject(subject, owner=owner, member=_member, reserved=_reserved)
        family = self.continued_family(continues, sub) if continues else None  # type: ignore[attr-defined]
        self.ring.refresh()
        block = self._block(
            parent=None, version=tokens.VERSION, iss=self.issuer, aud=list(audience or [self.issuer]),
            kid=self.kid, sub=sub, workspaces=workspaces, lanes=lanes, domains=domains,
            ceiling=coerce_effect(ceiling), specials=specials, budget=budget,
            nbf=int(not_before) if not_before else None, exp=now + ttl,
            depth=self.cfg.default_depth if depth is None else depth, fanout=fanout, pins=pins, egress=egress,
            admin=admin or None, note=note)
        if within is not None:
            problems = self._root_widening(within, block)
            if problems:
                raise InvalidRequest("this would grant more than the grant asking for it holds: " + ", ".join(problems))
        if sub.kind != "human":  # people keep looking during a freeze; agents get no new way in
            freeze_gate.check_new_grant(self, block)
        token = tokens.mint(self.key, block.to_data())
        g = self._built(token)
        g.check_structure()
        if family is not None:
            g.extra["env_family"] = family
        self._register(g, source="issued", by=by, env_family=family)
        if family is not None:
            # it reaches the computers that hold what the grant it continues read: it never starts clean (R1 #4)
            self._inherit_taint(self._stored_chain(continues), g)  # type: ignore[attr-defined]
            self._trace_family(g, continues, family, by)
        return g, token

    def _trace_family(self, g: Grant, continues: str | None, family: str, by: str) -> None:
        """The action log's row for a grant that continues a family (openajar.policy.families)."""
        from openajar.policy.trace_models import TraceEvent

        self.traces.write(TraceEvent(  # type: ignore[attr-defined]
            kind="grant_family", tool="grants.continue", principal=by, principal_kind="owner", grant_id=g.id,
            grant_path=g.path, status="ok",
            summary=f"grant {g.id} for {g.subject.id} keeps the environments, saves and logins of {continues}",
            args={"continues": continues, "family": family, "subject": g.subject.id}))

    def _root_widening(self, within: Grant, block: GrantBlock) -> list[str]:
        """What a new root grant would hold beyond the grant asking for it, sorted."""
        problems = widening_problems(within, block, tiers=self.cfg.brain_tiers, aliases=self.cfg.brain_tier_aliases)
        # a narrowed child inherits what it leaves out; a fresh root does not, so leaving one out widens it
        if block.budget is None and within.budgets():
            problems.append("budget")
        if block.domains is None and any(b.domains is not None for b in within.blocks):
            problems.append("domains")
        if within.fanout_limit() is not None:  # roots it issued would not count against its fan-out
            problems.append("fanout")
        return sorted(set(problems))

    @staticmethod
    def _built(token: str) -> Grant:
        """The Grant of a token this node just signed (revocation ids from its signatures)."""
        return Grant(blocks_from_token(tokens.inspect(token)), token_hash=tokens.token_hash(token))

    def attenuate(self, token: str, *, subject: Subject | dict | None = None, workspaces: list[str] | None = None,
                  lanes: list[str] | None = None, domains: list[str] | None = None,
                  ceiling: Effect | str | None = None, specials: Specials | dict | None = None,
                  budget: Budget | dict | None = None, ttl_s: int | None = None, depth: int | None = None,
                  fanout: int | None = None, pins: dict[str, str] | None = None, egress: list[str] | None = None,
                  admin: bool = False, note: str | None = None, by: str | None = None,
                  audience: list[str] | None = None) -> tuple[Grant, str]:
        """Derive a narrower child from a token. Any widening is refused with the fields named.
        `audience` narrows where the child is valid (`node:<id>`); a parent's audience always stays.
        A child never inherits admin unless it asks for it (and the parent has it). `subject.id`
        names a sub-agent: the child acts as the parent's id plus "/" plus that label
        (`claude-code/codex-worker`), with kind agent, flow or brain; it can never become a human,
        the panel or another identity. A child of the owner token is a fresh root grant. A person's
        grant is never narrowed (and is never a bearer token: resolve refuses it)."""
        _, parent = self.resolve(token, allow_human=True)
        if parent.human and not parent.owner_token:
            raise InvalidRequest("a member's grant cannot be narrowed or handed to an agent")
        now = int(self.clock())
        try:
            sub = None if subject is None else (subject if isinstance(subject, Subject) else Subject(**subject))
        except ValueError as e:
            raise InvalidRequest(f"invalid subject: {e}") from None
        if sub is not None and not parent.owner_token:
            sub = child_subject(parent.subject, sub)
            if problems := subject_problems(parent.subject, sub):
                raise InvalidRequest("this would widen the grant: " + ", ".join(problems))
        elif sub is not None:
            sub = sub.model_copy(update={"owner": parent.subject.owner}) if sub.owner == "owner" else sub
        exp = now + int(ttl_s) if ttl_s else None
        if parent.owner_token:
            # the owner token is not a chain; a child of it is a fresh root grant, narrowed from "everything"
            if workspaces is None and lanes is None:
                workspaces = ["*"]
            fresh, fresh_token = self.issue(
                subject=sub or Subject(id="owner-child"), _reserved=sub is None, workspaces=workspaces, lanes=lanes,
                domains=domains,
                ceiling=ceiling or Effect.IRREVERSIBLE,
                specials=specials if specials is not None else Specials.everything(), budget=budget,
                ttl_s=ttl_s, depth=depth, fanout=fanout, pins=pins, egress=egress, admin=admin,
                note=note, by=by or "owner")
            self._inherit_taint(parent, fresh)  # narrowed, so it never sheds what the owner token read
            return fresh, fresh_token
        child = self._block(
            parent=parent.id, version=parent.version, **subject_fields(parent.subject, sub, parent.version),
            workspaces=workspaces, lanes=lanes, domains=domains,
            ceiling=coerce_effect(ceiling) if ceiling is not None else None, specials=specials, budget=budget,
            nbf=None, exp=exp, depth=depth, fanout=fanout, pins=pins, egress=egress,
            admin=bool(admin), note=note, aud=list(audience) if audience is not None else None)
        problems = widening_problems(parent, child, tiers=self.cfg.brain_tiers, aliases=self.cfg.brain_tier_aliases)
        if problems:
            raise InvalidRequest("this would widen the grant: " + ", ".join(problems))
        new_token = tokens.append(token, child.to_data())
        g = self._built(new_token)
        g.check_structure()
        self._register(g, source="attenuated", by=by or parent.subject.id)
        return g, new_token

    def _block(self, *, parent: str | None, version: int, specials: Specials | dict | None,
               budget: Budget | dict | None, **fields: Any) -> GrantBlock:
        try:
            if isinstance(specials, dict):
                specials = Specials(**specials)
            if isinstance(budget, dict):
                budget = Budget(**budget)
            if version < 2:
                fields["rid"] = new_id("rv")  # a v1 chain (migration window) keeps v1 blocks
            block = GrantBlock(v=version, id=new_id("gr"), parent=parent, iat=int(self.clock()),
                               specials=specials, budget=budget, **fields)
            memory_rules.check_authority_scope(block)  # memory: workspaces inside the lane scope
            tokens.block_bytes(version, block.to_data())  # refuses values with no canonical form
            return block
        except ValueError as e:
            raise InvalidRequest(f"invalid grant: {e}") from None

    def _register(self, g: Grant, *, source: str, by: str | None, env_family: str | None = None) -> None:
        b = g.blocks[-1]
        with self.db.tx() as conn:
            if b.parent is not None:
                self._check_fanout(conn, Grant(g.blocks[:-1]))
            conn.execute(
                "INSERT INTO grants (id, parent_id, root_id, path, depth, rid, token_sha256, source, subject, block, "
                "issued_at, expires_at, issued_by, v, block_sha256, env_family) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (b.id, b.parent, g.root_id, g.path, g.depth, b.rid or "", g.token_hash, source,
                 json.dumps(g.subject.model_dump()), json.dumps(b.to_data()), float(b.iat),
                 g.expires_at, by, b.v, b.digest(), env_family))
        self.bus.publish("grant", {"id": b.id, "state": source})

    @staticmethod
    def _check_fanout(conn: Any, parent: Grant) -> None:
        limit = parent.fanout_limit()
        if limit is None:
            return
        n = conn.execute("SELECT COUNT(*) AS n FROM grants WHERE parent_id = ?", (parent.id,)).fetchone()["n"]
        if n >= limit:
            raise Denied("fanout", f"grant {parent.id} may hand out at most {limit} narrowed grants")

    @staticmethod
    @contextlib.contextmanager
    def channel_call(token: str | None) -> Iterator[None]:
        """Mark the code inside as running the channel `invoke` that carries `token`. Only the
        channel's invoke path sets this (openajar.node.channel), around every resolve of the call's
        token: it is what lets `resolve` take a controller's member call token (see resolve)."""
        mark = _ChannelMark(bare_token(token) or None)
        reset = _channel_token.set(mark)
        try:
            yield
        finally:
            mark.token = None  # a task this call started holds the same mark: it ends here too
            _channel_token.reset(reset)

    def resolve(self, token: str | None, *, allow_human: bool = False) -> tuple[Principal, Grant]:
        """Bearer token -> (Principal, Grant). Raises Denied (never echoes the token). A person's
        grant (subject kind human, other than the owner token) is refused with invalid_token unless
        `allow_human`: members are never bearer callers, so MCP and the panel's bearer path refuse
        them. The one exception is a member call: a human grant from a trusted controller (a
        foreign issuer), presented as the grant of the channel `invoke` being run (channel_call),
        with at most MEMBER_CALL_MAX_TTL_S of life (check_member_call). The controller ceiling and
        the grant's own limits apply as to any controller grant, and the principal is the member.
        Such a grant is marked `member_call`: a person acting remotely, never one at the node."""
        if not token or not str(token).strip():
            raise Denied("unauthenticated", "no token was presented")
        token = bare_token(token)
        if self.owner_token and hmac.compare_digest(token.encode(), self.owner_token.encode()):
            g = Grant([owner_block()], owner_token=True)
            return g.principal(), g
        opened, foreign, issuer = self._open(token)
        g = Grant(blocks_from_token(opened), token_hash=tokens.token_hash(token))
        if foreign is not None:
            g.extra["issuer"] = issuer
        g.check_structure()
        member_call = g.human and not allow_human and foreign is not None and self._is_channel_call(token)
        if g.human and not allow_human and not member_call:
            raise TokenInvalid("a member's grant is not a bearer token; members sign in to the panel")
        if not g.audience_allows(self.audiences()):
            raise Denied("audience", "this grant was issued for another node")
        self.check_live(g)
        if member_call:
            self.check_member_call(g)
            g.extra["member_call"] = True
        if foreign is None:
            self._check_registered(g)
        else:
            foreign.admit(issuer, g)
            g.limit = ControllerCeiling.from_config(self.node_config)  # the owner's ceiling on a controller
        return g.principal(), g

    def check_member_call(self, g: Grant) -> None:
        """A member call token is one call's credential, judged on this node's own clock (never the
        controller-adjusted one, which a controller moves by up to an hour): it lives at most
        MEMBER_CALL_MAX_TTL_S from its issue, was not issued in the future (beyond the clock skew),
        and is not past its expiry (the skew check_live allows never extends a member call)."""
        now = self.local_clock()
        auth = g.blocks[0]
        exp = g.expires_at
        if exp is None or exp - auth.iat > MEMBER_CALL_MAX_TTL_S or exp - now > MEMBER_CALL_MAX_TTL_S:
            raise TokenInvalid(f"a member's call grant from the controller lives at most "
                               f"{MEMBER_CALL_MAX_TTL_S // 60} minutes; this one is a standing credential")
        if auth.iat > now + self.cfg.clock_skew_s:
            raise TokenInvalid("this member's call grant was issued in the future; check the controller's clock")
        if exp <= now:
            raise Denied("expired", f"this grant expired at {iso(exp)}")

    @staticmethod
    def _is_channel_call(token: str) -> bool:
        mark = _channel_token.get()
        bound = mark.token if mark is not None else None
        return bound is not None and hmac.compare_digest(token.encode(), bound.encode())

    def _issued_standing_ok(self, issuer: str, effect: str) -> bool:
        """A standing approval a controller sent covers a call only while this node trusts its issuer
        (a live key) and within the owner's controller ceiling, as a grant from that issuer would be."""
        trusted = issuers._issuers(self)
        if trusted is None or not trusted.live(issuer):
            return False
        try:
            return ControllerCeiling.from_config(self.node_config).allows_effect(Effect(effect))
        except ValueError:
            return False

    def _open(self, token: str) -> tuple[tokens.Token, Any, str | None]:
        """Verify a token's signatures with the key it names: (token, TrustedIssuers or None,
        issuer). v2: this node's own grants name its issuer id and a kid in its key ring; any
        other issuer goes to the trusted issuers (policy/issuers.py). v1 (no kid): the active
        key, else a trusted issuer, while the migration window lasts."""
        version, authority = tokens.peek(token)
        if version < 2:
            if not self.cfg.accept_v1_tokens:
                raise TokenInvalid("this node no longer accepts version 1 grant tokens; ask for a new grant")
            if self.v1_accepted():
                try:
                    return tokens.open_token(token, self.public_key), None, None
                except TokenInvalid:
                    pass
            opened, found, issuer = issuers.open_foreign(self, token)
            return opened, found, issuer
        self.ring.refresh()
        if authority.get("iss") == self.issuer:
            return tokens.open_token(token, lambda _v, auth: self.ring.public_for(auth.get("kid"))), None, None
        return issuers.open_foreign(self, token)

    def _check_registered(self, g: Grant) -> None:
        rows = {r["id"]: r for r in self.db.all(
            f"SELECT id, parent_id, block, block_sha256, env_family FROM grants "
            f"WHERE id IN ({','.join('?' * len(g.blocks))})", g.chain_ids)}
        if g.root_id not in rows:
            raise Denied("unregistered", "this grant was not issued by this node")
        if rows[g.root_id]["env_family"]:  # it continues an earlier grant's family (openajar.policy.families)
            g.extra["env_family"] = rows[g.root_id]["env_family"]
        for i, b in enumerate(g.blocks):
            row = rows.get(b.id)
            if row is not None:
                # an id is bound to its exact block (the hash of its signed bytes, the root's too), so
                # a reused id cannot dodge fan-out or revocation, and a root signed elsewhere with a
                # shared key is not this node's grant
                self._check_row(row, b)
                continue
            if not self.cfg.allow_offline_attenuation:
                raise Denied("unregistered", "narrowed grants must be created through this node")
            if g.human:
                raise TokenInvalid("a member's grant cannot be narrowed")
            sub = Grant(g.blocks[:i + 1], token_hash=g.token_hash if i == len(g.blocks) - 1 else None)
            try:
                self._register(sub, source="observed", by=None)
            except Exception as e:
                if isinstance(e, Denied):
                    raise
                # another call registered this id first: it must be this very block, else the token
                # is refused like any other that does not match its registered chain
                row = self.db.one("SELECT id, parent_id, block, block_sha256 FROM grants WHERE id = ?", (b.id,))
                if row is None:
                    raise
                self._check_row(row, b)

    @staticmethod
    def _check_row(row: Any, b: GrantBlock) -> None:
        """The registry row of `b.id` holds exactly `b` (its parent and its signed bytes)."""
        same = row["block_sha256"] == b.digest() if row["block_sha256"] else json.loads(row["block"]) == b.to_data()
        if row["parent_id"] != b.parent or not same:
            raise TokenInvalid("the token does not match its registered chain")

    def check_live(self, g: Grant, now: float | None = None) -> None:
        """Time window, signing key and revocation of every block in the chain. Called on every
        call. A local revocation names a grant id (bound to one block by the registry) or a v2
        revocation id (derived from one block's signature); a v1 block's own `rid` is never
        trusted, since its author chose it (SEC-6)."""
        if g.owner_token:
            return
        foreign = "issuer" in g.extra
        if g.version < 2 and not self.v1_accepted(foreign=foreign):
            if not self.cfg.accept_v1_tokens:
                raise TokenInvalid("this node no longer accepts version 1 grants; ask for a new grant")
            raise Denied("retired_key", "this version 1 grant was signed before the owner rotated the grant key; "
                                        "ask for a new grant")
        if g.version >= 2 and not foreign:
            self.ring.refresh()
            self.ring.check(g.kid)
        g.check_time(self.clock() if now is None else now, self.cfg.clock_skew_s)
        rows = self._revocations(g)
        if rows:
            for row in rows:  # a freeze says so: the agent stops and tells the user, never looks for a way in
                if (said := freeze_gate.frozen_message(row["reason"])) is not None:
                    raise GrantFrozen(said)
            raise Denied("revoked", "this grant, or one it was narrowed from, was revoked")
        issuers.check_live(self, g)

    def _revocations(self, g: Grant, limit: int = 8) -> list[Any]:
        """The `revoked` rows that end this grant (it, a grant it was narrowed from, or, for a person's
        grant, the member's removal): grant_id, revoked_by and reason. Empty when none does."""
        ids = g.chain_ids
        if g.human:
            ids = [*ids, member_revocation(g.blocks[0].sub.id)]  # type: ignore[union-attr]
        rids = [b.rid for b in g.blocks if b.v >= 2 and b.rid]
        marks = ",".join("?" * len(ids))
        sql = f"SELECT grant_id, revoked_by, reason FROM revoked WHERE grant_id IN ({marks})"
        if rids:
            sql += f" OR rid IN ({','.join('?' * len(rids))})"
        return self.db.all(sql + f" LIMIT {int(limit)}", [*ids, *rids])

    def revoke(self, grant_id: str, *, by: str = "owner", reason: str | None = None) -> dict:
        """Revoke a grant (by grant id, or by the v2 revocation id of its block) and everything
        narrowed from it."""
        row = self.db.one("SELECT * FROM grants WHERE id = ?", (grant_id,))
        if row is None and grant_id.startswith("rv_"):
            row = self.db.one("SELECT * FROM grants WHERE rid = ? AND v >= 2", (grant_id,))
        if row is None:
            raise NotFound(f"no grant {grant_id}")
        grant_id = row["id"]
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM revoked WHERE grant_id = ?", (grant_id,)).fetchone():
                raise Conflict(f"grant {grant_id} is already revoked")
            conn.execute("INSERT INTO revoked (grant_id, rid, revoked_at, revoked_by, reason) VALUES (?, ?, ?, ?, ?)",
                         (grant_id, row["rid"] if row["v"] >= 2 else None, self.clock(), by,
                          (reason or "")[:300] or None))
        below = [r["id"] for r in self.db.all("SELECT id FROM grants WHERE path LIKE ? AND id != ?",
                                              (f"%/{grant_id}/%", grant_id))]
        self._must_ask.pop(grant_id, None)
        self.bus.publish("grant", {"id": grant_id, "state": "revoked", "cascades_to": below})
        return {"id": grant_id, "revoked": True, "cascades_to": below}

    def revoke_reaching(self, *, lanes: Iterable[str] = (), workspaces: Iterable[str] = (), everything: bool = False,
                        by: str = "owner", reason: str = "frozen") -> dict:
        """A freeze's revocation sweep (openajar.policy.freeze): every live grant that can reach these
        lanes or workspaces (or any lane), what was narrowed from them, and the standing approvals
        over them. Returns {grants, cascades_to, standing, errors}; never restores anything."""
        reach = freeze_gate.Reach(lanes=lanes, workspaces=workspaces, everything=everything)
        return freeze_gate.revoke_reaching(self, reach, by=by, reason=freeze_gate.revoke_reason(reach, reason))

    def grant_record(self, grant_id: str) -> dict:
        row = self.db.one("SELECT * FROM grants WHERE id = ?", (grant_id,))
        if row is None:
            raise NotFound(f"no grant {grant_id}")
        return self._record(row, self._revoked_map())

    def explain_grant(self, grant: str | dict) -> list[str]:
        """What a grant lets its holder do, in plain sentences (openajar.policy.explain), from its id or
        its record. A grant that allows nothing now says so in one sentence."""
        record = self.grant_record(grant) if isinstance(grant, str) else grant
        if record["state"] != "active":
            return [f"It is {record['state']}, so it allows nothing now."]
        try:
            g = self.grant_by_id(record["id"])
        except Denied as e:
            return [f"It allows nothing now: {e.message}"]
        return explain(g, routing=self.routing_now(), remaining=self.remaining(g))

    def list_grants(self, *, include_inactive: bool = False, viewer: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM grants", []
        if viewer is not None:
            sql += " WHERE path LIKE ?"
            params.append(f"%/{viewer}/%")
        revoked = self._revoked_map()
        out = [self._record(r, revoked) for r in self.db.all(sql + " ORDER BY issued_at DESC", params)]
        return out if include_inactive else [g for g in out if g["state"] == "active"]

    def _revoked_map(self) -> dict[str, float]:
        return {r["grant_id"]: r["revoked_at"] for r in self.db.all("SELECT grant_id, revoked_at FROM revoked")}

    def _record(self, row: Any, revoked: dict[str, float]) -> dict:
        path_ids = [p for p in row["path"].split("/") if p]
        via = next((p for p in path_ids if p in revoked), None)
        now = self.clock()
        state = "active"
        if via is not None:
            state = "revoked"
        elif row["expires_at"] is not None and row["expires_at"] < now:
            state = "expired"
        blocks = []
        for gid in path_ids:
            r = self.db.one("SELECT block, rid FROM grants WHERE id = ?", (gid,))
            try:
                if r is not None:
                    blocks.append(_stored_block(r))
            except (ValueError, Denied):  # a row this version no longer accepts (an Infinity budget)
                break
        projection = Grant(blocks).projection() if len(blocks) == len(path_ids) else None
        family = row["env_family"] if row["parent_id"] is None else (
            (self.db.one("SELECT env_family FROM grants WHERE id = ?", (row["root_id"],)) or {"env_family": None})
            ["env_family"])
        return {"id": row["id"], "parent_id": row["parent_id"], "root_id": row["root_id"], "depth": row["depth"],
                "env_family": family or row["root_id"],
                "subject": json.loads(row["subject"]), "source": row["source"], "state": state,
                "revoked": via is not None, "revoked_via": via, "revoked_at": iso(revoked.get(via)) if via else None,
                "issued_at": iso(row["issued_at"]), "expires_at": iso(row["expires_at"]),
                "issued_by": row["issued_by"], "scope": projection,
                "budget": self.remaining(Grant(blocks)) if projection else None}

    # ================================================================ child grants for brains
    def mint_child_token(self, principal: Principal | dict, *, purpose: str, workspace: str, lanes: list[str],
                         ceiling: Effect | str = Effect.COMMIT, specials: Iterable[str] | dict | None = None,
                         ttl_seconds: int = 3600, budget: dict | None = None,
                         pins: dict[str, str] | None = None) -> dict:
        """A child grant for a brain task, narrowed from the principal's own grant: only `lanes`
        (all in `workspace`), at most `ceiling` and never above the parent, never `brain` or
        `exec`, a budget no larger than the parent's, no further delegation, and expiring after
        `ttl_seconds` (never after the parent). `pins` (tool name to manifest hash) limits it to
        those tools as they are now. Revoking the parent revokes it. Returns
        {"token", "grant_id", "expires_at"}; the token exists only in this return value."""
        parent = self.grant_for(principal)
        if parent.human and not parent.owner_token:
            raise InvalidRequest("a member's grant cannot be narrowed or handed to an agent")
        lanes = self._child_lanes(parent, lanes, workspace)
        eff = coerce_effect(ceiling)
        if eff.rank is None:
            raise InvalidRequest("a child grant's ceiling must be read, draft, commit, pay or irreversible")
        child_ceiling = LADDER[min(eff.rank, parent.ceiling.rank)]
        child_specials = _child_specials(parent, specials, lanes)
        child_budget = self._narrow_budget(parent, budget or {})
        now = int(self.clock())
        ttl = max(60, min(int(ttl_seconds or 3600), self.cfg.max_grant_ttl_s))
        exp = now + ttl if parent.expires_at is None else min(now + ttl, parent.expires_at)
        sub_id = re.sub(r"[^A-Za-z0-9._:-]+", "-", purpose or "brain").strip("-._:")[:120] or "brain"
        sub = Subject(kind="brain", id=sub_id, owner=parent.subject.owner, thread=parent.subject.thread)
        if not parent.owner_token:
            sub = child_subject(parent.subject, sub, truncate=True)  # a sub-agent of the parent
        by = getattr(principal, "id", None) or "brains"
        if parent.owner_token:
            g, token = self.issue(subject=sub, lanes=lanes, ceiling=child_ceiling, specials=child_specials,
                                  budget=child_budget, ttl_s=exp - now, depth=0, fanout=0, pins=pins,
                                  note=(purpose or "")[:300] or None, by=by)
            self._inherit_taint(parent, g)
            return {"token": token, "grant_id": g.id, "expires_at": iso(g.expires_at)}
        child = self._block(parent=parent.id, version=parent.version,
                            **subject_fields(parent.subject, sub, parent.version), workspaces=None, lanes=lanes,
                            domains=None, ceiling=child_ceiling, specials=child_specials, budget=child_budget,
                            nbf=None, exp=exp, depth=0, fanout=0, pins=pins, egress=None, admin=False,
                            note=(purpose or "")[:300] or None)
        problems = widening_problems(parent, child, tiers=self.cfg.brain_tiers, aliases=self.cfg.brain_tier_aliases)
        if problems:
            raise InvalidRequest("this would widen the grant: " + ", ".join(problems))
        # The node holds the root key, so it re-signs the parent's registered chain and appends the
        # child (the parent's own token is never stored, only its hash). The authority block is
        # re-signed with the key its kid names, so the copy verifies like the original; the
        # copied blocks get new revocation ids, but a revocation by grant id still covers them.
        root_key = self.ring.private_for(parent.kid) if parent.version >= 2 else self.key
        token = tokens.mint(root_key, parent.blocks[0].to_data())
        for b in [*parent.blocks[1:], child]:
            token = tokens.append(token, b.to_data())
        g = self._built(token)
        g.check_structure()
        self._register(g, source="attenuated", by=by)
        return {"token": token, "grant_id": g.id, "expires_at": iso(g.expires_at)}

    def _child_lanes(self, parent: Grant, lanes: list[str], workspace: str) -> list[str]:
        """A child grant's lanes, once each: at least one, none frozen, all in `workspace` and
        covered by the parent."""
        lanes = list(dict.fromkeys(lanes or []))
        if not lanes:
            raise InvalidRequest("a child grant needs at least one lane")
        freeze_gate.check_child_lanes(self, lanes)
        for lane in lanes:
            try:
                ws, _ = parse_lane_id(lane)
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            if ws != workspace:
                raise InvalidRequest(f"lane {lane} is not in workspace {workspace}")
            if not parent.lane_allowed(lane):
                raise Denied("out_of_scope", f"the parent grant does not cover lane {lane}")
        return lanes

    @staticmethod
    def _narrow_budget(parent: Grant, wanted: dict) -> Budget | None:
        out: dict[str, Any] = {}
        for name in ("tokens", "usd", *BUDGET_LIMITS):
            values = [v for v in [wanted.get(name)] + [getattr(b, name) for _, b in parent.budgets()]
                      if v is not None]
            if values:
                out[name] = min(values)
        return Budget(**out) if out else None


def _child_specials(parent: Grant, specials: Iterable[str] | dict | None, lanes: list[str]) -> Specials:
    """A brain's child grant carries secret at most (never brain or exec), and only when the parent has it."""
    names = set(specials.keys() if isinstance(specials, dict) else specials or []) - {"brain", "exec"}
    unsupported = names - {"secret"}
    if unsupported:
        raise InvalidRequest(f"a brain's child grant cannot carry {', '.join(sorted(unsupported))}")
    child_specials = Specials(secret=LaneCap(lanes=lanes) if "secret" in names else None)
    if "secret" in names and parent.caps("secret") is None:
        raise Denied("special", "the parent grant does not include secret")
    return child_specials


def _root_subject(subject: Subject | dict, *, owner: str | None, member: bool, reserved: bool) -> Subject:
    """The subject of a new root grant, checked: never a person's outside issue_member_grant, never a
    subject the node keeps for its own grants, and its owner (the person behind an agent) set."""
    try:
        sub = subject if isinstance(subject, Subject) else Subject(**subject)
    except ValueError as e:
        raise InvalidRequest(f"invalid subject: {e}") from None
    if sub.kind == "human" and not member:
        raise InvalidRequest("a person's grant is made for a member (issue_member_grant, `openajar members`), "
                             "never as a token")
    if not reserved and any(sub.id.startswith(p) if p.endswith(":") else sub.id == p for p in RESERVED_SUBJECTS):
        # a Site API key's runs are told apart by this subject (siteapis.http): no other grant takes it
        raise InvalidRequest(f"the subject id {sub.id[:40]!r} is reserved for the node's own grants")
    if owner is not None:
        if owner != "owner" and not MEMBER_ID_RE.match(owner):
            raise InvalidRequest("owner is \"owner\" or a member id (m_...)")
        sub = sub.model_copy(update={"owner": owner})
    return sub


def _stored_block(row: Any) -> GrantBlock:
    """A registry row's block: its data, and for v2 the revocation id the registry derived."""
    data = json.loads(row["block"])
    return parse_block(data, rid=row["rid"] or None)
