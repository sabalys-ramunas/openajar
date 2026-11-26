"""The errors of agent environments. Each code has an entry in openajar/core/error_catalog.toml."""

from __future__ import annotations

from typing import Any

from openajar.core.errors import OpenajarError


class NoEnvironment(OpenajarError):
    """A terminal or browser call without `on`, in a chat that has no environment open."""
    code = "no_environment"


class WhichEnvironment(OpenajarError):
    """A call without `on` while more than one environment of the caller's grant family is open, on a
    client that sends no MCP session id (every chat of one agent looks the same then). Nothing ran:
    the agent names the computer with `on` (its title or env/<id>)."""
    code = "which_environment"


class UnsavedWork(OpenajarError):
    """environment_close found changes that are not committed, or commits that are not pushed.
    `details` lists them: {"dirty_files": [...], "unpushed": [...]}."""
    code = "unsaved_work"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class EnvironmentLimit(OpenajarError):
    """Opening one more environment would pass a limit of this node: how long one lives, how big it
    is, or the money it may spend in a month."""
    code = "environment_limit"


class EnvironmentFull(EnvironmentLimit):
    """The node already runs as many machines as its count limit allows (environments.toml max_live;
    vms.toml max_live is never below it, so it is the one count). Waiting helps: `retry_after` is the
    seconds until the soonest one may free, for the MCP error's retry_after line."""
    code = "environment_full"

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class EnvironmentNotReady(OpenajarError):
    """The chat has an environment, but a call cannot reach it right now: its machine is still
    starting, it is closing, or its tunnel is down (the node restarted, or the connection dropped).
    Nothing ran in its place on the node."""
    code = "environment_not_ready"


class NoDesktop(OpenajarError):
    """A screen tool (screen_snapshot, screen_act, screen_open) where no desktop runs: the environment's browser
    runs on the node, the desktop is off on this node, or it has not started yet."""
    code = "no_desktop"


class EnvironmentFailed(OpenajarError):
    """The environment's machine did not come up (no SSH, or its setup failed). Its VM is deleted.
    With `reason` (FAILURE_REASONS) the message starts with it ("capacity: ..."), so the catalog gives
    that cause's fix line."""
    code = "environment_failed"

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(f"{reason}: {message}" if reason else message)
        self.reason = reason


# Why an open failed, each with its own fix line in the error catalog (environment_failed.reasons).
FAILURE_REASONS = ("capacity", "network", "image", "clone", "setup")


class EnvironmentInUse(OpenajarError):
    """environment_open named the title of an environment another chat of the same agent still works
    in (a call in the last 10 minutes). It is not taken from that chat."""
    code = "environment_in_use"


class SavedByEarlierGrant(OpenajarError):
    """environment_open named a title this agent's computer has under an earlier agent grant of this
    computer (revoked, expired or frozen), whose family this grant does not reach: openajar 0.7.0 and
    before gave each new local grant a family of its own (openajar.policy.families). Nothing was opened,
    since a new machine would start empty; the owner gives them to this grant with `openajar
    environments adopt` (openajar.environments.adopt)."""
    code = "saved_by_earlier_grant"


class EnvironmentAlreadyOpen(OpenajarError):
    """environment_open named the title of an open environment of the same agent, with another
    repository than the one it has. The title names one computer; nothing was opened."""
    code = "environment_already_open"


class RepoNotConnected(OpenajarError):
    """environment_open named a repository the node cannot clone: it is not declared for workspace
    env with a credential in the vault, and it cannot be read without one (a private repository), or
    the node's credential for it was refused (a deploy key not added, a token revoked). Nothing was
    rented, or the machine was deleted. The human connects it on the node; the agent never does."""
    code = "repo_not_connected"
    access: Any = None  # what GitService.access found, when the node checked before renting


class RepoNotFound(OpenajarError):
    """environment_open named a repository its host answered "not found" for, asked with a login or a
    token that should see it: it does not exist, or that login cannot see it. Nothing was rented, and no
    card asks a person for a token (one would not help)."""
    code = "repo_not_found"


class RepoConnectFailed(OpenajarError):
    """A token or deploy key a person gave on a repository card did not work, and nothing was kept.
    `reason` says which check failed (bad_token, cannot_see, sso, org_approval, org_policy,
    no_contents, rate_limited, unreachable, not_yet)."""
    code = "repo_connect_failed"

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class SaveFailed(OpenajarError):
    """A save of an environment did not finish (its machine did not answer, its disk was full, or the
    snapshot failed). Nothing was deleted: the machine is kept, and the owner is told. With `reason`
    the message starts with it ("provider_unreachable: ..."), so the catalog gives that cause's fix line."""
    code = "save_failed"

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(f"{reason}: {message}" if reason else message)
        self.reason = reason


class RestoreFailed(OpenajarError):
    """An environment could not be resumed from its save. The save is untouched, so the next
    environment_open with the title tries again."""
    code = "restore_failed"


class SaveLimit(OpenajarError):
    """A save would pass a limit of this node: the file part is bigger than max_saves_gb allows, even
    after the oldest saves of the title were removed."""
    code = "save_limit"


class KeepLiveNotAllowed(OpenajarError):
    """keep "live" keeps the machine running and billed all month, so the owner opts in first
    (allow_keep_live), and at most max_live_kept are kept live at once."""
    code = "keep_live_not_allowed"


class CheckpointFailed(OpenajarError):
    """environment_checkpoint or environment_rollback could not do its work on the machine: there is
    no git repository under /work, git could not read one, or the machine did not answer. A checkpoint
    that failed leaves no half of itself behind."""
    code = "checkpoint_failed"


class CheckpointNotFound(OpenajarError):
    """environment_rollback named a checkpoint no repository under /work has: it was never made, or it
    is older than the ones kept (about 20 per environment), or a files-only resume left it behind.
    Nothing changed."""
    code = "checkpoint_not_found"


class RollbackRefused(OpenajarError):
    """environment_rollback found a repository in the middle of a merge, a rebase, a cherry-pick or a
    revert. Nothing changed: the agent finishes or aborts that first."""
    code = "rollback_refused"


class RecordingFailed(OpenajarError):
    """browser_record could not start or keep a recording: the machine has no ffmpeg and cannot install
    it (its network is none), the node has none (browser = "node"), or ffmpeg failed."""
    code = "recording_failed"
