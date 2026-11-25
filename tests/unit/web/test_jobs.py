"""Runs (`openajar run` jobs) as the app's Runs pages read them (openajar.api.web.jobs).

Jobs are real background commands in a terminal lane on the fake host, the setup the env tests use: they
run on this machine with DirectLauncher. They print what a test runner prints, so the summary is read
from real output.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from openajar.core.errors import InvalidRequest
from openajar.env import testsummary
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher
from openajar.env.tools import _source

JOBS = "/api/orgs/local/jobs"
TOKEN = "t" * 43


@pytest.fixture
def envs(client: TestClient, tmp_path: Path) -> Iterator[EnvManager]:
    mgr = EnvManager(tmp_path / "envhome", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0, max_timeout_s=60))
    client.app.state.api.runtime.register("envs", mgr)
    mgr.create("dev/run-shop", kind="terminal", preset="sandbox")
    yield mgr
    client.portal.call(mgr.shutdown)


def start(client: TestClient, mgr: EnvManager, cmd: str, **kwargs: Any) -> str:
    info = client.portal.call(functools.partial(mgr.job_start, "dev/run-shop", cmd, by="owner", **kwargs))
    return info.id


def wait_ended(client: TestClient, job: str, timeout: float = 15.0) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while True:
        body = client.get(f"{JOBS}/{job}").json()
        if body["state"] != "running":
            return body
        assert time.monotonic() < end, "the job did not end"
        time.sleep(0.1)


PYTEST_OUT = "collected 123 items\\n\\n==== 2 failed, 118 passed, 3 skipped in 12.34s ====\\n"


def test_a_computer_without_terminal_lanes_says_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from openajar.api.web import jobs

    monkeypatch.setattr(jobs, "manager", lambda api: None)
    body = client.get(JOBS).json()
    assert body["available"] is False and body["jobs"] == [] and "openajar run" in body["reason"]


def test_runs_are_listed_newest_first_with_how_they_ended(client: TestClient, envs: EnvManager) -> None:
    passed = start(client, envs, "echo ok")
    wait_ended(client, passed)
    failed = start(client, envs, f"printf '{PYTEST_OUT}'; exit 1")
    wait_ended(client, failed)
    body = client.get(JOBS).json()
    assert body["available"] is True
    assert [j["id"] for j in body["jobs"]] == [failed, passed]
    first, second = body["jobs"]
    assert first["state"] == "failed" and first["exit_code"] == 1
    assert second["state"] == "passed" and second["exit_code"] == 0
    assert first["lane"] == "dev/run-shop" and first["workspace"] == "dev" and first["lane_name"] == "run-shop"
    assert first["node"] == "dev" and first["started_by"] in ("The owner", "Owner")  # in words, never the id
    assert first["finished_at"] and first["duration_s"] is not None
    assert first["tests"] == {"runner": "pytest", "passed": 118, "failed": 2, "skipped": 3, "total": 123,
                              "source": "output"}
    assert "tests" not in second and "source" not in second  # not known: left out, never guessed
    only = client.get(JOBS, params={"state": "passed"}).json()["jobs"]
    assert [j["id"] for j in only] == [passed]


def wait_output(client: TestClient, job: str, stdout: str, stderr: str, timeout: float = 15.0) -> dict[str, Any]:
    """The run once it ended and its output reads in full. Its end can be seen before its output is on disk (a slow
    macOS runner read '' once), so this waits for the output, with a time limit only as the failure."""
    end = time.monotonic() + timeout
    while True:
        body = client.get(f"{JOBS}/{job}").json()
        out = body["output"]
        if body["state"] != "running" and (out["stdout"], out["stderr"]) == (stdout, stderr):
            return body
        assert time.monotonic() < end, f"the run's output never read in full: {body}"
        time.sleep(0.1)


def test_a_run_is_read_from_an_offset_until_its_end(client: TestClient, envs: EnvManager) -> None:
    job = start(client, envs, "printf 'hello '; printf 'world'; printf 'oops' >&2")
    body = wait_output(client, job, "hello world", "oops")
    assert body["output"]["next_stdout_offset"] == 11 and body["output"]["more"] is False
    later = client.get(f"{JOBS}/{job}", params={"stdout_offset": 6, "stderr_offset": 4}).json()
    assert later["output"]["stdout"] == "world" and later["output"]["stderr"] == ""
    assert later["output"]["stdout_offset"] == 6


def test_a_running_run_is_cancelled(client: TestClient, envs: EnvManager) -> None:
    job = start(client, envs, "sleep 30")
    assert client.get(f"{JOBS}/{job}").json()["state"] == "running"
    stopped = client.post(f"{JOBS}/{job}/cancel", json={})
    assert stopped.status_code == 200, stopped.text
    assert wait_ended(client, job)["state"] == "cancelled"


def test_the_branch_and_commit_are_kept_as_the_client_said(client: TestClient, envs: EnvManager) -> None:
    job = start(client, envs, "true", source={"branch": "feature/cart", "commit": "a" * 40, "dirty": True})
    body = wait_ended(client, job)
    assert body["source"] == {"branch": "feature/cart", "commit": "a" * 40, "dirty": True, "told_by": "client"}


def test_an_unknown_run_is_not_found(client: TestClient, envs: EnvManager) -> None:
    assert client.get(f"{JOBS}/job-0000000000").status_code == 404
    assert client.get(f"{JOBS}/not-a-job").status_code == 404


def test_the_source_a_client_sends_is_checked() -> None:
    assert _source({"branch": "main", "commit": "abc1234"}) == {"branch": "main", "commit": "abc1234", "dirty": None}
    for bad in ({"branch": "-x"}, {"commit": "not-hex"}, {"dirty": "yes"}, {"other": 1}, "main"):
        with pytest.raises(InvalidRequest):
            _source(bad)


@pytest.mark.parametrize(("text", "expected"), [
    ("==== 2 failed, 118 passed, 3 skipped in 12.34s ====", ("pytest", 118, 2, 3, 123)),
    ("118 passed, 1 warning in 3.20s", ("pytest", 118, 0, 0, 118)),
    ("\x1b[2m Tests \x1b[22m 2 failed | 118 passed | 3 skipped (123)", ("vitest", 118, 2, 3, 123)),
    ("Tests:       2 failed, 3 skipped, 118 passed, 123 total", ("jest", 118, 2, 3, 123)),
    ("Executed 42 tests, with 1 failure (0 unexpected) in 0.512 (0.600) seconds", ("swift", 41, 1, 0, 42)),
    ("Test run with 12 tests in 3 suites failed after 0.01 seconds with 2 issues.", ("swift", 10, 2, 0, 12)),
    ("=== RUN   TestA\n--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.01s)\n--- SKIP: TestC\nFAIL", ("go", 1, 1, 1, 3)),
])
def test_test_totals_are_read_from_the_runners_summary(text: str, expected: tuple[Any, ...]) -> None:
    got = testsummary.read("some output\n" + text + "\n")
    assert got is not None
    assert (got["runner"], got["passed"], got["failed"], got["skipped"], got["total"]) == expected
    assert got["source"] == "output"


def test_no_summary_is_none() -> None:
    assert testsummary.read("built in 3s\nall done") is None


def test_the_computer_says_what_it_is_doing(client: TestClient, envs: EnvManager) -> None:
    job = start(client, envs, "sleep 30")
    try:
        [row] = client.get("/api/orgs/local/nodes").json()
        assert row["jobs_running"] == 1
        assert isinstance(row["lanes_by_kind"], dict) and isinstance(row["lanes_active"], int)
        assert isinstance(row["approvals_waiting"], int)
        detail = client.get("/api/orgs/local/nodes/dev").json()
        assert detail["jobs_running"] == 1
    finally:
        client.post(f"{JOBS}/{job}/cancel", json={})
        wait_ended(client, job)


def test_vms_are_read_only_and_never_name_a_grant(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Vms:
        def list(self, *, include_past: bool = False, limit: int = 30) -> list[dict[str, Any]]:
            return [{"id": "vm_1", "lane": "dev/ci", "state": "ready", "profile": "small", "server_type": "cpx21",
                     "location": "hel1", "created_at": "2026-11-13T08:00:00+00:00", "ready_at": None,
                     "expires_at": "2026-11-13T09:00:00+00:00", "minutes_left": 40, "price_hourly_eur": 0.012,
                     "cost_eur_so_far": 0.01, "purpose": "run the shop tests", "created_by": "grant_abc",
                     "host": "203.0.113.9"}]

    api = client.app.state.api
    monkeypatch.setattr(api, "optional", lambda name: Vms() if name == "vms" else None)
    [vm] = client.get("/api/orgs/local/vms").json()["vms"]
    assert vm["started_by"] == "An agent" and "grant_abc" not in str(vm) and "host" not in vm
    monkeypatch.setattr(api, "optional", lambda name: None)
    body = client.get("/api/orgs/local/vms").json()
    assert body["available"] is False and body["vms"] == []


def test_the_summary_of_a_finished_run_is_kept(client: TestClient, envs: EnvManager) -> None:
    job = start(client, envs, f"printf '{PYTEST_OUT}'")
    wait_ended(client, job)
    first = client.get(JOBS).json()["jobs"][0]["tests"]
    assert client.get(JOBS).json()["jobs"][0]["tests"] == first
    assert asyncio.iscoroutinefunction(envs.job_read)
