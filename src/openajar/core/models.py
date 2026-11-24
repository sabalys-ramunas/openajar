"""Data shared across the node, the MCP surface and the CLI."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openajar.core import globs  # pure parsing helpers, shared with the node's URL checks

LANE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}/[a-z0-9][a-z0-9-]{0,40}$")
WORKSPACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")  # the workspace part of a lane id (fullmatch)
# A device's name (AUD-02): one pinned fingerprint plus one proxy that several lanes
# share, like one office PC with a browser profile per account. Example: biz-a-pc.
DEVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
# A proxy's label in secrets/proxies.json (`openajar proxy add lt1 ...`), used with fullmatch.
PROXY_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,40}")
# `none` means "off the device" wherever a device is named (lane set-device, the API's ?to=).
RESERVED_DEVICE_NAMES = frozenset({"none"})


def device_name_problem(v: str) -> str | None:
    if not DEVICE_NAME_RE.fullmatch(v):
        return f"invalid device name {v!r}: lowercase letters, digits and dashes, for example biz-a-pc"
    if v in RESERVED_DEVICE_NAMES:
        return f"{v!r} is not a device name: it means a lane on no device. Pick another, for example biz-a-pc"
    return None


def parse_lane_id(value: str) -> tuple[str, str]:
    """`workspace/name` -> (workspace, name). Raises ValueError if malformed."""
    if not isinstance(value, str) or not LANE_ID_RE.fullmatch(value):
        raise ValueError(f"an id must look like 'workspace/name' (lowercase, digits, dashes): {value!r}")
    workspace, name = value.split("/", 1)
    return workspace, name


def utcnow() -> datetime:
    return datetime.now(UTC)


MAX_SITES = 200


FRAME_PREFIX = "frame:"   # a site entry that admits the host only as a subframe document
COUNTRY_TOKEN = "@cc"     # `accounts.google.@cc`: that host on any of Google's country domains

# Google's country domains (the suffix after "google."), from https://www.google.com/supported_domains.
# Google's sign-in sets its cookies on the country domain of the exit IP too (a SetSID redirect
# through accounts.google.lt for a Lithuanian proxy), so a site entry `accounts.google.@cc` matches
# accounts.google.<one of these>. Only Google's own domains: never a bare TLD.
GOOGLE_COUNTRY_SUFFIXES: frozenset[str] = frozenset((
    "com", "ad", "ae", "com.af", "com.ag", "al", "am", "co.ao", "com.ar", "as", "at", "com.au",
    "az", "ba", "com.bd", "be", "bf", "bg", "com.bh", "bi", "bj", "com.bn", "com.bo", "com.br",
    "bs", "bt", "co.bw", "by", "com.bz", "ca", "cd", "cf", "cg", "ch", "ci", "co.ck", "cl", "cm",
    "cn", "com.co", "co.cr", "com.cu", "cv", "com.cy", "cz", "de", "dj", "dk", "dm", "com.do", "dz",
    "com.ec", "ee", "com.eg", "es", "com.et", "fi", "com.fj", "fm", "fr", "ga", "ge", "gg",
    "com.gh", "com.gi", "gl", "gm", "gr", "com.gt", "gy", "com.hk", "hn", "hr", "ht", "hu", "co.id",
    "ie", "co.il", "im", "co.in", "iq", "is", "it", "je", "com.jm", "jo", "co.jp", "co.ke",
    "com.kh", "ki", "kg", "co.kr", "com.kw", "kz", "la", "com.lb", "li", "lk", "co.ls", "lt", "lu",
    "lv", "com.ly", "co.ma", "md", "me", "mg", "mk", "ml", "com.mm", "mn", "com.mt", "mu", "mv",
    "mw", "com.mx", "com.my", "co.mz", "com.na", "com.ng", "com.ni", "ne", "nl", "no", "com.np",
    "nr", "nu", "co.nz", "com.om", "com.pa", "com.pe", "com.pg", "com.ph", "com.pk", "pl", "pn",
    "com.pr", "ps", "pt", "com.py", "com.qa", "ro", "ru", "rw", "com.sa", "com.sb", "sc", "se",
    "com.sg", "sh", "si", "sk", "com.sl", "sn", "so", "sm", "sr", "st", "com.sv", "td", "tg",
    "co.th", "com.tj", "tl", "tm", "tn", "to", "com.tr", "tt", "com.tw", "co.tz", "com.ua", "co.ug",
    "co.uk", "com.uy", "co.uz", "com.vc", "co.ve", "co.vi", "com.vn", "vu", "ws", "rs", "co.za",
    "co.zm", "co.zw", "cat"
))


def split_site(entry: str) -> tuple[bool, str]:
    """A stored site entry -> (frame only, domain pattern)."""
    if entry.startswith(FRAME_PREFIX):
        return True, entry[len(FRAME_PREFIX):]
    return False, entry


def _site_pattern(raw: str) -> str:
    """One site entry (without `frame:`) in the policy's globs syntax, or the one country form:
    `<host>.google.@cc` (or `*.google.@cc`), which matches that host on Google's own country
    domains only (GOOGLE_COUNTRY_SUFFIXES). `@cc` anywhere else is refused, so
    a bare TLD wildcard cannot be written."""
    from openajar.core.errors import InvalidRequest

    p = raw.strip().lower()
    if COUNTRY_TOKEN in p:
        stem = p.removesuffix("." + COUNTRY_TOKEN)
        if stem == p or COUNTRY_TOKEN in stem or not (stem == "google" or stem.endswith(".google")):
            raise InvalidRequest(f"{COUNTRY_TOKEN} only follows google., as in accounts.google.{COUNTRY_TOKEN}")
        return globs.validate_domain_pattern(stem + ".com").removesuffix(".com") + "." + COUNTRY_TOKEN
    return globs.validate_domain_pattern(p)


def validate_sites(patterns: list[str]) -> list[str]:
    """Normalize a lane's site list (openajar.node.egress): domain patterns in the policy's globs
    syntax (`instagram.com`, `*.instagram.com`), lowercase, each once, in the order given. Two
    narrow forms on top: `frame:<pattern>` admits the host only as a subframe (a captcha or a
    sign-in helper frame, never a page), and `<host>.google.@cc` matches that host on Google's
    country domains (accounts.google.lt, accounts.google.co.uk). `*` is refused: an empty list
    already means no lane restriction. Raises ValueError."""
    from openajar.core.errors import InvalidRequest

    if not isinstance(patterns, list):
        raise ValueError("sites must be a list of domain patterns")  # noqa: TRY004 - pydantic reads ValueError
    out: list[str] = []
    for raw in patterns:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("a site is a domain pattern such as instagram.com or *.instagram.com")
        frame, body = split_site(raw.strip().lower())
        try:
            p = _site_pattern(body)
        except InvalidRequest as e:
            raise ValueError(f"bad site {raw.strip()[:80]!r}: {e.message}") from None
        if p == "*":
            raise ValueError("a site list cannot be '*'; an empty list already means no lane restriction")
        entry = FRAME_PREFIX + p if frame else p
        if entry not in out:
            out.append(entry)
    if len(out) > MAX_SITES:
        raise ValueError(f"a lane lists at most {MAX_SITES} sites")
    return out


class LaneKind(StrEnum):
    BROWSER = "browser"
    TERMINAL = "terminal"   # a shell environment (openajar.env)
    SERVICE = "service"     # a supervised long-running process (openajar.env)
    ANDROID = "android"     # an Android instance (openajar.android)
    AGENT = "agent"         # the owner's own coding agent, hosted on the node (openajar.agents)


class Effect(StrEnum):
    """What a tool call does. Grants carry a ceiling on the ordered ladder plus a set of
    special capabilities; approvals route by effect (see docs/ARCHITECTURE.md section 5)."""
    # ordered ladder
    READ = "read"
    DRAFT = "draft"                # private writes (a draft, a form not yet submitted)
    COMMIT = "commit"              # publish or post on the bound site
    PAY = "pay"
    IRREVERSIBLE = "irreversible"  # delete, send
    # special capabilities (not on the ladder)
    SECRET = "secret"              # login and vault fill
    EXEC = "exec"                  # run commands in a lane
    INSTALL_OVERLAY = "install_overlay"  # add a program to one lane
    INSTALL_BASE = "install_base"        # re-bake a shared base image (many lanes)
    SERVICE = "service"            # run a long-running service
    BRAIN = "brain"                # spend model capacity (subscription, local or metered)
    AGENT = "agent"                # send to, attach to or stop an agent lane (policy lands in brief B3)
    VM = "vm"                      # rent, extend or delete a disposable cloud machine (openajar.vms)

    @property
    def rank(self) -> int | None:
        """Position on the ordered ladder, or None for a special capability."""
        return _LADDER.get(self)


# Specials named in the contracts whose grant field and checks are not built yet. Surfaces that
# list specials an owner may grant leave these out until the policy can carry them.
PENDING_SPECIALS = frozenset({Effect.AGENT})

# Every special a grant can carry (openajar.policy.grants), and what a controller ceiling may list.
SPECIALS = ("secret", "exec", "install_overlay", "install_base", "service", "brain",
            "memory", "vm")  # memory: a special with no effect of its own (memory_rules.py)

_LADDER = {Effect.READ: 0, Effect.DRAFT: 1, Effect.COMMIT: 2, Effect.PAY: 3, Effect.IRREVERSIBLE: 4}


class Preset(StrEnum):
    """Friendly isolation presets; beginners pick one of these and never see the dials."""
    ACCOUNT = "account"      # browser/android: identity proxy, private profile, no shell
    WORKBENCH = "workbench"  # trusted terminal: shares the node, installs freely
    SANDBOX = "sandbox"      # throwaway exec: private ephemeral fs, no network or allowlist
    SERVICE = "service"      # long-running: private persistent fs, allowlist, pinned programs
    LOCKED = "locked"        # strictest: deny-all network unless allowlisted, signed programs only
    AGENT = "agent"          # an agent lane: isolated network to model hosts and git remotes, shell on


# What people see. The ids stay stable in the API and node.toml; the dials sit behind "advanced".
PRESET_LABELS: dict[str, tuple[str, str]] = {
    "account": ("Sealed account", "one account's own browser, proxy and device; no shell"),
    "workbench": ("Shared desk", "a trusted terminal that shares the node and installs freely"),
    "sandbox": ("Scratch", "a throwaway space with no network and nothing kept"),
    "service": ("Service", "a long-running program with its own disk and an allowlist"),
    "locked": ("Locked down", "nothing gets out unless allowed, signed programs only"),
    "agent": ("Agent", "your own coding agent with its own home; it reaches only its model and your repos"),
}


class IsolationProfile(BaseModel):
    """The four isolation dials of a lane (docs/ARCHITECTURE.md section 3)."""
    network: str = "isolated"        # "shared" | "isolated" | "off"
    egress_allowlist: list[str] = Field(default_factory=list)  # hosts; empty = no restriction
    filesystem: str = "overlay"      # "shared" | "overlay" | "ephemeral"
    processes: str = "isolated"      # "shared" | "isolated"
    programs: str = "base"           # "base" | "overlay"
    volumes: list[str] = Field(default_factory=list)  # named shared volumes mounted rw
    base_image: str | None = None    # template ref, e.g. "dev-box@1"


class RunStatus(StrEnum):
    """Honest run outcomes. Never a bare 'done'."""
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED_VERIFIED = "succeeded_verified"
    SUCCEEDED_UNVERIFIED = "succeeded_unverified"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Principal(BaseModel):
    """Who is calling a tool."""
    kind: str = "agent"              # "agent" | "human" | "flow" | "brain"
    id: str                          # stable id of the caller (token subject)
    owner: str = "owner"             # the human who owns this principal
    thread: str | None = None        # agent thread or session, when known
    grant_id: str | None = None      # the grant the call runs under


class LaneState(StrEnum):
    COLD = "cold"            # profile on disk, no process
    WARMING = "warming"      # display, viewer and browser starting; self-check running
    WARM = "warm"            # browser up and idle
    ACTIVE = "active"        # a tool call holds the lane
    QUARANTINED = "quarantined"  # failed self-check; refuses traffic until reopened


class Signal(StrEnum):
    """Deterministic page state. Never inferred by a model reading the page."""
    OK = "ok"
    CAPTCHA = "captcha"
    CHECKPOINT = "checkpoint"      # the site asks a human a question (verify it's you, confirm a code)
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"


class ActionKind(StrEnum):
    CLICK = "click"
    TYPE = "type"      # human-rhythm typing into the focused or referenced field
    FILL = "fill"      # clear the field, then type
    SELECT = "select"  # pick an option by its label
    PRESS = "press"    # a key, for example "Enter"
    HOVER = "hover"
    SCROLL = "scroll"  # wheel-scroll the referenced element into view


class ProxyConfig(BaseModel):
    label: str
    host: str
    port: int
    username: str | None = None
    password: str | None = Field(default=None, repr=False)  # never logged, never returned by a tool
    # socks5: an agent environment's tunnel to its VM (openajar.environments), DNS through it too
    scheme: Literal["http", "socks5"] = "http"
    # False for an environment's tunnel: its browser carries no account, and it exits from the VM,
    # not from `host` (a socket on the node), so the exit-IP guard has nothing to protect or compare.
    check_exit: bool = True

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    def public(self) -> dict:
        """What may be shown to an agent or a log: no credentials."""
        return {"label": self.label, "host": self.host, "port": self.port}


class AccountRef(BaseModel):
    """The account a lane is expected to be logged into, checked from the site's own session
    (a cookie or the page's own data), never from page text (AUD-12)."""
    site: str                      # site key from openajar.node.sites, for example "instagram"
    handle: str | None = None      # what the owner calls it, for example "@acme.coffee"
    user_id: str | None = None     # the site's stable id once seen (c_user, ds_user_id, ...)


class LaneSpec(BaseModel):
    id: str
    kind: LaneKind = LaneKind.BROWSER
    engine: str = "camoufox"
    proxy_label: str | None = None
    locale: str = "en-US"
    account: AccountRef | None = None
    created_at: datetime = Field(default_factory=utcnow)
    # The lane's site list: domain patterns (openajar.core.globs syntax) its documents may load
    # from, checked on the node for every navigation (openajar.node.egress). Empty: no lane
    # restriction, as for every lane made before the list existed.
    sites: list[str] = Field(default_factory=list)
    # Strict egress: while an agent acts, a route blocks requests the guard refuses before they
    # leave. Off by default: any route stops the browser storing responses (egress-guard.md).
    egress_strict: bool = False
    # The device the lane runs on (AUD-02): it launches with the device's pinned fingerprint and
    # proxy, and keeps its own profile. None: the lane is a device of its own (its own pin).
    device: str | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        parse_lane_id(v)
        return v

    @field_validator("device")
    @classmethod
    def _valid_device(cls, v: str | None) -> str | None:
        problem = device_name_problem(v) if v is not None else None
        if problem:
            raise ValueError(problem)
        return v

    @field_validator("sites")
    @classmethod
    def _valid_sites(cls, v: list[str]) -> list[str]:
        return validate_sites(v)

    @property
    def workspace(self) -> str:
        return parse_lane_id(self.id)[0]

    @property
    def name(self) -> str:
        return parse_lane_id(self.id)[1]


class DeviceSpec(BaseModel):
    """A device (AUD-02): one pinned fingerprint plus one proxy, shared by the lanes
    that name it, like one office PC with a browser profile per account. The pin is made on the
    first launch of any of its lanes (or taken from a lane with `--from-lane`) and lives at
    `devices/<name>/fingerprint.json`; each lane on the device keeps a copy as its own pin."""
    name: str
    proxy_label: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    from_lane: str | None = None   # the lane whose pin the device took when it was made
    note: str | None = Field(default=None, max_length=200)
    # The one workspace (company) whose lanes it carries, set by its first lane: a device is one
    # computer, so two companies' accounts on it are linked by every site that sees them.
    workspace: str | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        problem = device_name_problem(v)
        if problem:
            raise ValueError(problem)
        return v


# ---------------------------------------------------------------- surfaces (AUD-01)

ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SITE_KEY_RE = re.compile(r"^[a-z][a-z0-9]{0,30}$")
LANG_CODE_RE = re.compile(r"^[a-z]{2}$")
ACTS_AS_KINDS = ("account", "fb_page", "li_company", "ig_business", "lane")
ACTS_AS_LABELS = {"account": "account", "fb_page": "Page", "li_company": "company page",
                  "ig_business": "business account", "lane": "lane session"}


class ActsAs(BaseModel):
    """Who a surface posts as. `account`: the lane's own login, pinned by the site's stable
    user id (the identity cookie, AUD-12). `lane`: the lane's own session on a site with no row in
    openajar.node.sites, so there is no identity cookie to pin (a site API, docs/SITE-APIS.md). The
    others are assets the login administers (a Facebook Page, a LinkedIn company page), named by
    their id on the site."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["account", "fb_page", "li_company", "ig_business", "lane"]
    user_id: str | None = Field(default=None, pattern=ASSET_ID_RE.pattern)  # kind account: the pinned id
    id: str | None = Field(default=None, pattern=ASSET_ID_RE.pattern)       # a Page or company id
    name: str | None = Field(default=None, max_length=120)                  # what the owner calls it

    @model_validator(mode="after")
    def _check(self) -> ActsAs:
        if self.kind == "lane":
            if self.user_id or self.id:
                raise ValueError("acts_as lane is the lane's own session: it names no id")
            return self
        if self.kind == "account" and (not self.user_id or self.id):
            raise ValueError("acts_as account pins the account's user_id (from the identity cookie)")
        if self.kind != "account" and (not self.id or self.user_id):
            raise ValueError(f"acts_as {self.kind} names the asset id")
        return self

    @property
    def asset_id(self) -> str:
        return self.user_id or self.id or ""

    def label(self) -> str:
        """'biz-a (Page)', '@biza.coffee (account)': for approval cards and summaries."""
        if self.kind == "lane":
            return self.name or ACTS_AS_LABELS["lane"]
        return f"{self.name or self.asset_id} ({ACTS_AS_LABELS[self.kind]})"


class Surface(BaseModel):
    """Who you post as, separate from the login you post through (AUD-01).

    A lane is a login; a surface is an identity that login acts as. `biz-a/facebook-page`
    acts as the biz-a Page through the lane `personal/facebook`; `biz-a/instagram` acts as its
    own account through the lane of the same name. Grants, approvals, schedules and connector
    tools target the surface (a grant scoped to workspace biz-a covers biz-a/facebook-page);
    the surface decides the lane. The surface id has the lane id's shape, and its workspace is
    the brand it speaks for, which may differ from the lane's."""
    model_config = ConfigDict(extra="forbid")

    id: str
    site: str                         # openajar.node.sites key, which is also the connector key
    lane: str                         # the login it acts through
    acts_as: ActsAs
    account_language: str = "en"      # the account's UI language (AUD-39), picks per-language names
    title: str | None = Field(default=None, max_length=120)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None

    @field_validator("id", "lane")
    @classmethod
    def _ids(cls, v: str) -> str:
        parse_lane_id(v)
        return v

    @field_validator("site")
    @classmethod
    def _site(cls, v: str) -> str:
        if not SITE_KEY_RE.match(v):
            raise ValueError("site must be a lowercase site key like 'facebook'")
        return v

    @field_validator("account_language")
    @classmethod
    def _lang(cls, v: str) -> str:
        if not LANG_CODE_RE.match(v):
            raise ValueError("account_language must be a two-letter code like en")
        return v

    @property
    def workspace(self) -> str:
        return parse_lane_id(self.id)[0]

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ViewerInfo(BaseModel):
    """Live view of a warm lane. x11vnc listens on a unix socket in a directory only agentosd's
    user can open, and is reached through agentosd's signed-link relay (/view, /viewer-ws) or the
    channel's vnc stream; it has no TCP listener, so another local user (the brain user included)
    cannot drive the browser. No raw websockify port is opened either (an unauthenticated one on
    localhost is reachable by any web page while a tunnel is open)."""
    display: str              # e.g. ":110"
    vnc_port: int | None = None    # a TCP upstream on 127.0.0.1: test fakes only, never a real node
    vnc_socket: str | None = None  # x11vnc's unix socket; preferred over vnc_port when set
    xauthority: str | None = None  # the display's Xauthority file: Xvfb only takes clients that hold its cookie
    viewer_port: int | None = None  # a raw websockify + noVNC port; None on nodes that no longer run one
    url_path: str = "/vnc.html?autoconnect=1&resize=scale"
    width: int | None = None   # the display's size in pixels: the pinned screen, the window at its pinned place
    height: int | None = None

    def tunnel_hint(self, ssh_host: str = "your-node", local_port: int | None = None) -> str:
        if self.viewer_port is None:
            return "the live view is served by agentosd through a signed link: ask for one with browser_view"
        local = local_port or self.viewer_port
        return (f"ssh -N -L {local}:localhost:{self.viewer_port} {ssh_host}  "
                f"then open http://localhost:{local}{self.url_path}")


class ControlState(StrEnum):
    """Who may act in a lane right now. Enforced by LaneManager.using(), so no agent action,
    flow step or brain task can touch a lane a human has taken."""
    AGENT = "agent"    # agents, flows and brains may act; the human view is read-only
    HUMAN = "human"    # a human took control; agent calls wait or get human_in_control
    PAUSED = "paused"  # a person stopped agents, or openajar holds the lane (control.reason); until handed back


class LaneControl(BaseModel):
    state: ControlState = ControlState.AGENT
    by: str | None = None              # who took control or paused (a human principal id)
    since: datetime | None = None
    takeback_requested: bool = False   # an agent asked for control back
    takeback_reason: str | None = None
    takeback_by: str | None = None
    last_human_input: datetime | None = None  # from the viewer relay, for idle hand-back
    reason: str | None = None          # why: "needs_human:checkpoint", "wrong_account", a note
    hold_until: datetime | None = None # a self-clearing hold (rate_limited backoff); None: until a human
    handback_note: str | None = None   # what the human said when handing back (shown to agents once)


class LaneInfo(BaseModel):
    spec: LaneSpec
    state: LaneState = LaneState.COLD
    last_used: datetime | None = None
    viewer: ViewerInfo | None = None
    exit_ip: str | None = None
    quarantine_reason: str | None = None
    control: LaneControl = Field(default_factory=LaneControl)
    signal: Signal | None = None        # the last signal a tool saw in this lane (None: none yet)
    signal_at: datetime | None = None
    hold: str | None = None             # why agents are held off the lane (a bad signal, a quarantine)
    hold_until: datetime | None = None  # when a self-clearing hold ends; None with a hold: a human clears it
    claim: dict[str, Any] | None = None  # who claims the lane for a multi-step job (openajar.node.claims)
    queue_length: int = 0               # callers waiting for the lane


class NavigateResult(BaseModel):
    url: str
    title: str
    status: int | None = None
    signal: Signal = Signal.UNKNOWN
    signal_detail: str | None = None     # which rule matched: "instagram challenge URL", "DataDome frame"
    account: dict[str, Any] | None = None  # {site, user_id, source, status: match|wrong_account|unpinned}


class SnapshotResult(BaseModel):
    url: str
    title: str
    text: str            # accessibility outline with [ref=eN] markers on actionable nodes
    ref_count: int
    truncated: bool = False
    signal: Signal = Signal.UNKNOWN
    signal_detail: str | None = None
    account: dict[str, Any] | None = None
    notices: list[str] = Field(default_factory=list)  # the dialog and tab lines on top of `text`
    frozen: bool = False  # a dialog froze the page, so `text` is only the notices


class ActionResult(BaseModel):
    ok: bool
    action: ActionKind
    ref: str | None = None
    url: str
    signal: Signal = Signal.UNKNOWN
    changed: bool | None = None  # read-back of the target's own state, when it can be checked
    detail: str | None = None
    signal_detail: str | None = None
    account: dict[str, Any] | None = None


class RunKind(StrEnum):
    FLOW = "flow"
    BRAIN = "brain"
    SCRIPT = "script"
    WORKFLOW = "workflow"
    PILOT = "pilot"  # openajar.pilot: browser_task's cheap worker on one lane
