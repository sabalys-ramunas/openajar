"""Grant blocks and their evaluation.

A token is a chain of blocks (see tokens.py). Block 0, the authority block, is minted by
the node and defines the grant. Each later block narrows it. Evaluation never merges
blocks: a call must satisfy every block on its own, so a block can only take permissions
away, however it was built.

Field semantics:
- `None` in an attenuation block means "no further restriction" (inherit).
- In the authority block, a missing lane scope means NO lanes, a missing ceiling means
  `read`, missing specials mean none, and `exp` is required.
- Lane scope of a block = `workspaces` (each as `ws/*`) plus `lanes` (patterns).
- The subject belongs to the authority block. A later block may keep it or derive a sub-agent
  from it: the id gains a label (`claude-code/codex-worker`) and the kind is agent, flow or
  brain, never human or panel; the owner never changes and a thread pin never moves.
- The egress allowlist starts from the authority block (its `egress`, else its `domains`,
  else nothing) and every later block can only shrink it.

Human grants: a member of the node (a person, never an agent)
holds a grant whose authority block's subject is `kind: human`. Only such a grant may carry the
`approve` special (which effects this person may decide, where, up to what amount) and the
`control` special (watch and take over a lane's live view). They are not effects: no call is
authorized by them, `ApprovalStore.decide` and the panel read them.

Versions (TOKENS.md): a v1 block carries its own revocation id (`rid`) and a child may carry a
full `sub`. A v2 authority block names its issuer (`iss`), audience (`aud`) and key (`kid`); a
v2 child carries only `label` (with `kind`) and `thread`, never a subject, and its revocation id
comes from its signature, so `rid` is never block data. Every block of one token has the
token's version.

Audience: `aud` (the nodes or orgs a grant is for) is required on a v2 authority block and is an
optional caveat on every other block, v1 included: a node accepts a token only when every block
that sets `aud` names it. A later block can narrow it (one node of the org) but never drop or move
it, because the earlier block stays in the chain and is checked on its own.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from openajar.core import globs
from openajar.core.errors import InvalidRequest
from openajar.core.models import SPECIALS, Effect, Principal
from openajar.policy import tokens
from openajar.policy.errors import Denied, TokenInvalid
from openajar.policy.memory_rules import MemoryCap, block_scope_problems, cap_widening  # memory: the special
from openajar.policy.util import iso

LADDER = (Effect.READ, Effect.DRAFT, Effect.COMMIT, Effect.PAY, Effect.IRREVERSIBLE)
HUMAN_SPECIALS = ("approve", "control")  # only in a grant whose subject is a human (a member)
OWNER_GRANT_ID = "owner"
DELEGATE_KINDS = ("agent", "flow", "brain")  # what a derived subject may be; never human or panel
SUBJECT_ID_MAX = 120
_SUBJECT_LABEL_RE = re.compile(r"^[A-Za-z0-9._:@-]+(/[A-Za-z0-9._:@-]+)*$")
PRINCIPAL_RE = r"^(node|org):[A-Za-z0-9._-]{1,100}$"   # an `iss` or `aud` value
KID_RE = r"^ed25519:[0-9a-f]{32}$"
RID_RE = r"^(rv_[a-z0-9]{8,40}|owner)$"
MAX_AUDIENCE = 16


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Subject(_Strict):
    kind: str = Field(default="agent", pattern=r"^(agent|human|flow|brain|panel)$")
    id: str = Field(min_length=1, max_length=120)
    owner: str = Field(default="owner", min_length=1, max_length=120)
    thread: str | None = Field(default=None, max_length=200)


class LaneCap(_Strict):
    """A special capability optionally limited to some lanes (secret, exec)."""
    lanes: list[str] | None = None

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_lane_pattern(p) for p in v]


class InstallCap(_Strict):
    sources: list[str] | None = None  # exact pinned sources, e.g. "npm:left-pad@1.3.0"


class EmptyCap(_Strict):
    pass


class ServiceCap(_Strict):
    max_ttl_s: int | None = Field(default=None, ge=1)
    ports: list[int] | None = None


class VmCap(_Strict):
    """Rent disposable cloud VMs (openajar.vms): optionally only as lanes matching `lanes`, only
    the named profiles (sizes the owner set up in vms.toml; "custom" allows a size of the caller's
    own), and only private repos matching `repos` (github.com/acme/* or github.com/acme/api) for
    vm_clone."""
    lanes: list[str] | None = None
    profiles: list[str] | None = None
    repos: list[str] | None = None

    @field_validator("repos")
    @classmethod
    def _repos(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_repo_pattern(p) for p in v]

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_lane_pattern(p) for p in v]


class BrainCap(_Strict):
    provider: str | None = None
    tier: str | None = None
    credential_owner: str | None = None
    quota_tokens: int | None = Field(default=None, ge=0)  # per UTC day
    quota_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)   # per UTC day


class ApproveCap(_Strict):
    """Which approvals a member may decide: these effects, on lanes inside the block's scope (and
    inside `lanes` when set), and `pay` only up to `max_usd` (no `max_usd`: no pay decisions)."""
    effects: list[Effect] = Field(min_length=1, max_length=len(Effect))
    lanes: list[str] | None = None
    max_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("effects")
    @classmethod
    def _effects(cls, v: list[Effect]) -> list[Effect]:
        if len(set(v)) != len(v):
            raise ValueError("approve lists each effect once")
        return v

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_lane_pattern(p) for p in v]


class ControlCap(_Strict):
    """Watch a lane's live view and, unless `watch_only`, take it over for at most `max_session_s`
    seconds at a time, on lanes inside the block's scope (and inside `lanes` when set)."""
    lanes: list[str] | None = None
    watch_only: bool | None = None
    max_session_s: int | None = Field(default=None, ge=1)

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_lane_pattern(p) for p in v]


class Specials(_Strict):
    secret: LaneCap | None = None
    exec: LaneCap | None = None
    install_overlay: InstallCap | None = None
    install_base: EmptyCap | None = None
    service: ServiceCap | None = None
    brain: BrainCap | None = None
    memory: MemoryCap | None = None  # memory: workspace memory (memory_rules.py)
    vm: VmCap | None = None             # disposable cloud VMs (openajar.vms)
    approve: ApproveCap | None = None   # human grants only
    control: ControlCap | None = None   # human grants only

    def names(self) -> set[str]:
        return {n for n in (*SPECIALS, *HUMAN_SPECIALS) if getattr(self, n) is not None}

    def human(self) -> set[str]:
        """The human-only specials this block sets (approve, control)."""
        return {n for n in HUMAN_SPECIALS if getattr(self, n) is not None}

    @classmethod
    def everything(cls) -> Specials:
        """Every effect special (what an owner-made agent grant may carry); never approve or control."""
        return cls(secret=LaneCap(), exec=LaneCap(), install_overlay=InstallCap(), install_base=EmptyCap(),
                   service=ServiceCap(), brain=BrainCap(), memory=MemoryCap.everything(),  # memory
                   vm=VmCap())


class Budget(_Strict):
    usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)  # Infinity or NaN: refused (SEC-10)
    tokens: int | None = Field(default=None, ge=0)
    # daily and hourly limits (openajar.policy.meters): reset at the owner's midnight, or on the hour
    usd_per_day: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tokens_per_day: int | None = Field(default=None, ge=0)
    commits_per_hour: int | None = Field(default=None, ge=0)
    commits_per_day: int | None = Field(default=None, ge=0)


BUDGET_LIMITS = ("usd_per_day", "tokens_per_day", "commits_per_hour", "commits_per_day")


class GrantBlock(_Strict):
    v: int = Field(default=1, ge=1, le=2)
    id: str = Field(pattern=r"^(gr_[a-z0-9]{8,40}|owner)$")
    rid: str | None = Field(default=None, pattern=RID_RE)  # v1: block data; v2: from the signature
    parent: str | None = None
    iat: int
    iss: str | None = Field(default=None, pattern=PRINCIPAL_RE)           # v2 authority only
    aud: list[str] | None = Field(default=None, min_length=1, max_length=MAX_AUDIENCE)  # a caveat on any block
    kid: str | None = Field(default=None, pattern=KID_RE)                 # v2 authority only
    sub: Subject | None = None
    label: str | None = Field(default=None, min_length=1, max_length=SUBJECT_ID_MAX)  # v2 children
    kind: str | None = Field(default=None, pattern=r"^(agent|flow|brain)$")            # with a label
    thread: str | None = Field(default=None, min_length=1, max_length=200)            # v2 children
    workspaces: list[str] | None = None
    lanes: list[str] | None = None
    domains: list[str] | None = None
    ceiling: Effect | None = None
    specials: Specials | None = None
    budget: Budget | None = None
    nbf: int | None = None
    exp: int | None = None
    depth: int | None = Field(default=None, ge=0, le=10)
    fanout: int | None = Field(default=None, ge=0, le=10_000)
    pins: dict[str, str] | None = None
    egress: list[str] | None = None
    admin: bool | None = None
    note: str | None = Field(default=None, max_length=300)

    @field_validator("aud")
    @classmethod
    def _aud(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            for a in v:
                if not re.fullmatch(PRINCIPAL_RE, a):
                    raise ValueError("an audience is node:<id> or org:<id>")
            if len(set(v)) != len(v):
                raise ValueError("an audience lists each node or org once")
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        if v is not None and not _SUBJECT_LABEL_RE.match(v):
            raise ValueError("a label is letters, digits and . _ : @ - in /-separated parts")
        return v

    @model_validator(mode="after")
    def _version_fields(self) -> GrantBlock:
        authority = self.parent is None
        v2_only = ("iss", "kid", "label", "kind", "thread")
        if self.v == 1:
            if self.rid is None:
                raise ValueError("a version 1 block carries a revocation id")
            if any(getattr(self, f) is not None for f in v2_only):
                raise ValueError("iss, kid, label, kind and thread need a version 2 block")
        elif authority:
            if self.iss is None or self.aud is None or self.kid is None:
                raise ValueError("a version 2 authority block names its issuer, audience and key (iss, aud, kid)")
            if any(getattr(self, f) is not None for f in ("label", "kind", "thread")):
                raise ValueError("label, kind and thread belong to narrowed blocks")
        else:
            if any(getattr(self, f) is not None for f in ("iss", "kid", "sub")):
                raise ValueError("a version 2 narrowed block cannot carry iss, kid or a subject; "
                                 "it may add a label")
            if (self.label is None) != (self.kind is None):
                raise ValueError("a label comes with its kind (agent, flow or brain)")
        if authority and self.sub is not None and self.sub.kind != "human" and self.specials is not None \
                and self.specials.human():
            raise ValueError("approve and control belong to a person's grant (subject kind human)")
        return self

    @field_validator("workspaces")
    @classmethod
    def _ws(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_segment(w) for w in v]

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_lane_pattern(p) for p in v]

    @field_validator("domains", "egress")
    @classmethod
    def _domains(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_domain_pattern(p) for p in v]

    @field_validator("ceiling")
    @classmethod
    def _ceiling(cls, v: Effect | None) -> Effect | None:
        if v is not None and v.rank is None:
            raise ValueError("the ceiling must be a ladder effect (read, draft, commit, pay, irreversible)")
        return v

    @field_validator("pins")
    @classmethod
    def _pins(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None:
            for name, h in v.items():
                if not name or not h.startswith("sha256:"):
                    raise ValueError("pins map tool names to sha256: manifest hashes")
        return v

    def lane_patterns(self, *, authority: bool) -> list[str] | None:
        if self.workspaces is None and self.lanes is None:
            return [] if authority else None
        return [f"{w}/*" for w in self.workspaces or []] + list(self.lanes or [])

    def to_data(self) -> dict:
        """The block as signed: v2 leaves out the revocation id (it is not block data)."""
        data = self.model_dump(mode="json", exclude_none=True)
        if self.v >= 2:
            data.pop("rid", None)
        return data

    def digest(self) -> str:
        """`sha256:` over the bytes this block is signed as (what the registry binds an id to)."""
        return "sha256:" + hashlib.sha256(tokens.block_bytes(self.v, self.to_data())).hexdigest()

    def declared_subject(self, parent: Subject) -> Subject | None:
        """The subject this narrowed block asks to act as, None when it keeps its parent's. v1:
        its `sub`. v2: the parent's id plus "/" plus `label` with `kind`, and `thread` pinned.
        ValueError when the derived id is too long."""
        if self.v < 2:
            return self.sub
        if self.label is None and self.thread is None:
            return None
        if self.label is None:
            return Subject(kind=parent.kind, id=parent.id, owner=parent.owner, thread=self.thread)
        return Subject(kind=self.kind or "agent", id=f"{parent.id}/{self.label}", owner=parent.owner,
                       thread=self.thread)


def subject_fields(parent: Subject, sub: Subject | None, version: int) -> dict:
    """GrantBlock fields that make a narrowed block act as `sub` (from child_subject): v1 a `sub`,
    v2 a `label`, `kind` and `thread`."""
    if sub is None:
        return {}
    if version < 2:
        return {"sub": sub}
    out: dict[str, Any] = {}
    if sub.id != parent.id or sub.kind != parent.kind:
        label = sub.id[len(parent.id) + 1:] if sub.id.startswith(parent.id + "/") else sub.id
        out.update(label=label, kind=sub.kind)
    if sub.thread is not None and sub.thread != parent.thread:
        out["thread"] = sub.thread
    return out


def parse_block(data: dict, *, version: int | None = None, rid: str | None = None,
                raw: bytes | None = None) -> GrantBlock:
    """A block from token or registry data. A token's v2 block must not carry `rid` (it is
    `rid`, derived from the signature) and its `raw` bytes must be exactly the canonical
    encoding of what this node reads them as, so no two verifiers read one block differently."""
    v = data.get("v", 1)
    if version is not None and v != version:
        raise TokenInvalid("a block's version does not match the token's")
    if isinstance(v, int) and v >= 2:
        if "rid" in data:
            raise TokenInvalid("a version 2 block cannot choose its revocation id")
        data = {**data, "rid": rid}
    try:
        block = GrantBlock.model_validate(data)
    except (ValidationError, InvalidRequest):
        raise TokenInvalid("the token carries a caveat this node does not understand") from None
    if raw is not None and block.v >= 2:
        try:
            same = tokens.block_bytes(block.v, block.to_data()) == raw
        except ValueError:
            same = False
        if not same:
            raise TokenInvalid("a block of the token is not in the canonical form this node reads it as")
    return block


def blocks_from_token(opened: tokens.Token) -> list[GrantBlock]:
    """The GrantBlocks of a verified token: v2 revocation ids from the signatures, v1 from data."""
    return [parse_block(b.data, version=opened.version, rid=b.rid if opened.version >= 2 else None,
                        raw=b.raw) for b in opened.blocks]


def subject_problems(parent: Subject, child: Subject) -> list[str]:
    """How a narrowed block's subject would take an identity its parent does not have. Empty
    when it keeps the parent's kind and id, or derives a sub-agent: the parent's id plus
    "/label", with kind agent, flow or brain (SEC-5)."""
    out = []
    if child.owner != parent.owner:
        out.append("subject.owner")
    if parent.thread is not None and child.thread is not None and child.thread != parent.thread:
        out.append("subject.thread")
    if (child.kind, child.id) != (parent.kind, parent.id):
        if child.kind not in DELEGATE_KINDS:
            out.append("subject.kind")
        label = child.id[len(parent.id) + 1:] if child.id.startswith(parent.id + "/") else ""
        if not _SUBJECT_LABEL_RE.match(label):
            out.append("subject.id")
    return out


def derived_subject(parent: Subject, child: Subject) -> Subject:
    """The subject a valid narrowed block acts as: its kind and id, the parent's owner, and the
    parent's thread unless it pins one."""
    return Subject(kind=child.kind, id=child.id, owner=parent.owner, thread=child.thread or parent.thread)


def child_subject(parent: Subject, wanted: Subject, *, truncate: bool = False) -> Subject:
    """The subject for a narrowed grant asked for as `wanted`: the parent's own when the kind
    and id match, else a sub-agent whose id is the parent's id plus "/" plus `wanted.id` (an id
    already under the parent's is kept). `truncate` shortens a label that does not fit
    (SUBJECT_ID_MAX); otherwise a too long id is refused."""
    if (wanted.kind, wanted.id) == (parent.kind, parent.id) or wanted.id.startswith(parent.id + "/"):
        sub_id = wanted.id
    else:
        sub_id = f"{parent.id}/{wanted.id}"
    if len(sub_id) > SUBJECT_ID_MAX:
        if not truncate:
            raise InvalidRequest(f"a narrowed grant's subject id is {parent.id!r} plus a label, at most "
                                 f"{SUBJECT_ID_MAX} characters in all")
        sub_id = sub_id[:SUBJECT_ID_MAX].rstrip("/")
        if len(sub_id) <= len(parent.id) + 1:
            return parent
    owner = parent.owner if wanted.owner == "owner" else wanted.owner  # another owner is refused later
    return Subject(kind=wanted.kind, id=sub_id, owner=owner, thread=wanted.thread or parent.thread)


def owner_block() -> GrantBlock:
    specials = Specials.everything().model_copy(update={"approve": ApproveCap(effects=list(Effect)),
                                                        "control": ControlCap()})
    return GrantBlock(id=OWNER_GRANT_ID, rid=OWNER_GRANT_ID, iat=0,
                      sub=Subject(kind="human", id="owner", owner="owner"),
                      workspaces=["*"], ceiling=Effect.IRREVERSIBLE, specials=specials, admin=True)


def fake_grant(subject: str, *, family: str | None = None, workspaces: list[str] | None = None) -> Grant:
    """An unsigned grant for a test's or a fake's context: every workspace by default, its root id (the
    family) derived from `subject` unless `family` names one, so the same subject is the same family
    unless a test says otherwise. Never resolved from a token, so it never reaches a real call."""
    root = family or "gr_" + hashlib.sha256(subject.encode()).hexdigest()[:12]
    return Grant([GrantBlock(v=1, id=root, rid="rv_" + root[3:], iat=0, sub=Subject(id=subject),
                             workspaces=workspaces or ["*"], ceiling=Effect.IRREVERSIBLE)])


def coerce_effect(effect: Any) -> Effect:
    """An effect by name; an unknown one is refused (step 1 of the authorization order)."""
    if isinstance(effect, Effect):
        return effect
    try:
        return Effect(str(effect))
    except ValueError:
        raise Denied("unknown_effect", f"{str(effect)[:40]!r} is not an effect this node knows") from None


def tier_rank(tier: str, tiers: list[str], aliases: dict[str, str]) -> int | None:
    t = aliases.get(tier.lower(), tier.lower())
    return tiers.index(t) if t in tiers else None


@dataclass
class Grant:
    """A verified chain of blocks plus where it came from. Pure: no I/O."""
    blocks: list[GrantBlock]
    token_hash: str | None = None
    owner_token: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # the node owner's ceiling on a controller (openajar.policy.ceiling), set on grants from the
    # controller's org: it lowers `ceiling` and takes away the specials it does not allow
    limit: Any = None

    # ---- identity ----
    @property
    def id(self) -> str:
        return self.blocks[-1].id

    @property
    def root_id(self) -> str:
        return self.blocks[0].id

    @property
    def env_family(self) -> str:
        """The family whose environments, saves and title-bound logins this grant reaches
        (openajar.environments): its root grant's id, or, for a local agent grant that replaced an
        earlier one of this computer, the family that one had (openajar.policy.families). Only for
        environments: the checks of who asked (self-approval, a task's owner, taint) key on root_id."""
        return str(self.extra.get("env_family") or self.root_id)

    @property
    def chain_ids(self) -> list[str]:
        return [b.id for b in self.blocks]

    @property
    def rids(self) -> list[str]:
        return [b.rid for b in self.blocks]

    @property
    def path(self) -> str:
        return "/" + "/".join(self.chain_ids) + "/"

    @property
    def depth(self) -> int:
        return len(self.blocks) - 1

    @property
    def subject(self) -> Subject:
        """The authority block's subject, or the sub-agent a later block derived from it. A block
        that would take another identity is ignored here (check_structure refuses it)."""
        sub = self.blocks[0].sub
        assert sub is not None
        for b in self.blocks[1:]:
            try:
                wanted = b.declared_subject(sub)
            except ValueError:
                continue
            if wanted is not None and not subject_problems(sub, wanted):
                sub = derived_subject(sub, wanted)
        return sub

    @property
    def version(self) -> int:
        return self.blocks[0].v

    @property
    def kid(self) -> str | None:
        return self.blocks[0].kid

    def principal(self) -> Principal:
        s = self.subject
        return Principal(kind=s.kind, id=s.id, owner=s.owner, thread=s.thread, grant_id=self.id)

    @property
    def expires_at(self) -> int | None:
        exps = [b.exp for b in self.blocks if b.exp is not None]
        return min(exps) if exps else None

    @property
    def not_before(self) -> int | None:
        nbfs = [b.nbf for b in self.blocks if b.nbf is not None]
        return max(nbfs) if nbfs else None

    def audience_allows(self, audiences: set[str]) -> bool:
        """Every block that names an audience names one of these (the node's own `aud` values)."""
        return all(set(b.aud) & audiences for b in self.blocks if b.aud is not None)

    @property
    def admin(self) -> bool:
        return bool(self.blocks[0].admin) and all(b.admin is not False for b in self.blocks[1:])

    # ---- structure (checked once when the token is resolved) ----
    def check_structure(self) -> None:
        auth = self.blocks[0]
        if auth.parent is not None or auth.sub is None:
            raise TokenInvalid("the authority block is malformed")
        if auth.exp is None and not self.owner_token:
            raise TokenInvalid("the grant has no expiry")
        if not self.owner_token and any(OWNER_GRANT_ID in (b.id, b.rid) for b in self.blocks):
            # "owner" names the node token's own grant; a token block using it could read the
            # owner's approvals and traces (viewer filters match on grant ids)
            raise TokenInvalid("the token uses a reserved grant id")
        if any(b.v != auth.v for b in self.blocks):
            raise TokenInvalid("the token mixes block versions")
        if auth.sub.kind != "human" and any(b.specials is not None and b.specials.human() for b in self.blocks):
            raise TokenInvalid("approve and control belong to a person's grant; an agent's grant cannot carry them")
        seen = set()
        sub = auth.sub
        for i, b in enumerate(self.blocks):
            if b.id in seen:
                raise TokenInvalid("the token repeats a block")
            seen.add(b.id)
            if i > 0:
                if b.parent != self.blocks[i - 1].id:
                    raise TokenInvalid("the token's blocks are not a chain")
                try:
                    wanted = b.declared_subject(sub)
                except ValueError:
                    raise TokenInvalid(f"a narrowed grant's subject id is at most {SUBJECT_ID_MAX} characters") from None
                if wanted is not None:
                    problems = subject_problems(sub, wanted)
                    if "subject.owner" in problems:
                        raise TokenInvalid("a narrowed grant cannot change its owner")
                    if "subject.thread" in problems:
                        raise TokenInvalid("a narrowed grant cannot move to another thread")
                    if problems:
                        raise TokenInvalid("a narrowed grant cannot take another identity; it may only "
                                           "add a sub-agent label to its parent's subject")
                    sub = derived_subject(sub, wanted)
            if b.depth is not None and len(self.blocks) - 1 - i > b.depth:
                raise Denied("depth", f"grant {b.id} allows {b.depth} more levels of delegation")
        if auth.sub.kind == "human" and sub.kind != "human":
            # a person's grant is never handed to an agent (attenuate refuses it); a block appended
            # offline must not turn a member's call into an agent's with the member's exemptions
            raise TokenInvalid("a person's grant cannot be narrowed into an agent's, flow's or brain's")

    def check_time(self, now: float, skew: float) -> None:
        for b in self.blocks:
            if b.nbf is not None and now < b.nbf - skew:
                raise Denied("not_yet_valid", f"this grant is valid from {iso(b.nbf)}")
            if b.exp is not None and now > b.exp + skew:
                raise Denied("expired", f"this grant expired at {iso(b.exp)}")

    # ---- scope ----
    def has_lane_scope(self) -> bool:
        pats = self.blocks[0].lane_patterns(authority=True)
        return bool(pats) and all(
            (p := b.lane_patterns(authority=False)) is None or bool(p) for b in self.blocks[1:]
        )

    def lane_allowed(self, lane_id: str) -> bool:
        for i, b in enumerate(self.blocks):
            pats = b.lane_patterns(authority=i == 0)
            if pats is None:
                continue
            if not any(globs.lane_match(p, lane_id) for p in pats):
                return False
        return True

    def domain_allowed(self, host: str) -> bool:
        for b in self.blocks:
            if b.domains is not None and not any(globs.domain_match(p, host) for p in b.domains):
                return False
        return True

    def egress_lists(self) -> list[list[str]]:
        """The egress allowlists a sink must be inside, one per block that sets one. Each block's
        list is its `egress`, else its `domains`. The authority block always contributes (with
        neither, an empty list: every sink is outside), so a narrowed grant can only shrink the
        set, never open a sink its authority did not have (SEC-3)."""
        auth = self.blocks[0]
        first = auth.egress if auth.egress is not None else auth.domains
        lists = [list(first or [])]
        for b in self.blocks[1:]:
            lst = b.egress if b.egress is not None else b.domains
            if lst is not None:
                lists.append(lst)
        return lists

    def egress_allows(self, host: str | None) -> bool:
        """Is this sink inside every block's egress allowlist? A call with no host is outside."""
        if host is None:
            return False
        return all(any(globs.domain_match(p, host) for p in lst) for lst in self.egress_lists())

    def fanout_limit(self) -> int | None:
        """How many narrowed grants this grant may hand out: the smallest fan-out of any block in
        its chain, so a child never hands out more than an ancestor allows."""
        limits = [b.fanout for b in self.blocks if b.fanout is not None]
        return min(limits) if limits else None

    # ---- effects ----
    @property
    def ceiling(self) -> Effect:
        ranks = [(self.blocks[0].ceiling or Effect.READ).rank]
        ranks += [b.ceiling.rank for b in self.blocks[1:] if b.ceiling is not None]
        if self.limit is not None:
            ranks.append(self.limit.max_effect.rank)
        return LADDER[min(r for r in ranks if r is not None)]

    def caps(self, name: str) -> list[Any] | None:
        """The cap objects for a special from every block that constrains it, or None when
        some block does not grant it."""
        if self.limit is not None and not self.limit.allows_special(name):
            return None
        auth = self.blocks[0].specials or Specials()
        # a special with no grant field yet (core PENDING_SPECIALS, such as agent) is never granted
        first = getattr(auth, name, None)
        if first is None:
            return None
        out = [first]
        for b in self.blocks[1:]:
            if b.specials is None:
                continue
            cap = getattr(b.specials, name, None)
            if cap is None:
                return None
            out.append(cap)
        return out

    def specials(self) -> set[str]:
        return {n for n in SPECIALS if self.caps(n) is not None}

    # ---- a person's grant: deciding approvals and controlling lanes ----
    @property
    def human(self) -> bool:
        """A member's grant (or the owner's): the subject the grant acts as, after every block, is a
        person (check_structure refuses a person's grant narrowed into another kind)."""
        return self.blocks[0].sub is not None and self.subject.kind == "human"

    def workspace(self) -> str | None:
        """The one workspace every lane of this grant is in, or None (several, a pattern, none)."""
        for i, b in enumerate(self.blocks):
            pats = b.lane_patterns(authority=i == 0)
            found = {p.split("/", 1)[0] if "/" in p else p for p in pats or []}
            if len(found) == 1 and "*" not in (ws := found.pop()):
                return ws
        return None

    def covers(self, lane_id: str | None, workspace: str | None = None) -> bool:
        """Is this lane (or, with no lane, every lane of `workspace`; with neither, every lane) inside
        the grant's lane scope? What a person's grant sees and decides on."""
        if lane_id is not None:
            return self.lane_allowed(lane_id)
        return _lanes_within([f"{workspace}/*" if workspace else "*/*"], self)

    def approve_problem(self, effect: str | None, lane_id: str | None, *, workspace: str | None = None,
                        amount_usd: float | None = None) -> str | None:
        """Why this grant may not approve an approval of `effect` on `lane_id`, or None when it may.
        Pure (no liveness, family or four-eyes check: ApprovalStore.decide adds those). Every block
        must allow it: the lane inside its scope and its `approve.lanes`, the effect in its
        `approve.effects`, and for pay a known amount at most its `approve.max_usd`. A question
        (no effect) needs approve, or control that is not watch-only, on its workspace."""
        if self.owner_token:
            return None
        if not self.human:
            return "only a person decides approvals; an agent's grant never does"
        if not self.covers(lane_id, workspace):
            return f"this member does not cover {lane_id or workspace or 'every workspace'}"
        approve = self.caps("approve")
        if effect is None:
            control = self.caps("control")
            if approve is None and (control is None or any(c.watch_only for c in control)):
                return "this member does not answer questions"
            return None
        if approve is None:
            return "this member does not decide approvals"
        try:
            eff = Effect(effect)
        except ValueError:
            return f"{effect[:40]!r} is not an effect this node knows"
        for cap in approve:
            if eff not in cap.effects:
                return f"this member does not decide {eff.value}"
            if cap.lanes is not None and (lane_id is None or not any(globs.lane_match(p, lane_id)
                                                                     for p in cap.lanes)):
                return f"this member does not decide on {lane_id or 'this lane'}"
            if eff == Effect.PAY:
                if cap.max_usd is None:
                    return "this member does not decide pay (no max_usd)"
                if amount_usd is None or not amount_usd <= cap.max_usd:
                    shown = "an unknown amount" if amount_usd is None else f"{amount_usd:.2f} USD"
                    return f"{shown} is over this member's limit of {cap.max_usd:.2f} USD"
        return None

    def control_terms(self, lane_id: str) -> dict | None:
        """What this grant may do with a lane's live view: {"watch_only", "max_session_s"} (the
        strictest of every block), or None when it carries no control for this lane."""
        caps = self.caps("control")
        if caps is None or not self.lane_allowed(lane_id):
            return None
        if any(c.lanes is not None and not any(globs.lane_match(p, lane_id) for p in c.lanes) for c in caps):
            return None
        limits = [c.max_session_s for c in caps if c.max_session_s is not None]
        return {"watch_only": any(c.watch_only for c in caps), "max_session_s": min(limits) if limits else None}

    def effect_allowed(self, effect: Effect) -> bool:
        if effect.rank is not None:
            return effect.rank <= self.ceiling.rank
        return self.caps(effect.value) is not None

    def pins(self) -> list[dict[str, str]]:
        return [b.pins for b in self.blocks if b.pins is not None]

    def pins_allow(self, tool: str, manifest: str) -> bool:
        return all(p.get(tool) == manifest for p in self.pins())

    def budgets(self) -> list[tuple[str, Budget]]:
        return [(b.id, b.budget) for b in self.blocks if b.budget is not None]

    # ---- readable projection (what the panel and `grants show` display) ----
    def projection(self) -> dict:
        s = self.subject
        return {
            "id": self.id, "root_id": self.root_id, "chain": self.chain_ids, "depth": self.depth,
            "subject": s.model_dump(), "admin": self.admin,
            "ceiling": self.ceiling.value,
            "specials": sorted(self.specials() | {n for n in HUMAN_SPECIALS if self.caps(n) is not None}),
            "blocks": [b.to_data() for b in self.blocks],
            "not_before": iso(self.not_before), "expires_at": iso(self.expires_at),
            "owner_token": self.owner_token, "version": self.version,
        }


# ---------------------------------------------------------------- narrowing checks

def _lanes_within(child: list[str], grant: Grant) -> bool:
    for i, b in enumerate(grant.blocks):
        pats = b.lane_patterns(authority=i == 0)
        if pats is None:
            continue
        if not all(any(globs.lane_pattern_within(c, p) for p in pats) for c in child):
            return False
    return True


def _domains_within(child: list[str], parents: list[list[str]]) -> bool:
    return all(all(any(globs.domain_pattern_within(c, p) for p in lst) for c in child) for lst in parents)


def widening_problems(parent: Grant, child: GrantBlock, *, tiers: list[str], aliases: dict[str, str]) -> list[str]:
    """Fields of `child` that try to grant more than `parent` holds. Empty means it narrows.
    Evaluation would ignore a widening anyway; this exists to fail loudly when narrowing."""
    problems = [*_scope_widening(parent, child), *_specials_widening(parent, child, tiers, aliases),
                *_budget_widening(parent, child), *_limits_widening(parent, child),
                *_delegation_widening(parent, child), *_subject_widening(parent, child)]
    return sorted(set(problems))


def _scope_widening(parent: Grant, child: GrantBlock) -> list[str]:
    """Lanes, domains, egress and the effect ceiling."""
    problems: list[str] = []
    child_pats = child.lane_patterns(authority=False)
    if child_pats is not None and not _lanes_within(child_pats, parent):
        problems.append("lanes")
    if child.domains is not None:
        parents = [b.domains for b in parent.blocks if b.domains is not None]
        if not _domains_within(child.domains, parents):
            problems.append("domains")
    if child.egress is not None and not _domains_within(child.egress, parent.egress_lists()):
        problems.append("egress")
    if child.ceiling is not None and child.ceiling.rank > parent.ceiling.rank:
        problems.append("ceiling")
    return problems


def _specials_widening(parent: Grant, child: GrantBlock, tiers: list[str], aliases: dict[str, str]) -> list[str]:
    """A special the parent does not hold, or caps wider than every block of the parent that grants it."""
    if child.specials is None:
        return []
    problems: list[str] = []
    for name in child.specials.names():
        caps = parent.caps(name)
        if caps is None:
            problems.append(f"specials.{name}")
            continue
        ccap = getattr(child.specials, name)
        problems += [f"specials.{name}.{p}" for p in _cap_widening(name, ccap, caps, tiers, aliases)]
    problems += block_scope_problems(child, parent)  # memory: workspaces inside the lane scope
    return problems


def _budget_widening(parent: Grant, child: GrantBlock) -> list[str]:
    """A budget past any of the parent's: no budget is past every one."""
    if child.budget is None:
        return []
    problems: list[str] = []
    for _, b in parent.budgets():
        if b.usd is not None and (child.budget.usd is None or child.budget.usd > b.usd):
            problems.append("budget.usd")
        if b.tokens is not None and (child.budget.tokens is None or child.budget.tokens > b.tokens):
            problems.append("budget.tokens")
        for name in BUDGET_LIMITS:  # a child never asks for a larger daily or hourly limit
            mine, theirs = getattr(child.budget, name), getattr(b, name)
            if theirs is not None and mine is not None and mine > theirs:
                problems.append(f"budget.{name}")
    return problems


def _limits_widening(parent: Grant, child: GrantBlock) -> list[str]:
    """Its time window, admin and audience."""
    problems: list[str] = []
    if child.exp is not None and parent.expires_at is not None and child.exp > parent.expires_at:
        problems.append("exp")
    if child.nbf is not None and parent.not_before is not None and child.nbf < parent.not_before:
        problems.append("nbf")
    if child.admin and not parent.admin:
        problems.append("admin")
    if child.aud is not None and not _audience_within(child.aud, parent):
        problems.append("aud")
    return problems


def _delegation_widening(parent: Grant, child: GrantBlock) -> list[str]:
    """Its tool pins, and how deep and wide it may delegate."""
    problems: list[str] = []
    if child.pins is not None and any(any(p.get(t) != h for t, h in child.pins.items()) for p in parent.pins()):
        problems.append("pins")
    if child.depth is not None:
        remaining = _remaining_depth(parent)
        if remaining is not None and child.depth > remaining - 1:
            problems.append("depth")
    if child.fanout is not None and (limit := parent.fanout_limit()) is not None and child.fanout > limit:
        problems.append("fanout")
    return problems


def _subject_widening(parent: Grant, child: GrantBlock) -> list[str]:
    """A subject the child may not declare under this parent."""
    try:
        wanted = child.declared_subject(parent.subject)
    except ValueError:
        return ["subject.id"]
    return subject_problems(parent.subject, wanted) if wanted is not None else []


def _audience_within(child: list[str], parent: Grant) -> bool:
    """Is every audience of `child` inside each audience the parent's blocks set? A node is inside
    its org (`org:<id>`), since a node only accepts an org audience from its own org."""
    for b in parent.blocks:
        if b.aud is None:
            continue
        orgs = any(a.startswith("org:") for a in b.aud)
        if not all(a in b.aud or (orgs and a.startswith("node:")) for a in child):
            return False
    return True


def _remaining_depth(grant: Grant) -> int | None:
    rem = None
    n = len(grant.blocks)
    for i, b in enumerate(grant.blocks):
        if b.depth is not None:
            r = b.depth - (n - 1 - i)
            rem = r if rem is None else min(rem, r)
    return rem


def _patterns_wider(child: list[str] | None, parent: list[str] | None, within: Callable[[str, str], bool]) -> bool:
    """The parent limits these patterns and the child does not, or names one outside every parent pattern."""
    return parent is not None and (child is None or not all(any(within(c, q) for q in parent) for c in child))


def _set_wider(child: list[Any] | None, parent: list[Any] | None) -> bool:
    """The parent limits this list and the child does not, or holds something the parent does not."""
    return parent is not None and (child is None or not set(child) <= set(parent))


def _limit_wider(child: Any, parent: Any) -> bool:
    """The parent sets this limit and the child sets none, or a larger one."""
    return parent is not None and (child is None or child > parent)


def _lanes_wider(child: Any, p: Any) -> bool:
    return _patterns_wider(child.lanes, p.lanes, globs.lane_pattern_within)


def _lane_cap_widening(child: Any, p: Any) -> list[tuple[str, bool]]:
    return [("lanes", _lanes_wider(child, p))]


def _vm_widening(child: Any, p: Any) -> list[tuple[str, bool]]:
    return [("lanes", _lanes_wider(child, p)), ("profiles", _set_wider(child.profiles, p.profiles)),
            ("repos", _patterns_wider(child.repos, p.repos, globs.repo_pattern_within))]


def _install_widening(child: Any, p: Any) -> list[tuple[str, bool]]:
    return [("sources", _set_wider(child.sources, p.sources))]


def _service_widening(child: Any, p: Any) -> list[tuple[str, bool]]:
    return [("max_ttl_s", _limit_wider(child.max_ttl_s, p.max_ttl_s)), ("ports", _set_wider(child.ports, p.ports))]


def _approve_widening(child: Any, p: Any) -> list[tuple[str, bool]]:
    # a parent with no max_usd approves no pay at all, so any child max is wider
    max_usd = child.max_usd is not None and (p.max_usd is None or child.max_usd > p.max_usd)
    return [("lanes", _lanes_wider(child, p)), ("effects", not set(child.effects) <= set(p.effects)),
            ("max_usd", max_usd)]


def _control_widening(child: Any, p: Any) -> list[tuple[str, bool]]:
    return [("lanes", _lanes_wider(child, p)), ("watch_only", bool(p.watch_only and not child.watch_only)),
            ("max_session_s", _limit_wider(child.max_session_s, p.max_session_s))]


# the special's caps, field by field: (field, wider than this parent block's?)
_CAP_WIDENING: dict[str, Callable[[Any, Any], list[tuple[str, bool]]]] = {
    "secret": _lane_cap_widening, "exec": _lane_cap_widening, "vm": _vm_widening,
    "install_overlay": _install_widening, "service": _service_widening, "approve": _approve_widening,
    "control": _control_widening,
}


def _brain_widening(child: Any, p: Any, tiers: list[str], aliases: dict[str, str]) -> list[str]:
    out = [f for f in ("provider", "credential_owner") if getattr(p, f) is not None and getattr(child, f) != getattr(p, f)]
    if p.tier is not None:
        pr = tier_rank(p.tier, tiers, aliases)
        cr = tier_rank(child.tier, tiers, aliases) if child.tier else None
        if pr is None or cr is None or cr > pr:
            out.append("tier")
    return out + [f for f in ("quota_tokens", "quota_usd") if _limit_wider(getattr(child, f), getattr(p, f))]


def _cap_widening(name: str, child: Any, parents: list[Any], tiers: list[str], aliases: dict[str, str]) -> list[str]:
    """The fields of the child's caps for special `name` wider than one of the parent blocks' caps."""
    out: list[str] = []
    for p in parents:
        if name == "brain":
            out += _brain_widening(child, p, tiers, aliases)
        elif name == "memory":  # memory: workspaces and types shrink, flags only turn off
            out += cap_widening(child, p)
        elif name in _CAP_WIDENING:
            out += [field for field, wider in _CAP_WIDENING[name](child, p) if wider]
    return out
