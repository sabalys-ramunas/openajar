"""An environment with a closed network (limited) and a bound secret on a person's own machine
(openajar.vms.own), for real: the story of tests/unit/test_environments_network_docker.py, run against a
container on the stand-in machine of tests/unit/test_environments_own_docker.py. The machine is closed from
outside the container (the entrypoint's wall, copied in over ssh), and its only way out is the node's
proxy, forwarded into it through the machine.

Skipped unless that file's machine can run (see its docstring). It reaches the internet (a registry)."""

from __future__ import annotations

import pytest
from test_environments_network_docker import (  # noqa: F401 - the same story, on a person's machine
    test_a_limited_machine_with_a_bound_secret,
)
from test_environments_own_docker import _why_not, box, node  # noqa: F401 - the fixtures

pytestmark = [pytest.mark.skipif(_why_not() is not None, reason=_why_not() or ""), pytest.mark.network]
