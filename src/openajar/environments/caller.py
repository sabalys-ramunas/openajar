"""Who a tool call is, as environments see it: its grant, its grant family and its binding.

A call reaches the environments of its grant family (the root grant, or for a local agent grant that
replaced an earlier one, the family it continues: openajar.policy.families), and one without `on` goes to
the environment bound to it: the MCP session (one chat), else the grant (stdio). The tools read
these from the call's ToolContext; the "environments" service takes the call itself
(`EnvironmentManager.bound_to_call`), so a lower layer (whoami, in openajar.node) asks it without
importing this package.
"""

from __future__ import annotations

from typing import Any


def binding_of(session: str | None, grant_id: str | None) -> str:
    """What an environment is bound to: the MCP session (one chat), else the grant (stdio)."""
    if session:
        return f"session:{session}"
    return f"grant:{grant_id or 'owner'}"


def grant_of(ctx: Any) -> Any:
    """The call's grant, or None (the owner with no policy on the node)."""
    return getattr(ctx, "grant", None)


def grant_id(ctx: Any) -> str:
    """The call's grant id; with no grant, the agent principal's ("agent:<id>") or "owner"."""
    g = grant_of(ctx)
    gid = getattr(g, "id", None)
    if gid:
        return str(gid)
    pid = getattr(getattr(ctx, "principal", None), "id", None)
    return f"agent:{pid}" if pid else "owner"


def grant_family(grant: Any) -> str | None:
    """The family whose environments a grant reaches: its root grant's id, or the family a local agent
    grant continues from the one it replaced (Grant.env_family, openajar.policy.families)."""
    found = getattr(grant, "env_family", None) or getattr(grant, "root_id", None)
    return str(found) if found else None


def family(ctx: Any) -> str | None:
    """The caller's grant family (grant_family), whose environments it reaches. None: the owner
    with no policy on the node, who reaches every one."""
    g = grant_of(ctx)
    if g is None:
        return None
    return grant_family(g) or grant_id(ctx)


def binding(ctx: Any) -> str:
    """The call's binding: its MCP session when the client sends one, else its grant."""
    meta = getattr(ctx, "meta", None)
    return binding_of(getattr(meta, "session", None), grant_id(ctx))
