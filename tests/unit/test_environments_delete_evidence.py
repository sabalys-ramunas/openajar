"""A delete needs positive evidence; silence is never permission (the 0.7.0 audit: S1-01, S1-02, S1-05, S1-16,
S2-01, S6-01). Each test here lost work on 0.7.0: a VM reaper that deleted a machine before the environments'
reaper had saved it, a reaper pass that one environment's error stopped for all, a reaper that read a missing
service as a deleted machine, a snapshot sweep that took an unreadable save for none, and a files-only save that
missed staged work. On the fake disk cloud of test_environments_saves (real git, real save scripts)."""

from __future__ import annotations

import json
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_environments_saves import TITLE, disk, git, node, opened, work_on_it  # noqa: F401 - node is the fixture
from testkit.timing import until

from openajar.environments import saving
from openajar.environments.saves import open_payload
from openajar.vms.manager import ENV_SAVE_GRACE


class Awake:
    """The VM reaper's awake clock (time.monotonic in the node): seconds that only move when a test says so."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _vm(node, env_id: str):  # noqa: F811
    return node["vms"].store.get(node["mgr"].get(env_id).vm_id)


# ------------------------------------------------------------------ S1-01: the VM reaper waits for the save


async def test_a_restart_past_the_limit_and_the_grace_saves_before_the_vm_reaper_may_delete(node):  # noqa: F811
    """The node was off through the limit plus ENV_SAVE_GRACE (overnight): at the start the VM loop's first tick
    can come before the environments' pass. It holds the machine until that pass saved the work."""
    out = await opened(node, keep="save")
    work_on_it(disk(node, out["id"]) / "work" / "shop")
    mgr, vms = node["mgr"], node["vms"]
    morning = mgr.get(out["id"]).expires_at + timedelta(hours=12)
    first = await vms.reap_once(morning)  # the VM loop's tick, before any environments' pass
    assert first["deleted"] == [] and _vm(node, out["id"]).state == "ready"
    got = await mgr.reap_once(morning)
    env = mgr.get(out["id"])
    assert got["closed"] == [out["id"]] and got["saved"][0]["save"] == env.last_save_id
    assert "saved as" in env.closed_reason and env.vm_state == "deleted"
    assert mgr.saves().find(env.last_save_id)[0].snapshot is not None


async def test_both_reapers_own_loops_after_a_restart_past_the_limit_save_the_work(node):  # noqa: F811
    """The two services' real loops, started in the node's order (the VMs first), with the clock 12 h past the
    limit: the work is saved before the machine goes."""
    out = await opened(node, keep="save")
    head = work_on_it(disk(node, out["id"]) / "work" / "shop")
    mgr, vms = node["mgr"], node["vms"]
    morning = mgr.get(out["id"]).expires_at + timedelta(hours=12)
    mgr._clock = vms._clock = lambda: morning
    await vms.start()
    await mgr.start()
    assert await until(lambda: not mgr.get(out["id"]).open, timeout=60)
    env = mgr.get(out["id"])
    assert env.last_save_id and "saved as" in env.closed_reason, env.closed_reason
    assert mgr.saves().find(env.last_save_id)[0].head == head


async def test_the_grace_counts_awake_time_from_the_first_sighting(node):  # noqa: F811
    """Past the limit the environments' reaper gets ENV_SAVE_GRACE of this process's awake time, counted from when
    the VM reaper first saw the machine expired, never from the wall clock (a laptop that slept has used none)."""
    out = await opened(node, keep="save")
    mgr, vms = node["mgr"], node["vms"]
    vms._awake = awake = Awake()
    rec = _vm(node, out["id"])
    morning = rec.expires_at + ENV_SAVE_GRACE + timedelta(hours=10)
    node["cloud"].full.add(rec.provider_id)  # the save cannot be made: only the grace decides
    await mgr.reap_once(morning)
    assert (await vms.reap_once(morning))["deleted"] == []  # first seen expired now: the whole grace is left
    awake.advance(ENV_SAVE_GRACE.total_seconds() - 60)
    assert (await vms.reap_once(morning + timedelta(minutes=14)))["deleted"] == []
    awake.advance(120)
    assert (await vms.reap_once(morning + timedelta(minutes=16)))["deleted"] == [rec.id]


async def test_the_vm_reaper_never_deletes_a_machine_under_its_save(node):  # noqa: F811
    """A Hetzner snapshot can take longer than the grace: while a save of the environment runs, its machine
    stays."""
    out = await opened(node, keep="save")
    mgr, vms = node["mgr"], node["vms"]
    vms._awake = awake = Awake()
    rec = _vm(node, out["id"])
    late = rec.expires_at + timedelta(hours=1)
    await mgr.reap_once()  # a whole pass now (neither idle nor past its limit): only the grace and the save hold
    async with mgr._lock(f"save:{out['id']}"):
        assert (await vms.reap_once(late))["deleted"] == []
        awake.advance(ENV_SAVE_GRACE.total_seconds() * 3)
        assert (await vms.reap_once(late))["deleted"] == [] and _vm(node, out["id"]).state == "ready"
    assert (await vms.reap_once(late))["deleted"] == [rec.id]  # the save is done: past the grace it goes


async def test_a_wake_gives_the_environments_reaper_a_full_pass_first(node):  # noqa: F811
    """After a wake the VM reaper waits for a whole environments' pass that began after it, whatever the grace."""
    out = await opened(node, keep="save")
    mgr, vms = node["mgr"], node["vms"]
    vms._awake = awake = Awake()
    rec = _vm(node, out["id"])
    late = rec.expires_at + timedelta(hours=1)
    await mgr.reap_once()  # a whole pass now (neither idle nor past its limit)
    await vms.reap_once(late)
    awake.advance(ENV_SAVE_GRACE.total_seconds() + 60)
    vms.note_wake()
    assert (await vms.reap_once(late))["deleted"] == []  # no environments' pass since the wake
    node["cloud"].full.add(rec.provider_id)
    await mgr.reap_once(late)  # it tried, and could not save: the owner was told
    assert (await vms.reap_once(late))["deleted"] == [rec.id]


async def test_the_owner_is_never_told_kept_for_a_machine_that_is_gone(node):  # noqa: F811
    """0.7.0 said "Its machine is kept and still bills" a second after the VM reaper deleted it."""
    out = await opened(node, keep="save")
    mgr, vms = node["mgr"], node["vms"]
    rec = _vm(node, out["id"])
    rec.state, rec.delete_reason = "deleting", "time limit"
    vms.store.put(rec)
    await mgr._tell_failed(mgr.get(out["id"]), "no terminal or service lane env/x.")
    [(_title, body)] = [t for t in node["told"] if "could not be saved" in t[0]]
    assert "is kept" not in body and "still bills" not in body and "machine is being deleted" in body, body


def test_the_catch_up_note_says_kept_only_for_a_machine_that_is_kept():
    from openajar.local.wake import note

    record = {"at": "2026-11-26T08:00:00+00:00", "reason": "started", "closed": [], "saved": [],
              "kept": [{"id": "e1", "title": "Shop", "why": "no lane", "last_save": None, "machine": "deleted"},
                       {"id": "e2", "title": "Blog", "why": "disk full", "last_save": None, "machine": "kept"}]}
    words = note(record)
    assert "e1 (Shop) could not be saved, and its machine is deleted" in words, words
    assert "e2 (Blog) could not be saved, so its machine is kept" in words, words


# ------------------------------------------------------------------ S1-16, S6-01: one environment's error


async def test_one_environments_error_never_stops_the_pass_for_the_others(node, monkeypatch):  # noqa: F811
    a = await opened(node, keep="save")
    b = await opened(node, keep="save", title="Second page", session="chat-2")
    work_on_it(disk(node, b["id"]) / "work" / "shop")
    mgr = node["mgr"]
    real = mgr._busy_for_idle

    async def broken(env):
        if env.id == a["id"]:
            raise RuntimeError("a record a newer version wrote")
        return await real(env)

    monkeypatch.setattr(mgr, "_busy_for_idle", broken)
    idle = datetime.now(UTC) + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(idle)
    assert got["closed"] == [b["id"]] and mgr.get(b["id"]).last_save_id  # the healthy one was saved and closed
    assert [e["id"] for e in got["errors"]] == [a["id"]] and mgr.get(a["id"]).open
    told = [t for t in node["told"] if a["id"] in t[1] and "could not check" in t[0]]
    assert len(told) == 1, node["told"]
    await mgr.reap_once(idle + timedelta(minutes=1))
    assert len([t for t in node["told"] if "could not check" in t[0]]) == 1  # told once, not every minute


async def test_a_full_disk_on_the_node_is_a_failed_save_the_owner_hears_of(node, monkeypatch):  # noqa: F811
    """saves.tmp() raised OSError (ENOSPC) outside the save's try: nothing saved and nobody told."""
    out = await opened(node, keep="save")
    mgr = node["mgr"]
    store = mgr.saves()

    def full():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "tmp", full)
    got = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(minutes=1))
    assert got["save_failed"] == [out["id"]] and mgr.get(out["id"]).open
    assert "No space left" in (mgr.get(out["id"]).save_error or "")
    assert any("could not be saved" in t for t, _ in node["told"])


# ------------------------------------------------------------------ S2-01: a service that is missing


async def test_the_environments_reaper_holds_while_the_vms_service_is_missing(node):  # noqa: F811
    """vms.toml did not read at the start: no VM service. Its machine is not "deleted", it is not known."""
    out = await opened(node, keep="none")
    mgr, rt = node["mgr"], node["rt"]
    vms = rt.get("vms")
    rt.register("vms", None)
    try:
        got = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(hours=1))
    finally:
        rt.register("vms", vms)
    env = mgr.get(out["id"])
    assert got["gone"] == [] and got["closed"] == [] and env.open and env.closed_reason is None
    assert got["held"] == [out["id"]]
    assert vms.store.get(env.vm_id).state == "ready"


async def test_the_vm_reaper_holds_an_environments_machine_while_that_service_is_missing(node):  # noqa: F811
    """environments.toml did not read at the start: nothing could save the machine, so nothing deletes it."""
    out = await opened(node, keep="save")
    mgr, vms, rt = node["mgr"], node["vms"], node["rt"]
    vms._awake = awake = Awake()
    rec = _vm(node, out["id"])
    rt.register("environments", None)
    try:
        late = rec.expires_at + timedelta(hours=10)
        assert (await vms.reap_once(late))["deleted"] == []
        awake.advance(ENV_SAVE_GRACE.total_seconds() * 4)
        got = await vms.reap_once(late)
    finally:
        rt.register("environments", mgr)
    assert got["deleted"] == [] and vms.store.get(rec.id).state == "ready"
    assert got["overdue"] == [rec.id]  # the owner hears of a machine past its limit that nothing deletes


# ------------------------------------------------------------------ S1-05: the snapshot sweep


async def test_the_sweep_deletes_nothing_while_a_save_does_not_read(node):  # noqa: F811
    out = await opened(node, keep="save")
    mgr, cloud = node["mgr"], node["cloud"]
    work_on_it(disk(node, out["id"]) / "work" / "shop")
    closed = await node["call"]("environment_close")
    snap = closed["saved"]["snapshot"]["id"]
    _m, path = mgr.saves().find(closed["saved"]["save_id"])
    manifest = path / "manifest.json"
    good = manifest.read_text()
    manifest.write_text(good.replace('"kind": "save"', '"kind": "checkpoint"'))  # a newer version's kind
    now = datetime.now(UTC)
    for minutes in (0, 31, 62):
        got = await mgr.sweep_snapshots(now + timedelta(minutes=minutes), force=True)
        assert got.get("deleted", []) == [] and snap in cloud.snapshots, got
    manifest.write_text(good)
    assert mgr.saves().find(closed["saved"]["save_id"])[0].snapshot.id == snap


async def test_the_sweep_deletes_nothing_while_the_saves_folder_is_missing(node):  # noqa: F811
    await opened(node, keep="save")
    mgr, cloud = node["mgr"], node["cloud"]
    closed = await node["call"]("environment_close")
    snap = closed["saved"]["snapshot"]["id"]
    root = mgr.saves().root
    aside = root.with_name("saves-aside")
    root.rename(aside)
    try:
        now = datetime.now(UTC)
        for minutes in (0, 31, 62):
            got = await mgr.sweep_snapshots(now + timedelta(minutes=minutes), force=True)
            assert got.get("deleted", []) == [] and snap in cloud.snapshots, got
    finally:
        aside.rename(root)


async def test_the_sweeps_grace_counts_from_its_own_first_sighting(node):  # noqa: F811
    """A snapshot no save points to, made days ago: the provider's creation time gives it no grace any more."""
    await opened(node, keep="save")
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    kept = await mgr.save_env(mgr.store.open()[0], kind="save")  # a real save: its snapshot is pointed to
    cloud.snapshots["snap-old"] = {"agentos-home": vms.home_id, "agentos-node": vms.node, "agentos-save": "sdeadbeef01"}
    cloud.image_created["snap-old"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    now = datetime.now(UTC)
    first = await mgr.sweep_snapshots(now, force=True)
    assert first["deleted"] == [] and first["waiting"] == ["snap-old"]
    later = await mgr.sweep_snapshots(now + saving.SNAPSHOT_GRACE + timedelta(minutes=1), force=True)
    assert later["deleted"] == ["snap-old"] and kept.snapshot.id in cloud.snapshots


# ------------------------------------------------------------------ S1-02: staged work in a files-only save


async def _names(mgr, save_id: str) -> list[str]:
    m, path = mgr.saves().find(save_id)
    key = await mgr._saves_key(create=False) if m.encrypted else None
    with tempfile.TemporaryDirectory() as t:
        files = open_payload(m, path, Path(t), key)
        with tarfile.open(files["files.tgz"]) as tf:
            return tf.getnames()


def _packs_less(save: str) -> str:
    """SAVE with its file list cut short (only what 0.7.0's ls-files -m -o took, and not what git status lists):
    the check after the file part must catch it."""
    cut = save.replace("--diff-filter=d HEAD", "--diff-filter=d HEAD -- no-such")
    cut = cut.replace("""then printf '%s\\0' "$p" >> "$LIST"; fi  # status-list""", "then :; fi")
    assert cut.count("no-such") == 1 and "status-list" not in cut
    return cut


async def _restore(mgr, save_id: str) -> dict:
    """The save's restore.json (its deleted paths, services and containers)."""
    m, path = mgr.saves().find(save_id)
    key = await mgr._saves_key(create=False) if m.encrypted else None
    with tempfile.TemporaryDirectory() as t:
        files = open_payload(m, path, Path(t), key)
        return json.loads(Path(files["restore.json"]).read_text())


def _stage(work: Path) -> None:
    """Work that is only staged: a new file and an edit added with git add, a file removed with git rm."""
    (work / "staged_new.js").write_text("// staged, never committed\n")
    (work / "app.js").write_text("console.log('staged edit')\n")
    git(work, "add", "staged_new.js", "app.js")
    git(work, "rm", "-q", "old.txt")
    (work / "loose.js").write_text("// untracked\n")


async def test_a_files_only_save_holds_staged_work_and_the_resume_brings_it_back(node):  # noqa: F811
    """snapshots = false (the same file part as every autosave and rescue): the staged new file, the staged edit
    and the git rm come back on a fresh machine."""
    mgr = node["mgr"]
    mgr.settings = mgr.settings.model_copy(update={"snapshots": False})
    out = await opened(node, keep="save")
    work = disk(node, out["id"]) / "work" / "shop"
    _stage(work)
    closed = await node["call"]("environment_close")
    names = await _names(mgr, closed["saved"]["save_id"])
    assert {"work/shop/staged_new.js", "work/shop/app.js", "work/shop/loose.js"} <= set(names), names
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert again["resumed"]["from"] == "files" and again["resumed"]["missing"] == [], again["resumed"]
    w = disk(node, again["id"]) / "work" / "shop"
    assert (w / "staged_new.js").read_text() == "// staged, never committed\n"
    assert (w / "app.js").read_text() == "console.log('staged edit')\n"
    assert not (w / "old.txt").exists() and (w / "loose.js").exists()


async def test_a_staged_file_in_a_repository_with_no_commit_is_saved(node):  # noqa: F811
    mgr = node["mgr"]
    mgr.settings = mgr.settings.model_copy(update={"snapshots": False})
    out = await opened(node, keep="save")
    fresh = disk(node, out["id"]) / "work" / "fresh"
    fresh.mkdir(parents=True)
    git(fresh, "init", "-q", "-b", "main")
    (fresh / "first.py").write_text("print('first')\n")
    git(fresh, "add", "first.py")
    m = await mgr.save_env(mgr.get(out["id"]), kind="autosave", snapshot=False)
    assert "work/fresh/first.py" in await _names(mgr, m.id)


async def test_a_rescue_is_never_called_whole_when_git_lists_a_path_it_does_not_hold(node, monkeypatch):  # noqa: F811
    """The check after the file part: a path git status lists that the payload does not hold fails the save (the
    machine is kept and the owner told) instead of "saved it as ... (0 files)" and a delete."""
    out = await opened(node, keep="none")
    work = disk(node, out["id"]) / "work" / "shop"
    _stage(work)
    mgr = node["mgr"]
    real = saving.scripts.SAVE
    # a SAVE that packs less than git lists (as 0.7.0's ls-files -m -o did): the check must catch it
    monkeypatch.setattr(saving.scripts, "SAVE", _packs_less(real))
    got = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(minutes=1))
    env = mgr.get(out["id"])
    assert got["save_failed"] == [out["id"]] and env.open and _vm(node, out["id"]).state == "ready"
    assert "staged_new.js" in (env.save_error or "") and "does not hold" in env.save_error
    assert not any("was saved, then" in t for t, _ in node["told"])
    monkeypatch.setattr(saving.scripts, "SAVE", real)
    later = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(minutes=12))
    assert later["closed"] == [out["id"]]
    m, _ = mgr.saves().find(later["saved"][0]["save"])
    assert "work/shop/staged_new.js" in await _names(mgr, m.id)


async def test_a_file_untracked_and_ignored_is_rescued_and_never_deleted_on_resume(node):  # noqa: F811
    """R1 P1: `git rm --cached` + .gitignore left a path git status lists that the first file list of the fix did not take,
    so every rescue failed and the machine was deleted past the grace with all its work (0.7.0 rescued it). The
    file part now holds every file git status lists that is on the disk, and a path on the disk is never deleted."""
    out = await opened(node, keep="none")
    work = disk(node, out["id"]) / "work" / "shop"
    work_on_it(work)  # two commits no remote has, an edit, a new file, a deletion
    (work / "sale1.js").write_text("// part 1, edited after it stopped being tracked\n")
    git(work, "rm", "-q", "--cached", "sale1.js")
    ignore = work / ".gitignore"
    ignore.write_text((ignore.read_text() if ignore.exists() else "") + "\nsale1.js\n")
    mgr = node["mgr"]
    got = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(minutes=1))
    assert got.get("save_failed") is None and got["closed"] == [out["id"]], (got, mgr.get(out["id"]).save_error)
    sid = got["saved"][0]["save"]
    assert "work/shop/sale1.js" in await _names(mgr, sid)
    deleted = [p for paths in (await _restore(mgr, sid))["deleted"].values() for p in paths]
    assert "old.txt" in deleted and "sale1.js" not in deleted, deleted
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    w = disk(node, again["id"]) / "work" / "shop"
    assert (w / "sale1.js").read_text() == "// part 1, edited after it stopped being tracked\n"
    assert not (w / "old.txt").exists()


async def test_a_staged_change_the_work_tree_took_back_does_not_fail_the_rescue(node):  # noqa: F811
    """R1 P1's second case: `git status` says MM for a change staged and then undone in the work tree."""
    out = await opened(node, keep="none")
    work = disk(node, out["id"]) / "work" / "shop"
    work_on_it(work)
    target = work / "sale2.js"  # committed by work_on_it
    was = target.read_text()
    target.write_text(was + "staged, then taken back\n")
    git(work, "add", target.name)
    target.write_text(was)
    mgr = node["mgr"]
    got = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(minutes=1))
    assert got.get("save_failed") is None and got["closed"] == [out["id"]], (got, mgr.get(out["id"]).save_error)


async def test_a_rescue_that_misses_files_holds_its_machine_past_the_grace(node, monkeypatch):  # noqa: F811
    """R1 P1's second line: a rescue the check fails keeps its machine for as long as it takes (the owner is told
    so), never only for the VM reaper's grace; a later save that holds everything lets it go."""
    out = await opened(node, keep="none")
    work = disk(node, out["id"]) / "work" / "shop"
    _stage(work)
    mgr, vms = node["mgr"], node["vms"]
    vms._awake = awake = Awake()
    real = saving.scripts.SAVE
    monkeypatch.setattr(saving.scripts, "SAVE", _packs_less(real))
    late = mgr.get(out["id"]).expires_at + timedelta(minutes=1)
    assert (await mgr.reap_once(late))["save_failed"] == [out["id"]]
    assert "staged_new.js" in " ".join(mgr.get(out["id"]).save_unheld or [])
    held = mgr.holds_machine(out["id"])
    assert held and "did not hold" in held, held
    told = [b for t, b in node["told"] if "could not be saved" in t]
    assert told and "until a save holds those files or you close it" in told[-1], told
    assert told[-1].count("Its machine is kept") == 1, told[-1]  # R1 #12: it said "kept Its machine is kept"
    for minutes in (5, 20, 60):
        awake.advance(ENV_SAVE_GRACE.total_seconds() + 60)
        assert (await vms.reap_once(late + timedelta(minutes=minutes)))["deleted"] == []
        assert _vm(node, out["id"]).state == "ready"
    monkeypatch.setattr(saving.scripts, "SAVE", real)
    later = await mgr.reap_once(late + timedelta(minutes=70))
    assert later["closed"] == [out["id"]] and mgr.holds_machine(out["id"]) is None


def test_the_words_of_a_save_that_misses_files_leave_the_machine_to_the_notification():
    """R1 #12: unheld_words ended "Its machine is kept", and the notification went on "Its machine is kept and still
    bills"."""
    from openajar.environments import savecheck

    words = savecheck.unheld_words(["/work/shop/a.js", "/work/shop/b.js"])
    assert words.endswith("so it is not called whole.") and "machine" not in words, words
