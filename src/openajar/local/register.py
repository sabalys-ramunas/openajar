"""The local node's agent grant and the clients on this computer that use it.

The grant is what `openajar connect` issues an agent by default (openajar.client.choices): every
workspace, up to commit, with the specials environments (vm, exec and secret on lanes env/* only) and
memory_read. Its token goes where connect keeps one (the macOS Keychain, or a chmod 600 file on
Linux) under the connection name `local`, and Claude Code (user scope) and Codex reach the node as
`openajar-local` through the headers helper, so no token sits in their config. The skills are copied
as connect copies them (openajar.client.ops.Client.register_clients does all of that).

Run again, it keeps the grant while the stored token still works on this node, its terms are
the default ones (also what a release's default specials grant today) and it has more than a fifth of
its life left; otherwise it issues a new grant. A grant's expiry is signed into it, so a renewal is a
new grant too. The new grant continues the family of the one it replaces when that one is this
computer's own (openajar.policy.families): the agent keeps its saved and open computers and its
title-bound logins across an expiry, a renewal or new default terms (audit S7-01). Not after the owner
revoked that grant or a freeze did: then the new grant starts a family of its own, and the output says
that `openajar environments adopt` gives the earlier computers to it after a look. A grant it replaces is
revoked, except one it renews: that one still works for the chats open now and ends on its own. The service renews the grant by itself before it ends (renew_due, from `openajar local serve`).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from datetime import UTC, datetime
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
from openajar.policy.families import LOCAL_AGENT_NOTE
from openajar.policy.freeze import FREEZE_MARK

GRANT_NOTE = LOCAL_AGENT_NOTE  # how a grant of this computer's own agent is told from one connect issued
REPLACED = "openajar local issued a new grant for this computer"  # the reason of openajar's own replacement
RENEW_AT = 0.8  # the part of its life after which the grant is renewed (72 of 90 days)


LOCK = ".agent-grant.lock"  # in the node's home: one renewal or registration of the agent grant at a time


@contextlib.contextmanager
def grant_lock(home: Path) -> Iterator[None]:
    """One renewal or registration of this computer's agent grant at a time. `openajar local start` registers the
    clients while the service it just started renews (R1 #14): both issued a grant, and the Keychain and the
    `local` connection could end up naming different ones, which stopped every later renewal. The second now
    reads what the first stored, and keeps it."""
    fd = os.open(Path(home) / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the file releases the lock


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


def _seconds(value: object) -> float | None:
    """A record's ISO time as seconds since the epoch."""
    if not value:
        return None
    return datetime.fromisoformat(str(value)).timestamp()


def _due(record: dict, now: float) -> bool:
    """Has the grant used RENEW_AT of its life?"""
    issued, expires = _seconds(record.get("issued_at")), _seconds(record.get("expires_at"))
    if issued is None or expires is None or expires <= issued:
        return False
    return now >= issued + RENEW_AT * (expires - issued)


def _predecessor(policy, previous: Connection | None) -> tuple[dict | None, str | None]:
    """(the grant whose family a new grant keeps, None) or (None, a line for the owner). It is the grant the `local`
    connection names, when that is this computer's own agent grant (a root agent grant `openajar local` issued on
    this node) and it is live, expired, or revoked by openajar's own replacement. One the owner revoked, or a
    freeze revoked, is not continued (R1 #5: a revoke for a leaked token must not hand its computers on): the new
    grant starts a family of its own, and the line says how the owner gives them over after a look."""
    if previous is None or not previous.agent_grant_id:
        return None, None
    try:
        record = policy.grant_record(previous.agent_grant_id)
    except Exception:  # noqa: BLE001 - one from a home that is gone: nothing to continue
        return None, None
    blocks = (record.get("scope") or {}).get("blocks") or [{}]
    if record.get("parent_id") or (record.get("subject") or {}).get("kind") != "agent" \
            or blocks[0].get("note") != GRANT_NOTE:
        return None, None
    if record.get("state") != "revoked":
        return record, None
    why = policy.revoked_reason(record["id"]) or ""
    if why == REPLACED:
        return record, None
    how = "a freeze revoked it" if why.startswith(FREEZE_MARK) else \
        "you revoked it" + (f" ({why[:120]})" if why else "")
    return None, (f"  it starts without the computers of {record['id']}, since {how}: their saves, open environments "
                  "and logins stay that grant's. `openajar environments adopt` lists them, and `openajar environments "
                  "adopt --yes` gives them to this grant once you have looked")


def _issue(policy, before: dict | None) -> tuple[str, dict, list[str]]:
    """A new agent grant for this computer, keeping the family of `before` (its subject too, so a
    computer renamed since keeps its agent's computers): (token, record, lines)."""
    subject = (before or {}).get("subject", {}).get("id") or f"{DEFAULT_CLIENT}@{computer_name()}"
    grant, token = policy.issue(subject={"kind": "agent", "id": subject}, workspaces=["*"],
                                ceiling=DEFAULT_CEILING, specials=panel_specials(list(DEFAULT_SPECIALS)),
                                ttl_s=ttl_seconds(DEFAULT_AGENT_TTL), by="owner", note=GRANT_NOTE,
                                continues=before["id"] if before else None)
    lines = [(f"Agent grant {grant.id} issued for {subject}: every workspace, up to {DEFAULT_CEILING}, "
              f"specials {', '.join(sorted(DEFAULT_SPECIALS))}")]
    if before:
        lines.append(f"  it keeps the computers of {before['id']}: their saves, open environments and logins")
    return token, policy.grant_record(grant.id), lines


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
        if record is not None and held and not _due(record, policy.clock()):
            return held, record, [f"Agent grant {record['id']} kept", *_left_behind(home, policy, record)]
        before, fresh = _predecessor(policy, previous)
        token, new, lines = _issue(policy, before)
        if fresh:
            lines.append(fresh)
        old = previous.agent_grant_id if previous is not None else None
        if record is not None:  # renewed: chats open now still hold the old one, which ends on its own
            lines.append(f"  it renews {old}, which still works for the chats open now until "
                         f"{record.get('expires_at')}")
        elif old and old != new["id"]:
            with contextlib.suppress(Exception):  # one from a home that is gone: nothing to revoke here
                policy.revoke(old, by="owner", reason=REPLACED)
                lines.append(f"  revoked the grant it replaces, {old}")
        return token, new, [*lines, *_left_behind(home, policy, new)]
    finally:
        policy.close()


def _same_home(remote_home: str | None, home: Path) -> bool:
    if not remote_home:
        return False
    with contextlib.suppress(OSError):
        return Path(remote_home).expanduser().resolve() == Path(home).expanduser().resolve()
    return False


def renew_due(home: Path, *, client: Client | None = None) -> list[str]:
    """The service's renewal (`openajar local serve`, at its start and every few hours): when the `local`
    connection's grant for this home still works and has used RENEW_AT of its life, issue the next one (the
    same family), store its token where the clients read it and name it in the connection. The old one is not
    revoked: chats open now still hold it, and it ends on its own. Returns what it did, in lines; [] when
    nothing was due. An ended grant is left to `openajar local start` or `setup agents`."""
    home = Path(home).expanduser()
    client = client or Client(out=lambda _: None)
    cfg = local_home.read(home)
    if cfg is None:
        return []
    with grant_lock(home):
        return _renew(home, client, cfg)


def _renew(home: Path, client: Client, cfg) -> list[str]:
    """renew_due's work, under grant_lock: the connection and the token are read after the lock is held."""
    from openajar.policy.service import PolicyService

    previous = client.records.get(NAME)
    if previous is None or previous.credential != "agent_grant" or not _same_home(previous.remote_home, home):
        return []
    store = client.tokens(previous.token_store)
    policy = PolicyService(home, node_config=cfg)
    try:
        record = _held_grant(policy, store.get(NAME), previous)
        if record is None or not _due(record, policy.clock()):
            return []
        token, new, lines = _issue(policy, _predecessor(policy, previous)[0])  # a live one: always continued
        try:
            store.put(NAME, token)
        except Exception:
            # the clients would never read it: it must not stay live unused
            policy.revoke(new["id"], by="owner", reason="openajar local could not store the renewed grant")
            raise
        client.records.save(previous.model_copy(update={
            "agent_grant_id": new["id"], "agent_subject": (new.get("subject") or {}).get("id"),
            "agent_expires_at": _iso(new.get("expires_at"))}))
        lines[0] = (f"Renewed the agent grant {previous.agent_grant_id} with {new['id']}, before it ends on "
                    f"{record.get('expires_at')}; chats open now keep the old one until then")
        return lines
    finally:
        policy.close()


def _left_behind(home: Path, policy, record: dict) -> list[str]:
    """A line when earlier agent grants of this computer left saves or open environments no live grant reaches
    (openajar 0.7.0 and before gave each new grant a family of its own): the command that gives them back."""
    subject = (record.get("subject") or {}).get("id")
    if not subject:
        return []
    families = {f["family"] for f in policy.earlier_families(subject, record.get("env_family") or record["id"])}
    if not families:
        return []
    from openajar.environments.saves import SaveStore

    saves = [m for m in SaveStore(home).all() if m.family in families]
    opened = 0
    db = home / "environments.db"
    if db.exists():
        from openajar.environments.store import EnvironmentStore

        opened = sum(1 for e in EnvironmentStore(db).open() if e.family in families)
    if not saves and not opened:
        return []
    what = [f"{len(saves)} save{'s' if len(saves) != 1 else ''}" if saves else "",
            f"{opened} open environment{'s' if opened != 1 else ''}" if opened else ""]
    return [(f"  {' and '.join(w for w in what if w)} of earlier agent grants of this computer are not this "
             "grant's yet: `openajar environments adopt` lists them, and `--yes` gives them to it")]


def register_clients(home: Path, port: int, *, claude: bool = True, codex: bool = True, skills: bool = True,
                     client: Client | None = None) -> list[str]:
    """Issue (or keep) the local agent grant, store its token, and register Claude Code and Codex as
    `openajar-local` at http://127.0.0.1:<port>/mcp, and the skills. Returns lines saying what was done.
    `client`: tests pass one with a temporary home and fake tools."""
    home = Path(home).expanduser()
    client = client or Client(out=lambda _: None)
    kind = client.store_kind("auto")
    if local_home.read(home) is None:  # no node here yet: _grant says to start it first (and no lock file is made)
        return _register(home, port, client, kind, ClientChoice(claude=claude, codex=codex, skills=skills))
    with grant_lock(home):  # never beside the service's renewal: the token and the connection name one grant
        return _register(home, port, client, kind, ClientChoice(claude=claude, codex=codex, skills=skills))


def _register(home: Path, port: int, client: Client, kind: str, choice: ClientChoice) -> list[str]:
    token, record, lines = _grant(home, client, kind)
    conn = Connection(name=NAME, ssh_host="localhost", remote_home=str(home), remote_port=port, local_port=port,
                      node_name=NAME, token_store=kind, service="none", credential="agent_grant",
                      agent_grant_id=record.get("id"), agent_subject=(record.get("subject") or {}).get("id"),
                      agent_ceiling=DEFAULT_CEILING, agent_specials=sorted(DEFAULT_SPECIALS),
                      agent_expires_at=_iso(record.get("expires_at")))
    result = client.connect_here(conn, token, choice)
    local_home.mark_registered(home, choice.skills)  # its serve keeps the skills it installed up to date, or none
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
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)
