"""Computers that outlive a grant (audit S7-01, U5-01, U5-02, U5-03): a local agent grant that replaces an
earlier one of this computer keeps reaching its saved and open computers; one a 0.7.0 node left behind is
named to the agent instead of opening a blank machine, and the owner gives it to the current grant with
`openajar environments adopt` (a dry run first, nothing deleted, in the audit trail); a different agent's
computers are never taken; a save imported on the same node shares its snapshot, and forgetting one copy
keeps the snapshot while the other still names it.

On the saves harness of test_environments_saves: real git and tar on folders, a fake cloud."""

from __future__ import annotations

import json
import tarfile

import pytest
from test_environments_saves import REPO, TITLE, node, opened  # noqa: F401  (node is a fixture)
from test_vault_support import make_vault, private_home
from typer.testing import CliRunner

from openajar.core.models import Effect, utcnow
from openajar.environments import logins
from openajar.environments.adopt import Adoption
from openajar.environments.saves import SaveStore, export_save, import_save
from openajar.local.register import GRANT_NOTE
from openajar.policy.wrap import ToolCallError

SUBJECT = "claude-code@robins-mac"


def local_grant(policy, *, subject: str = SUBJECT, note: str | None = GRANT_NOTE, continues: str | None = None):
    """An agent grant as `openajar local` issues it for this computer (or, with note None, as `openajar
    connect` issues one over the API for another computer)."""
    kw = {"continues": continues} if continues else {}
    return policy.issue(subject={"kind": "agent", "id": subject}, workspaces=["*"], ceiling=Effect.COMMIT,
                        specials={"vm": {}, "exec": {}}, note=note, **kw)


async def saved_by(node, token, title=TITLE, session="chat-1"):  # noqa: F811
    out = await opened(node, keep="save", title=title, tok=token, session=session)
    closed = await node["call"]("environment_close", on=out["on"], tok=token, session=session)
    assert closed["saved"]["verified"], closed
    return closed["saved"]


@pytest.fixture
def policy(node):  # noqa: F811
    return node["rt"].get("policy")


@pytest.fixture
async def vault(tmp_path):
    (tmp_path / "vault").mkdir()
    v = make_vault(private_home(tmp_path / "vault"))
    await v.start(control=False)
    yield v
    await v.stop()


def adoption(node, policy, vault=None):  # noqa: F811
    mgr = node["mgr"]
    return Adoption(policy=policy, store=mgr.store, saves=mgr.saves(), vault=vault)


# ------------------------------------------------------------------ the replacement keeps the family


async def test_a_grant_that_replaces_this_computers_grant_resumes_its_save(node, policy):  # noqa: F811
    """U5-01's run with the fix: a save, a new grant issued the way `setup agents` issues it when the held one
    lapsed (the old one revoked), then a new chat on the new grant: environment() lists the save and the title
    resumes it, from the snapshot, with the work."""
    old, old_token = local_grant(policy)
    saved = await saved_by(node, old_token)
    _new, new_token = local_grant(policy, continues=old.id)
    policy.revoke(old.id, by="owner", reason="openajar local issued a new grant for this computer")
    listed = await node["call"]("environment", tok=new_token, session="chat-2")
    assert [s["title"] for s in listed["saved"]] == [TITLE] and "earlier" not in listed
    again = await node["call"]("environment_open", title=TITLE, tok=new_token, session="chat-2")
    assert again["opened"] == "resumed" and again["resumed"]["save_id"] == saved["save_id"], again
    assert again["resumed"]["from"] == "snapshot"


# ------------------------------------------------------------------ what 0.7.0 left behind: named, then adopted


async def stranded(node, policy, vault=None):  # noqa: F811
    """What a 0.7.0 node left after `setup agents` replaced an expired grant: the old grant's save of TITLE
    and its open "Docs" machine, a login bound by title to its computer, and a new grant of a family of
    its own."""
    old, old_token = local_grant(policy)
    saved = await saved_by(node, old_token)
    docs = await opened(node, keep="save", title="Docs", tok=old_token, session="chat-1")
    login = None
    if vault is not None:
        login = await logins.add(vault, "https://github.com/login", user="me", password="pw-for-a-test-0001",
                                 scope={"title": TITLE, "family": old.id, "agent": SUBJECT})
    new, new_token = local_grant(policy)  # a family of its own, as before 0.8.1
    policy.revoke(old.id, by="owner", reason="openajar local issued a new grant for this computer")
    return {"old": old, "new": new, "token": new_token, "save": saved["save_id"], "docs": docs["id"],
            "login": login}


async def test_a_title_an_earlier_grant_saved_is_named_to_the_agent_not_opened_blank(node, policy):  # noqa: F811
    s = await stranded(node, policy)
    servers = len(node["cloud"].servers)
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_open", title=TITLE, tok=s["token"], session="chat-2")
    assert e.value.code == "saved_by_earlier_grant", e.value
    assert "openajar environments adopt" in e.value.message and TITLE in e.value.message
    assert len(node["cloud"].servers) == servers  # nothing rented: no blank machine, no second bill
    listed = await node["call"]("environment", tok=s["token"], session="chat-2")
    assert listed["saved"] == [] and listed["open"] == []
    assert listed["earlier"]["titles"] == sorted(["Docs", TITLE])
    assert "openajar environments adopt" in listed["earlier"]["note"] and "—" not in listed["earlier"]["note"]
    # a new title still opens a new computer
    fresh = await opened(node, keep="none", title="Something new", tok=s["token"], session="chat-2")
    assert fresh["opened"] == "new"


async def test_another_agents_computers_are_not_named_to_it(node, policy):  # noqa: F811
    """A title is the agent's own choice: an agent of another subject (or one connect issued for another
    computer) is never told about this computer's agent's titles, and opens its own."""
    await stranded(node, policy)
    _, other_token = local_grant(policy, subject="claude-code@another-mac")
    out = await opened(node, keep="none", title=TITLE, tok=other_token, session="chat-3")
    assert out["opened"] == "new"
    _, remote_token = local_grant(policy, note=None)  # same name, issued over the API for another computer
    listed = await node["call"]("environment", tok=remote_token, session="chat-4")
    assert "earlier" not in listed


async def test_adopt_lists_first_then_moves_once_and_writes_the_audit_trail(node, policy, vault):  # noqa: F811
    s = await stranded(node, policy, vault)
    a = adoption(node, policy, vault)
    plan = await a.plan()
    assert plan["to"]["grant"] == s["new"].id and plan["to"]["family"] == s["new"].id
    assert [f["family"] for f in plan["from"]] == [s["old"].id] and plan["from"][0]["state"] == "revoked"
    assert [m["id"] for m in plan["saves"]] == [s["save"]] and [e["id"] for e in plan["environments"]] == [s["docs"]]
    assert [x["id"] for x in plan["logins"]] == [s["login"].ref]
    # the dry run changed nothing
    m, _ = node["mgr"].saves().find(s["save"])
    assert m.family == s["old"].id and node["mgr"].get(s["docs"]).family == s["old"].id

    done = await a.apply()
    assert done["moved"] is True
    m, path = node["mgr"].saves().find(s["save"])
    assert m.family == s["new"].id and path == node["mgr"].saves().folder(m.family, m.title, m.id)
    docs = node["mgr"].get(s["docs"])
    assert docs.family == s["new"].id and docs.binding.startswith("kept:") and docs.lease is None
    meta = await vault.get_entry(s["login"].ref)
    assert meta.config["envs"]["scopes"] == [{"title": TITLE, "family": s["new"].id, "agent": SUBJECT}]
    rows, _ = policy.traces.query(kind="environments_adopted", limit=10)
    assert len(rows) == 1 and rows[0].args["from"] == [s["old"].id] and rows[0].args["to"] == s["new"].id
    assert rows[0].args["saves"] == [s["save"]] and rows[0].args["environments"] == [s["docs"]]

    # the agent has them back: the save resumes, the open machine is its own again
    listed = await node["call"]("environment", tok=s["token"], session="chat-2")
    assert "earlier" not in listed and [x["title"] for x in listed["saved"]] == [TITLE]
    again = await node["call"]("environment_open", title=TITLE, tok=s["token"], session="chat-2")
    assert again["opened"] == "resumed" and again["resumed"]["save_id"] == s["save"]
    back = await node["call"]("environment_open", title="Docs", tok=s["token"], session="chat-5")
    assert back["id"] == s["docs"] and back["opened"] in ("attached", "taken_over"), back

    # once: nothing is left to move
    left = await a.plan()
    assert left["saves"] == [] and left["environments"] == [] and left["logins"] == []


async def test_adopt_never_takes_another_subjects_or_a_remote_clients_computers(node, policy):  # noqa: F811
    s = await stranded(node, policy)
    other, other_token = local_grant(policy, subject="claude-code@another-mac")
    other_save = await saved_by(node, other_token, title="Theirs", session="chat-7")
    remote, remote_token = local_grant(policy, note=None)  # what connect issues over the API: no local note
    remote_save = await saved_by(node, remote_token, title="Remote", session="chat-8")
    policy.revoke(other.id, by="owner")
    policy.revoke(remote.id, by="owner")
    a = adoption(node, policy)
    plan = await a.plan()
    assert [f["family"] for f in plan["from"]] == [s["old"].id]
    assert [x["family"] for x in plan["left"]] == [other.id] and "another" in plan["left"][0]["why"]
    await a.apply()
    store = node["mgr"].saves()
    assert store.find(other_save["save_id"])[0].family == other.id
    assert store.find(remote_save["save_id"])[0].family == remote.id


async def test_a_title_the_current_grant_has_too_moves_under_a_name_of_its_own(node, policy):  # noqa: F811
    """The new grant opened the same title again (a blank machine, then saved): neither save may be resumed
    in place of the other, nor pruned by the other's keep_last, so the moved one takes "(earlier)"."""
    old, old_token = local_grant(policy)
    first = await saved_by(node, old_token)
    _new, new_token = local_grant(policy)  # a family of its own, as before 0.8.1
    blank = await saved_by(node, new_token, session="chat-2")  # 0.7.0 opened the title again, empty
    policy.revoke(old.id, by="owner")
    a = adoption(node, policy)
    plan = await a.plan()
    [moved] = plan["saves"]
    assert moved["id"] == first["save_id"] and moved["as"] == f"{TITLE} (earlier)"
    await a.apply()
    store = node["mgr"].saves()
    assert store.find(first["save_id"])[0].title == f"{TITLE} (earlier)"
    assert store.find(blank["save_id"])[0].title == TITLE
    again = await node["call"]("environment_open", title=f"{TITLE} (earlier)", tok=new_token, session="chat-9")
    assert again["resumed"]["save_id"] == first["save_id"]


async def test_adopt_needs_a_current_local_grant(node, policy):  # noqa: F811
    from openajar.core.errors import InvalidRequest

    old, old_token = local_grant(policy)
    await saved_by(node, old_token)
    policy.revoke(old.id, by="owner")
    with pytest.raises(InvalidRequest, match="setup agents"):
        await adoption(node, policy).plan()


# ------------------------------------------------------------------ a snapshot two saves share


async def test_forgetting_one_of_two_saves_that_share_a_snapshot_keeps_it(node, tmp_path):  # noqa: F811
    """U5-03: a same-node import shares the original's snapshot. Forgetting the original leaves the snapshot to
    the import (which still resumes from it); forgetting the import, the last one naming it, deletes it."""
    await opened(node, keep="save")
    closed = await node["call"]("environment_close")
    sid, snap = closed["saved"]["save_id"], closed["saved"]["snapshot"]["id"]
    store, key = node["mgr"].saves(), await node["mgr"]._saves_key(create=False)
    f = tmp_path / "save.tar"
    export_save(store, sid, f, key)
    copy = import_save(store, f, key, family=store.find(sid)[0].family + "x")
    assert copy.snapshot.id == snap
    first = await node["mgr"].forget(None, None, sid)
    assert first["forgot"] == [sid] and first["snapshots_deleted"] == 0
    assert first["snapshots_kept"] == [{"id": snap, "save": sid, "used_by": [copy.id]}]
    assert snap in node["cloud"].snapshots
    second = await node["mgr"].forget(None, None, copy.id)
    assert second["snapshots_deleted"] == 1 and snap not in node["cloud"].snapshots


async def test_pruning_keeps_a_snapshot_another_save_names(node, tmp_path):  # noqa: F811
    await opened(node, keep="save")
    closed = await node["call"]("environment_close")
    sid, snap = closed["saved"]["save_id"], closed["saved"]["snapshot"]["id"]
    store, key = node["mgr"].saves(), await node["mgr"]._saves_key(create=False)
    f = tmp_path / "save.tar"
    export_save(store, sid, f, key)
    copy = import_save(store, f, key, family="g_elsewhere")
    m, _ = store.find(sid)
    from datetime import timedelta

    days = timedelta(days=node["mgr"].settings.saved_days)
    store.write_manifest(copy.model_copy(update={"saved_at": m.saved_at + timedelta(days=20)}))  # the copy is younger
    await node["mgr"].prune_saves(m.saved_at + days - timedelta(hours=12))  # told first
    out = await node["mgr"].prune_saves(m.saved_at + days + timedelta(hours=1))
    assert out["removed"] == [sid] and snap in node["cloud"].snapshots  # the copy still resumes from it


async def test_a_save_that_does_not_read_holds_forget_and_prune_of_a_snapshot(node, tmp_path):  # noqa: F811
    """U5-03 under S1-05: whether another save names a snapshot is known only while every save reads. The copy
    that shares it does not read (a newer version's manifest): forgetting or pruning the original deletes
    nothing and keeps the save, saying why; once the copy reads again, the forget keeps the snapshot for it."""
    from datetime import timedelta

    mgr, cloud = node["mgr"], node["cloud"]
    await opened(node, keep="save")
    closed = await node["call"]("environment_close")
    sid, snap = closed["saved"]["save_id"], closed["saved"]["snapshot"]["id"]
    store, key = mgr.saves(), await mgr._saves_key(create=False)
    f = tmp_path / "save.tar"
    export_save(store, sid, f, key)
    copy = import_save(store, f, key, family="g_elsewhere")
    _m, path = store.find(copy.id)
    manifest = path / "manifest.json"
    good = manifest.read_text()
    manifest.write_text(good.replace('"kind": "save"', '"kind": "checkpoint"'))  # it does not read now
    held = await mgr.forget(None, None, sid)
    assert held["forgot"] == [] and held["snapshots_deleted"] == 0, held
    assert [k["id"] for k in held["kept"]] == [sid] and "does not read" in held["kept"][0]["why"]
    m, _ = store.find(sid)
    days = timedelta(days=mgr.settings.saved_days)
    await mgr.prune_saves(m.saved_at + days - timedelta(hours=12))  # told first
    pruned = await mgr.prune_saves(m.saved_at + days + timedelta(hours=1))
    assert pruned["removed"] == [] and snap in cloud.snapshots and store.find(sid)
    manifest.write_text(good)
    out = await mgr.forget(None, None, sid)
    assert out["forgot"] == [sid] and out["snapshots_deleted"] == 0 and snap in cloud.snapshots
    assert out["snapshots_kept"] == [{"id": snap, "save": sid, "used_by": [copy.id]}]


# ------------------------------------------------------------------ save-import says whose it is


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    """A node's home with this computer's agent grant replaced the 0.7.0 way (the old one revoked, the new
    one a family of its own). OPENAJAR_HOME and HOME point at temporary folders: the CLI reads nothing else."""
    from openajar.policy.service import PolicyService

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    (tmp_path / "user").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (home / "environments.toml").write_text("encrypt_saves = false\n")
    policy = PolicyService(home)
    old, _ = local_grant(policy)
    new, _ = local_grant(policy)  # this computer's agent now, a family of its own (0.7.0)
    policy.revoke(old.id, by="owner")
    policy.close()
    return home, old, new


def _an_export(tmp_path, family: str) -> tuple:
    from openajar.environments.saves import SaveManifest, new_save_id, seal_payload

    store = SaveStore(tmp_path / "elsewhere")
    tmp = store.tmp()
    with tarfile.open(tmp / "files.tgz", "w:gz"):
        pass
    sid = new_save_id()
    m = SaveManifest(id=sid, family=family, title="Shop", env_id="e0000000000", saved_at=utcnow(),
                     checksums=seal_payload(tmp, sid, None), encrypted=False)
    store.commit(tmp, m)
    f = tmp_path / "shop.tar"
    export_save(store, sid, f, None)
    return f, m


def test_save_import_gives_it_to_this_computers_current_agent_and_says_so(tmp_path, owner_home):
    """U5-02: the default family was the one in the file (the revoked grant's), and the command said "resumes
    it". Now it is the current local agent grant's, and the words name it."""
    from openajar.environments.cli import app as env_cli

    home, old, new = owner_home
    f, _ = _an_export(tmp_path, old.id)
    out = CliRunner().invoke(env_cli, ["--home", str(home), "save-import", str(f)])
    assert out.exit_code == 0, out.output
    [m] = SaveStore(home).all()
    assert m.family == new.id and SUBJECT in out.output and "resumes it" in out.output and "—" not in out.output


def test_save_import_into_a_family_no_live_grant_has_says_nobody_resumes_it(tmp_path, owner_home):
    from openajar.environments.cli import app as env_cli

    home, old, _new = owner_home
    f, _ = _an_export(tmp_path, old.id)
    out = CliRunner().invoke(env_cli, ["--home", str(home), "save-import", str(f), "--family", old.id])
    assert out.exit_code == 0, out.output
    [m] = SaveStore(home).all()
    assert m.family == old.id and "no agent can resume it" in out.output and "resumes it" not in out.output
    bad = CliRunner().invoke(env_cli, ["--home", str(home), "save-import", str(f), "--family", "gr_nothere"])
    assert bad.exit_code == 1 and "grants list" in bad.output


def test_the_owners_list_marks_saves_of_an_earlier_grant(tmp_path, owner_home):
    from openajar.environments.cli import app as env_cli

    home, old, _new = owner_home
    f, _ = _an_export(tmp_path, old.id)
    CliRunner().invoke(env_cli, ["--home", str(home), "save-import", str(f), "--family", old.id])
    text = CliRunner().invoke(env_cli, ["--home", str(home), "saves"]).output
    assert "earlier agent grant" in text and "openajar environments adopt" in text and "—" not in text
    rows = json.loads(CliRunner().invoke(env_cli, ["--home", str(home), "saves", "--json"]).output)["saved"]
    assert rows[0]["earlier"] is True


def test_the_adopt_command_lists_without_yes_and_moves_with_it(tmp_path, owner_home):
    from openajar.environments import adopt_cli  # noqa: F401  (its command joins the app)
    from openajar.environments.cli import app as env_cli

    home, old, new = owner_home
    f, _ = _an_export(tmp_path, old.id)
    CliRunner().invoke(env_cli, ["--home", str(home), "save-import", str(f), "--family", old.id])
    dry = CliRunner().invoke(env_cli, ["--home", str(home), "adopt"])
    assert dry.exit_code == 0, dry.output
    assert "Shop" in dry.output and "--yes" in dry.output and "—" not in dry.output
    assert SaveStore(home).all()[0].family == old.id
    done = CliRunner().invoke(env_cli, ["--home", str(home), "adopt", "--yes"])
    assert done.exit_code == 0 and "Moved" in done.output, done.output
    assert SaveStore(home).all()[0].family == new.id
    again = CliRunner().invoke(env_cli, ["--home", str(home), "adopt"])
    assert "Nothing to move" in again.output
