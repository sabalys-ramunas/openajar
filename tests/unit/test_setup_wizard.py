"""`openajar setup`, the guided setup of local mode, run end to end with fakes.

Every outside thing is a fake: the Hetzner and GitHub APIs, gh, docker, the local service and its
registration of Claude Code and Codex (openajar.local, built by another builder), and the test VM.
HOME and OPENAJAR_HOME are temp folders, so nothing touches this computer's real setup. The vault
is a real one, in the temp home.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core import webapp
from openajar.setup import steps, webstep

HZ_TOKEN = "hz_" + "h" * 40
GH_TOKEN = "gho_" + "g" * 36
PASTED = "github_pat_" + "p" * 40


@dataclass
class State:
    running: bool
    pid: int | None = None
    port: int = 7410
    url: str = "http://127.0.0.1:7410/mcp"
    log_path: Path | None = None
    note: str = ""


@dataclass
class World:
    """What the fakes saw and what they answer."""
    home: Path
    hetzner_ok: set = field(default_factory=lambda: {HZ_TOKEN})
    docker: tuple = (True, True, False)
    gh_installed: bool = True
    gh_signed_in: bool = True
    github_users: dict = field(default_factory=lambda: {GH_TOKEN: "octo", PASTED: "octo"})
    running: bool = False
    snapshot: bool | None = True  # the Hetzner project has the dev snapshot (None: Hetzner did not say)
    snapshot_fails: str | None = None
    web_version: str = "0.0.0"  # set to openajar's own version by the fixture
    web_fails: str | None = None
    calls: list = field(default_factory=list)

    # the outside world
    def hetzner_check(self, token, api_url):
        self.calls.append(("hetzner_check", token == HZ_TOKEN))
        return None if token in self.hetzner_ok else "Hetzner: unauthorized: the token was refused"

    def dev_snapshot_state(self, home):
        self.calls.append(("snapshot?",))
        return self.snapshot

    def build_dev_snapshot(self, home, say):
        self.calls.append(("build snapshot",))
        if self.snapshot_fails:
            from openajar.vms.models import VmError

            raise VmError(self.snapshot_fails)
        say("creating build VM vm_1 (cx23 in nbg1 from ubuntu-24.04)")
        self.snapshot = True
        return 0.0123

    def docker_state(self, docker_bin, image):
        return self.docker

    def get_docker_image(self, image, docker_bin, pull):
        self.calls.append(("build", image))
        self.docker = (True, True, True)
        return True

    def gh_available(self):
        return self.gh_installed

    def gh(self, *args, interactive=False):
        self.calls.append(("gh", args[:2], interactive))
        if args[:2] == ("auth", "status"):
            return (0, "github.com\n  ✓ Logged in to github.com account octo (keyring)\n") if self.gh_signed_in \
                else (1, "You are not logged into any GitHub hosts.")
        if args[:2] == ("auth", "login"):
            self.gh_signed_in = True
            return 0, ""
        if args[:2] == ("auth", "token"):
            return (0, GH_TOKEN + "\n") if self.gh_signed_in else (1, "")
        raise AssertionError(args)

    def github_user(self, token):
        self.calls.append(("github_user",))
        user = self.github_users.get(token)
        return (user, None) if user else (None, "GitHub refused it (expired, revoked or mistyped)")

    def get_web_app(self, home):
        """The release's web app, as openajar.setup.webapp.install leaves it (tests/unit/test_web_install.py
        tests the real download)."""
        from openajar.setup.webapp import Installed, WebAppMissing, _write_state

        self.calls.append(("web install",))
        if self.web_fails:
            raise WebAppMissing(self.web_fails)
        app_dir = webapp.installed_dir(home)
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "index.html").write_text("<!doctype html><title>openajar</title>")
        _write_state(home, version=self.web_version, sha256="0" * 64, wanted=True)
        return Installed(version=self.web_version, sha256="0" * 64, files=1, path=app_dir)

    def open_test_vm(self, home):
        self.calls.append(("test_vm",))
        return "Linux openajar-vm 6.8.0-49-generic #49-Ubuntu SMP x86_64 GNU/Linux", 0.0106

    # openajar.local (the contract of builder L1a)
    def status(self, home):
        return State(running=self.running, log_path=home / "logs" / "service.log")

    def start(self, home, port=None):
        self.calls.append(("start", port))
        self.running = True
        return self.status(home)

    def stop(self, home):
        self.calls.append(("stop",))
        self.running = False
        return self.status(home)

    def register_clients(self, home, port, *, claude=True, codex=True, skills=True):
        self.calls.append(("register", port, claude, codex, skills))
        return ["Agent grant g_1 issued for claude-code@laptop: every workspace, up to write, specials none",
                "Token stored in macOS Keychain (service openajar, account openajar-local)"] + [
            w for w, on in (("Claude Code: added openajar to ~/.claude.json", claude),
                                ("Codex: added openajar to ~/.codex/config.toml", codex),
                                ("Skills: openajar-environment", skills)) if on]


@pytest.fixture
def world(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("HOME", str(user))  # nothing reaches the real ~/.claude.json, ~/.codex, ~/.openajar
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    w = World(home)
    for name in ("hetzner_check", "docker_state", "get_docker_image", "gh_available", "gh", "github_user",
                 "open_test_vm", "dev_snapshot_state", "build_dev_snapshot"):
        monkeypatch.setattr(steps, name, getattr(w, name))
    modules = {"service": SimpleNamespace(status=w.status, start=w.start, stop=w.stop),
               "register": SimpleNamespace(register_clients=w.register_clients),
               "paths": SimpleNamespace(default_home=lambda: user / ".openajar", DEFAULT_PORT=7410)}
    monkeypatch.setattr(steps, "local", lambda name: modules[name])
    _keep_fields(monkeypatch)
    # The web app: the tests run as from a checkout that built it, so the web step asks nothing (the
    # web step's own tests below take the build away).
    from openajar import __version__

    w.web_version = __version__
    dist = tmp_path / "checkout" / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>openajar</title>")
    monkeypatch.delenv("OPENAJAR_PANEL_DIR", raising=False)
    monkeypatch.delenv("OPENAJAR_NO_WEB", raising=False)
    monkeypatch.setattr(webapp, "checkout_dist", lambda: dist)
    monkeypatch.setattr(webstep, "get_web_app", w.get_web_app)
    return w


def _keep_fields(monkeypatch):
    """environments.toml's keep keys (default_keep, allow_keep_live) come with kept environments
    (openajar.environments.models). Until that model has them, the tests read and write them on a model
    that does, so the step is tested as it runs with them."""
    from typing import Literal

    from openajar.environments import models

    if "default_keep" in models.EnvSettings.model_fields:
        return

    class WithKeep(models.EnvSettings):
        default_keep: Literal["none", "save", "live"] = "none"
        allow_keep_live: bool = False

    monkeypatch.setattr(models, "EnvSettings", WithKeep)


def run(*args, input=None):
    return CliRunner().invoke(app, ["setup", *args], input=input, prog_name="openajar")


def _no_secrets(out: str) -> None:
    for token in (HZ_TOKEN, GH_TOKEN, PASTED):
        assert token not in out
    assert "—" not in out  # never an em dash in what a person reads


def test_quick_start_with_hetzner_and_gh(world):
    home = world.home
    # quick start; Hetzner; the token; gh's login; yes to any repository; yes to the test VM
    r = run(input="1\n1\n" + HZ_TOKEN + "\n1\n\n\n\ny\n")  # the keep and ssh questions take their defaults
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert "Welcome to openajar" in out and "about EUR 0.011 an hour" in out
    assert "nothing until a VM opens" in out
    for n, title in enumerate(("VMs", "GitHub", "Limits", "Your agents", "The web app", "SSH from your shell",
                               "Health check", "A test VM"), 1):
        assert f"Step {n} of 8: {title}" in out
    assert "The web app is served from this checkout's own build" in out
    # ssh openajar-<id> from the person's shell: one Include at the top of ~/.ssh/config
    ssh_config = Path(os.environ["HOME"]) / ".ssh" / "config"
    assert ssh_config.read_text().splitlines()[1] == f"Include {home}/ssh/config"
    # VMs: the token where `openajar vm setup` keeps it, 0600; vms.toml says hetzner
    token_file = home / "secrets" / "hetzner.token"
    assert token_file.read_text().strip() == HZ_TOKEN and oct(token_file.stat().st_mode & 0o777) == "0o600"
    vms = tomllib.loads((home / "vms.toml").read_text())
    assert vms["provider"] == "hetzner" and vms["location"] == "nbg1"
    # GitHub: gh's token, sealed in the vault, any repository on
    saved = json.loads((home / "git" / "login.json").read_text())
    assert saved["login"] == "octo" and saved["source"] == "gh" and saved["any_repo"] is True
    assert GH_TOKEN not in (home / "git" / "login.json").read_text()
    assert (home / "vault.db").exists() and (home / "secrets" / "vault.key").exists()
    assert "When gh signs in again or the token changes, run `openajar setup github` again" in out
    # limits: the low local defaults
    env = tomllib.loads((home / "environments.toml").read_text())
    assert env["monthly_eur"] == 10 and env["max_hours"] == 4 and env["max_live"] == 2
    # saves when idle by default (offered first), and never kept running unless the owner says so
    assert env["default_keep"] == "save" and "allow_keep_live" not in env
    assert "How should an environment stay after the work?" in out and "Single use" in out
    assert out.index("1  Saves when idle (recommended)") < out.index("2  Single use")
    # the service started, both agents and the skills connected on its port
    assert ("start", None) in world.calls and ("register", 7410, True, True, True) in world.calls
    assert "runs on this computer at http://127.0.0.1:7410/mcp" in out
    assert "Agent grant" not in out and "Token stored in macOS Keychain" in out
    # the check, the test VM, the summary and the one next step
    assert "ready  Service  running at http://127.0.0.1:7410/mcp" in out
    assert "ready  GitHub   octo (from gh): any repository it can reach" in out
    assert "ready  Agents   connected: Claude Code, Codex, Skills" in out
    assert ("test_vm",) in world.calls and "It answered: Linux openajar-vm" in out and "EUR 0.011" in out
    assert "All set. Nothing bills until an agent opens a VM." in out
    assert "Next: start a new Claude Code session and ask:" in out
    assert "work in openajar on github.com/octo/<repo>" in out


def test_the_vault_holds_the_gh_token_for_the_gateway(world):
    import asyncio

    from openajar.commands.base import cli_runtime
    from openajar.git import keys as keymod
    from openajar.git import login

    r = run("github", input="1\n\n")
    assert r.exit_code == 0, r.output
    vault = cli_runtime().get("vault")
    assert asyncio.run(vault.get_secret(login.REF, purpose="test")) == GH_TOKEN
    [cred] = [c for c in (keymod.from_meta(m) for m in asyncio.run(vault.list_secrets())) if c]
    assert cred.any_repo and cred.host == "github.com"


def test_step_by_step_asks_each_setting(world):
    home = world.home
    world.docker = (False, False, False)
    # step by step; Hetzner; token; Helsinki; medium; no gh: paste a token, no to any repository;
    # limits 25, 6, 3; single use; Claude Code yes, Codex no, skills yes; no test VM
    world.gh_installed = False
    r = run(input="2\n1\n" + HZ_TOKEN + "\n3\n2\n1\n" + PASTED + "\nn\n25\n6\n3\n2\ny\nn\ny\nn\nn\n")
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    vms = tomllib.loads((home / "vms.toml").read_text())
    assert vms["location"] == "hel1" and vms["default_profile"] == "medium"
    assert vms["profiles"]["medium"]["min_cpu"] == 4 and "small" in vms["profiles"]
    saved = json.loads((home / "git" / "login.json").read_text())
    assert saved["source"] == "token" and saved["any_repo"] is False
    assert "Connect repositories one by one: openajar environments repo add github.com/<owner>/<name>" in out
    env = tomllib.loads((home / "environments.toml").read_text())
    assert (env["monthly_eur"], env["max_hours"], env["max_live"]) == (25, 6, 3)
    assert env["default_keep"] == "none" and "allow_keep_live" not in env
    assert "Environments: single use, deleted when the work is done." in out
    assert ("register", 7410, True, False, True) in world.calls
    assert ("test_vm",) not in world.calls and "No test VM" in out


def test_yes_with_docker_asks_nothing_and_never_opens_a_test_vm(world):
    world.gh_signed_in = False
    r = run("--yes", "--docker", input="")
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert tomllib.loads((world.home / "vms.toml").read_text())["provider"] == "docker"
    assert ("build", "agentos-env:latest") in world.calls
    assert "public repositories only" in out and not (world.home / "git" / "login.json").exists()
    assert ("test_vm",) not in world.calls and ("register", 7410, True, True, True) in world.calls
    assert "Choose" not in out  # no question was asked


def test_yes_reads_the_tokens_from_files(world, tmp_path):
    hz, ghf = tmp_path / "hz", tmp_path / "gh"
    hz.write_text(HZ_TOKEN + "\n")
    ghf.write_text(PASTED + "\n")
    r = run("--yes", "--hetzner-token-file", str(hz), "--github-token-file", str(ghf))
    assert r.exit_code == 0, r.output
    _no_secrets(r.output)
    assert (world.home / "secrets" / "hetzner.token").read_text().strip() == HZ_TOKEN
    saved = json.loads((world.home / "git" / "login.json").read_text())
    assert saved["source"] == "token" and saved["any_repo"] is True  # yes is the default in local mode


def test_a_refused_hetzner_token_is_asked_again_and_never_saved(world):
    r = run("vms", input="1\nnot-a-token\n" + HZ_TOKEN + "\n\n\n")  # then the location and size defaults
    out = r.output
    assert r.exit_code == 0, out
    assert "That token did not work: Hetzner: unauthorized" in out and "not-a-token" not in out
    assert (world.home / "secrets" / "hetzner.token").read_text().strip() == HZ_TOKEN


def test_hetzner_offers_the_dev_snapshot_and_says_what_it_costs(world):
    world.snapshot = False
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\ny\n")  # token, location, size, yes to the snapshot
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert "each VM installs the browser when it opens. That adds 2 to 4 minutes" in out
    assert "a few minutes and costs a cent or two" in out and "about 4 GB" in out and "per GB a month" in out
    assert ("build snapshot",) in world.calls and "  creating build VM vm_1" in out
    assert "The dev snapshot is built" in out and "EUR 0.012" in out


def test_no_to_the_dev_snapshot_builds_nothing_and_says_how_to_later(world):
    world.snapshot = False
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\nn\n")
    assert r.exit_code == 0, r.output
    assert ("build snapshot",) not in world.calls
    assert "`openajar vm image build dev`" in r.output and "—" not in r.output


def test_yes_never_builds_the_dev_snapshot(world, tmp_path):
    world.snapshot = False
    hz = tmp_path / "hz"
    hz.write_text(HZ_TOKEN + "\n")
    r = run("--yes", "--hetzner-token-file", str(hz))
    assert r.exit_code == 0, r.output
    assert ("build snapshot",) not in world.calls and "No snapshot for now" in r.output


def test_a_dev_snapshot_there_already_is_not_offered_again(world):
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\n")
    assert r.exit_code == 0, r.output
    assert "The dev snapshot is there" in r.output and ("build snapshot",) not in world.calls


def test_a_failed_dev_snapshot_build_says_environments_still_work(world):
    world.snapshot, world.snapshot_fails = False, "the setup script failed with exit code 100: E: no xvfb"
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\ny\n")
    assert r.exit_code == 0, r.output
    assert "The dev snapshot was not built: the setup script failed" in r.output
    assert "Environments still work" in r.output


def test_hetzner_that_cannot_say_about_the_snapshot_offers_nothing(world):
    world.snapshot = None
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\n")
    assert r.exit_code == 0, r.output
    assert "could not check for the dev snapshot" in r.output and ("build snapshot",) not in world.calls


def test_docker_says_its_image_has_the_browser(world):
    r = run("--yes", "--docker", input="")
    assert r.exit_code == 0, r.output
    assert "The machine image has each environment's browser" in r.output
    assert ("snapshot?",) not in world.calls


def test_skipping_the_hetzner_token_saves_nothing(world):
    r = run("vms", input="1\n\n")
    assert r.exit_code == 0, r.output
    assert "No Hetzner token saved" in r.output and not (world.home / "secrets" / "hetzner.token").exists()


def test_a_refused_github_token_saves_nothing(world):
    world.gh_installed = False
    r = run("github", input="1\nghp_wrongwrongwrong\n")
    assert r.exit_code == 0, r.output
    assert "That token did not work: GitHub refused it" in r.output and "ghp_wrong" not in r.output
    assert not (world.home / "git" / "login.json").exists()


def test_gh_login_runs_interactively_when_not_signed_in(world):
    world.gh_signed_in = False
    r = run("github", input="1\n\n")
    assert r.exit_code == 0, r.output
    assert ("gh", ("auth", "login"), True) in world.calls
    assert json.loads((world.home / "git" / "login.json").read_text())["source"] == "gh"


def test_github_again_keeps_or_removes_the_saved_login(world):
    assert run("github", input="1\n\n").exit_code == 0
    kept = run("github", input="1\nn\n")  # keep it, but no to any repository now
    assert kept.exit_code == 0, kept.output
    assert "Keep the saved login (octo)" in kept.output
    assert json.loads((world.home / "git" / "login.json").read_text())["any_repo"] is False
    gone = run("github", input="5\n")  # keep, gh, token, skip, remove
    assert gone.exit_code == 0, gone.output
    assert "Removed the saved GitHub login" in gone.output and not (world.home / "git" / "login.json").exists()


def test_vms_again_restarts_a_running_service(world):
    world.running = True
    r = run("vms", "--docker", input="\nn\n")  # Docker (the default with --docker), no image build
    assert r.exit_code == 0, r.output
    assert ("stop",) in world.calls and "Restarted the openajar service" in r.output


def test_without_the_local_runtime_it_says_so(world, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(f"No module named 'openajar.local.{name}'", name=f"openajar.local.{name}")

    monkeypatch.setattr(steps, "local", missing)
    r = run("agents")
    assert r.exit_code == 0 and "not part of this build of openajar yet" in r.output
    r = run("check")
    assert "to do  Service  not part of this build yet" in r.output


def test_check_says_each_piece_in_plain_words(world):
    r = run("check")
    out = r.output
    assert r.exit_code == 0, out
    assert "to do  Service  not running: `openajar setup agents` starts it" in out
    assert "to do  VMs      not set up: openajar setup vms" in out
    assert "note   GitHub   no login: public repositories only" in out
    assert "ready  Limits" in out


def test_stopping_early_says_how_to_go_on(world):
    r = run(input="1\n")  # quick start, then the input ends
    assert r.exit_code == 1 and "Setup stopped" in r.output and "openajar setup --yes" in r.output


def test_an_unknown_step_is_refused(world):
    r = run("browsers")
    assert r.exit_code == 2 and "is not a step" in r.output


def test_home_defaults_to_the_local_one(world, monkeypatch):
    monkeypatch.delenv("OPENAJAR_HOME")
    r = run("limits", "--yes")
    assert r.exit_code == 0, r.output
    assert (Path(os.environ["HOME"]) / ".openajar" / "environments.toml").exists()


# ------------------------------------------------------------------ the snapshot's outside calls, with a fake Hetzner


def test_the_snapshot_check_reads_hetzner_and_tells_missing_from_unknown(tmp_path, monkeypatch):
    from openajar.core.errors import ServiceUnavailable
    from openajar.vms.manager import VmManager
    from openajar.vms.models import VmError

    answers: list = []

    class Hetzner:
        async def resolve_image(self, image):
            assert image == "snapshot:dev"
            if answers[0] is not None:
                raise answers[0]
            return "123"

    monkeypatch.setattr(VmManager, "provider", lambda self: Hetzner())
    for answer, state in ((None, True), (VmError("no snapshot named dev; build it"), False),
                          (ServiceUnavailable("Hetzner did not answer"), None)):
        answers[:] = [answer]
        assert steps.dev_snapshot_state(tmp_path) is state


def test_the_snapshot_build_is_vm_image_build_dev_with_the_browser(tmp_path, monkeypatch):
    from openajar.environments import machine
    from openajar.environments.vmbrowser import desktop_install_script, install_script
    from openajar.vms.manager import VmManager, image_script

    seen = {}

    async def build_image(self, name, *, profile=None, script=None, on_step=None):
        seen.update(name=name, script=script, follow_ip=self.follow_ip)
        on_step("taking the snapshot")
        return {"image": name, "cost_eur": 0.02}

    monkeypatch.setattr(VmManager, "build_image", build_image)
    said: list = []
    assert steps.build_dev_snapshot(tmp_path, said.append) == 0.02
    # the environment's setup too: a VM from the snapshot has its marker and pnpm, so its setup check is one
    # call; and no updates on their own mid-session (they are applied while the image is built)
    # and its desktop: the snapshot holds the screen's desktop too, so a machine from it has nothing to install
    script = image_script(machine.UPDATES_OFF + machine.setup_install() + install_script() + desktop_install_script())
    assert script == machine.dev_image_script()
    assert seen == {"name": "dev", "script": script, "follow_ip": True}
    assert script.index("OPENAJAR_SETUP_SCRIPT") < script.index("cloud-init clean")
    assert said == ["taking the snapshot"]


def test_keep_running_is_allowed_only_after_the_cost_is_said_and_confirmed(world):
    # the limits as they are, the third option, then no to the cost: it saves when idle and live stays off
    keep = "\n\n\n"
    r = run("limits", input=keep + "3\nn\n")
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert "about EUR 7.80 a month" in out and "Not allowed." in out
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "save" and "allow_keep_live" not in env
    # again, and yes: allowed, while the default stays save (an agent keeps one running only when asked)
    r = run("limits", input=keep + "3\ny\n")
    assert r.exit_code == 0, r.output
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "save" and env["allow_keep_live"] is True
    assert "kept running when you ask (it bills every hour)" in r.output
    # again: the default is what is set now, so Enter keeps it and nothing asks about the cost
    r = run("limits", input=keep + "\n")
    assert r.exit_code == 0 and "about EUR 7.80" not in r.output
    assert tomllib.loads((world.home / "environments.toml").read_text())["allow_keep_live"] is True
    # single use now: live is turned off with it
    r = run("limits", input=keep + "2\n")
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "none" and env["allow_keep_live"] is False


def test_yes_saves_when_idle_and_asks_nothing_about_keeping(world):
    r = run("--yes", "--docker", input="")
    assert r.exit_code == 0, r.output
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "save" and "allow_keep_live" not in env
    assert "Keeping  saved when idle" in r.output


def test_ssh_adds_the_include_once_and_keeps_the_rest(world):
    """`openajar setup ssh`: asked once, default yes, the line at the top, the person's config kept as it was."""
    ssh_config = Path(os.environ["HOME"]) / ".ssh" / "config"
    ssh_config.parent.mkdir(mode=0o700)
    ssh_config.write_text("Host *\n  ForwardAgent yes\n")
    r = run("ssh", input="\n")
    assert r.exit_code == 0, r.output
    _no_secrets(r.output)
    assert "Include " in r.output and "Added." in r.output
    text = ssh_config.read_text()
    assert text.splitlines()[1] == f"Include {world.home}/ssh/config" and text.endswith("Host *\n  ForwardAgent yes\n")
    again = run("ssh", input="")
    assert again.exit_code == 0 and "reaches your environments already" in again.output
    assert ssh_config.read_text() == text


def test_no_to_ssh_changes_nothing_and_says_the_other_command(world):
    r = run("ssh", input="n\n")
    assert r.exit_code == 0, r.output
    assert not (Path(os.environ["HOME"]) / ".ssh" / "config").exists()
    assert f"ssh -F {world.home}/ssh/config openajar-<id>" in r.output


# ---- the web app step (openajar.setup.webstep): the release's prebuilt copy, default yes


@pytest.fixture
def no_build(world, tmp_path, monkeypatch):
    """An installed openajar: no checkout build beside the code, so the step offers the release's copy."""
    monkeypatch.setattr(webapp, "checkout_dist", lambda: tmp_path / "no-checkout" / "web" / "dist")
    return world


def test_the_web_step_adds_the_app_by_default(no_build):
    r = run("web", input="\n")
    assert r.exit_code == 0, r.output
    assert "Add the web app" in r.output and "Added the web app" in r.output and "sha256 checked" in r.output
    assert ("web install",) in no_build.calls
    st = webapp.status(no_build.home)
    assert st.source == "installed" and st.installed == no_build.web_version and not st.mismatched
    _no_secrets(r.output)


def test_a_no_is_remembered_and_yes_mode_then_leaves_it_out(no_build):
    r = run("web", input="n\n")
    assert r.exit_code == 0 and "No web app" in r.output and "openajar setup web" in r.output
    assert webapp.status(no_build.home).wanted is False
    assert ("web install",) not in no_build.calls
    r = run("--yes", "--docker", input="")  # the question's default is now the earlier no
    assert r.exit_code == 0, r.output
    assert ("web install",) not in no_build.calls
    assert "not installed (you said no)" in r.output  # the health check's line


def test_no_web_skips_the_step_without_a_question(no_build):
    r = run("--yes", "--docker", "--no-web", input="")
    assert r.exit_code == 0, r.output
    assert "Skipped the web app (--no-web)" in r.output and ("web install",) not in no_build.calls
    assert webapp.status(no_build.home).wanted is None  # a skip is not a no: the next setup asks again


def test_yes_mode_adds_the_app(no_build):
    r = run("--yes", "--docker", input="")
    assert r.exit_code == 0, r.output
    assert ("web install",) in no_build.calls and "Added the web app" in r.output


def test_an_app_from_another_release_is_updated_without_a_question(no_build):
    no_build.get_web_app(no_build.home)
    from openajar.setup.webapp import _write_state

    _write_state(no_build.home, version="0.0.1", sha256="0" * 64, wanted=True)
    assert webapp.status(no_build.home).mismatched
    r = run("check", input="")
    assert "to do" in r.output and "version 0.0.1 is installed, but this openajar is" in r.output
    r = run("web", input="")
    assert r.exit_code == 0 and "is 0.0.1, and this openajar is" in r.output and "updating it" in r.output
    assert not webapp.status(no_build.home).mismatched


def test_step_by_step_can_remove_the_app(no_build):
    run("web", input="\n")
    r = run("web", input="n\n")  # "Keep it?" no
    assert r.exit_code == 0 and "Removed" in r.output
    st = webapp.status(no_build.home)
    assert st.source == "none" and st.wanted is False and not webapp.installed_dir(no_build.home).exists()


def test_no_network_says_so_and_setup_goes_on(no_build):
    no_build.web_fails = "could not reach the release: no network"
    r = run("--yes", "--docker", input="")
    assert r.exit_code == 0, r.output
    assert "Could not add the web app: could not reach the release" in r.output
    assert "The node works without it" in r.output and "the web app" in r.output.split("Still to do:")[1]


def test_your_own_machine_is_offered_and_says_the_one_command(world):
    """No machine yet: setup names the command that brings one in, and changes nothing."""
    r = run("vms", input="own\n")
    assert r.exit_code == 0, r.output
    assert "Your own machine: a Linux box you reach over ssh" in r.output
    assert "openajar environments machine add NAME user@host" in r.output
    assert not (world.home / "vms.toml").exists()
    _no_secrets(r.output)


def test_a_machine_already_brought_in_becomes_where_environments_open(world):
    from openajar.vms import own

    world.home.mkdir(mode=0o700, exist_ok=True)
    own.save(world.home, own.Machine(name="homebox", host="192.168.1.20", user="me"))
    r = run("vms", input="own\n")
    assert r.exit_code == 0, r.output
    assert "Environments open as containers on your machine homebox" in r.output
    vms = tomllib.loads((world.home / "vms.toml").read_text())
    assert (vms["provider"], vms["location"]) == ("own", "homebox")
    again = run("vms", input="\n")  # asked again: your own machine is the default now
    assert again.exit_code == 0 and "your machine homebox" in again.output


def test_digitalocean_is_offered_as_experimental_and_says_so_before_it_asks(world, monkeypatch):
    from openajar.setup import digitalocean as step

    seen = []
    monkeypatch.setattr(step, "digitalocean_check", lambda token, api_url: seen.append(token) or None)
    made_up = "dop_v1_" + "x" * 24  # put together here: not a real token
    r = run("vms", input=f"digitalocean\n{made_up}\nn\n")
    assert r.exit_code == 0, r.output
    assert "DigitalOcean (experimental, not thoroughly tested)" in r.output
    assert "DigitalOcean is experimental in this version" in r.output
    assert r.output.index("DigitalOcean is experimental in this version") < r.output.index("Paste the DigitalOcean token")
    assert seen == [made_up] and made_up not in r.output
    assert tomllib.loads((world.home / "vms.toml").read_text())["provider"] == "digitalocean"
    path = world.home / "secrets" / "digitalocean.token"
    assert path.read_text().strip() == made_up and oct(path.stat().st_mode & 0o777) == "0o600"
    _no_secrets(r.output)
