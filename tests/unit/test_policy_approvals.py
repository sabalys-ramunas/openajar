"""The approval queue: long-poll waits, questions, visibility, signed phone links, sweeping."""

from __future__ import annotations

import asyncio
import threading

import pytest
from test_policy_support import Clock, make_service

from openajar.core.errors import ApprovalPending, InvalidRequest, NotFound
from openajar.core.models import Effect
from openajar.policy import bind_request_token, wrap_tool
from openajar.policy.errors import Conflict, Denied
from openajar.policy.service import PolicyService
from openajar.policy.tools import tool_specs


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, rt = make_service(tmp_path, clock=clock)
    yield svc, rt, clock
    svc.close()


async def make_pending(svc, content="x"):
    _g, tok = svc.issue(subject={"id": "agent"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    ctx = svc.context(svc.resolve(tok)[1], tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/insta", content=content, summary="Post it")
    return e.value.approval_id, tok


def tools(rt):
    return {s.name: wrap_tool(s, rt) for s in tool_specs(rt)}


async def test_wait_returns_when_decided_in_process(env):
    svc, rt, _ = env
    ap, tok = await make_pending(svc)

    async def decide_soon():
        await asyncio.sleep(0.05)
        svc.approvals.decide(ap, "approve", by="owner", note="ship it")

    asyncio.get_running_loop().create_task(decide_soon())
    with bind_request_token(tok):
        st = await tools(rt)["approval_wait"](approval_id=ap, wait_s=5)
    assert st["state"] == "approved" and st["note"] == "ship it" and "lane_post" in st["next"]


async def test_wait_sees_decisions_from_another_process(env, tmp_path):
    svc, rt, _ = env
    ap, tok = await make_pending(svc)
    other = PolicyService(tmp_path, clock=svc.clock)
    t = threading.Timer(0.1, lambda: other.approvals.decide(ap, "deny", by="owner:cli"))
    t.start()
    with bind_request_token(tok):
        st = await tools(rt)["approval_wait"](approval_id=ap, wait_s=5)
    t.join()
    assert st["state"] == "denied"


async def test_wait_times_out_and_caps_at_120s(env):
    svc, rt, _ = env
    ap, tok = await make_pending(svc)
    loop = asyncio.get_running_loop()
    start = loop.time()
    st = await svc.approvals.wait(ap, 0.1)
    assert st.state == "pending" and loop.time() - start < 1
    with bind_request_token(tok), pytest.raises(Exception, match="invalid"):
        await tools(rt)["approval_wait"](approval_id=ap, wait_s=-1)
    assert not svc.approvals._waiters


async def test_status_is_hidden_from_other_grants(env):
    svc, rt, _ = env
    ap, tok = await make_pending(svc)
    _, stranger = svc.issue(subject={"id": "other"}, workspaces=["biz-a"])
    with bind_request_token(stranger), pytest.raises(Exception, match="not_found"):
        await tools(rt)["approval_wait"](approval_id=ap, wait_s=0)
    # a parent sees what its children asked
    _, child = svc.attenuate(tok, ceiling=Effect.COMMIT)
    ctx = svc.context(svc.resolve(child)[1], tool="t")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="c")
    with bind_request_token(tok):
        assert (await tools(rt)["approval_wait"](approval_id=e.value.approval_id, wait_s=0))["state"] == "pending"


async def test_status_without_an_id_lists_only_your_own_pending(env):
    svc, rt, _ = env
    ap_a, tok_a = await make_pending(svc)
    _, tok_b = svc.issue(subject={"id": "other"}, workspaces=["biz-a"])
    with bind_request_token(tok_a):
        q_a = (await tools(rt)["human_ask"](question="Post at 9 or 12?", options=["9", "12"]))["approval_id"]
    with bind_request_token(tok_b):
        await tools(rt)["human_ask"](question="Post at 9?")
    with bind_request_token(tok_a):
        out = await tools(rt)["approval_wait"]()
    assert {p["approval_id"] for p in out["pending"]} == {ap_a, q_a}
    assert "next" in out


async def test_human_ask_question_flow(env):
    svc, rt, _ = env
    _, tok = svc.issue(subject={"id": "agent"}, workspaces=["biz-a"])
    t = tools(rt)
    with bind_request_token(tok):
        q = await t["human_ask"](question="Post at 9 or 12?", options=["9", "12"])
        with pytest.raises(Exception, match="invalid"):
            await t["human_ask"](question="  ")
        with pytest.raises(Exception, match="invalid"):
            await t["human_ask"](question="x", options=["a", "a"])
    with pytest.raises(InvalidRequest):
        svc.approvals.decide(q["approval_id"], "approve", by="owner", answer="10")
    with pytest.raises(InvalidRequest):
        svc.approvals.decide(q["approval_id"], "approve", by="owner")
    svc.approvals.decide(q["approval_id"], "approve", by="owner", answer="12")
    with bind_request_token(tok):
        st = await t["approval_wait"](approval_id=q["approval_id"], wait_s=1)
    assert st["kind"] == "question" and st["answer"] == "12" and st["options"] == ["9", "12"]


def test_unknown_approval(env):
    svc, _, _ = env
    with pytest.raises(NotFound):
        svc.approvals.get("ap_nope")
    with pytest.raises(NotFound):
        svc.approvals.decide("ap_nope", "approve", by="o")
    with pytest.raises(InvalidRequest):
        svc.approvals.list(state="bogus")


async def test_batch(env):
    svc, _, _ = env
    a, _ = await make_pending(svc, "a")
    b, _ = await make_pending(svc, "b")
    svc.approvals.decide(b, "deny", by="o")
    res = svc.approvals.batch([a, b, "ap_missing"], "approve", by="o")
    assert res[a] == "approved" and res[b].startswith("conflict") and res["ap_missing"].startswith("not_found")


async def test_signed_action_links(env):
    svc, _, clock = env
    ap, _ = await make_pending(svc)
    exp = int(clock.t + 600)
    sig = svc.approvals.sign_action(ap, "deny", exp)
    with pytest.raises(Denied):  # no link approves, even one signed for it (SEC-8)
        svc.approvals.decide_by_link(ap, "approve", exp, svc.approvals.sign_action(ap, "approve", exp))
    with pytest.raises(InvalidRequest):
        svc.approvals.decide_by_link(ap, "deny", exp, svc.approvals.sign_action(ap, "review", exp))
    with pytest.raises(InvalidRequest):
        svc.approvals.decide_by_link(ap, "deny", exp + 1, sig)
    with pytest.raises(InvalidRequest):
        # a changed first character: always another signature (a changed end could be the real one, 1 run in 4096)
        svc.approvals.decide_by_link(ap, "deny", exp, ("B" if sig[0] == "A" else "A") + sig[1:])
    assert svc.approvals.decide_by_link(ap, "deny", exp, sig).decided_by == "owner:push"
    with pytest.raises(Conflict):
        svc.approvals.decide_by_link(ap, "deny", exp, sig)  # single use
    ap2, _ = await make_pending(svc, "other")
    old = int(clock.t - 1)
    with pytest.raises(Conflict):
        svc.approvals.decide_by_link(ap2, "deny", old, svc.approvals.sign_action(ap2, "deny", old))


async def test_sweeper_expires_pending_and_unused_approved(env):
    svc, _, clock = env
    a, _ = await make_pending(svc, "a")
    b, _ = await make_pending(svc, "b")
    svc.approvals.decide(b, "approve", by="o")
    events = []
    svc.bus.subscribe(lambda kind, p: events.append((kind, p.get("id"), p.get("state"))))
    clock.advance(svc.cfg.approval_ttl_s + 5)
    assert svc.approvals.sweep() == 2
    assert {svc.approvals.get(a).state, svc.approvals.get(b).state} == {"expired"}
    assert ("approval", a, "expired") in events


async def test_approval_api_shape(env):
    svc, _, _ = env
    ap, _ = await make_pending(svc, {"caption": "hello", "password": "hunter2"})
    pub = svc.approvals.get(ap).public()
    assert pub["state"] == "pending" and pub["effect"] == "commit" and pub["lane"] == "biz-a/insta"
    assert pub["content_hash"].startswith("sha256:") and pub["requested_by"]["id"] == "agent"
    assert "hello" in pub["preview"]["text"] and "hunter2" not in pub["preview"]["text"]
    assert pub["created_at"].endswith("Z")
