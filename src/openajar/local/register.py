"""The local node's agent grant and the clients on this computer that use it.

The grant is what `openajar connect` issues an agent by default (openajar.client.choices): every
workspace, up to commit, with the specials environments (vm, exec and secret on lanes env/* only) and
memory_read. Its token goes where connect keeps one (the macOS Keychain, or a chmod 600 file on
Linux) under the connection name `local`, and Claude Code (user scope) and Codex reach the node as
`openajar-local` through the headers helper, so no token sits in their config. The skills are copied
as connect copies them (openajar.client.ops.Client.register_clients does all of that).

Run again, it keeps the grant while the stored token still works on this node and its terms are
the default ones (also what a release's default specials grant today); otherwise it issues a new grant and
revokes the one it replaces.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from openajar.api.bridge import panel_specials, ttl_seconds
from openajar.client.choices import (
    DEFAULT_AGENT_TTL,
    DEFAULT_CEILING,
    DEFAULT_CLIENT,
    DEFAULT_SPECIALS,
    ClientChoice,
    computer_name,
)
from openajar.client.claude import RESTART_NOTE
from openajar.client.ops import Client
from openajar.client.state import Connection
from openajar.local import home as local_home
from openajar.local.home import LocalError
from openajar.local.paths import NAME

GRANT_NOTE = "openajar local: Claude Code and Codex on this computer"


def _held_grant(policy, token: str | None, previous: Connection | None) -> dict | None:
    """The grant record of the token this computer holds, while it still works here and has the
    default terms; else None."""
    if not token or previous is None or previous.credential != "agent_grant":
        return None
    if sorted(previous.agent_specials) != sorted(DEFAULT_SPECIALS) or previous.agent_ceiling != DEFAULT_CEILING:
        return None
    try:
        _, grant = policy.resolve(token)
    except Exception:  # noqa: BLE001 - revoked, expired, or from another home: a new one is issued
        return None
    if grant.id != previous.agent_grant_id:
        return None
    record = policy.grant_record(grant.id)
    # the same names can grant more in a later release (0.4.0 added secret on env/* lanes to environments, for
    # login): a grant issued before that lacks it, and is replaced, so an upgrade needs no `openajar connect`
    if not set(panel_specials(list(DEFAULT_SPECIALS))) <= set((record.get("scope") or {}).get("specials") or []):
        return None
    return record


def _grant(home: Path, client: Client, kind: str) -> tuple[str, dict, list[str]]:
    """(token, grant record, lines): the grant this computer keeps, or a new one."""
    from openajar.policy.service import PolicyService

    cfg = local_home.read(home)
    if cfg is None:
        raise LocalError(f"there is no local node in {home}", "run `openajar local start` first")
    previous = client.records.get(NAME)
    held = client.tokens(kind).get(NAME)  # type: ignore[arg-type]
    policy = PolicyService(home, node_config=cfg)
    try:
        record = _held_grant(policy, held, previous)
        if record is not None and held:
            return held, record, [f"Agent grant {record['id']} kept"]
        subject = f"{DEFAULT_CLIENT}@{computer_name()}"
        grant, token = policy.issue(subject={"kind": "agent", "id": subject}, workspaces=["*"],
                                    ceiling=DEFAULT_CEILING, specials=panel_specials(list(DEFAULT_SPECIALS)),
                                    ttl_s=ttl_seconds(DEFAULT_AGENT_TTL), by="owner", note=GRANT_NOTE)
        lines = [(f"Agent grant {grant.id} issued for {subject}: every workspace, up to {DEFAULT_CEILING}, "
                  f"specials {', '.join(sorted(DEFAULT_SPECIALS))}")]
        old = previous.agent_grant_id if previous is not None else None
        if old and old != grant.id:
            with contextlib.suppress(Exception):  # one from a home that is gone: nothing to revoke here
                policy.revoke(old, by="owner", reason="openajar local issued a new grant for this computer")
                lines.append(f"  revoked the grant it replaces, {old}")
        return token, policy.grant_record(grant.id), lines
    finally:
        policy.close()


def register_clients(home: Path, port: int, *, claude: bool = True, codex: bool = True, skills: bool = True,
                     client: Client | None = None) -> list[str]:
    """Issue (or keep) the local agent grant, store its token, and register Claude Code and Codex as
    `openajar-local` at http://127.0.0.1:<port>/mcp, and the skills. Returns lines saying what was done.
    `client`: tests pass one with a temporary home and fake tools."""
    home = Path(home).expanduser()
    client = client or Client(out=lambda _: None)
    kind = client.store_kind("auto")
    token, record, lines = _grant(home, client, kind)
    conn = Connection(name=NAME, ssh_host="localhost", remote_home=str(home), remote_port=port, local_port=port,
                      node_name=NAME, token_store=kind, service="none", credential="agent_grant",
                      agent_grant_id=record.get("id"), agent_subject=(record.get("subject") or {}).get("id"),
                      agent_ceiling=DEFAULT_CEILING, agent_specials=sorted(DEFAULT_SPECIALS),
                      agent_expires_at=_iso(record.get("expires_at")))
    result = client.connect_here(conn, token, ClientChoice(claude=claude, codex=codex, skills=skills))
    local_home.mark_registered(home, skills)  # its serve keeps the skills it installed up to date, or none
    lines.append(f"Token {'stored in' if result.token_changed else 'already in'} {client.tokens(kind).describe(NAME)}")
    lines += [f"{line.client}: {line.state} {line.detail}".rstrip() for line in result.clients]
    if result.claude_profiles:
        lines.append(f"  {RESTART_NOTE}")
    if result.claude_lines:
        lines += ["To add it to Claude Code by hand:", *[f"  {line}" for line in result.claude_lines]]
    lines += [f"  {note}" for note in result.notes]
    return lines


def _iso(value: object) -> str | None:
    """A grant's expiry as an ISO date string (the policy keeps seconds since the epoch)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)
