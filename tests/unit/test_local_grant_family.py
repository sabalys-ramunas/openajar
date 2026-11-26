"""The local agent grant keeps its family (audit S7-01, U5-01): environments, saves and title-bound logins
belong to the grant family, and `openajar local start` / `setup agents` issue a new grant when the held one
expired, was revoked or frozen, or its terms changed. The replacement continues the family of the one it
replaces across an expiry, a renewal or new terms, so the agent still reaches its computers (not after the
owner's revoke or a freeze: then adopt gives them over); the service renews the grant before it expires; and
only this computer's own agent grant (the same subject, issued by `openajar local`) is ever continued.

HOME is a temporary folder and tokens go to the file store; no command runs."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from testkit.timing import until

from openajar.api.bridge import panel_specials, ttl_seconds
from openajar.client.choices import DEFAULT_AGENT_TTL, DEFAULT_CEILING, DEFAULT_SPECIALS, Tools, computer_name
from openajar.client.ops import Client
from openajar.client.state import Connection, Paths
from openajar.core.errors import InvalidRequest
from openajar.environments import caller
from openajar.local import home as local_home
from openajar.local.paths import NAME
from openajar.local.register import GRANT_NOTE, register_clients, renew_due
from openajar.policy.service import PolicyService

DAY = 86400.0
SUBJECT = f"claude-code@{computer_name()}"


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "not-this-one"))
    return home


@pytest.fixture
def node(tmp_path, user_home):
    home = tmp_path / "openajar"
    local_home.ensure(home, 7410)
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(), out=lambda _: None)
    return SimpleNamespace(home=home, client=client)


def policy_of(node, clock=None) -> PolicyService:
    return PolicyService(node.home, node_config=local_home.read(node.home), **({"clock": clock} if clock else {}))


def held(node, *, days_ago: float, specials: list[str] | None = None, note: str = GRANT_NOTE,
         subject: str = SUBJECT) -> str:
    """A local agent grant issued `days_ago` days ago (90 days of life), its token stored and the `local`
    connection naming it, as `openajar local start` left them. Returns its id."""
    then = time.time() - days_ago * DAY
    policy = policy_of(node, clock=lambda: then)
    try:
        grant, token = policy.issue(subject={"kind": "agent", "id": subject}, workspaces=["*"], ceiling=DEFAULT_CEILING,
                                    specials=specials or panel_specials(list(DEFAULT_SPECIALS)),
                                    ttl_s=ttl_seconds(DEFAULT_AGENT_TTL), by="owner", note=note)
    finally:
        policy.close()
    kind = node.client.store_kind("auto")
    node.client.tokens(kind).put(NAME, token)
    node.client.records.save(Connection(
        name=NAME, ssh_host="localhost", remote_home=str(node.home), remote_port=7410, local_port=7410,
        node_name=NAME, token_store=kind, service="none", credential="agent_grant", agent_grant_id=grant.id,
        agent_subject=subject, agent_ceiling=DEFAULT_CEILING, agent_specials=sorted(DEFAULT_SPECIALS)))
    return grant.id


def resolved(node) -> tuple[object, dict]:
    """The grant the stored token is now, as a call resolves it, and its record."""
    policy = policy_of(node)
    try:
        token = node.client.tokens(node.client.store_kind("auto")).get(NAME)
        _, grant = policy.resolve(token)
        return grant, policy.grant_record(grant.id)
    finally:
        policy.close()


def family_of(grant) -> str | None:
    """The family environments see for a call with this grant (openajar.environments.caller)."""
    return caller.family(SimpleNamespace(grant=grant))


def state_of(node, grant_id: str) -> str:
    policy = policy_of(node)
    try:
        return policy.grant_record(grant_id)["state"]
    finally:
        policy.close()


# ------------------------------------------------------------------ re-issued: the family stays


def test_an_expired_grant_then_setup_agents_keeps_the_agents_computers(node):
    """S7-01 as the audit ran it: the 90 days ran out, then `openajar setup agents` (what every hint says)."""
    old = held(node, days_ago=91)
    assert state_of(node, old) == "expired"
    lines = register_clients(node.home, 7410, client=node.client, codex=False)
    grant, record = resolved(node)
    assert grant.id != old and record["state"] == "active" and "issued" in lines[0]
    assert family_of(grant) == old  # the computers, saves and logins of the old grant are this one's
    assert any(f"keeps the computers of {old}" in line for line in lines), lines
    assert grant.subject.id == SUBJECT


def test_after_a_freeze_and_an_unfreeze_the_new_grant_starts_without_the_computers_and_names_adopt(node):
    """R1 #5: a freeze (an agent that misbehaved) is not carried over by itself. The new grant has a family of its
    own; the output says so, and `openajar environments adopt` gives the earlier computers to it after a look."""
    from openajar.policy.freeze_store import FreezeRecord, FreezeStore

    old = held(node, days_ago=3)
    store = FreezeStore(node.home)
    store.put(FreezeRecord("node", "node", "a test", "owner", time.time()))
    policy = policy_of(node)
    try:
        policy.revoke_reaching(everything=True, by="owner", reason="a test")
    finally:
        policy.close()
    assert state_of(node, old) == "revoked"
    with pytest.raises(Exception, match="frozen"):  # no new way in while it is frozen
        register_clients(node.home, 7410, client=node.client, codex=False)
    store.remove("node")
    lines = register_clients(node.home, 7410, client=node.client, codex=False)
    grant, _ = resolved(node)
    assert grant.id != old and family_of(grant) == grant.id
    said = next((line for line in lines if "starts without the computers" in line), "")
    assert old in said and "a freeze revoked it" in said and "openajar environments adopt" in said, lines
    assert "—" not in "\n".join(lines)


def test_after_the_owner_revoked_the_grant_the_new_one_starts_without_its_computers(node):
    """R1 #5: a revoke for a leaked token must not hand the machines, saves and logins on to the next grant."""
    old = held(node, days_ago=3)
    policy = policy_of(node)
    try:
        policy.revoke(old, by="owner", reason="the token leaked")
    finally:
        policy.close()
    lines = register_clients(node.home, 7410, client=node.client, codex=False)
    grant, _ = resolved(node)
    assert family_of(grant) == grant.id != old
    assert any("you revoked it (the token leaked)" in line and "adopt" in line for line in lines), lines
    assert not any(f"keeps the computers of {old}" in line for line in lines)


def test_a_grant_replaced_for_new_default_terms_keeps_the_family_and_is_revoked(node):
    """A release that changes the default specials replaces the grant (0.4.0 added secret on env/*)."""
    lanes = {"lanes": ["env/*"]}
    old = held(node, days_ago=10, specials={"vm": lanes, "exec": lanes, "memory": {}})
    lines = register_clients(node.home, 7410, client=node.client, codex=False)
    grant, record = resolved(node)
    assert grant.id != old and "secret" in record["scope"]["specials"]
    assert family_of(grant) == old and state_of(node, old) == "revoked"
    assert any(f"revoked the grant it replaces, {old}" in line for line in lines)


def test_a_family_carries_on_across_several_replacements(node):
    from openajar.local.register import REPLACED

    first = held(node, days_ago=200)
    register_clients(node.home, 7410, client=node.client, codex=False)
    second, _ = resolved(node)
    policy = policy_of(node)
    try:
        policy.revoke(second.id, by="owner", reason=REPLACED)  # as openajar's own replacement revokes it
    finally:
        policy.close()
    register_clients(node.home, 7410, client=node.client, codex=False)
    third, _ = resolved(node)
    assert len({first, second.id, third.id}) == 3
    assert family_of(second) == family_of(third) == first


def test_a_grant_of_another_kind_is_replaced_as_before_without_its_family(node):
    """Only this computer's own local grant carries on: one `openajar local` did not issue (an older
    `up --here`, a grant someone named here by hand) is replaced as before, with a family of its own."""
    old = held(node, days_ago=91, note="issued by hand")
    register_clients(node.home, 7410, client=node.client, codex=False)
    grant, _ = resolved(node)
    assert grant.id != old and family_of(grant) == grant.id


# ------------------------------------------------------------------ renewed before it ends


def test_setup_renews_a_grant_past_four_fifths_of_its_life_and_lets_the_old_one_run_out(node):
    old = held(node, days_ago=75)  # 83 % of 90 days
    lines = register_clients(node.home, 7410, client=node.client, codex=False)
    grant, _ = resolved(node)
    assert grant.id != old and family_of(grant) == old
    assert state_of(node, old) == "active"  # chats open now still hold it: it ends on its own
    assert any("renews" in line and old in line for line in lines), lines
    assert node.client.records.get(NAME).agent_grant_id == grant.id


def test_a_grant_with_time_left_is_kept(node):
    old = held(node, days_ago=30)
    lines = register_clients(node.home, 7410, client=node.client, codex=False)
    assert lines[0] == f"Agent grant {old} kept"


def test_the_service_renews_the_grant_on_its_own(node):
    """`openajar local serve` checks at its start and then every few hours (renew_due): the token the
    clients read changes, the connection names the new grant, and the family is the same."""
    old = held(node, days_ago=80)
    lines = renew_due(node.home, client=node.client)
    grant, _ = resolved(node)
    assert grant.id != old and family_of(grant) == old and lines and old in lines[0]
    conn = node.client.records.get(NAME)
    assert conn.agent_grant_id == grant.id and conn.agent_expires_at
    assert renew_due(node.home, client=node.client) == []  # fresh now: nothing to do
    assert "—" not in "\n".join(lines)


def test_the_service_never_renews_for_a_connection_of_another_home(node, tmp_path):
    held(node, days_ago=80)
    other = tmp_path / "other-home"
    local_home.ensure(other, 7411)
    assert renew_due(other, client=node.client) == []


def test_the_service_leaves_an_ended_grant_to_setup(node):
    """An expired or revoked grant is not the service's to replace: `setup agents` does it (keeping the family
    of an expired one), so a grant the owner revoked never comes back by itself."""
    old = held(node, days_ago=91)
    assert renew_due(node.home, client=node.client) == []
    assert node.client.records.get(NAME).agent_grant_id == old


# ------------------------------------------------------------------ who may continue a family


def test_a_family_continues_only_for_the_same_subject_of_a_root_agent_grant_of_this_node(tmp_path):
    home = tmp_path / "h"
    policy = PolicyService(home)
    try:
        sub = {"kind": "agent", "id": SUBJECT}
        old, old_token = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE)
        new, new_token = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE,
                                      continues=old.id)
        assert new.env_family == old.id and family_of(new) == old.id
        _, again = policy.resolve(new_token)  # resolving its token carries the family too
        assert again.env_family == old.id and family_of(again) == old.id
        assert policy.grant_record(new.id)["env_family"] == old.id
        with pytest.raises(InvalidRequest, match="subject"):  # another agent never takes it
            policy.issue(subject={"kind": "agent", "id": "claude-code@another"}, workspaces=["*"],
                         ceiling="commit", continues=old.id)
        child, _ = policy.attenuate(old_token, subject={"id": "worker"}, workspaces=["*"])
        with pytest.raises(InvalidRequest, match="root"):  # a narrowed grant is no family's head
            policy.issue(subject=sub, workspaces=["*"], ceiling="commit", continues=child.id)
        with pytest.raises(InvalidRequest, match="no grant"):
            policy.issue(subject=sub, workspaces=["*"], ceiling="commit", continues="gr_nothere")
        # one that continues the new one keeps the first's family; one narrowed from it reaches the same
        _, third_token = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", continues=new.id)
        _, third = policy.resolve(third_token)
        assert third.env_family == old.id
        _, worker_token = policy.attenuate(third_token, subject={"id": "worker"}, workspaces=["*"])
        _, worker = policy.resolve(worker_token)
        assert family_of(worker) == old.id
    finally:
        policy.close()


def test_a_continued_family_is_in_the_audit_trail(tmp_path):
    home = tmp_path / "h"
    policy = PolicyService(home)
    try:
        sub = {"kind": "agent", "id": SUBJECT}
        old, _ = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE)
        new, _ = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE, continues=old.id)
        rows, _ = policy.traces.query(kind="grant_family", limit=50)
        assert rows and rows[0].grant_id == new.id and rows[0].args["continues"] == old.id
        assert rows[0].args["family"] == old.id
    finally:
        policy.close()


def test_the_api_never_continues_a_family():
    """A remote client's grant comes from POST /api/grants (openajar connect), which has no way to name
    one: only `openajar local` on this computer continues a family."""
    import inspect

    from openajar.api.bridge import PolicyAdapter

    assert "continues" not in inspect.signature(PolicyAdapter.issue_grant).parameters


async def test_the_service_looks_at_its_start_and_then_every_few_hours(tmp_path, monkeypatch):
    """`openajar local serve` runs renew_grant beside the node (only for the home `start` registered)."""
    import asyncio

    from openajar.local import node as local_node

    seen: list = []
    monkeypatch.setattr("openajar.local.register.renew_due", lambda home: seen.append(home) or ["renewed"])
    task = asyncio.create_task(local_node.renew_grant(tmp_path, every_s=3600))
    await until(lambda: seen == [tmp_path])  # at once, not after the first interval
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_workspace_import_never_makes_a_grant_that_passes_for_this_computers_own():
    """A grant an export file defines is issued again on import (reissue_grants): never with the note that marks
    this computer's own agent grant, so a file cannot make one that `adopt` or `save-import` would give computers
    to."""
    from datetime import UTC, datetime

    from openajar.workspaces.transfer import import_ as wsimport

    plan = SimpleNamespace(options=SimpleNamespace(reissue_grants=True), workspace_from="acme", workspace_to="acme",
                           blockers=[], notes=[], grants=[], _grants=[])
    contents = SimpleNamespace(grants=[{"id": "gr_x", "subject": {"kind": "agent", "id": SUBJECT}, "note": GRANT_NOTE,
                                        "workspaces": ["acme"]}])
    wsimport._plan_grants(plan, contents, SimpleNamespace(policy=None), datetime.now(UTC))
    assert plan._grants and plan._grants[0]["note"] is None


def test_a_continued_grant_keeps_the_taint_of_the_grant_it_continues(tmp_path):
    """R1 #4: the new grant reaches the same machines, saves and logins, which hold what the old one read; it started
    with a clean taint ledger, which elsewhere only the owner in person can give (clear_taint)."""
    from openajar.policy.taint import family_key

    policy = PolicyService(tmp_path / "h")
    try:
        sub = {"kind": "agent", "id": SUBJECT}
        old, _ = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE)
        policy.taint.add(family_key(old), {"private", "untrusted"}, source="the agent read a private mail and a page")
        _, token = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE, continues=old.id)
        _, resolved_new = policy.resolve(token)
        assert family_of(resolved_new) == old.id  # the same computers
        assert {"private", "untrusted"} <= policy.taint.get(family_key(resolved_new))
        fresh, _ = policy.issue(subject=sub, workspaces=["*"], ceiling="commit", note=GRANT_NOTE)
        assert policy.taint.get(family_key(fresh)) == set()  # a grant of a family of its own starts clean
    finally:
        policy.close()


def test_setup_and_the_services_renewal_at_once_issue_one_grant_and_agree_on_it(node, monkeypatch):
    """R1 #14: `openajar local start` registers the clients while the service it started renews. Both issued a new
    grant, and the token and the `local` connection could name different ones, which stopped every later renewal.
    Here the first to issue waits for the other to reach the same step; with the lock the other never does."""
    import threading

    from openajar.local import register

    old = held(node, days_ago=80)
    real, entered, second, issued = register._issue, threading.Event(), threading.Event(), []

    def issue(policy, before):
        if entered.is_set():
            issued.append("second")
            second.set()
        else:
            entered.set()
            second.wait(2.0)  # the other comes in here at once, were there no lock; with it, it never does
            issued.append("first")
        return real(policy, before)

    monkeypatch.setattr(register, "_issue", issue)
    out: dict = {}
    service = threading.Thread(target=lambda: out.setdefault("renew", renew_due(node.home, client=node.client)))
    service.start()
    assert entered.wait(10)
    register_clients(node.home, 7410, client=node.client, codex=False)
    service.join(30)
    grant, _ = resolved(node)
    assert issued == ["first"], issued
    assert node.client.records.get(NAME).agent_grant_id == grant.id != old
    assert family_of(grant) == old
