"""How fast an environment comes up after its VM answers (openajar.environments.bringup, machine, setup.sh).

A live open from the `dev` snapshot spent 43 s in the setup check and 17 s in the tools top-up
(2026-11-16). The snapshot never ran setup.sh, so it had no ready marker: the node waited for cloud-init
in 5 s polls, then ran the whole setup (apt-get update and install, then corepack's pnpm). Now:

- the snapshot build runs setup.sh, so a new snapshot has the marker and pnpm (one call);
- on an older snapshot the check waits for cloud-init inside one call (`cloud-init status --wait`),
  and setup.sh runs apt only for a package that is missing;
- the tools top-up runs only where the check found no pnpm.

The scripts run here for real with stand-ins for cloud-init, dpkg-query and apt-get on PATH and the
machine's folders moved under tmp_path."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from test_environments import node, opened  # noqa: F401  (node is a fixture)
from testkit.timing import budget

from openajar.environments import bringup, machine


def stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)


def sh(script: str, bin_dir: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", **env})


# ------------------------------------------------------------------ the setup check


def test_the_check_answers_as_soon_as_cloud_init_is_done(tmp_path):
    """cloud-init is done 0.4 s into the check: the answer comes then, not at a later poll."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "timeout", 'shift; exec "$@"')
    stub(bin_dir, "cloud-init", 'case "$*" in *--wait*) sleep 0.4 ;; esac; echo "status: done"')
    script = machine.SETUP_STATE.replace(machine.READY_MARKER, str(tmp_path / "ready"))
    t = time.monotonic()
    out = sh(script, bin_dir)
    took = time.monotonic() - t
    assert out.stdout.splitlines()[0] == "status: done" and 0.4 <= took < budget(2.0), (out, took)  # not SETUP_WAIT_S (20 s)
    assert machine.boot_done_without_setup(out.stdout.splitlines()[0])
    assert bringup.SETUP_POLL_S <= 1.0


def test_the_check_says_whether_pnpm_is_there(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "ready").write_text("2026-11-16T00:00:00Z\n")
    script = machine.SETUP_STATE.replace(machine.READY_MARKER, str(tmp_path / "ready"))
    without = sh(script, bin_dir).stdout
    assert without.splitlines() == ["ready", "tools: no pnpm"] and machine.needs_pnpm(without)
    stub(bin_dir, "pnpm", "exit 0")
    with_it = sh(script, bin_dir).stdout
    assert with_it.splitlines() == ["ready", "tools: pnpm"] and not machine.needs_pnpm(with_it)
    assert machine.needs_pnpm("ready\n")  # an answer that does not say: top it up, as before


async def test_no_top_up_where_the_machine_has_pnpm(node, caplog):  # noqa: F811
    node["machine"].setup = "ready\ntools: pnpm"
    with caplog.at_level(logging.INFO, logger="openajar.environments"):
        out = await opened(node, repo="example.test/me/shop")
    assert out["state"] == "ready"
    ran = [c for c, _ in node["machine"].ran]
    assert machine.TOOLS_TOP_UP not in ran and ran.count(machine.SETUP_STATE) == 1
    said = [r.getMessage() for r in caplog.records]
    assert not any(": tools top-up took " in m for m in said)
    assert any(": apt ready took " in m for m in said)  # now beside the tunnels, the browser and the clone


async def test_the_top_up_runs_where_pnpm_is_missing(node):  # noqa: F811
    node["machine"].setup = "ready\ntools: no pnpm"
    await opened(node, repo="example.test/me/shop")
    assert machine.TOOLS_TOP_UP in [c for c, _ in node["machine"].ran]


# ------------------------------------------------------------------ setup.sh on a machine that has it all


def _setup_here(tmp_path: Path, installed: set[str]) -> tuple[subprocess.CompletedProcess, Path]:
    root = tmp_path / "root"
    for d in ("etc", "var/lib", "work"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "etc" / "environment").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls"
    stub(bin_dir, "dpkg-query", 'for a; do p=$a; done; case " $INSTALLED " in *" $p "*) printf installed ;; '
                                '*) echo "dpkg-query: no packages found matching $p" >&2; exit 1 ;; esac')
    for name in ("apt-get", "corepack", "curl"):
        stub(bin_dir, name, f'echo "{name} $*" >> {log}')
    stub(bin_dir, "node", "echo v22.11.0")
    stub(bin_dir, "pnpm", "exit 0")
    stub(bin_dir, "uv", "exit 0")
    # one pass: a root under any ".../worktrees/..." must not have its own "/work" moved again
    text = re.sub(r"/etc/|/var/lib/|/work", lambda m: str(root) + m.group(0), machine.setup_script())
    out = sh(text, bin_dir, INSTALLED=" ".join(sorted(installed)))
    return out, log


PKGS = {"ca-certificates", "curl", "git", "build-essential", "pkg-config", "python3", "python3-venv", "iproute2",
        "procps", "jq", "unzip", "rsync", "less"}


def test_setup_skips_apt_when_every_package_is_there(tmp_path):
    out, log = _setup_here(tmp_path, PKGS)
    assert out.returncode == 0, out.stderr
    assert not log.exists() or "apt-get" not in log.read_text()
    assert (tmp_path / "root" / "var/lib/agentos-env/ready").exists()


def test_setup_still_installs_a_missing_package(tmp_path):
    out, log = _setup_here(tmp_path, PKGS - {"jq"})
    assert out.returncode == 0, out.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "apt-get update -q" and calls[1].startswith("apt-get install -y -q") and " jq" in calls[1]
    assert calls[-1] == "apt-get clean"
