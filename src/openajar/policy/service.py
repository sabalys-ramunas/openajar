"""The policy service: grants, principal resolution, budgets, taint and authorization.

`build(runtime)` creates and registers five runtime services:
  policy     PolicyService   (this module)
  approvals  ApprovalStore   (openajar.policy.approvals)
  traces     TraceStore      (openajar.policy.traces)
  notifier   Notifier        (openajar.policy.notifier)
  ask_first  AskFirst        (openajar.policy.ask_first: "Ask me first" per routine and account)

Authorization order for every `ctx.authorize(effect, ...)` (see ROUTING.md):
  1. the effect is known                      else denied: unknown_effect
  2. the grant is in its time window and no block in its chain is revoked
  2b. no freeze covers the call (openajar.freeze), and the action log records: anything but a read
     is refused with audit_unwritable once nothing was recorded for audit_fail_closed_s
  3. the lane and domain are inside every block's scope
  4. the effect is under the ceiling, or the special is granted (with its parameters)
  5. the budget is not spent; a stated cost is reserved atomically, and the day's meters
     (openajar.policy.meters) are debited in the same step: a limit reached refuses the call
  6. the routing table decides auto / approval / deny; a commit runs on its own unless "Ask me
     first" is on for its routine or account (ask_first.py); pay and irreversible always ask
  6b. on a lane of the caller's own agent environment (openajar.environments) nothing asks: a rule
     that would ask runs on its own, and 7 and 7b do not apply (pay and irreversible still ask)
  7. the lethal-trifecta overlay can turn auto into approval
  7b. after untrusted content in the call's context, the effects in `[taint] downgrade` need a
     person, and only a standing approval bound to what the owner saw covers them
  8. approval: a standing approval, else a matching approved one-off, else a new pending

Members: a person other than the owner holds member grants,
one per scope, made by `issue_member_grant` from fixed roles (MEMBER_ROLES). Their subject is
`kind: human`; they carry `approve` and `control`, are never bearer tokens (`resolve`, and so
MCP, refuses them), are never narrowed, and die together with `revoke_member`. The one human
grant a call may carry is a controller's short-lived per-call token for a member acting in its
panel, and only through the channel's `invoke` (`channel_call`, TOKENS.md).

Where the code lives: this module builds the service (its state, tool surface and background
loop). PolicyContext is in context.py. PolicyService takes the rest from mixins, one module each,
all sharing the state its __init__ sets up:
  authorize.py   the decision itself: `authorize_call`, which runs the order above, with the
                 routing, taint and approval steps (6 to 8)
  registry.py    keys, issuing, narrowing, resolving and revoking grants (step 2)
  families.py    the environment family a replacing local agent grant keeps, and the ones left behind
  scope.py       lane, domain and effect inside the grant (steps 3 and 4)
  budgets.py     budgets, brain quotas and the day's meters (step 5)
  lane_pace.py   a lane's posting pace and waking hours (step 5, for a commit)
  taint.py       the taint ledger, and starting, clearing and showing it (read by 7 and 7b)
  members.py     people other than the owner, and their grants
  principals.py  the live grant of a caller resolved earlier (panel, runner, brains)
  run_grants.py  a person's approval as one planned run's authority (the agenda)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from openajar.core.config import NodeConfig, home_from_env
from openajar.core.errors import OpenajarError
from openajar.core.tools import ToolSpec
from openajar.policy import cards, memory_rules  # memory: memory_rules
from openajar.policy.approvals import ApprovalStore
from openajar.policy.ask_first import AskFirst
from openajar.policy.authorize import AuthorizeMixin
from openajar.policy.budgets import (
    BudgetsMixin,
)
from openajar.policy.confirm import DeviceStore, default_registry
from openajar.policy.confirm_chat import ChatConfirm
from openajar.policy.confirmation import ConfirmationStore
from openajar.policy.errors import Denied
from openajar.policy.families import FamiliesMixin
from openajar.policy.grants import Grant
from openajar.policy.keys import KeyRing, link_key
from openajar.policy.lane_pace import LanePaceMixin
from openajar.policy.manifest import manifest_hash, requires_lane
from openajar.policy.members import (
    MembersMixin,
)
from openajar.policy.memory_port import MemoryPolicy  # memory: the memory service's policy port
from openajar.policy.meters import Meters
from openajar.policy.notifier import Notifier
from openajar.policy.pace import PaceStore
from openajar.policy.principals import PrincipalsMixin
from openajar.policy.registry import (
    GrantRegistryMixin,
)
from openajar.policy.routing import PolicyConfig, RoutingTable, load_policy_config
from openajar.policy.run_grants import (
    RunGrantsMixin,
)
from openajar.policy.scope import ScopeMixin
from openajar.policy.scrub import runtime_scrub
from openajar.policy.store import DB
from openajar.policy.taint import (
    TaintLedger,
    TaintMixin,
)
from openajar.policy.telegram import TelegramService
from openajar.policy.traces import EventBus, TraceEvent, TraceStore

log = logging.getLogger("openajar.policy")
TELEGRAM_SYNC_S = 5.0  # how long one background tick waits for Telegram to take message edits

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY,
  parent_id TEXT, root_id TEXT NOT NULL, path TEXT NOT NULL, depth INTEGER NOT NULL,
  rid TEXT NOT NULL, token_sha256 TEXT, source TEXT NOT NULL,
  subject TEXT NOT NULL, block TEXT NOT NULL,
  issued_at REAL NOT NULL, expires_at REAL, issued_by TEXT
);
CREATE INDEX IF NOT EXISTS grants_parent ON grants(parent_id);
CREATE TABLE IF NOT EXISTS revoked (
  grant_id TEXT PRIMARY KEY, rid TEXT UNIQUE, revoked_at REAL NOT NULL, revoked_by TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS first_use (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS taint (
  session TEXT NOT NULL, label TEXT NOT NULL, source TEXT, at REAL NOT NULL, PRIMARY KEY (session, label)
);
CREATE TABLE IF NOT EXISTS taint_origins (
  context TEXT NOT NULL, family TEXT NOT NULL, kind TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
  source TEXT, first_at REAL NOT NULL, last_at REAL NOT NULL, PRIMARY KEY (context, kind, host)
);
CREATE TABLE IF NOT EXISTS taint_turns (
  grant_id TEXT PRIMARY KEY, family TEXT NOT NULL, at REAL NOT NULL, seed TEXT
);
"""


def _migrate(db: DB) -> None:
    """Registry columns added for token v2, and revocations keyed by grant id (SEC-6)."""
    cols = {r["name"] for r in db.all("PRAGMA table_info(grants)")}
    if "v" not in cols:
        db.execute("ALTER TABLE grants ADD COLUMN v INTEGER NOT NULL DEFAULT 1")
    if "block_sha256" not in cols:
        db.execute("ALTER TABLE grants ADD COLUMN block_sha256 TEXT")
    if "env_family" not in cols:  # the family a local agent grant continues (openajar.policy.families)
        db.execute("ALTER TABLE grants ADD COLUMN env_family TEXT")
    # a v2 revocation id comes from the block's signature, so it names exactly one grant; v1 ids
    # were block data (an offline child could copy one) and are no longer used to revoke
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS grants_rid_v2 ON grants(rid) WHERE v >= 2")
    if db.one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'revocations'") is not None:
        with db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO revoked (grant_id, rid, revoked_at, revoked_by, reason) "
                         "SELECT grant_id, NULL, MIN(revoked_at), revoked_by, reason FROM revocations "
                         "GROUP BY grant_id")
            conn.execute("DROP TABLE revocations")


def _panel_url(node_config: NodeConfig | None) -> str:
    """Where this node serves its panel (on its MCP port), for links without a public URL."""
    port = getattr(node_config, "mcp_port", 0) or 7400  # 0 picks a port at bind time: the default then
    return f"http://localhost:{port}"


class PolicyService(AuthorizeMixin, GrantRegistryMixin, FamiliesMixin, MembersMixin, PrincipalsMixin, ScopeMixin,
                    BudgetsMixin, LanePaceMixin, TaintMixin, RunGrantsMixin):
    """Runtime service "policy". Its methods outside the authorization decision come from the mixins
    (see the module docstring); every one of them reads the state set up here."""

    def __init__(self, home: Path, *, node_config: NodeConfig | None = None, config: PolicyConfig | None = None,
                 runtime: Any = None, clock: Callable[[], float] = time.time, transport: Any = None):
        self.home = Path(home)
        self.node_config = node_config
        self.runtime = runtime
        self.clock = clock
        self.local_clock = clock  # this node's own: the channel moves `clock` to the controller's
        self.cfg = config or load_policy_config(self.home)
        self.ring = KeyRing(self.home / "secrets", clock=clock)
        self.db = DB(self.home / "policy.db", SCHEMA, busy_timeout_ms=self.cfg.db_busy_timeout_ms)
        _migrate(self.db)
        self.meters = Meters(self.db, cfg=self.cfg.meters, clock=clock)
        self.pace = PaceStore(self.db, clock=clock)  # posting pace and waking hours per lane (AUD-13)
        self._pin_zones: dict[str, str] = {}  # the last zone read from each lane's pinned fingerprint
        self.local_id = self._local_id()
        self.bus = EventBus()
        self.owner_token = (node_config.mcp_token if node_config else "") or ""
        self.traces = TraceStore(self.home / "traces.db", bus=self.bus, busy_timeout_ms=self.cfg.trace_busy_timeout_ms,
                                 retention_days=self.cfg.trace_retention_days,
                                 scrub=[self.owner_token] if len(self.owner_token) >= 6 else [], clock=clock)
        if runtime is not None:
            self.traces.value_scrub = runtime_scrub(runtime)
        self.approvals = ApprovalStore(self.db, bus=self.bus, link_key=link_key(self.home / "secrets", self.key),
                                       clock=clock,
                                       approval_ttl_s=self.cfg.approval_ttl_s,
                                       standing_effects=self.cfg.routing.standing_effects,
                                       content_effects=self.cfg.routing.content_effects)
        self.notifier = Notifier(self.home, self.db, link_signer=self.approvals.sign_action, clock=clock,
                                 transport=transport, panel_url=_panel_url(node_config))
        self.approvals.notifier = self.notifier
        self.approvals.policy = self.cfg.approvals
        self.approvals.check_live = self.check_live  # a decider's grant is re-checked on every decision
        self.approvals.issued_ok = self._issued_standing_ok
        self.approvals.issued_confirm_s = 3600.0 * getattr(node_config, "controller_standing_confirm_hours", 24)
        # the card the panel shows (openajar.policy.cards), from the content the approval binds
        self.approvals.card_builder = lambda tool, lane, content: cards.build_card(tool, lane, content,
                                                                                   runtime=self.runtime)
        self.approvals.card_files = lambda ap, param: cards.read_image(self.runtime, ap.card, param)
        if runtime is not None:  # previews, cards and the controller's copy never hold a vault value
            self.approvals.scrub = runtime_scrub(runtime)
        self.taint = TaintLedger(self.db, ttl_s=self.cfg.session_ttl_s, clock=clock)
        self._must_ask: dict[str, set[str]] = {}  # grant id -> what always waits for a person (must_ask)
        self.devices = DeviceStore(self.db, clock=lambda: self.clock())  # a person's devices (confirm.py)
        self.confirm_methods = default_registry(self.devices, telegram_paired=lambda: self.telegram.paired())
        self.confirmation = ConfirmationStore(self.db, base=self.cfg.confirmation, clock=lambda: self.clock(),
                                              methods=self.confirm_methods)  # how strong a yes must be
        self.approvals.confirmation = self.confirmation.current
        self.approvals.methods = self.confirm_methods
        self.approvals.backfill_requires()
        self.approvals.may_decide = lambda ap, person: self.person_grant(ap, person) is not None
        self.chat = ChatConfirm(self)  # approval_confirm (confirm_chat.py)
        self.telegram = TelegramService(self, transport=transport)  # approve from your phone (telegram.py)
        self.notifier.telegram = self.telegram
        # "Ask me first" (ask_first.py): a switch nobody set follows the setting's commit.when
        self.ask_first = AskFirst(self.db, clock=lambda: self.clock(), default=self.confirmation.ask_first_default)
        self.memory_rules = MemoryPolicy(self)  # memory: the port openajar.memory takes as `policy`
        self._task: asyncio.Task | None = None

    def routing_now(self) -> RoutingTable:
        """The routing table as it reads now: a person may have changed `commit.when` since it loaded."""
        return self.cfg.routing.model_copy(update={"ask_first_default": self.ask_first.default})

    def apply_setting_to_waiting(self, *, by: str) -> list[dict]:
        """A person applies the setting to what already waits (docs/CONFIRMATIONS.md, Approvals carry it),
        and Activity says so. Reached from the CLI and the panel only: a controller has no way here."""
        moved = self.approvals.apply_setting_to_waiting()
        shown = ", ".join(f"{m['id']} from {m['was']} to {m['now']}" for m in moved[:5])
        rest = f", and {len(moved) - 5} more" if len(moved) > 5 else ""
        count = f"{len(moved)} waiting approval{'' if len(moved) == 1 else 's'}"
        self.traces.write(TraceEvent(kind="confirmation_applied", status="ok", principal=by, principal_kind="human",
                                     summary=f"applied the setting to {count}{': ' + shown + rest if moved else ''}",
                                     args={"moved": moved[:50]}))
        return moved

    # ================================================================ tool surface
    def visible_tools(self, token: str | None, specs: list[ToolSpec], *, runtime: Any = None) -> list[ToolSpec]:
        """The tools a token may see: effect within the grant, pins matching, lane tools only
        for grants with a lane scope, and `hidden_without` services present."""
        try:
            _, g = self.resolve(token)
        except OpenajarError:
            return []
        rt = runtime if runtime is not None else self.runtime
        # a tool above the node's controller ceiling stays listed, so calling it says why (controller_ceiling)
        own = replace(g, limit=None) if g.limit is not None else g
        out = []
        for s in specs:
            if rt is not None and s.hidden_without and not rt.has(s.hidden_without):
                continue
            if not own.effect_allowed(s.effect):
                continue
            if g.pins() and not g.pins_allow(s.name, manifest_hash(s)):
                continue
            if requires_lane(s) and not g.has_lane_scope():
                continue
            if not memory_rules.spec_visible(g, s):  # memory: memory tools need the memory special
                continue
            if s.view is not None:  # narrowed to what this grant may use (a connector tool's surfaces)
                seen = s.view(g.lane_allowed)
                if seen is None:
                    continue
                s = replace(s, seen=seen)
            out.append(s)
        return out

    def check_pins(self, g: Grant, spec: ToolSpec) -> None:
        if g.pins() and not g.pins_allow(spec.name, manifest_hash(spec)):
            raise Denied("pins", f"this grant does not allow {spec.name} as it is now (tool manifest changed or not pinned)")

    # ================================================================ lifecycle
    async def tick(self) -> None:
        """One round of background work. Safe to call by hand (tests, CLI)."""
        try:
            await self.db.run(self.approvals.sweep)  # expiries: off the loop, a CLI's write lock never stalls it
            await self.traces.aflush()
            await self.notifier.flush()
            with contextlib.suppress(asyncio.TimeoutError):  # Telegram being slow never holds up this loop
                # a decision made anywhere shows on its Telegram message
                await asyncio.wait_for(self.telegram.sync_outcomes(), TELEGRAM_SYNC_S)
        except Exception:
            log.exception("policy background tick failed")

    async def start(self, *, interval_s: float = 5.0) -> None:
        if self._task is not None:
            return
        try:
            self.traces.end_unfinished()  # calls a killed agentosd cut off (docs/BENCH.md, chaos)
        except Exception:
            log.exception("could not close the calls a stop of agentosd cut off")

        async def loop() -> None:
            last_daily = 0.0
            while True:
                await self.tick()
                try:
                    self.telegram.supervise()  # the poller runs while a bot is paired or pairing
                except Exception:
                    log.exception("the Telegram poller could not be started or stopped")
                if time.monotonic() - last_daily > 86400 or last_daily == 0.0:
                    last_daily = time.monotonic()
                    await self.traces.db.run(self.traces.purge)  # retention: off the loop
                    await self.db.run(self._purge_policy)
                    await asyncio.to_thread(self._purge_captures)
                await asyncio.sleep(interval_s)

        self.notifier._loop = asyncio.get_running_loop()  # an approval made on policy.db's worker kicks the flush
        self._task = asyncio.get_running_loop().create_task(loop())

    def _purge_captures(self) -> None:
        """Screenshots kept beside the action log (openajar.audit.captures) go when its rows do, also on a
        lane that stopped keeping them."""
        try:
            from openajar.policy.capture_files import prune

            prune(self.home, retention_days=self.traces.retention_days)
        except Exception:
            log.exception("kept screenshots could not be pruned")

    def _purge_policy(self) -> None:
        self.taint.purge()
        self.meters.purge()
        self.pace.purge()
        self.memory_rules.purge()  # memory: context receipts of ended families

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.telegram.stop()

    def close(self) -> None:
        self.db.close()
        self.traces.close()  # writes what waits and signs a shutdown checkpoint


def build(runtime: Any) -> PolicyService:
    """Composition-root hook: build the policy services and register policy, approvals,
    traces, notifier and ask_first on the runtime."""
    cfg = runtime.get("config") if runtime is not None else None
    home = cfg.home if cfg is not None else home_from_env()
    svc = PolicyService(home, node_config=cfg, runtime=runtime)
    if runtime is not None:
        runtime.register("policy", svc)
        runtime.register("approvals", svc.approvals)
        runtime.register("traces", svc.traces)
        runtime.register("notifier", svc.notifier)
        runtime.register("ask_first", svc.ask_first)
        runtime.register("confirmation", svc.confirmation)
        svc.notifier.attach(runtime)  # one push when a lane is held (lanes are registered before services)
        svc.attach_lanes(runtime)  # a person's own publish goes on the lane's posting pace
        from openajar.policy import ledger

        try:
            ledger.build(runtime, home)  # every lane's likes, follows and posts, whoever made them
        except Exception:
            log.exception("the action ledger could not be opened; caps count only each engine's own actions")
    return svc


