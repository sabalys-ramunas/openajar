"""Data for agent environments: the record of one environment, and the node's settings for them."""

from __future__ import annotations

import re
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SETTINGS_FILE = "environments.toml"
WORKSPACE = "env"                  # every environment's lanes live in this workspace
ID_RE = re.compile(r"^e[a-z0-9]{3,15}$")   # new ones are e + 10 (openajar.environments.manager.new_id)

# What is stored. "working" and "idle" are read from the last call when a person or an agent asks.
StoredState = Literal["starting", "ready", "closing", "closed", "failed"]
OPEN_STATES = ("starting", "ready", "closing")
LIVE_STATES = ("starting", "ready")   # what environment_open hands back (shown as starting, ready, working, idle)
WORKING = timedelta(minutes=2)     # a call this recent: the environment is "working"
IDLE = timedelta(minutes=10)       # no call for this long: "idle"
QUIET = timedelta(minutes=10)      # the default take_over_minutes: no call this long, another chat may take it over
CALLS_KEPT = 50
HANDOFFS_KEPT = 5
BRANCHES_KEPT = 200   # branches an environment created, as the git gateway reads them
# The sizes an agent asks for by name: at least this many CPUs and GB of memory. On Hetzner the
# cheapest server type in the node's location that has them (openajar.vms plans it); on the docker
# provider a container with these limits, at most the computer's (openajar.vms.docker.SIZE_TYPES).
SIZES: dict[str, tuple[int, int]] = {"small": (2, 4), "medium": (4, 8), "large": (8, 16), "xlarge": (16, 32)}
SizeName = Literal["small", "medium", "large", "xlarge"]
# Where an environment's browser runs: "node", on the node with its traffic through the machine (a SOCKS
# tunnel); "vm", on the machine itself, driven and watched over the node's SSH connection
# (openajar.environments.vmbrowser), for a node with no browser of its own (a Mac).
BrowserWhere = Literal["node", "vm"]
# What happens to an environment's machine when the work stops (openajar.environments.saving):
# "none": single use, deleted on close, idle or the time limit (a rescue save first if work is unsaved);
# "live": the machine is kept and billed until a final close; "save": its whole state is saved (a disk
# snapshot and a file-level save) and the machine deleted, and environment_open with its title resumes it.
Keep = Literal["none", "live", "save"]
KEEPS = ("none", "live", "save")
# Hetzner's list price for snapshot storage, EUR per GB a month (hetzner.com/cloud, "Snapshots"; net of
# VAT, as of 2026). The provider bills a snapshot by its compressed size (the image's image_size).
SNAPSHOT_EUR_PER_GB_MONTH = 0.0119


class EnvSettings(BaseModel):
    """`$OPENAJAR_HOME/environments.toml`. The VM limits of vms.toml (max_live, max_ttl_minutes,
    monthly_budget_eur) hold as well; these are the environments' own, and the tighter one wins."""
    model_config = ConfigDict(extra="forbid")
    idle_minutes: int = Field(default=60, ge=5)           # no call, command or shell this long: closed (when saved)
    upgrade_quiet_minutes: int = Field(default=30, ge=5)  # a keep "live" machine this quiet: its security updates run
    default_hours: float = Field(default=2.0, gt=0, le=72)
    # The limits that bound environment_open, which never asks a person (docs/design/environments.md).
    # They hold without `openajar environments allow`, which only changes them.
    max_hours: float = Field(default=8.0, gt=0, le=72)    # the longest life, extend included (and vms.toml max_ttl_minutes)
    max_live: int | None = Field(default=3, ge=0, le=50)  # open at once; None: vms.toml max_live
    monthly_eur: float | None = Field(default=50.0, ge=0)  # booked a month, open ones to their time limit; 0 or None: none
    profile: str | None = None                            # the vms.toml profile to rent; None: its default
    max_size: SizeName = "xlarge"                         # the biggest machine an agent may ask for (SIZES)
    image: str = "auto"                                   # auto: snapshot:dev when the node has it, else Ubuntu
    setup_timeout_s: int = Field(default=900, ge=60)      # the machine's setup (cloud-init) must end in this
    unsaved_notice_minutes: int = Field(default=30, ge=1)  # before the time limit, tell the owner of unsaved work
    reap_interval_s: int = Field(default=60, ge=5)
    # One chat holds a live machine (its lease, openajar.environments.picking). Another chat of the agent
    # takes it over only with take_over=true, and only after the holder made no call for this long.
    take_over_minutes: int = Field(default=10, ge=1, le=1440)
    browser: BrowserWhere = "node"                        # where each new environment's browser runs
    # With browser = "vm": the machine's screen is a desktop (a window manager, a dock with Browser, Terminal
    # and Files, the agent's commands in a terminal window; openajar.environments.vmbrowser). false: the
    # browser alone fills the screen, as before.
    desktop: bool = True
    # Keeping an environment (openajar.environments.saving, docs/design/environments.md "Keeping").
    default_keep: Keep = "save"                           # environment_open's keep when the agent names none
    allow_keep_live: bool = False                         # keep "live" bills the machine all month: opt in
    max_live_kept: int = Field(default=1, ge=0, le=20)    # environments kept live at once
    keep_last: int = Field(default=3, ge=1, le=50)        # saves kept for one title, newest first
    max_saves_gb: float = Field(default=5.0, gt=0)        # every save's file part on this node together
    saved_days: int = Field(default=30, ge=1, le=3650)    # a save older than this is removed (the owner is told first)
    snapshot_eur_per_gb_month: float = Field(default=SNAPSHOT_EUR_PER_GB_MONTH, ge=0)
    autosave_minutes: int = Field(default=15, ge=5)       # a file-level save this often while a keep "save" one works
    snapshots: bool = True                                # keep "save" makes a disk snapshot (false: files only)
    keep_paths: list[str] = Field(default_factory=list)   # extra folders on the machine a save holds (opt in)
    keep_paths_max_mb: int = Field(default=500, ge=1)     # their size, at most
    encrypt_saves: bool = True                            # a save's files sealed with a key the vault holds
    save_signins: bool = True                             # a save holds the browser's sign-ins (opt out per environment)
    # The network (openajar.environments.network): full, limited (registries, code hosts and allow_hosts)
    # or none. An agent may only narrow it; `openajar environments allow --network/--host` changes it.
    network: Literal["full", "limited", "none"] = "full"
    allow_hosts: list[str] = Field(default_factory=list)  # more hosts limited reaches (api.example.com, .example.com)
    # The owner's proxies (labels of secrets/proxies.json) a "vm" browser exits through: each new environment
    # takes the one fewest open environments use (openajar.environments.browserproxy). Empty: no proxy.
    browser_proxies: list[str] = Field(default_factory=list)
    # The language a proxied browser claims (navigator.languages, Accept-Language, Intl): "exit" follows the exit's
    # country (lt-LT, lt, en-US, en at a Lithuanian exit), as a browser there would; or fixed tags ("en-US", or
    # "de-DE, de, en") for an agent that should read sites in that language wherever the exit is.
    browser_language: str = "exit"
    # Pausing (openajar.environments.pausing): on a provider that pauses with memory (Morph), a save that
    # ends the machine pauses it instead of a snapshot and a delete (false: as on any other provider).
    pause_saves: bool = True
    # Prepared machines and the warm pool (openajar.environments.preparing, prepbuild and warmpool).
    prepared_builds: bool = True                          # build a prepared machine for a repository that says how
    prepared_days: int = Field(default=14, ge=1, le=3650)  # one no open used for this long is deleted
    prepared_max: int = Field(default=5, ge=0, le=50)     # prepared machines on this node, at most
    warm: int = Field(default=0, ge=0, le=10)             # machines kept booted for the next open (each one bills)
    warm_minutes: int = Field(default=50, ge=5, le=1440)  # an unclaimed warm machine is deleted after this
    # The replay of each environment's browser (openajar.environments.replay): frames kept on the node.
    replay: bool = True                                   # its browser holds no account and no vault entry
    replay_days: int = Field(default=14, ge=1, le=365)    # kept this long after the environment closed
    # Proofs (openajar.environments.proofs and recording): a video's longest recording, and whether
    # environment_pull_request puts the proofs in the pull request (on the repository's agentos-proofs branch).
    record_max_s: int = Field(default=180, ge=10, le=900)
    pr_proofs: bool = True

    @field_validator("keep_paths")
    @classmethod
    def _keep_paths(cls, v: list[str]) -> list[str]:
        for p in v:
            if not re.match(r"^/[A-Za-z0-9._/-]+$", p) or ".." in p or p.rstrip("/") in ("", "/work"):
                raise ValueError(f"keep_paths are absolute folders on the machine, like /root/.cache/ms-playwright: {p!r}")
        return [p.rstrip("/") for p in v]

    @field_validator("allow_hosts")
    @classmethod
    def _allow_hosts(cls, v: list[str]) -> list[str]:
        from openajar.environments.network import check_host

        return list(dict.fromkeys(check_host(h) for h in v))

    @field_validator("browser_proxies")
    @classmethod
    def _browser_proxies(cls, v: list[str]) -> list[str]:
        from openajar.core.models import PROXY_LABEL_RE

        labels = [str(label).strip() for label in v]
        for label in labels:
            if not PROXY_LABEL_RE.fullmatch(label):
                raise ValueError(f"not a proxy label: {label!r} (a label of `openajar proxy list`, like lt1)")
        return list(dict.fromkeys(labels))

    @field_validator("browser_language")
    @classmethod
    def _browser_language(cls, v: str) -> str:
        from openajar.environments.exitplace import EXIT_LANGUAGE, parse_languages

        tags = parse_languages(v)
        return ", ".join(tags) if tags else EXIT_LANGUAGE

    @model_validator(mode="after")
    def _live_needs_opt_in(self) -> EnvSettings:
        if self.default_keep == "live" and not self.allow_keep_live:
            raise ValueError('default_keep = "live" needs allow_keep_live = true (it bills the machine all month)')
        return self


def load_settings(home: Path) -> EnvSettings:
    path = Path(home) / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    return EnvSettings(**data)


def update_settings(home: Path, updates: dict) -> Path:
    """Merge `updates` into environments.toml in `home` (a None value leaves its key as it is), after
    checking that agentosd would start with the result. Returns the file's path."""
    import tomli_w

    from openajar.core.config import write_private

    path = Path(home) / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data.update({k: v for k, v in updates.items() if v is not None})
    EnvSettings(**data)  # refuse what agentosd would not start with
    write_private(path, tomli_w.dumps(data))
    return path


class Call(BaseModel):
    at: datetime
    tool: str
    summary: str = ""
    ok: bool = True
    exit_code: int | None = None              # a command's exit code (terminal_exec): ok is the tool's own answer
    error: str | None = None                  # the error's code when the call was refused or failed
    client: str | None = None                 # the MCP client that made it ("claude-code 2.1.3"), when it said


class Push(BaseModel):
    branch: str
    commit: str                               # "" for a delete
    at: datetime
    deleted: bool = False                     # the push deleted the branch on the remote


class Handoff(BaseModel):
    """Another chat took the environment over (environment_open with its title and take_over=true):
    the chat that had it is told so on its next call."""
    binding: str                              # the chat that lost it
    at: datetime
    lease: str | None = None                  # the hash of the handle it held (openajar.environments.picking)


class Machine(BaseModel):
    provider: str = ""
    server_type: str = ""
    cpu: int | None = None
    memory_gb: float | None = None
    location: str = ""
    size: str | None = None                   # small, medium, large, xlarge: the biggest of SIZES it has
    swap: str | None = None                   # what swap it has, in words (machine.SWAP_ON): "8 GB, compressed in memory (zram)"
    updates: str | None = None                # a keep "live" machine's security updates, in words (openajar.environments.updates)


class Environment(BaseModel):
    id: str                                   # e7k2
    title: str
    state: StoredState = "starting"
    grant_id: str                             # the grant that opened it (owner: "owner")
    grant_root: str | None = None             # its family (the grant's root): only that family reaches it
    binding: str                              # "session:<mcp-session-id>", else "grant:<grant id>"
    lease: str | None = None                  # sha256 of the handle its holding chat passes as `on`; the
                                              # handle itself is never stored (openajar.environments.picking)
    bound_at: datetime | None = None          # when its session last opened or attached it: a session's
                                              # calls without `on` go to the one it took last
    agent: str = "an agent"                   # who opened it, in words (the grant's subject)
    subject: str | None = None                # the grant's subject id, as the policy has it
    member: str | None = None                 # the member whose agent opened it; None: the owner's agent
    client: str | None = None                 # the MCP client's own name, when it said one
    vm_id: str | None = None
    repo: str | None = None                   # github.com/me/shop
    ref: str | None = None
    branch: str | None = None
    workdir: str = "/work"
    via_gateway: bool = False                 # cloned through the node's git gateway (a declared remote)
    image: str = ""
    machine: Machine = Field(default_factory=Machine)
    price_hourly_eur: float | None = None
    created_at: datetime
    ready_at: datetime | None = None
    last_call_at: datetime | None = None
    expires_at: datetime
    closed_at: datetime | None = None
    closed_reason: str | None = None
    cost_eur: float | None = None             # set when it closes; while open, read from its VM
    error: str | None = None
    error_code: str | None = None             # the error's code when it failed (repo_not_connected, environment_failed)
    error_reason: str | None = None           # environment_failed's cause (errors.FAILURE_REASONS): capacity, network...
    dismissed_at: datetime | None = None      # a failed open the agent closed: out of environment()'s `failed` list
                                              # (kept in the history and the owner's views)
    calls: list[Call] = Field(default_factory=list)   # newest last, at most CALLS_KEPT
    pushed: list[Push] = Field(default_factory=list)
    unsaved_noticed: bool = False             # the owner was told it has unsaved work and will not close idle
    handoffs: list[Handoff] = Field(default_factory=list)  # chats it was taken from, newest last
    # The branches it created on the remote (pushed first, not there before): the only existing branches
    # it may delete or force-push (openajar.git.gateway). Kept with the record, so a restart keeps them.
    own_branches: list[str] = Field(default_factory=list)
    # The commit each of those branches had when it last pushed it there: a branch is its own only while
    # the remote still has that commit (someone else may delete and recreate the name, or add to it).
    own_heads: dict[str, str] = Field(default_factory=dict)
    browser: BrowserWhere = "node"            # where its browser runs, as the setting was when it opened
    vm_state: str | None = None               # its machine at close: deleted (the provider says it is gone),
                                              # deleting (the node still retries), none (none was rented),
                                              # paused (its save keeps it, asleep: openajar.environments.pausing)
    keep: Keep = "none"                       # what happens to it when the work stops (Keep)
    restore_from: str | None = None           # the save it resumes (openajar.environments.saving), until it is ready
    resumed: dict | None = None               # what the resume brought back (environment_open's `resumed`)
    note: str | None = None                   # the agent's own words for the next save's resume note
    last_save_id: str | None = None
    last_save_at: datetime | None = None
    save_error: str | None = None             # the last save that failed, in words (the machine was kept)
    save_noticed: bool = False                # the owner was told a save failed
    save_tried_at: datetime | None = None     # the last save, made or failed (a failed one is retried later)
    keep_signins: bool = True                 # a save holds its browser's sign-ins (openajar.environments.signins)
    pull_requests: list[dict] = Field(default_factory=list)  # environment_pull_request opened (url, number, branch)
    # Its network (openajar.environments.networking), as it opened: the level, the names of the secrets
    # bound for its app (never a value), and the hosts its network refused (host, count, last).
    network: Literal["full", "limited", "none"] = "full"
    secrets: list[str] = Field(default_factory=list)
    # The owner's proxy its browser exits through (a label of browser_proxies, picked at open and kept by its
    # saves, so a resume keeps the exit IP its sign-ins know). None: no proxy. Never its credentials.
    browser_proxy: str | None = None
    blocked: list[dict] = Field(default_factory=list)
    # Prepared machines and the app (openajar.environments.preparing): what the open found and did with the
    # repository's .openajar/environment.toml, and the dev server it started.
    prepared: dict | None = None
    app: dict | None = None
    warm_claimed: bool = False                # its machine came from the warm pool (openajar.environments.warmpool)
    watched_at: datetime | None = None        # a person last watched it (live view, a port link): openajar.environments.watching
    resumed_by: str | None = None             # "owner": the owner resumed it from a save, held by no chat (openajar.environments.waking)
    bringup: dict | None = None               # its bring-up's steps and seconds, once ready or failed (openajar.environments.timings)
    # Its machine stopped for good while it was open: the provider said so (openajar.environments.liveness).
    gone_at: datetime | None = None
    gone_why: str | None = None               # in words: "its container stopped (docker says exited)"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"not an environment id: {v!r}")
        return v

    @property
    def family(self) -> str:
        """The grant family it belongs to: the root of the grant that opened it."""
        return self.grant_root or self.grant_id

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def terminal_lane(self) -> str:
        return f"{WORKSPACE}/{self.id}"

    @property
    def browser_lane(self) -> str:
        return f"{WORKSPACE}/{self.id}-browser"

    @property
    def lanes(self) -> dict[str, str]:
        return {"terminal": self.terminal_lane, "browser": self.browser_lane}

    def shown_state(self, now: datetime) -> str:
        """What people see: starting, ready, working, idle, gone (its machine stopped), closing, closed or failed."""
        if self.state != "ready":
            return self.state
        if self.gone_at is not None:
            return "gone"
        last = self.last_call_at
        if last is not None and now - last <= WORKING:
            return "working"
        if now - (last or self.ready_at or self.created_at) >= IDLE:
            return "idle"
        return "ready"

    def last_activity(self) -> datetime:
        return max(t for t in (self.last_call_at, self.ready_at, self.created_at) if t is not None)
