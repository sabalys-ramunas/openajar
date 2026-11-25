"""Data for terminal and service lanes (openajar.env)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from openajar.core.models import IsolationProfile, LaneKind, LaneState, Preset, parse_lane_id, utcnow

NetworkMode = Literal["shared", "isolated", "off"]
FilesystemMode = Literal["shared", "overlay", "ephemeral"]
ProcessesMode = Literal["shared", "isolated"]
ProgramsMode = Literal["base", "overlay"]
InstallSource = Literal["apt", "pip", "npm"]
RestartPolicy = Literal["always", "on-failure", "never"]


class Limits(BaseModel):
    """cgroup v2 limits for the whole lane (every exec, shell and service in it together)."""
    memory_mb: int | None = None     # memory.max
    cpus: float | None = None        # cpu.max quota, in CPUs
    pids: int | None = None          # pids.max
    disk_mb: int | None = None       # hard cap on the lane's writable layer
    tmp_mb: int = 256                # size of the private /tmp


class Policy(BaseModel):
    """What a lane allows, derived from its preset and dials. Shown to humans as-is."""
    shell: bool = True               # interactive shells
    exec: bool = True                # one-shot commands
    install: bool = True             # terminal_install into the overlay
    signed_only: bool = False        # locked: apt only (signed repos), allowlisted packages and programs
    noexec_writable: bool = False    # writable dirs are mounted noexec (only base programs run)
    seccomp: bool = True             # syscall filter on isolated workloads
    egress_default: Literal["allow", "deny"] = "allow"  # what an empty allowlist means
    tier: int = 0                    # isolation tier (docs/ARCHITECTURE.md section 5)
    warning: str | None = None       # loud label for weaker lanes


class HealthCheck(BaseModel):
    """Run a command inside the lane, or probe an HTTP URL on the lane's loopback."""
    cmd: list[str] | None = None
    http: str | None = None
    interval_s: float = 10.0
    timeout_s: float = 5.0
    retries: int = 3                 # consecutive failures before a restart
    start_period_s: float = 5.0

    @field_validator("http")
    @classmethod
    def _loopback_only(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from urllib.parse import urlsplit
        u = urlsplit(v)
        if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("health http probe must be http://127.0.0.1:<port>/... (the lane's own loopback)")
        return v


class ServiceConfig(BaseModel):
    command: list[str]
    restart: RestartPolicy = "always"
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    stable_after_s: float = 30.0     # a run this long resets the backoff
    max_restarts: int | None = None  # give up (state failed) after this many restarts in a row
    health: HealthCheck | None = None
    env: dict[str, str] = Field(default_factory=dict)
    log_max_bytes: int = 1_000_000
    log_backups: int = 3
    stop_timeout_s: float = 10.0


class RemoteVia(BaseModel):
    """The SSH host a machine is reached through (openajar.vms.own: a container on a person's own
    machine listens on that machine's loopback only, so every connection to it goes through the
    node's SSH login on the machine). The key and the pinned host key are the node's files."""
    host: str
    port: int = 22
    user: str = "root"
    key_path: str
    known_hosts: str
    host_alias: str


class RemoteHost(BaseModel):
    """A lane whose commands run on another machine over SSH (openajar.vms): a disposable cloud
    VM. The key and the pinned host key live in the node's own files, never on the lane."""
    vm_id: str                       # the openajar VM record (vm_...)
    host: str                        # the VM's public IP
    port: int = 22
    user: str = "root"
    key_path: str                    # private key for this VM only (0600, under OPENAJAR_HOME/vms)
    known_hosts: str                 # one line: the host key openajar made for this VM
    host_alias: str                  # HostKeyAlias, so the pin does not depend on the IP
    provider: str = "hetzner"
    label: str = ""                  # what people see, e.g. "Hetzner cx22 in nbg1"
    environment: str | None = None   # the agent environment this VM is the machine of (openajar.environments)
    via: RemoteVia | None = None     # set: `host` is an address on that SSH host, and is reached through it


class EnvLane(BaseModel):
    id: str
    kind: LaneKind = LaneKind.TERMINAL
    preset: Preset = Preset.SANDBOX
    profile: IsolationProfile
    limits: Limits = Field(default_factory=Limits)
    policy: Policy = Field(default_factory=Policy)
    service: ServiceConfig | None = None
    exit_proxy: str | None = None             # proxy label: the forwarder chains to it (credentials stay on the host)
    allowed_programs: list[str] = Field(default_factory=list)  # locked: argv[0] allowlist
    remote: RemoteHost | None = None          # set: commands run on that machine over SSH (a VM lane)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        parse_lane_id(v)
        return v

    @property
    def workspace(self) -> str:
        return parse_lane_id(self.id)[0]

    @property
    def name(self) -> str:
        return parse_lane_id(self.id)[1]

    @property
    def key(self) -> str:
        """Filesystem- and cgroup-safe key (ids never contain dots)."""
        ws, name = parse_lane_id(self.id)
        return f"{ws}.{name}"

    @property
    def isolated(self) -> bool:
        """True when workloads run inside the bubblewrap sandbox (not env0). A VM lane is never
        isolated in this sense: nothing of it runs on the node (see `contained`). Running in the
        sandbox is not the same as being kept off the node: a sandbox that shares the node's
        filesystem or processes still reaches it (`contained`, openajar.env.exposure)."""
        if self.remote is not None:
            return False
        p = self.profile
        return not (p.filesystem == "shared" and p.processes == "shared" and p.network == "shared")

    @property
    def contained(self) -> bool:
        """Workloads cannot reach the node's own files, secrets or programs: another machine, or a
        bubblewrap sandbox whose filesystem and processes are both its own. The one rule behind
        `openajar.env.exposure.reaches_node`; read its docstring for why each dial counts."""
        if self.remote is not None:
            return True
        p = self.profile
        return self.isolated and p.filesystem != "shared" and p.processes != "shared"


class ExecResult(BaseModel):
    exit_code: int | None            # None when killed by a signal
    signal: str | None = None        # e.g. "SIGKILL"
    stdout: str
    stderr: str
    stdout_encoding: Literal["utf-8", "base64"] = "utf-8"
    stderr_encoding: Literal["utf-8", "base64"] = "utf-8"
    stdout_bytes: int = 0            # total bytes the command wrote, before truncation
    stderr_bytes: int = 0
    truncated: bool = False
    timed_out: bool = False
    oom_killed: bool = False
    oom: list[dict] | None = None    # on a VM: what its kernel killed for memory meanwhile (openajar.env.oom)
    duration_s: float


class ShellInfo(BaseModel):
    id: str
    lane: str
    state: Literal["running", "exited"]
    exit_code: int | None = None
    cols: int
    rows: int
    created_at: datetime
    last_activity: datetime
    attached: int = 0                # live web terminal clients
    unread_bytes: int = 0


JobState = Literal["running", "exited", "cancelled", "timed_out", "lost"]


class JobInfo(BaseModel):
    """A background command in a lane (terminal_job_start). Its output is logged on the node, so a
    caller can disconnect and read it later from any offset. `lost` means the node stopped while
    it ran; `note` says why a job ended early (a cancel, a timeout, the lane being stopped)."""
    id: str
    lane: str
    cmd: str
    cwd: str | None = None
    state: JobState = "running"
    exit_code: int | None = None     # None while running, and when killed by a signal
    signal: str | None = None
    oom_killed: bool = False
    oom: list[dict] | None = None    # on a VM: what its kernel killed for memory since it started (openajar.env.oom)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_s: float | None = None
    timeout_s: float
    stdout_bytes: int = 0            # bytes the command wrote (also past the log cap)
    stderr_bytes: int = 0
    stdout_dropped: int = 0          # bytes past the node's log cap, counted and not kept
    stderr_dropped: int = 0
    sync: dict | None = None         # what the start synced: dest, files, changed, deleted, bytes
    source: dict | None = None       # the git branch and commit of the synced tree, as the client said
    by: str | None = None            # the principal that started it
    note: str | None = None
    vm_wid: str | None = None        # on a VM: the id of its files there (it runs detached), for a reattach


class ServiceStatus(BaseModel):
    lane: str
    desired: Literal["running", "stopped"]
    state: Literal["stopped", "starting", "running", "backoff", "failed", "stopping"]
    pid: int | None = None
    health: Literal["none", "starting", "healthy", "unhealthy"] = "none"
    restarts: int = 0
    last_exit: int | None = None
    last_exit_at: datetime | None = None
    started_at: datetime | None = None
    next_restart_in_s: float | None = None
    expires_at: datetime | None = None
    log_path: str | None = None
    detail: str | None = None


class EgressStats(BaseModel):
    allowed: int = 0
    denied: int = 0
    last_denied: str | None = None
    bytes_out: int = 0
    bytes_in: int = 0


class EnvLaneInfo(BaseModel):
    lane: EnvLane
    state: LaneState = LaneState.COLD
    last_used: datetime | None = None
    quarantine_reason: str | None = None
    isolation: str = ""              # one honest line about what this lane really gets
    disk: dict | None = None         # {"used_mb", "cap_mb"}
    shells: int = 0
    execs: int = 0
    service: ServiceStatus | None = None
    egress: EgressStats | None = None


class ImageTemplate(BaseModel):
    """A declarative base image: packages, files and env baked into a read-only layer."""
    name: str
    description: str = ""
    apt: list[str] = Field(default_factory=list)
    pip: list[str] = Field(default_factory=list)
    npm: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)   # absolute path -> content
    env: dict[str, str] = Field(default_factory=dict)
    placeholder: bool = False        # ships as a name only (nothing to build yet)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,40}$", v):
            raise ValueError(f"template name must be lowercase letters, digits and dashes: {v!r}")
        return v


class ImageInfo(BaseModel):
    name: str
    version: int
    ref: str                         # "dev-box@1"
    template_hash: str
    content_hash: str
    base_fingerprint: str            # hash of the node's package state the layer was built on
    built_at: datetime
    size_bytes: int = 0
    env: dict[str, str] = Field(default_factory=dict)
    manifest: dict = Field(default_factory=dict)  # what got installed (dpkg, pip, npm)
