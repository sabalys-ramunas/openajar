"""Agent environments (openajar.environments): open, the routing of calls without `on`, no asking inside,
close with the unsaved-work check, the reaper, and the parsers. The VM is the fake cloud of openajar.vms
and its commands are answered by a fake (no SSH here); tests/unit/test_environments_docker.py runs the
same story on a real machine."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from datetime import timedelta

import pytest
from test_policy_support import Clock, make_service

from openajar.core.errors import PolicyDenied
from openajar.core.models import Effect, utcnow
from openajar.dev.lanes import FakeLaneManager
from openajar.env.execs import in_bash
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.models import ExecResult
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments import bringup, machine, routing
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import Environment, EnvSettings
from openajar.environments.probes import GIT_STATE, parse_git_state, parse_ports
from openajar.environments.tools import tool_specs as env_tool_specs
from openajar.policy.wrap import ToolCallError, bind_request_token, wrap_tool
from openajar.vms.manager import VmManager
from openajar.vms.models import VmSettings
from openajar.vms.provider import FakeProvider

TITLE = "Black Friday page"


async def ok_probe(remote, *, timeout_s=15):
    return True, ""


async def readable_repo(url, env, *, timeout_s=20, allow_file=False):
    """Every repository reads (environment_open's check before it rents; no network here)."""
    return True, b""


class FakeTunnel:
    def __init__(self, remote, *, gateway_port, socks_port=None):
        self.remote, self.gateway_port, self.socks_port = remote, gateway_port, socks_port or 41080
        self.alive = False

    async def start(self):
        self.alive = True

    async def stop(self):
        self.alive = False


class Machine:
    """What the fake VM answers: setup state, the repo's git state, listening ports."""

    def __init__(self):
        self.setup = "ready"
        self.git = "repo=/work/shop\nbranch=main\nahead=0\ndirty_count=0\n"
        self.ports = 'LISTEN 0 511 127.0.0.1:3000 0.0.0.0:* users:(("node",pid=7,fd=20))\n' \
                     'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
        self.ran: list[tuple[str, str | None]] = []
        self.ran_in: list[tuple[str, str | None, str | None]] = []  # with what went to the command's stdin

    async def exec(self, lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None, oom_check=False):
        self.ran.append((cmd, grant_id))
        self.ran_in.append((cmd, grant_id, stdin))
        out = ""
        if cmd == machine.SETUP_STATE:
            out = self.setup + "\n"
        elif cmd == "sh -s" and stdin == machine.setup_script():
            self.setup = "ready"  # the setup script writes its marker
            out = ""
        elif cmd == GIT_STATE:
            out = self.git
        elif cmd.startswith("ss "):
            out = self.ports
        elif cmd == machine.SWAP_ON:
            out = "swap: 8192 MB zram\n"
        elif "git clone" in cmd:
            out = "main\n"
        else:
            out = "ran\n"
        return ExecResult(exit_code=0, stdout=out, stderr="", duration_s=0.01)


@pytest.fixture
async def node(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    policy, rt = make_service(home, clock=Clock())
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0))
    fake = Machine()
    monkeypatch.setattr(envs, "exec", fake.exec)
    rt.register("envs", envs)
    prov = FakeProvider()
    vms = VmManager(home, runtime=rt, provider=prov, settings=VmSettings(ssh_from=["198.51.100.7/32"]),
                    node_name="node-1", probe=ok_probe)
    rt.register("vms", vms)
    lanes = FakeLaneManager()
    rt.register("lanes", lanes)
    monkeypatch.setattr(bringup, "Tunnel", FakeTunnel)
    # Single use, as an owner who chose it: most tests here exercise close, the reaper and unsaved work on
    # a machine that is deleted. The default (saves when idle) is test_environments_saves's.
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings(default_keep="none"))
    mgr.repo_probe = readable_repo
    mgr.slot_poll_s, mgr.slot_wait_s, mgr.ready_wait_s = 0.01, 0.05, 0.05  # a full node, a starting machine: short waits
    rt.register("environments", mgr)
    grant, token = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                specials={"vm": {}, "exec": {}})
    from openajar.env import tool_specs as term_specs

    specs = {s.name: s for s in [*env_tool_specs(rt), *term_specs(rt)]}
    calls = {name: wrap_tool(spec, rt) for name, spec in specs.items()}

    async def call(name, *, session="chat-1", tok=token, **kw):
        with bind_request_token(tok, session=session):
            return await calls[name](**kw)

    yield {"rt": rt, "policy": policy, "mgr": mgr, "vms": vms, "prov": prov, "lanes": lanes, "machine": fake,
           "envs": envs, "grant": grant, "token": token, "call": call}
    await mgr.shutdown()
    await vms.shutdown()
    await envs.shutdown()
    policy.close()


async def opened(node, **kw):
    """environment_open, which never asks a person."""
    out = await node["call"]("environment_open", title=kw.pop("title", TITLE), **kw)
    assert out.get("status") != "approval_pending", out
    return out


# ------------------------------------------------------------------ open


def _run_clone_locally(cmd: str, tmp_path):
    """The environment's clone command, run on this machine against a local repository with a
    `feature` branch, with /work moved under tmp_path."""
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    for args in (["commit", "-q", "--allow-empty", "-m", "one"], ["branch", "feature"]):
        subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                       check=True)
    # /work first: the repository's own path (a test folder under any ".../worktrees/...") must not be moved
    local = (cmd.replace("/work", str(tmp_path / "work"))
                .replace(shlex.quote("https://example.test/me/shop.git"), shlex.quote(str(src))))
    return subprocess.run(["sh", "-c", local], check=False, capture_output=True, text=True, cwd=tmp_path,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)})


async def test_environment_names_the_branch_checked_out_now(node):
    """After the agent switched branches, environment() still said the branch it was cloned on."""
    await opened(node, repo="example.test/me/shop")
    node["machine"].git = "repo=/work/shop\nbranch=fix/rail\nahead=1\ndirty_count=0\n"
    out = await node["call"]("environment")
    assert out["branch"] == out["git"]["branch"] == "fix/rail"


async def test_a_ref_is_checked_out_inside_the_clone(node, tmp_path):
    """`ref` was checked out in the shell's own folder, not the clone (a real Hetzner open with
    ref="main" failed with "not a git repository", 2026-11-13). The clone command runs for real
    against a local repository, so the checkout must land in it."""
    await opened(node, repo="example.test/me/shop", ref="feature")
    [cmd] = [c for c, _ in node["machine"].ran if "git clone" in c]
    out = await asyncio.to_thread(_run_clone_locally, cmd, tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "feature"


async def test_a_snapshot_that_never_ran_the_setup_gets_it_from_the_node(node):
    """A `dev` snapshot from `openajar vm image build` (its own script) boots with cloud-init done and
    no setup marker: the node runs setup.sh over SSH instead of waiting out setup_timeout_s (a real
    Hetzner environment sat in starting for this, 2026-11-13)."""
    node["machine"].setup = "status: done"
    out = await opened(node, repo="example.test/me/shop")
    assert out["state"] == "ready", out
    assert [c for c, _ in node["machine"].ran].count("sh -s") == 1


async def test_open_never_asks_then_rents_clones_and_is_ready(node):
    """No standing approval, and the agent read a web page first: opening one still asks nobody (the
    owner's decision of 2026-11-13). The node's limits bound it instead."""
    call = node["call"]
    ctx = node["policy"].context(node["grant"], tool="browser_snapshot")
    ctx.taint("untrusted", source="browser_snapshot", kind="web_page", host="evil.example")
    assert node["policy"].approvals.list_standing() == []
    out = await call("environment_open", title=TITLE, repo="example.test/me/shop")
    assert out.get("status") != "approval_pending" and out["state"] == "ready", out
    assert node["policy"].approvals.list(state="pending") == []
    # what it rented is on the record, as a vm call
    [end] = [e for e in node["policy"].traces.query(kind="tool_end", limit=50)[0] if e.tool == "environment_open"]
    assert end.effect == "vm" and end.status == "ok"
    assert out["workdir"] == "/work/shop" and out["branch"] == "main" and out["repo"] == "example.test/me/shop"
    assert "lanes" not in out  # `on` reaches it: a lane's name in the result was taken for one (round 4)
    assert out["machine"]["provider"] == "fake"
    # the node's own live view, never a page of the web app (the public repository has none)
    assert set(out["links"]) == {"browser"} and "/environments/" not in str(out["links"]["browser"])
    # the agent finds its terminal without guessing: the tools it needs are named, and where the rest are
    # a Python repository's agent is not told to run npm (the id is random and may spell it)
    assert "npm" not in out["next"].replace(out["id"], "")
    for words in ("calls without `on` go here", 'terminal_exec(cmd="git status") runs a command, in /work/shop too',
                  "file_read", "file_edit", "proof=", "environment_pull_request(title)",
                  "terminal_job_start(", "terminal_job_read", "in your list", 'tools_more("terminals")',
                  'tools_more("studio")', "links.browser"):
        assert words in out["next"], words
    # the VM is the environment's, its browser lane was made by the node, with the tunnel as its proxy
    rec = node["vms"].store.get(node["mgr"].get(out["id"]).vm_id)
    assert rec.environment == out["id"] and rec.lane_id == f"env/{out['id']}"
    browser = node["lanes"].get(f"env/{out['id']}-browser")
    assert browser.spec.proxy_label == f"env-{out['id']}" and browser.spec.account is None
    proxy = node["lanes"].proxies_loader()[f"env-{out['id']}"]
    assert proxy.server == "socks5://127.0.0.1:41080"
    # a public repo that is not declared is cloned straight from the VM, with no gateway token
    [(clone, grant)] = [r for r in node["machine"].ran if "git clone" in r[0]]
    assert "https://example.test/me/shop.git" in clone and "/work/shop" in clone and grant is None


async def test_the_browser_starts_beside_the_clone_and_the_open_never_waits_for_it(node, monkeypatch):
    """The first browser_navigate waited 10 to 14 s for the machine's browser (evals, 2026-11-18): it
    starts during the bring-up now, in the background. A start that fails is only logged; the first
    browser call starts it again, as before."""
    from openajar.core.models import LaneState

    lanes, mgr = node["lanes"], node["mgr"]
    gate = asyncio.Event()
    real_open = lanes.open

    async def slow_open(lane_id, **kw):
        await gate.wait()
        return await real_open(lane_id, **kw)

    monkeypatch.setattr(lanes, "open", slow_open)
    out = await opened(node)
    assert out["state"] == "ready"  # ready while the browser still starts
    lane = f"env/{out['id']}-browser"
    assert lanes.get(lane).state != LaneState.WARM and mgr._warming
    gate.set()
    await asyncio.gather(*mgr._warming)
    assert lanes.get(lane).state == LaneState.WARM

    # a close while it still starts does not wait for it: the start is cancelled
    gate.clear()
    third = await opened(node, title="Third one", session="chat-3")
    starting = mgr.__dict__["_browser_starts"][third["id"]]
    await asyncio.wait_for(node["call"]("environment_close", on=third["on"], session="chat-3"), 5)
    assert starting.cancelled() and mgr.get(third["id"]).state == "closed"

    async def broken(lane_id, **kw):
        raise RuntimeError("no display")

    monkeypatch.setattr(lanes, "open", broken)
    again = await opened(node, title="Second one", session="chat-2")
    await asyncio.gather(*mgr._warming)
    assert again["state"] == "ready" and mgr.get(again["id"]).open


async def test_the_live_view_link_stays_the_same_and_says_how_long_it_and_older_ones_work(node):
    """environment() signed a new live-view link on every call, and the agent could not tell whether the
    one the human had open still worked (2026-11-16). The same link comes back while it is valid; a new
    one only when it is close to its end, and the one before still opens until its own expiry."""
    from openajar.node.viewer_tokens import ViewMode, ViewTokens

    node["lanes"].take_control = lambda *a, **kw: None  # a lane manager the viewer can hand over
    out = await opened(node)
    first = out["links"]["browser"]
    assert first and first.startswith("http://localhost:")
    again = await node["call"]("environment")
    assert again["links"]["browser"] == first
    note = again["links"]["browser_note"]
    assert "stays the same on every call" in note and "keeps working until its own expiry" in note
    assert "30 minutes after its link was made" in note and "\u2014" not in note
    lane = f"env/{out['id']}-browser"
    tokens = node["rt"].get("viewer_links") or ViewTokens.for_config(node["rt"].require("config"))
    held = node["mgr"].view_links[lane]
    node["mgr"].view_links[lane] = tokens.issue(lane, ViewMode.CONTROL, 60)  # a minute left: too close to its end
    newer = await node["call"]("environment")
    assert newer["links"]["browser"] != first
    assert tokens.verify(held.token, lane).nonce == held.nonce  # the link given before still opens


async def test_cloud_init_sets_up_a_plain_ubuntu_machine(node):
    out = await opened(node)
    rec = node["vms"].store.get(node["mgr"].get(out["id"]).vm_id)
    data = node["prov"].user_data[rec.provider_id]
    assert "runcmd:" in data and machine.SETUP_PATH in data and "ssh_pwauth: false" in data
    assert "Node 22" in machine.setup_script() and "uv" in machine.setup_script()


async def test_a_new_title_opens_another_and_a_new_chat_reattaches_by_title(node):
    call = node["call"]
    a = await opened(node)
    again = await call("environment_open", title=TITLE.upper())  # its title, in any case: the same one
    assert again["id"] == a["id"] and again["opened"] == "attached"
    other_title = await call("environment_open", title="Something else")  # the title names the computer
    assert other_title["id"] != a["id"] and other_title["opened"] == "new"
    ran = await call("terminal_exec", cmd="pwd")  # this session took "Something else" last: calls go there
    assert ran["environment"] == {"id": other_title["id"], "title": "Something else"}
    await call("environment_open", title=TITLE)  # attaching again makes it this session's
    ran = await call("terminal_exec", cmd="pwd")
    assert ran["environment"]["id"] == a["id"]
    await call("environment_close", on=other_title["on"])
    # another chat of the same grant takes it over only with take_over=true, once the first was quiet 10 minutes
    mgr = node["mgr"]
    with pytest.raises(ToolCallError, match="^environment_in_use"):
        await call("environment_open", title=TITLE, session="chat-2")
    mgr._clock = lambda: utcnow() + timedelta(minutes=11)
    with pytest.raises(ToolCallError, match="^environment_in_use"):
        await call("environment_open", title=TITLE, session="chat-2")
    other = await call("environment_open", title=TITLE, session="chat-2", take_over=True)
    assert other["id"] == a["id"] and other["opened"] == "taken_over"
    assert mgr.get(a["id"]).binding == "session:chat-2"


async def test_another_grant_does_not_reattach_or_see_it(node):
    a = await opened(node)
    _g2, tok2 = node["policy"].issue(subject={"id": "agent-2"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                     specials={"vm": {}, "exec": {}})
    seen = await node["call"]("environment", session="chat-9", tok=tok2)
    assert seen["environment"] is None and seen["open"] == []
    with pytest.raises(ToolCallError, match="no environment"):
        await node["call"]("environment", on=a["id"], session="chat-9", tok=tok2)


async def test_open_past_the_nodes_limit_is_refused(node):
    node["mgr"].settings.max_live = 1
    await opened(node)
    with pytest.raises(ToolCallError, match="environment_full"):
        await node["call"]("environment_open", title="Second", session="chat-2")
    with pytest.raises(ToolCallError, match="environment_limit"):
        await node["call"]("environment_open", title="Too long", session="chat-3", hours=500)


def test_the_limits_hold_without_openajar_environments_allow():
    s = EnvSettings()  # no environments.toml: what a node has before anyone ran `openajar environments allow`
    assert (s.max_live, s.max_hours, s.monthly_eur) == (3, 8.0, 50.0)


async def test_a_fourth_open_is_refused_with_the_limit_and_the_command_that_raises_it(node):
    """No standing approval, after a page read: three open without asking anyone, the fourth is refused
    by the default max_live, and the error says which limit and how the owner raises it."""
    ctx = node["policy"].context(node["grant"], tool="browser_snapshot")
    ctx.taint("untrusted", source="browser_snapshot", kind="web_page", host="evil.example")
    for n in range(3):
        out = await opened(node, title=f"Work {n}", session=f"chat-{n}")
        assert out["state"] == "ready", out
    with pytest.raises(ToolCallError, match="environment_full") as err:
        await node["call"]("environment_open", title="Fourth", session="chat-4")
    words = str(err.value)
    assert "already runs 3 machines, its limit at once" in words and "openajar environments allow --max-live" in words
    assert "destroy" not in words and "fewer hours" not in words
    assert len(node["mgr"].store.open()) == 3
    # too long a life: the same, naming --max-hours (8 hours by default)
    with pytest.raises(ToolCallError, match="environment_limit") as err:
        await node["call"]("environment_open", title="Long", session="chat-5", hours=9)
    assert "at most 8 hours" in str(err.value) and "--max-hours" in str(err.value)


async def test_a_machine_whose_setup_fails_is_deleted_and_says_why(node):
    node["machine"].setup = "status: error\nE: Unable to locate package nodejs"
    with pytest.raises(ToolCallError, match=r"environment_failed: setup: environment e\w+ did not start: the "
                                            "machine's setup failed") as err:
        await opened(node)
    from openajar.core.catalog import mcp_lines
    message = str(err.value).removeprefix("environment_failed: ")
    assert mcp_lines("environment_failed", message)[0].startswith("fix: The machine did not finish its setup")
    env = node["mgr"].store.recent(1)[0]
    assert env.closed_reason.startswith("failed to start") and node["vms"].store.get(env.vm_id).state == "deleted"
    assert (env.state, env.error_code, env.error_reason) == ("failed", "environment_failed", "setup")
    # the record stays: environment() lists it under failed, with why
    listed = await node["call"]("environment")
    assert listed["failed"][0]["id"] == env.id and listed["failed"][0]["reason"] == "setup"


async def test_the_browser_and_the_clone_come_up_at_once_and_each_step_is_logged(node, caplog, monkeypatch):
    """Opens took 145 to 300 s: every step's time is logged at INFO, and the browser, the clone and the
    tools top-up run at once once the tunnels are up (the browser waits here until the clone has begun)."""
    import asyncio
    import logging

    mgr = node["mgr"]
    began = asyncio.Event()
    browser, clone = mgr._make_browser, mgr._clone

    async def slow_browser(env, **kw):
        await asyncio.wait_for(began.wait(), 5)  # only ends once the clone runs beside it
        await browser(env, **kw)

    async def cloning(env):
        began.set()
        await clone(env)

    monkeypatch.setattr(mgr, "_make_browser", slow_browser)
    monkeypatch.setattr(mgr, "_clone", cloning)
    with caplog.at_level(logging.INFO, logger="openajar.environments"):
        out = await opened(node, repo="example.test/me/shop")
    assert out["state"] == "ready"
    said = [r.getMessage() for r in caplog.records]
    for step in ("VM create", "VM ready", "setup check", "apt ready", "tunnels and git", "browser", "clone",
                 "tools top-up"):
        assert any(f": {step} took " in m for m in said), step
    assert any(m.startswith(f"environment {out['id']} ready (/work/shop) in ") and "clone " in m for m in said)


async def test_an_open_the_cloud_has_no_room_for_says_what_was_tried_and_what_next(node):
    node["prov"].unavailable = set(node["prov"].prices)  # no room for any type, anywhere
    with pytest.raises(ToolCallError, match="environment_failed: capacity: environment e\\w+ did not start: Fake "
                                            "has no room for cx23, cpx22, cpx42 or ccx23 in nbg1, fsn1 or hel1 now") as err:
        await opened(node)
    words = str(err.value)
    assert "try again in 5 minutes. Nothing bills" in words and "\u2014" not in words
    assert "size=" not in words  # every type a bigger size would try just had no room: none is offered
    assert "Available now" not in words  # the provider does not say what has room: nothing is claimed
    env = node["mgr"].store.recent(1)[0]
    assert (env.state, env.error_reason, env.vm_state) == ("failed", "capacity", "none")


async def test_a_size_the_cloud_has_no_room_for_offers_the_sizes_it_lists_now_and_rents_none(node):
    """size=large had no room in any location (2026-11-16) and the error said to try xlarge or wait,
    while medium opened at once. The error names the sizes the provider lists as orderable now, nearest
    first, and says which are smaller than asked; it never rents one of them by itself."""
    prov = node["prov"]
    prov.unavailable = {"cpx42"}  # large's server type: no room anywhere, though the provider lists it
    prov.listed = {"cpx42": ["nbg1", "fsn1", "hel1"], "ccx23": ["nbg1", "fsn1"], "cx23": ["nbg1"], "cax11": [], "cpx22": []}
    with pytest.raises(ToolCallError, match="environment_failed: capacity") as err:
        await opened(node, size="large")
    words = str(err.value)
    assert ('Available now: medium (4 CPU, 8 GB, less than you asked for): environment_open(size="medium"); '
            'or small (2 CPU, 4 GB, less than you asked for): environment_open(size="small")') in words, words
    assert "xlarge" not in words and "Nothing bills: no machine was made" in words and "\u2014" not in words
    assert prov.made_in == [] and len(words) < 500
    env = node["mgr"].store.recent(1)[0]
    assert (env.state, env.error_reason) == ("failed", "capacity")


# ------------------------------------------------------------------ routing


async def test_terminal_calls_without_on_go_to_the_environment_and_never_wait(node):
    call = node["call"]
    with pytest.raises(ToolCallError, match="no_environment"):
        await call("terminal_exec", cmd="echo hi")
    out = await opened(node)
    # untrusted page text in this grant's context does not hold a command on its own VM
    ctx = node["policy"].context(node["grant"], tool="browser_snapshot")
    ctx.taint("untrusted", source="browser_snapshot")
    res = await call("terminal_exec", cmd="npm test", cwd="/work/shop")
    assert res["exit_code"] == 0 and res.get("status") != "approval_pending"
    sent = in_bash("cd '/work/shop' 2>/dev/null || { echo openajar-no-such-folder >&2; exit 97; }\nnpm test")
    assert (sent, node["grant"].id) in node["machine"].ran
    env = node["mgr"].get(out["id"])
    assert env.last_call_at is not None and [c.tool for c in env.calls][-1] == "terminal_exec"
    assert env.calls[-1].summary == "npm test"
    # with `on`, a call behaves as before: another lane is another lane
    with pytest.raises(ToolCallError):
        await call("terminal_exec", on="dev/other", cmd="echo hi")


async def test_a_command_on_another_vm_lane_still_asks_after_untrusted_text(node):
    await opened(node)
    await node["vms"].create("dev/plain", ttl_minutes=30)
    ctx = node["policy"].context(node["grant"], tool="browser_snapshot")
    ctx.taint("untrusted", source="browser_snapshot")
    out = await node["call"]("terminal_exec", on="dev/plain", cmd="echo hi")
    assert out["status"] == "approval_pending"


async def test_browser_calls_without_on_resolve_to_the_browser_lane(node):
    out = await opened(node)
    rt = node["rt"]
    kwargs: dict = {"url": "http://localhost:3000"}
    routing.route(rt, "browser_navigate", kwargs, session="chat-1", grant_id=node["grant"].id)
    assert kwargs["on"] == f"env/{out['id']}-browser"
    kept = {"on": "biz-a/social", "url": "https://example.com"}
    routing.route(rt, "browser_navigate", kept, session="chat-1", grant_id=node["grant"].id)
    assert kept["on"] == "biz-a/social"
    other: dict = {"url": "x"}
    with pytest.raises(Exception, match="no environment") as err:
        routing.route(rt, "browser_navigate", other, session="chat-2", grant_id="gr_other")
    # an agent doing account work that forgot `on` is told so, not only to rent a machine
    assert "account browser" in str(err.value) and "`on`" in str(err.value) and "browsers_list" in str(err.value)


async def test_studio_checks_get_the_environments_proxy(node):
    from openajar.studio.browser.pool import review_proxy

    out = await opened(node)
    with routing.studio_proxy(node["rt"], "ui_check", {"url": "http://localhost:3000"}, session="chat-1",
                              grant_id=node["grant"].id) as lane:
        assert lane == f"env/{out['id']}"
        assert review_proxy.get() == {"server": "socks5://127.0.0.1:41080", "bypass": "<-loopback>"}
    assert review_proxy.get() is None
    with routing.studio_proxy(node["rt"], "ui_check", {"url": "x"}, session="chat-2", grant_id="g") as lane:
        assert lane is None and review_proxy.get() is None


async def test_a_studio_check_never_checks_the_node_while_the_environment_cannot_be_reached(node):
    """With an environment bound, ui_check goes through its tunnel or fails: while the machine starts,
    after a restart of the node or while the tunnel is down, it says so and how to go on."""
    from openajar.core.tools import ToolSpec
    from openajar.studio.browser.pool import review_proxy

    seen: list = []

    async def ui_check(ctx, *, url: str) -> dict:
        """A stand-in for studio's ui_check: notes the browser proxy it would check through."""
        seen.append(review_proxy.get())
        return {"checked": url}

    call = wrap_tool(ToolSpec("ui_check", ui_check, Effect.READ, lane_arg=None), node["rt"])
    out = await opened(node)
    mgr = node["mgr"]

    async def check(session: str = "chat-1"):
        with bind_request_token(node["token"], session=session):
            return await call(url="http://localhost:3000")

    assert (await check())["checked"] and seen[-1]["server"].startswith("socks5://")
    await mgr.shutdown()  # agentosd restarted: the machine is there, its tunnel is not yet
    with pytest.raises(ToolCallError, match="environment_not_ready") as err:
        await check()
    assert out["id"] in str(err.value) and "environment(on=...)" in str(err.value) and len(seen) == 1
    env = mgr.get(out["id"])
    env.state = "starting"
    mgr.store.put(env)
    with pytest.raises(ToolCallError, match="environment_not_ready.*starting"):
        await check()
    env.state = "ready"
    mgr.store.put(env)
    await mgr.reap_once()  # the reaper brings the tunnel back
    assert (await check())["checked"] and seen[-1]["server"].startswith("socks5://")
    # a chat with no environment: studio checks what the node reaches, as before
    await check(session="chat-2")
    assert seen[-1] is None


def test_routed_tools_take_on_as_optional_in_their_schema():
    from openajar.core.tools import ToolSpec
    from openajar.mcp.registry import tool_params

    async def lane_exec(ctx, *, on: str, cmd: str) -> dict:
        return {}

    [on, _cmd] = tool_params(ToolSpec("terminal_exec", lane_exec, Effect.EXEC))
    assert not on.required and on.default is None and "environment" in on.description
    async def vm_create(ctx, *, on: str) -> dict:
        return {}

    [kept] = tool_params(ToolSpec("vm_create", vm_create, Effect.VM))
    assert kept.required


# ------------------------------------------------------------------ status and close


async def test_environment_reads_ports_git_and_calls(node):
    out = await opened(node, repo="example.test/me/shop")
    await node["call"]("terminal_exec", cmd="ls")
    st = await node["call"]("environment")
    assert st["id"] == out["id"] and st["state"] == "working"
    assert st["ports"] == [{"port": 3000, "process": "node", "link": None}]
    assert st["git"] == {"branch": "main", "ahead": 0, "dirty": 0, "pushed": [], "deleted": []}
    assert st["calls"][0]["tool"] == "terminal_exec" and st["unsaved"] is None
    assert st["calls"][0]["ok"] is True and st["calls"][0]["exit_code"] == 0


async def test_every_machine_gets_swap_and_says_so_in_its_notes(node):
    """Field report 2026-11-16: an 8 GB machine with no swap OOM-killed a 4.1 GB compile."""
    await opened(node, repo="example.test/me/shop")
    assert any(cmd == machine.SWAP_ON for cmd, _ in node["machine"].ran)
    st = await node["call"]("environment")
    assert st["machine"]["swap"] == "8 GB, compressed in memory (zram)"


async def test_close_refuses_unsaved_work_then_discard_deletes_everything(node):
    out = await opened(node, repo="example.test/me/shop")
    node["machine"].git = ("repo=/work/shop\nbranch=feature\nahead=1\ndirty_count=1\n"
                           "dirty= M index.html\nunpushed=abc1234 Thirty percent off\n")
    with pytest.raises(ToolCallError, match="unsaved_work") as err:
        await node["call"]("environment_close")
    assert "index.html" in str(err.value) and "abc1234 Thirty percent off" in str(err.value)
    closed = await node["call"]("environment_close", discard=True)
    assert closed["id"] == out["id"] and closed["state"] == "closed" and closed["cost_eur"] is not None
    env = node["mgr"].get(out["id"])
    assert node["vms"].store.get(env.vm_id).state == "deleted" and env.closed_reason.endswith("(discarded)")
    assert f"env/{out['id']}-browser" in node["lanes"].deleted
    assert node["lanes"].proxies_loader() == {}
    with pytest.raises(ToolCallError, match="no_environment"):
        await node["call"]("terminal_exec", cmd="echo after")


async def test_close_refuses_files_in_no_git_repo_and_says_how_to_keep_them(node):
    """T3: a computer with no repository closed and deleted a new file in /work without a word. Files in no
    git repository are unsaved work for keep "none": the refusal lists them and says to download them."""
    out = await opened(node)
    node["machine"].git = "loose=notes/important.txt\nloose=out/report.txt\nloose_count=2\n"
    with pytest.raises(ToolCallError, match="unsaved_work") as err:
        await node["call"]("environment_close")
    words = str(err.value)
    assert "files in /work in no git repository: notes/important.txt, out/report.txt" in words
    assert 'environment_download(path="<file or folder>")' in words and "discard=true" in words
    assert "commit it and push" not in words.lower() and "\u2014" not in words
    state = await node["call"]("environment")
    assert state["unsaved"]["loose_files"] == ["notes/important.txt", "out/report.txt"]
    closed = await node["call"]("environment_close", on=out["on"], discard=True)
    assert closed["state"] == "closed"


def test_the_files_in_no_repo_come_after_the_repos_and_are_never_the_main_one():
    from openajar.environments.probes import main_repo
    repos = parse_git_state("repo=/work/shop\nbranch=main\nahead=0\ndirty_count=0\nloose=a b.txt\nloose_count=60\n")
    assert [r["dir"] for r in repos] == ["/work/shop", "/work"] and repos[1]["loose"] and repos[1]["dirty"] == 60
    assert main_repo(repos, "/work")["dir"] == "/work/shop"
    unsaved = EnvironmentManager.unsaved_of(repos)
    assert unsaved["loose_files"] == ["a b.txt"] and unsaved["loose_count"] == 60 and unsaved["dirty_files"] == []
    assert EnvironmentManager.unsaved_of(parse_git_state("loose_count=0\n")) is None


async def test_close_of_another_grants_environment_is_refused(node):
    a = await opened(node)
    _g2, tok2 = node["policy"].issue(subject={"id": "agent-2"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                     specials={"vm": {}, "exec": {}})
    with pytest.raises(ToolCallError, match="no environment"):
        await node["call"]("environment_close", on=a["id"], session="chat-9", tok=tok2)
    assert node["mgr"].get(a["id"]).open


@pytest.mark.parametrize("scope", ["lane", "workspace", "node"])
async def test_a_freeze_stops_every_environment_tool_and_closes_nothing(node, tmp_path, scope):
    # the environment tools name no lane, so each reaches the environment's lanes itself: a freeze of its
    # terminal lane, of the `env` workspace or of the node refuses them, and its machine is left as it is
    from openajar.freeze.service import FreezeService

    out = await opened(node)
    fz = FreezeService(node["rt"], home=tmp_path / "fz")
    node["rt"].register("freeze", fz)
    target = {"lane": f"env/{out['id']}", "workspace": "env", "node": fz.node_name}[scope]
    fz.mark(scope, target, reason="stop", by="owner")
    for tool, kw in (("environment", {}), ("environment_close", {"discard": True}),
                     ("environment_open", {"title": TITLE})):
        with pytest.raises(ToolCallError, match="^frozen"):
            await node["call"](tool, **kw)
    with pytest.raises(ToolCallError, match="^frozen"):  # by its handle from another chat
        await node["call"]("environment", on=out["on"], session="chat-2")
    if scope != "lane":  # a new one would have its lane in the frozen workspace (a lane freeze is that lane)
        with pytest.raises(ToolCallError, match="^frozen"):
            await node["call"]("environment_open", title="Another page", session="chat-2")
        assert [e.id for e in node["mgr"].store.open()] == [out["id"]]
    env = node["mgr"].get(out["id"])
    assert env.open and node["vms"].store.get(env.vm_id).state != "deleted"
    assert f"env/{out['id']}-browser" not in node["lanes"].deleted
    if scope == "lane":  # another chat's listing leaves the frozen one out and says so
        listed = await node["call"]("environment", session="chat-3")
        assert listed["open"] == [] and "frozen" in listed


# ------------------------------------------------------------------ reaper


async def test_the_reaper_closes_an_idle_clean_environment(node):
    out = await opened(node)
    mgr = node["mgr"]
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later)
    assert got["closed"] == [out["id"]] and mgr.get(out["id"]).state == "closed"
    assert mgr.get(out["id"]).closed_reason.startswith("idle")


async def test_the_reapers_screen_check_is_no_use_and_an_idle_desktop_still_closes(node, monkeypatch):
    """The node asks a desktop's screen on every pass (vmbrowser.check_screen) with a command on the terminal
    lane, and every command notes use of that lane: the check must put the time back, or an environment with a
    desktop would never close for idleness."""
    import json

    from openajar.environments import vmbrowser

    out = await opened(node)
    mgr, envs, fake = node["mgr"], node["envs"], node["machine"]

    def to_vm(e):
        e.browser = "vm"

    env = mgr.store.update(out["id"], to_vm)
    vm = vmbrowser.VmBrowsers(mgr)
    mgr._vm_browsers = vm
    vm._screens[env.id] = (1920, 1080)  # its desktop is up
    monkeypatch.setattr(vm, "_require", lambda lane_id: (env, None))
    asked: list[str] = []
    healthy = json.dumps({"ok": True, "display": True, "desktop": True, "browser": False})

    async def exec_(lane_id, cmd, **kw):  # as EnvManager.exec: every command notes use of its lane
        envs.store.touch(lane_id)
        if cmd == vmbrowser.ctl_command():
            asked.append(json.loads(kw["stdin"])["action"])
            return ExecResult(exit_code=0, stdout=(healthy if asked[-1] == "status" else '{"ok": true}') + "\n",
                              stderr="", duration_s=0.01)
        return await fake.exec(lane_id, cmd, **kw)

    monkeypatch.setattr(envs, "exec", exec_)
    idle_before = mgr._last_used(env)
    soon = utcnow() + timedelta(minutes=1)
    await mgr.reap_once(soon)
    await asyncio.gather(*vm._screen_tasks.values())
    assert asked == ["status"]  # the screen was checked, and found healthy
    assert mgr._last_used(mgr.get(env.id)) == idle_before  # and the check was no use of the machine
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later)
    assert got["closed"] == [env.id] and mgr.get(env.id).closed_reason.startswith("idle")


async def test_a_person_working_in_the_live_view_keeps_the_environment_open(node):
    """Input from a human in the environment's browser (the live view, taken over) is activity: the
    reaper does not close a clean environment someone is using."""
    out = await opened(node)
    mgr, lanes = node["mgr"], node["lanes"]
    idle = timedelta(minutes=mgr.settings.idle_minutes)
    later = utcnow() + idle + timedelta(minutes=1)
    browser = lanes.lanes[f"env/{out['id']}-browser"]
    browser.control = browser.control.model_copy(update={"last_human_input": later - timedelta(minutes=2)})
    got = await mgr.reap_once(later)
    assert got["closed"] == [] and mgr.get(out["id"]).open
    # once the person stops too, an idle stretch closes it
    got = await mgr.reap_once(later + idle)
    assert got["closed"] == [out["id"]]


async def test_the_reaper_keeps_unsaved_work_and_tells_the_owner_once(node, monkeypatch):
    out = await opened(node)
    mgr = node["mgr"]
    told: list[str] = []

    async def notify(title, body):
        told.append(title)

    monkeypatch.setattr(mgr, "_notify", notify)
    node["machine"].git = "repo=/work/shop\nbranch=main\nahead=0\ndirty_count=2\ndirty= M a\ndirty=?? b\n"
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    first = await mgr.reap_once(later)
    second = await mgr.reap_once(later + timedelta(minutes=1))
    assert first["kept_unsaved"] == [out["id"]] and second["kept_unsaved"] == []
    assert len(told) == 1 and "not pushed" in told[0] and mgr.get(out["id"]).open
    # at its time limit it gets a rescue save first (openajar.environments.saving; test_environments_saves
    # has the real one). This fake machine cannot make one, so it is kept and the owner hears so: never a
    # silent discard
    end = await mgr.reap_once(mgr.get(out["id"]).expires_at + timedelta(seconds=1))
    assert end["closed"] == [] and end["save_failed"] == [out["id"]] and mgr.get(out["id"]).open
    assert "could not be saved" in told[-1]


async def test_the_reaper_marks_an_environment_whose_vm_is_gone(node):
    out = await opened(node)
    mgr, vms = node["mgr"], node["vms"]
    await vms.destroy(mgr.get(out["id"]).vm_id, reason="by hand")
    got = await mgr.reap_once()
    assert got["gone"] == [out["id"]] and mgr.get(out["id"]).closed_reason == "its machine was deleted"


async def test_the_vm_reaper_leaves_an_environments_idle_vm_to_the_environment(node):
    out = await opened(node)
    vms = node["vms"]
    rec = vms.store.get(node["mgr"].get(out["id"]).vm_id)
    # idle into the last minutes of a paid hour: a plain VM would go now
    later = rec.created_at + timedelta(minutes=58)
    await vms.reap_once(later)
    assert vms.store.get(rec.id).state == "ready"


async def test_a_restart_keeps_the_environment_and_the_reaper_brings_its_tunnel_back(node):
    out = await opened(node)
    mgr = node["mgr"]
    await mgr.shutdown()  # agentosd stops: the VM stays, the tunnels go
    assert mgr.proxies() == {} and node["vms"].store.get(mgr.get(out["id"]).vm_id).state == "ready"
    got = await mgr.reap_once()
    assert got["tunnels"] == [out["id"]] and f"env-{out['id']}" in mgr.proxies()
    assert mgr.get(out["id"]).state == "ready"


async def test_the_budget_counts_the_hours_open_environments_have_booked(node):
    """An open environment can still bill to its time limit: the next open counts those hours, not
    only what was spent so far. cx23 at EUR 0.006 an hour; its dearer fallbacks are dropped when they do
    not fit what is left, and never refuse an open the planned type fits."""
    mgr = node["mgr"]
    mgr.settings = EnvSettings(monthly_eur=0.04, default_keep="none")
    a = await opened(node, hours=4)                  # booked: 4 h x 0.006 = 0.024 (0.006 spent so far)
    assert a["state"] == "ready"
    with pytest.raises(ToolCallError, match="environment_limit") as err:
        await opened(node, title="Second", session="chat-2", hours=4)  # 4 h x 0.006 = 0.024 more
    assert "booked" in str(err.value)
    assert len(mgr.store.open()) == 1
    await node["call"]("environment_close")
    again = await opened(node, title="Second", session="chat-2", hours=4)
    assert again.get("state") == "ready", again


async def test_extend_checks_the_budget_and_the_longest_life(node):
    from openajar.environments.errors import EnvironmentLimit

    mgr, vms = node["mgr"], node["vms"]
    out = await opened(node, hours=1)
    mgr.settings = EnvSettings(monthly_eur=0.04, default_keep="none")
    with pytest.raises(EnvironmentLimit, match="budget") as err:
        await mgr.extend(out["id"], 10 * 60)         # up to the longest life, 8 h x 0.006 = 0.048
    assert "openajar environments allow --monthly-eur" in str(err.value)
    mgr.settings = EnvSettings(default_keep="none")
    vms.settings = vms.settings.model_copy(update={"monthly_budget_eur": 0.04})
    with pytest.raises(EnvironmentLimit, match="budget"):
        await mgr.extend(out["id"], 10 * 60)         # the node's VM budget holds too
    vms.settings = vms.settings.model_copy(update={"monthly_budget_eur": 50.0})
    env = await mgr.extend(out["id"], 100 * 60)       # past the longest life: up to it, no further
    assert env.expires_at == env.created_at + timedelta(hours=mgr.settings.max_hours)
    assert vms.store.get(env.vm_id).expires_at == env.expires_at
    with pytest.raises(EnvironmentLimit, match="longest life of 8 hours") as err:
        await mgr.extend(out["id"], 30)
    assert "openajar environments allow --max-hours" in str(err.value)


async def test_extend_moves_the_vms_limit_too(node):
    out = await opened(node, hours=1)
    mgr = node["mgr"]
    before = mgr.get(out["id"]).expires_at
    env = await mgr.extend(out["id"], 30)
    assert env.expires_at - before == timedelta(minutes=30)
    assert node["vms"].store.get(env.vm_id).expires_at == env.expires_at


# ------------------------------------------------------------------ pieces


def test_ports_and_git_state_are_read_from_the_machines_own_output():
    ss = ('LISTEN 0 511 *:3000 *:* users:(("node",pid=12,fd=20))\n'
          'LISTEN 0 511 [::]:3000 [::]:* users:(("node",pid=12,fd=21))\n'
          'LISTEN 0 128 127.0.0.1:3129 0.0.0.0:* users:(("sshd-session",pid=3,fd=9))\n'
          'LISTEN 0 4096 127.0.0.1:5432 0.0.0.0:*\n')
    assert parse_ports(ss) == [{"port": 3000, "process": "node", "link": None},
                               {"port": 5432, "process": None, "link": None}]
    repos = parse_git_state("repo=/work/a\nbranch=main\nahead=2\ndirty_count=1\ndirty= M x.py\n"
                            "unpushed=abc first\nunpushed=def second\n")
    assert repos == [{"dir": "/work/a", "branch": "main", "ahead": 2, "dirty": 1, "dirty_files": ["x.py"],
                      "unpushed": ["abc first", "def second"], "stash_count": 0, "stashes": [], "error": None}]
    assert EnvironmentManager.unsaved_of(repos) == {"dirty_files": ["x.py"], "unpushed": ["abc first", "def second"],
                                                    "stashes": []}
    clean = {**repos[0], "ahead": 0, "dirty": 0, "dirty_files": [], "unpushed": []}
    assert EnvironmentManager.unsaved_of([clean]) is None
    stashed = parse_git_state("repo=/work/a\nstash_count=1\nstash=stash@{0} WIP on main\n")
    assert EnvironmentManager.unsaved_of(stashed)["stashes"] == ["stash@{0} WIP on main"]
    unreadable = parse_git_state("repo=/work/b\nerror=git cannot read it\n")
    assert EnvironmentManager.unsaved_of([clean, *unreadable])["unreadable"] == ["/work/b"]


def test_the_shown_state_follows_the_last_call():
    now = utcnow()
    env = Environment(id="e7k2", title="t", grant_id="g", binding="grant:g", state="ready", created_at=now,
                      ready_at=now, expires_at=now + timedelta(hours=1))
    assert env.shown_state(now) == "ready"
    env.last_call_at = now
    assert env.shown_state(now + timedelta(minutes=1)) == "working"
    assert env.shown_state(now + timedelta(minutes=11)) == "idle"
    with pytest.raises(ValueError):
        Environment(id="../x", title="t", grant_id="g", binding="b", created_at=now, expires_at=now)


async def test_a_grant_without_the_vm_special_cannot_open_one(node):
    _g, tok = node["policy"].issue(subject={"id": "reader"}, workspaces=["*"], ceiling=Effect.COMMIT)
    with pytest.raises((ToolCallError, PolicyDenied)):
        await node["call"]("environment_open", title=TITLE, tok=tok)
    assert node["mgr"].store.open() == []


async def test_the_grant_openajar_connect_gives_opens_one_and_works_only_on_env_lanes(node):
    # the `environments` special (openajar connect, openajar dev up): vm and exec on env/* only. The open
    # names no lane, so its check waits for the lane the node picks (env/<id>)
    from openajar.api.bridge import panel_specials
    from openajar.dev.node import AGENT_SPECIALS

    specials = panel_specials(["environments", "memory_read"])
    assert {k: specials[k] for k in ("vm", "exec")} == {k: AGENT_SPECIALS[k] for k in ("vm", "exec")}
    _g, tok = node["policy"].issue(subject={"id": "claude-code"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                   specials=specials)
    out = await opened(node, tok=tok, session="chat-c", repo="example.test/me/shop")
    assert out["state"] == "ready", out
    ran = await node["call"]("terminal_exec", tok=tok, session="chat-c", cmd="npm test", cwd=out["workdir"])
    assert ran["exit_code"] == 0
    with pytest.raises((ToolCallError, PolicyDenied)):  # an account lane is not covered
        await node["call"]("terminal_exec", tok=tok, session="chat-c", on="personal/code", cmd="ls")


async def test_a_job_on_the_environment_is_cancelled_with_the_connect_grant(node, monkeypatch):
    # terminal_job_cancel names a job, not a lane: the check waits for the job's lane (env/<id>)
    from types import SimpleNamespace

    from openajar.api.bridge import panel_specials
    from openajar.env.models import JobInfo

    _g, tok = node["policy"].issue(subject={"id": "claude-code"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                   specials=panel_specials(["environments"]))
    out = await opened(node, tok=tok, session="chat-j")
    info = JobInfo(id="job-1", lane=f"env/{out['id']}", cmd="npm run dev", timeout_s=60)
    cancelled = info.model_copy(update={"state": "cancelled"})
    monkeypatch.setattr(node["envs"], "job", lambda _id: SimpleNamespace(info=info))

    async def job_cancel(_id):
        return cancelled

    monkeypatch.setattr(node["envs"], "job_cancel", job_cancel)
    done = await node["call"]("terminal_job_cancel", tok=tok, session="chat-j", job="job-1")
    assert done["state"] == "cancelled"
    # a job on an account lane stays out of reach: the handler's own check refuses it
    info = JobInfo(id="job-2", lane="personal/code", cmd="ls", timeout_s=60)
    lane = node["envs"].get(f"env/{out['id']}")
    monkeypatch.setattr(node["envs"], "get", lambda _id: lane)
    with pytest.raises((ToolCallError, PolicyDenied), match="exec is not granted for personal/code"):
        await node["call"]("terminal_job_cancel", tok=tok, session="chat-j", job="job-2")


# ------------------------------------------------------------------ unsaved work, with real git

GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def _git(*args: str, cwd) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, env={**GIT_ENV, "HOME": str(cwd)}, capture_output=True,
                          text=True, check=True).stdout


def _machine_here(root):
    """envs.exec running the command on this computer, with the machine's /work at `root`."""
    import asyncio

    async def run(lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None):
        proc = await asyncio.create_subprocess_exec("sh", "-c", cmd.replace("/work", str(root)),
                                                    env={**GIT_ENV, "HOME": str(root.parent)},
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return ExecResult(exit_code=proc.returncode, stdout=out.decode().replace(str(root), "/work"),
                          stderr=err.decode(), duration_s=0.01)

    return run


async def test_unsaved_work_is_every_repo_every_branch_stashes_and_new_files(node, tmp_path, monkeypatch):
    """What close and the reaper must never delete without discard: commits on any local branch not on
    a remote, stashes, untracked files, and all of that in every repository under /work."""
    from openajar.environments.errors import UnsavedWork

    out = await opened(node, repo="example.test/me/shop")
    mgr = node["mgr"]
    up, work = tmp_path / "up.git", tmp_path / "machine" / "work"
    work.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", str(up), cwd=tmp_path)
    shop = work / "shop"
    _git("clone", "-q", str(up), str(shop), cwd=tmp_path)
    (shop / "index.html").write_text("<h1>Sale</h1>\n")
    _git("add", ".", cwd=shop)
    _git("commit", "-qm", "first", cwd=shop)
    _git("push", "-q", "origin", "main", cwd=shop)
    monkeypatch.setattr(node["envs"], "exec", _machine_here(work))

    async def refused(why: str) -> dict:
        with pytest.raises(UnsavedWork) as err:
            await mgr.close(out["id"])
        later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
        assert out["id"] not in (await mgr.reap_once(later))["closed"], why
        assert mgr.get(out["id"]).open, why
        return err.value.details

    # 1. a commit on another local branch, while the checked-out one is pushed and clean
    _git("checkout", "-q", "-b", "feature", cwd=shop)
    (shop / "a.txt").write_text("a\n")
    _git("add", ".", cwd=shop)
    _git("commit", "-qm", "Feature work", cwd=shop)
    _git("checkout", "-q", "main", cwd=shop)
    details = await refused("a commit on another branch")
    assert any("Feature work" in c for c in details["unpushed"]), details
    _git("push", "-q", "origin", "feature", cwd=shop)

    # 2. a stash
    (shop / "index.html").write_text("<h1>Bigger sale</h1>\n")
    _git("stash", "-q", cwd=shop)
    details = await refused("a stash")
    assert details.get("stashes") and "stash@{0}" in details["stashes"][0], details
    _git("stash", "drop", "-q", cwd=shop)

    # 3. a second repository under /work (not the one opened with), with only an untracked file
    notes = work / "tools" / "notes"
    notes.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(notes), cwd=tmp_path)
    (notes / "todo.md").write_text("ship it\n")
    details = await refused("another repository")
    assert any("todo.md" in f for f in details["dirty_files"]), details
    (notes / "todo.md").unlink()

    # 4. an untracked file in the opened one
    (shop / "new.css").write_text("h1 {}\n")
    details = await refused("an untracked file")
    assert any("new.css" in f for f in details["dirty_files"]), details
    (shop / "new.css").unlink()

    closed = await mgr.close(out["id"])
    assert closed.state == "closed"
