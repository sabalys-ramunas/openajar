"""The environment's browser and live view on a person's own machine (openajar.vms.own), for real: the
same stories as tests/unit/test_environments_vmbrowser_docker.py, run here against a container on the
stand-in machine of tests/unit/test_environments_own_docker.py. Playwright on this computer drives the
Camoufox in that container through the environment's tunnel, which goes through the machine's ssh.

Skipped unless that file's machine can run (see its docstring) and Playwright is installed."""

from __future__ import annotations

import uuid

import pytest
from test_environments_own_docker import _why_not, box, register  # noqa: F401 - box is the fixture
from test_environments_vmbrowser_docker import (  # noqa: F401 - the same stories, on a person's machine
    _ready,
    test_a_tunnel_whose_ssh_was_killed_comes_back_with_its_browser_and_live_view,
    test_the_browser_runs_on_the_machine,
)
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

pytestmark = pytest.mark.skipif(_why_not() is not None or not _ready(),
                                reason=_why_not() or "needs Playwright and the image built with its browser")


@pytest.fixture
async def node(tmp_path, box):  # noqa: F811
    from openajar.node.lanes import build_lane_manager

    home = tmp_path / "h"  # short: the tunnel's sockets live in a temp folder, the lanes' under home
    home.mkdir(mode=0o700)
    policy, rt = make_service(home)
    envs = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0, max_timeout_s=600))
    rt.register("envs", envs)
    register(home, box)
    node_name = f"t-{uuid.uuid4().hex[:8]}"
    vms = VmManager(home, runtime=rt, settings=VmSettings(provider="own", location="testbox", docker_memory_gb=4),
                    node_name=node_name)
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
        box.sh(f"docker rm -f -v $(docker ps -aq --filter label=agentos-home={vms.home_id}) 2>/dev/null; true")
        box.sh("sed -i '/openajar-machine-/d' /root/.ssh/authorized_keys 2>/dev/null; true")
        assert box.containers(f"agentos-home={vms.home_id}") == []
