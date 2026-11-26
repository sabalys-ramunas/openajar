"""The environment's browser and live view on a machine handed over whole (openajar.vms.whole), for real:
the story of tests/unit/test_environments_vmbrowser_docker.py, run against the stand-in server of
tests/unit/test_environments_whole_docker.py. Playwright on this computer drives the Camoufox that
`machine add --whole` installed on the machine, through the environment's tunnel.

Skipped unless that file's server can run (see its docstring) and Playwright is installed."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from test_environments_vmbrowser_docker import (  # noqa: F401 - the same story, on a whole machine
    test_the_browser_runs_on_the_machine,
)
from test_environments_whole_docker import NAME, _why_not, box, register, unhold  # noqa: F401 - box is the fixture
from test_policy_support import make_service

from openajar.core.config import NodeConfig
from openajar.core.models import Effect
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments.manager import EnvironmentManager
from openajar.environments.models import EnvSettings
from openajar.vms.manager import VmManager
from openajar.vms.models import VmSettings


def _playwright() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(_why_not() is not None or not _playwright(),
                                reason=_why_not() or "needs Playwright")


@pytest.fixture
async def node(tmp_path, box):  # noqa: F811
    from openajar.node.lanes import build_lane_manager

    home = tmp_path / "h"  # short: the tunnel's sockets live in a temp folder, the lanes' under home
    home.mkdir(mode=0o700)
    policy, rt = make_service(home)
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0, max_timeout_s=600))
    rt.register("envs", envs)
    await asyncio.to_thread(register, home, box)
    node_name = f"t-{uuid.uuid4().hex[:8]}"
    vms = VmManager(home, runtime=rt, settings=VmSettings(provider="own", location=NAME), node_name=node_name)
    rt.register("vms", vms)
    lanes = build_lane_manager(NodeConfig(home=home, self_check=False), node_ip_probe=None)
    rt.register("lanes", lanes)
    await lanes.start()
    mgr = EnvironmentManager(home, runtime=rt, settings=EnvSettings(browser="vm"))
    rt.register("environments", mgr)
    grant, _ = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                            specials={"vm": {}, "exec": {}})
    try:
        yield {"mgr": mgr, "envs": envs, "lanes": lanes, "grant": grant, "node_name": node_name, "home": home}
    finally:
        for env in mgr.store.open():
            await mgr.close(env.id, discard=True, reason="test end")
        await lanes.shutdown()
        await mgr.shutdown()
        await vms.shutdown()
        await envs.shutdown()
        policy.close()
        # the close ended the browser and its screen on the machine, as it ends them on a rented VM by deleting it
        left = box.sh("pgrep -af '[c]amoufox|[X]vnc|[X]vfb|[x]11vnc|[v]mbrowser_ctl' || true").strip()
        unhold(box)
        assert left == "", left
