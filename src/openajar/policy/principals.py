"""Principals: finding the live grant of a caller the node resolved earlier.

Part of PolicyService (openajar.policy.service), as a mixin. In-process callers (the panel API, the
runner, brains) hold a Principal, and durable runs a grant id, instead of a token; these methods
turn either back into a live grant, fail closed: an unknown grant, a broken chain, a revoked or an
expired one is refused with Denied, never treated as the owner or as no grant at all.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.core.models import Principal
from openajar.policy import tokens
from openajar.policy.errors import Denied, GrantFrozen
from openajar.policy.grants import (
    OWNER_GRANT_ID,
    Grant,
    GrantBlock,
    Specials,
    Subject,
    owner_block,
    subject_problems,
)
from openajar.policy.registry import _stored_block
from openajar.policy.traces import TraceEvent

if TYPE_CHECKING:
    from openajar.policy.context import Decision, PolicyContext

log = logging.getLogger("openajar.policy")


class PrincipalsMixin:
    """Principal and stored-grant lookups of PolicyService (the attributes it reads, `db`, `cfg`,
    `traces`, `owner_token`, `clock`, are set by PolicyService.__init__)."""

    # ================================================================ principals (panel, runner, brains)
    # In-process callers (the panel API, the runner, brains) hold a Principal the policy resolved
    # earlier; these methods find its grant again by id. The owner's grant is "owner" (a token
    # block can never use that id, see Grant.check_structure).

    def owner_grant(self) -> Grant:
        return Grant([owner_block()], owner_token=True)

    def device_ceiling(self) -> Grant:
        """What a grant issued with the owner's device credential must fit inside (`issue(within=...)`):
        policy.device_grants, alive for its max_ttl_s from now. Never signed or registered: a bound
        only, as an admin grant is one for what it issues."""
        cfg = self.cfg.device_grants
        try:
            block = GrantBlock(v=tokens.VERSION, id="gr_deviceceiling", iat=int(self.clock()), iss=self.issuer,
                               aud=[self.issuer], kid=self.kid,
                               sub=Subject(kind="agent", id="device-ceiling"), workspaces=list(cfg.workspaces),
                               lanes=list(cfg.lanes) if cfg.lanes is not None else None, ceiling=cfg.ceiling,
                               specials=Specials(**cfg.specials),
                               exp=int(self.clock()) + int(cfg.max_ttl_s) + 5)  # a grant asked for in the same breath
        except ValueError as e:
            raise InvalidRequest(f"policy.device_grants is not a valid grant: {e}") from None
        return Grant([block])

    def grant_for(self, principal: Principal | dict | None) -> Grant:
        """The live grant `principal` acts under. Raises Denied when it has no grant, the grant
        is unknown here, expired or revoked (fail closed)."""
        gid = principal.get("grant_id") if isinstance(principal, dict) else getattr(principal, "grant_id", None)
        if not gid or not isinstance(gid, str):
            raise Denied("unregistered", "this caller acts under no grant")
        if gid == OWNER_GRANT_ID:
            return self.owner_grant()
        row = self.db.one("SELECT path FROM grants WHERE id = ?", (gid,))
        if row is None:
            raise Denied("unregistered", "this grant was not issued by this node")
        blocks = []
        for bid in [p for p in row["path"].split("/") if p]:
            r = self.db.one("SELECT block, rid FROM grants WHERE id = ?", (bid,))
            if r is None:
                raise Denied("unregistered", "this grant's chain is incomplete on this node")
            blocks.append(_stored_block(r))
        g = Grant(blocks)
        g.check_structure()
        self.check_live(g)
        return g

    def principal_context(self, principal: Principal | dict, *, tool: str | None = None,
                          lane_id: str | None = None, tags: Iterable[str] = ()) -> PolicyContext:
        """A ToolContext for a principal acting outside MCP (panel requests, scheduled runs)."""
        g = self.grant_for(principal)
        p = principal if isinstance(principal, Principal) else _principal_from(principal, g)
        return self.context(g, principal=p, tool=tool, lane_id=lane_id, tags=tags)

    def owner_context(self, *, tool: str | None = None) -> PolicyContext:
        """The owner in person (a signed-in panel session)."""
        g = self.owner_grant()
        return self.context(g, principal=g.principal(), tool=tool, in_person=True)

    def authenticate_panel(self, token: str | None) -> Principal | None:
        """Bearer token -> Principal for the panel API: the owner token gives the owner, a grant
        token that grant's principal (the API then limits it to the grant's scope). None for
        anything that does not resolve; never raises, never echoes the token."""
        try:
            principal, _ = self.resolve(token)
        except OpenajarError:
            return None
        except Exception:  # noqa: BLE001  (a token that breaks the parser is simply refused)
            log.warning("panel token check failed; refusing it")
            return None
        return principal

    def refusal(self, token: str | None) -> tuple[str, str] | None:
        """(code, message) for a token refused for a reason the caller must be told (a grant a freeze
        revoked: `frozen`), else None (refused as unauthorized, or not refused). Never raises."""
        try:
            self.resolve(token)
        except GrantFrozen as e:
            return e.code, e.message
        except Exception:  # noqa: BLE001
            return None
        return None

    def is_owner_token(self, token: str | None) -> bool:
        """True for the node's own token and for any admin grant: owner-equivalent tokens that
        must never be handed to a brain or a child process. Also True for a token that could not be
        checked (a database error, not a refusal), so the caller holds it back rather than hand on
        what may be an admin grant."""
        if not token or not str(token).strip():
            return False
        token = str(token).strip()
        if self.owner_token and hmac.compare_digest(token.encode(), self.owner_token.encode()):
            return True
        try:
            _, g = self.resolve(token)
        except OpenajarError:  # refused: it acts as no one, so handing it on gives nothing away
            return False
        except Exception:  # noqa: BLE001  (it could not be read now: treated as the owner's, so it stays here)
            log.warning("a token could not be checked; it is held back as if it were the owner's")
            return True
        return g.admin

    def _trace_refused(self, principal: Any, tool: str | None, effect: Any, lane_id: str | None,
                       err: OpenajarError) -> None:
        pid = getattr(principal, "id", None) or (principal.get("id") if isinstance(principal, dict) else None)
        self.traces.write(TraceEvent(kind="authorize", tool=tool, lane=lane_id, status="denied",
                                     effect=str(getattr(effect, "value", effect))[:40], summary=err.message,
                                     principal=pid))

    async def authorize(self, principal: Principal | dict, effect: Any, *, lane_id: str | None = None,
                        domain: str | None = None, summary: str | None = None, content: Any = None,
                        cost: Any = None, facts: dict | None = None, tool: str | None = None) -> Decision:
        """Authorize `effect` under the principal's grant with the full routing (scope, ceiling,
        budget, approvals, trifecta), for callers outside MCP: the runner's scheduled, triggered
        and API runs. Raises Denied or ApprovalPending; both are traced."""
        tool = tool or "runner"
        try:
            ctx = self.principal_context(principal, tool=tool, lane_id=lane_id)
        except Denied as e:
            self._trace_refused(principal, tool, effect, lane_id, e)
            raise
        return await ctx.authorize(effect, lane_id=lane_id, domain=domain, summary=summary, content=content,
                                   cost=cost, facts=facts)

    def _stored_chain(self, grant_id: str | None) -> Grant:
        """A stored grant's chain, live or not (grant_by_id checks it is live; charge_spend charges
        what already ran). Denied unknown_grant when it or a block of its chain is not stored here."""
        row = self.db.one("SELECT path, token_sha256 FROM grants WHERE id = ?", (grant_id,)) if grant_id else None
        if row is None:
            raise Denied("unknown_grant", "the grant this was started under is not known on this node")
        ids = [p for p in row["path"].split("/") if p]
        rows = {r["id"]: r for r in self.db.all(
            f"SELECT id, block, rid, env_family FROM grants WHERE id IN ({','.join('?' * len(ids))})", ids)}
        if not ids or any(i not in rows for i in ids):
            raise Denied("unknown_grant", "the grant this was started under is not fully known on this node")
        g = Grant([_stored_block(rows[i]) for i in ids], token_hash=row["token_sha256"])
        g.check_structure()
        if rows[ids[0]]["env_family"]:  # its root continues an earlier grant's family (openajar.policy.families)
            g.extra["env_family"] = rows[ids[0]]["env_family"]
        return g

    # ================================================================ stored grants (the runner)
    # Durable runs outlive the call that created them, so they carry a grant id instead of a
    # token. These two methods turn that id back into a live grant, fail closed.

    def grant_by_id(self, grant_id: str | None) -> Grant:
        """The live grant stored under `grant_id` (OWNER_GRANT_ID is the node owner's grant).
        Raises Denied with reason unknown_grant (None, never issued here, or a broken chain),
        revoked (it or a grant it was narrowed from), expired or not_yet_valid."""
        if grant_id == OWNER_GRANT_ID:
            return Grant([owner_block()], owner_token=True)
        g = self._stored_chain(grant_id)
        self.check_live(g)
        return g

    def context_for_grant(self, grant_id: str | None, *, principal: Principal | None = None,
                          tool: str | None = None, **kw: Any) -> PolicyContext:
        """A ToolContext for work started earlier under a stored grant (queued runs, schedules,
        webhook triggers). Re-checks the grant now: Denied (see grant_by_id) when it is unknown,
        revoked or expired. `principal` defaults to the grant's own subject."""
        g = self.grant_by_id(grant_id)
        return self.context(g, principal=principal or g.principal(), tool=tool, **kw)


def _principal_from(data: dict, g: Grant) -> Principal:
    """A stored principal (a run's creator) with the grant's identity as the fallback. It may
    name the grant's subject or a sub-agent of it, never another identity (SEC-5)."""
    base = g.principal().model_dump()
    base.update({k: data[k] for k in ("kind", "id", "thread") if isinstance(data.get(k), str) and data[k]})
    try:
        claimed = Subject(kind=base["kind"], id=base["id"], owner=g.subject.owner)
        if subject_problems(g.subject, claimed):
            return g.principal()
        return Principal.model_validate({**base, "grant_id": g.id})
    except ValueError:
        return g.principal()
