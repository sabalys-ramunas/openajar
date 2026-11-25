"""Errors with stable codes, so the MCP layer can map them to clear tool errors."""

from __future__ import annotations


class OpenajarError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidRequest(OpenajarError):
    code = "invalid"


class ConfirmRequired(InvalidRequest):
    """The change is one a site would notice (a lane moved onto another device: a new computer to
    its site), so it needs an explicit yes. Nothing was changed. `details` holds the plan: what
    changes and the warnings, in plain words."""
    code = "confirm_required"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class LaneNotFound(OpenajarError):
    code = "lane_not_found"


class LaneExists(OpenajarError):
    code = "lane_exists"


class LaneBusy(OpenajarError):
    """All warm slots are active and none freed up within busy_timeout, or the lane stayed taken
    (by the calls ahead in its queue, or another caller's claim) for queue_timeout. The message
    names who holds the lane, since when, and the caller's place in the queue; `details` has the
    same as data when the lane's queue was the cause."""
    code = "busy"


class ClaimLost(OpenajarError):
    """The caller's claim on a lane ended before this call (it expired, a human took control, a
    hold or a freeze, a revoked grant, the lane closed for memory, or the node restarted), so the
    call did nothing. Told once, on the holder's next call on that lane."""
    code = "claim_lost"


class LaneQuarantined(OpenajarError):
    code = "quarantined"


class RefNotFound(OpenajarError):
    """A snapshot ref no longer resolves (page changed). Take a new snapshot."""
    code = "ref_not_found"


class EngineError(OpenajarError):
    code = "engine_error"


class PolicyDenied(OpenajarError):
    """The caller's grant does not allow this call (scope, effect, budget, expiry, revoked)."""
    code = "denied"


class AuditUnwritable(OpenajarError):
    """The action log has not been able to record anything for a while (the database and its
    spill file both refuse rows), so calls that change anything are refused until it can.
    Reading still works (openajar.policy.audit_health)."""
    code = "audit_unwritable"


class ApprovalPending(OpenajarError):
    """The call needs a human co-sign. Not a failure: the agent waits or checks back."""
    code = "approval_pending"

    def __init__(self, message: str, approval_id: str, why: str | None = None, confirm: dict | None = None):
        super().__init__(message)
        self.approval_id = approval_id
        self.why = why  # why it waits when the routing table alone would not say (after untrusted text)
        self.confirm = confirm  # how strong the yes must be, and whether a person can give it in chat


class ServiceUnavailable(OpenajarError):
    """A runtime service this tool needs is not configured on this node."""
    code = "unavailable"


class NotFound(OpenajarError):
    code = "not_found"


class MachineGone(OpenajarError):
    """An environment's machine stopped for good: its provider says the container or server no longer runs or
    no longer exists (openajar.environments.liveness). Nothing ran on it. The message says when, why, and how
    the same title goes on (a new machine, from the newest save when there is one)."""
    code = "machine_gone"


class HumanInControl(OpenajarError):
    """A human has taken control of this lane. The agent waits (browser_wait_handback) or asks
    for it back (browser_request_control); it must not retry the action in a loop."""
    code = "human_in_control"


class AgentPaused(OpenajarError):
    """Agents are held off this lane: a person stopped them, or openajar paused it itself after a
    challenge or a rate limit. The message says which (openajar.core.pauses)."""
    code = "agent_paused"


class Frozen(AgentPaused):
    """The lane, its workspace, this node or the whole mesh is frozen: no agent acts there until
    the owner unfreezes it, and grants that reached it were revoked. Do not retry; tell your user."""
    code = "frozen"


class Conflict(OpenajarError):
    """The request clashes with what exists: a duplicate, a state that already changed (an
    approval decided twice, a revoked grant), or a plan that would break a rule. Nothing was
    changed."""
    code = "conflict"


class Cancelled(OpenajarError):
    """The caller cancelled the request before it finished (a node channel invoke, a run)."""
    code = "cancelled"


class DeadlineExceeded(OpenajarError):
    """The request ran past its deadline and was stopped."""
    code = "deadline_exceeded"


class FormChanged(OpenajarError):
    """The form a click or Enter would submit no longer holds what was read for its approval: the
    page changed a field, where the form goes, or the form itself between the read and the click,
    or the page could not be read at all for the approval. Nothing was clicked."""
    code = "form_changed"


class PublishChanged(OpenajarError):
    """A person approved a publish, but the act, run again, made the page send other content
    (another text, target, media or any other field the digest binds): nothing was sent, and the
    old approval is retired. The commit gate (openajar.node.commitgate, AUD-03)."""
    code = "publish_changed"


class PublishUnbound(OpenajarError):
    """A publish the commit gate cannot bind whole, so no person could approve exactly what it
    sends: a body too large, too deep or in a format the node cannot read, or media the node never
    saw uploaded (or could not hash). Nothing was sent (openajar.node.commitgate, AUD-03)."""
    code = "publish_unbound"



class EgressRefused(PolicyDenied):
    """The lane's egress guard refused a navigation or a request: the host is outside the lane's
    site list or the egress list of the grant driving the lane, or the request is a publish the
    grant does not allow (docs/design/egress-guard.md). The page stays where it was. Do not
    retry; tell the human which host was refused."""
    code = "egress_refused"
