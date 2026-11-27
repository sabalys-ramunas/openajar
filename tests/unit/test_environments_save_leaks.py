"""Saves never leak a snapshot or collide (the 2026-11-15 test: Hetzner made two snapshots while its API
was out of reach or the server was locked, openajar recorded failures, and both billed until a person
deleted them). On the fake disk cloud of test_environments_saves."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_environments_saves import disk, node, opened, work_on_it  # noqa: F401 - node is the fixture

from openajar.core import catalog
from openajar.environments import saving
from openajar.policy.wrap import ToolCallError
from openajar.vms.hetzner import HetznerProvider
from openajar.vms.models import VmError


async def test_a_snapshot_made_though_its_call_failed_is_adopted(node):  # noqa: F811
    """Hetzner's API dropped the connection after it started the image: the save finds it by its label."""
    out = await opened(node, keep="save")
    work_on_it(disk(node, out["id"]) / "work" / "shop")
    node["cloud"].lost_snapshot = 1
    closed = await node["call"]("environment_close")
    assert closed["state"] == "closed" and closed["saved"]["snapshot"] is not None
    snap = closed["saved"]["snapshot"]["id"]
    assert list(node["cloud"].snapshots) == [snap]  # one image, and the save points to it
    assert (await node["mgr"].sweep_snapshots(force=True))["deleted"] == []


async def test_a_failed_save_deletes_the_snapshot_the_provider_made_for_it(node, monkeypatch):  # noqa: F811
    out = await opened(node, keep="save")
    mgr = node["mgr"]

    async def broken_commit(*a, **kw):
        raise saving.SaveFailed("the vault cannot give the saves key")

    monkeypatch.setattr(mgr, "_commit_save", broken_commit)
    node["cloud"].lost_snapshot = 1
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_close")
    assert e.value.code == "save_failed"
    assert node["cloud"].snapshots == {}  # nothing left billing
    assert mgr.get(out["id"]).open  # the machine is kept


async def test_a_locked_server_gets_its_own_fix_line(node):  # noqa: F811
    await opened(node, keep="save")
    node["cloud"].locked_snapshot = 1
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_close")
    assert e.value.code == "save_failed" and e.value.message.startswith("provider_busy: ")
    fix = catalog.mcp_lines("save_failed", e.value.message)[0]
    assert "Wait 2 minutes" in fix and "free disk space" not in fix


def test_each_cause_has_its_own_fix_line():
    lines = {r: catalog.mcp_lines("save_failed", f"{r}: x")[0] for r in
             ("provider_unreachable", "provider_busy", "machine_unreachable", "snapshot_failed")}
    assert len(set(lines.values())) == 4
    assert "tries the save again by itself" in lines["provider_unreachable"]
    assert "free disk space" in catalog.mcp_lines("save_failed", "saving failed on its machine: disk")[0]
    assert saving.snapshot_reason(VmError("x", provider_code="unreachable")) == "provider_unreachable"
    assert saving.snapshot_reason(VmError("x", provider_code="rate_limit_exceeded")) == "provider_busy"


async def test_saves_of_one_environment_never_run_at_once(node):  # noqa: F811
    out = await opened(node, keep="save")
    mgr, cloud = node["mgr"], node["cloud"]
    inside, most = 0, 0
    made = cloud.create_snapshot

    async def counted(*a, **kw):
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        await asyncio.sleep(0.05)
        try:
            return await made(*a, **kw)
        finally:
            inside -= 1

    cloud.create_snapshot = counted
    env = mgr.get(out["id"])
    await asyncio.gather(mgr.save_env(env, kind="save"), mgr.save_env(env, kind="save"),
                         node["call"]("environment_save"))
    assert most == 1


async def test_the_reaper_leaves_an_environment_alone_while_an_agent_closes_it(node):  # noqa: F811
    out = await opened(node, keep="save")
    mgr = node["mgr"]
    async with mgr._lock(f"close:{out['id']}"):
        mgr.store.update(out["id"], lambda e: setattr(e, "state", "closing"))  # as _teardown marks it
        result = await mgr.reap_once(datetime.now(UTC) + timedelta(hours=1))
    assert result["closed"] == [] and not result.get("save_failed")
    assert node["cloud"].snapshots == {}  # no second save started under the close


async def test_the_sweep_deletes_old_orphan_snapshots_and_tells_the_owner(node):  # noqa: F811
    await opened(node, keep="save")
    mgr, cloud, vms = node["mgr"], node["cloud"], node["vms"]
    home = {"agentos-home": vms.home_id, "agentos-node": vms.node}
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    cloud.snapshots["snap-old"] = {**home, "agentos-save": "sdeadbeef01"}
    cloud.image_created["snap-old"] = old
    cloud.snapshots["snap-new"] = {**home, "agentos-save": "sdeadbeef02"}  # made just now: it waits
    cloud.snapshots["snap-other-home"] = {"agentos-home": "someone-else", "agentos-save": "sdeadbeef03"}
    cloud.image_created["snap-other-home"] = old
    cloud.snapshots["snap-image"] = {**home, "agentos-image": "dev"}  # an image build, never a save
    cloud.image_created["snap-image"] = old
    kept = await mgr.save_env(mgr.store.open()[0], kind="save")  # a real save's snapshot stays
    cloud.image_created[kept.snapshot.id] = old
    swept = await mgr.sweep_snapshots(force=True)  # the grace runs from this process's own first sight (S1-05)
    assert swept["deleted"] == [] and sorted(swept["waiting"]) == ["snap-new", "snap-old"]
    later = datetime.now(UTC) + saving.SNAPSHOT_GRACE + timedelta(minutes=1)
    assert sorted((await mgr.sweep_snapshots(later, force=True))["deleted"]) == ["snap-new", "snap-old"]
    assert {"snap-other-home", "snap-image", kept.snapshot.id} <= set(cloud.snapshots)
    assert any("Leftover environment snapshots" in t for t, _ in node["told"])
    assert (await mgr.sweep_snapshots(later + timedelta(minutes=1)))  == {}  # once per SNAPSHOT_SWEEP


# ---- the Hetzner client: backoff on locked, rate limits and an API out of reach ----

def hetzner(answers: list, seen: list) -> HetznerProvider:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    p = HetznerProvider("tok", transport=httpx.MockTransport(handle))
    p.retry_delays = (0.0, 0.0, 0.0)
    return p


def err(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code.replace("_", " ")}})


async def test_hetzner_retries_a_read_through_connect_errors():
    seen: list = []
    p = hetzner([httpx.ConnectError("down"), httpx.ConnectTimeout("slow"),
                 httpx.Response(200, json={"image": {"id": 5, "status": "available"}})], seen)
    assert (await p.get_image("5"))["status"] == "available"
    assert seen == ["GET"] * 3


async def test_hetzner_retries_a_locked_snapshot_and_a_rate_limit():
    seen: list = []
    action = {"id": 9, "status": "success"}
    p = hetzner([err(423, "locked"), err(429, "rate_limit_exceeded"),
                 httpx.Response(201, json={"image": {"id": 77}, "action": action})], seen)
    assert await p.create_snapshot("1", "d", {"agentos-save": "s1"}) == "77"
    assert seen == ["POST"] * 3


async def test_hetzner_never_repeats_a_create_that_may_have_reached_it():
    seen: list = []
    p = hetzner([httpx.ReadTimeout("no answer"), httpx.Response(201, json={"image": {"id": 1}})], seen)
    with pytest.raises(VmError) as e:
        await p.create_snapshot("1", "d", {})
    assert e.value.provider_code == "unreachable" and seen == ["POST"]


async def test_hetzner_gives_up_after_its_tries():
    seen: list = []
    p = hetzner([httpx.ConnectError("down")] * 4, seen)
    with pytest.raises(VmError) as e:
        await p.get_image("5")
    assert "unreachable" in e.value.message and len(seen) == 4


async def test_hetzner_lists_a_saves_snapshots_by_label():
    seen: list = []
    img = {"id": 3, "status": "creating", "created": "2026-11-15T22:20:39+00:00", "image_size": None,
           "disk_size": 40, "labels": {"agentos-save": "s1"}}
    p = hetzner([httpx.Response(200, json={"images": [img], "meta": {"pagination": {"next_page": None}}})], seen)
    got = await p.list_images({"agentos-save": "s1"})
    assert got == [{"id": "3", "status": "creating", "created": img["created"], "gb": 40.0,
                    "labels": {"agentos-save": "s1"}}]


# ---- a close that takes long goes on in the background ----

async def test_a_slow_close_says_still_working_and_finishes_in_the_background(node, monkeypatch):  # noqa: F811
    from openajar.core import progress

    await opened(node, keep="save")
    cloud = node["cloud"]
    release = asyncio.Event()
    made = cloud.create_snapshot

    async def slow(*a, **kw):
        await release.wait()
        return await made(*a, **kw)

    cloud.create_snapshot = slow
    monkeypatch.setattr(progress, "LONG_CALL_S", 0.2)
    first = await node["call"]("environment_close")
    assert first["state"] == "closing" and "background" in first["next"] and "discard=true" in first["next"]
    seen = await node["call"]("environment")
    assert seen["background"].startswith("closing")
    release.set()
    monkeypatch.setattr(progress, "LONG_CALL_S", 30.0)
    again = await node["call"]("environment_close")  # joins the running close, never a second save
    assert again["state"] == "closed" and again["saved"]["snapshot"] is not None
    assert len(cloud.snapshots) == 1


# ---- a dev server does not keep a keep "save" machine from its idle save ----

async def test_a_running_dev_server_does_not_block_the_idle_save(node):  # noqa: F811
    from openajar.core.models import utcnow

    out = await opened(node, keep="save")
    mgr = node["mgr"]
    await node["envs"].job_start(f"env/{out['id']}", "pnpm dev --port 5321 --host 0.0.0.0", cwd="/work/shop")
    got = await mgr.reap_once(utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1))
    assert got["closed"] == [out["id"]]
    m = mgr.saves().find(mgr.get(out["id"]).last_save_id)[0]
    assert [s.cmd for s in m.services] == ["pnpm dev --port 5321 --host 0.0.0.0"]  # restored on resume


async def test_a_build_still_counts_as_busy(node):  # noqa: F811
    from openajar.core.models import utcnow

    out = await opened(node, keep="save")
    mgr = node["mgr"]
    await node["envs"].job_start(f"env/{out['id']}", "pnpm build", cwd="/work/shop")
    got = await mgr.reap_once(utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1))
    assert got["closed"] == [] and mgr.get(out["id"]).open


def test_which_commands_serve():
    from openajar.environments.activity import serves

    for cmd in ("pnpm dev --port 5321", "npm run dev", "vite", "python -m http.server 8000", "npm start",
                "uvicorn app:app", "PORT=3000 node index.js", "next dev"):
        assert serves(cmd), cmd
    for cmd in ("vite build", "npm run build", "pytest -q", "make test", "pnpm test", "cargo build", "sleep 100"):
        assert not serves(cmd), cmd
