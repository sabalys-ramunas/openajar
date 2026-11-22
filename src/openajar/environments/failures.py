"""Why an environment did not come up, in words an agent acts on, and the error environment_open raises.

A bring-up that fails records its cause on the environment (error, error_code and, for
environment_failed, error_reason: capacity, network, image, clone or setup). The record stays in the
history and in environment()'s `failed` list. environment_open raises the error itself, never a
result that looks like an open: the error catalog gives each reason its own fix line, and the words
give a concrete wait where waiting helps.
"""

from __future__ import annotations

from typing import Any

from openajar.core.errors import OpenajarError
from openajar.environments.errors import (
    EnvironmentFailed,
    EnvironmentFull,
    EnvironmentLimit,
    RepoNotConnected,
    RestoreFailed,
)
from openajar.environments.models import SIZES, Environment

NETWORK_WORDS = ("unreachable", "public ipv4", "could not reach")
FAILED_SHOWN = 3
CLASSES: dict[str, type[OpenajarError]] = {"environment_limit": EnvironmentLimit, "environment_full": EnvironmentFull,
                                          "repo_not_connected": RepoNotConnected, "restore_failed": RestoreFailed}


def bigger_size(env: Environment, top: str) -> str | None:
    """The next size above the one the environment asked for, up to `top` (max_size)."""
    names = list(SIZES)
    have = env.machine.size if env.machine.size in names else "small"
    i = names.index(have) + 1
    return names[i] if i < len(names) and i <= names.index(top) else None


CAPACITY_CODES = ("resource_unavailable", "placement_error", "unavailable")


def is_capacity(e: BaseException) -> bool:
    """Did the provider have no room (a capacity failure)?"""
    return getattr(e, "provider_code", None) in CAPACITY_CODES


def room_words(room: list[dict]) -> str:
    """'Available now: medium (4 CPU, 8 GB, less than you asked for): environment_open(size="medium")', or
    for sizes the provider does not say about (Sizing.sizes_with_room, "now" false), 'Not tried yet, so it
    may have room: ...'."""
    def part(r: dict) -> str:
        less = ", less than you asked for" if r.get("smaller") else ""
        return f"{r['size']} ({r['cpu']} CPU, {r['memory_gb']} GB{less}): environment_open(size=\"{r['size']}\")"

    now = [part(r) for r in room if r.get("now", True)]
    maybe = [part(r) for r in room if not r.get("now", True)]
    out = ["Available now: " + "; or ".join(now)] if now else []
    out += ["Not tried yet, so it may have room: " + "; or ".join(maybe)] if maybe else []
    return ". ".join(out)


def classify(e: BaseException, env: Environment, top: str,
             room: list[dict] | None = None) -> tuple[str | None, str]:
    """(reason, words) for what failed a bring-up. reason None: no known cause. `room`: the sizes the
    provider lists as available now (Sizing.sizes_with_room), which a capacity failure offers."""
    msg = str(getattr(e, "message", e)) or type(e).__name__
    reason = getattr(e, "reason", None)
    if isinstance(e, EnvironmentFailed) and reason:
        return reason, msg.removeprefix(f"{reason}: ")
    code = getattr(e, "provider_code", None)
    bigger = bigger_size(env, top)
    if code == "image_too_big":
        tip = f' Ask for size="{bigger}".' if bigger else ""
        return "image", f"{msg}.{tip}"
    if code in CAPACITY_CODES:  # never a size whose every type just had no room (Sizing.sizes_with_room)
        if room:
            return "capacity", f"{msg}. {room_words(room)}. Nothing bills: no machine was made"
        return "capacity", f"{msg}. Nothing bills: no machine was made"
    if getattr(e, "code", None) == "vm_error" and any(w in msg.lower() for w in NETWORK_WORDS):
        return "network", f"{msg.rstrip('.')}. Try again in 2 minutes" if "try again in" not in msg else msg
    return None, msg


def error_for(env: Environment) -> OpenajarError:
    """The error environment_open raises for a failed one, with its id, title and cause."""
    code = env.error_code or "environment_failed"
    if code == "repo_not_connected":  # its words say the machine was deleted, and its fix must fit (repos.MESSAGE_MAX)
        return RepoNotConnected(env.error or code)
    head = f"environment {env.id} did not start: "
    if code == "environment_failed":
        return EnvironmentFailed(head + (env.error or "no reason was given"), reason=env.error_reason)
    return CLASSES.get(code, EnvironmentFailed)(head + (env.error or code))


def failed_rows(mgr: Any, family: str | None, reaches: Any, *, since_s: float = 3600) -> list[dict]:
    """The family's opens that failed in the last hour, newest first, for environment()'s `failed`: not
    one the agent closed (environment_close of it dismisses it), nor one whose title is open again."""
    now = mgr._clock()
    out = []
    live = {" ".join(e.title.split()).lower() for e in mgr.store.open() if family is None or e.family == family}
    for e in mgr.store.recent(50):
        if e.state != "failed" or (family is not None and e.family != family) or not reaches(e):
            continue
        if e.dismissed_at is not None or " ".join(e.title.split()).lower() in live:
            continue
        if (now - e.created_at).total_seconds() > since_s:
            continue
        out.append({"id": e.id, "title": e.title, "error_code": e.error_code, "reason": e.error_reason,
                    "error": (e.error or "")[:300], "at": mgr.summary(e)["created_at"]})
        if len(out) >= FAILED_SHOWN:
            break
    return out
