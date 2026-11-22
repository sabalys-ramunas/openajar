"""What still answers to the names before the rename (docs/LEGACY-NAMES.md; legacy-name throughout).

Everything here runs against a temporary HOME and fakes: no test reaches this computer's Claude Code,
Codex, launchd, systemd or keychain."""

# legacy-name: begin (this file names the old identifiers on purpose; scripts/rename_to_openajar.py keeps them)
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from openajar.client import skills
from openajar.client.claude import ClaudeCode
from openajar.client.codex import CodexConfig
from openajar.client.state import Paths
from openajar.core import config as core_config
from openajar.local import service
from openajar.ops import backup

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


# ---- the variables, the home folder, the config folder


def _env_of(**env: str) -> dict:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAJAR_", "AGENTOS_"))}  # legacy-name
    out = subprocess.run([sys.executable, "-c", "import openajar, json, os; print(json.dumps(dict(os.environ)))"],
                         env={**clean, **env}, capture_output=True, text=True, check=True).stdout
    import json
    return json.loads(out)


def test_an_old_variable_is_read_when_the_new_one_is_not_set():
    assert _env_of(AGENTOS_HOME="/old")["OPENAJAR_HOME"] == "/old"  # legacy-name
    assert _env_of(AGENTOS_HOME="/old", OPENAJAR_HOME="/new")["OPENAJAR_HOME"] == "/new"  # legacy-name
    assert _env_of(AGENTOS_TEST_SLOT="53")["OPENAJAR_TEST_SLOT"] == "53"  # legacy-name


def test_the_home_is_the_old_folder_as_it_is_when_only_that_one_exists(user_home):
    assert core_config.local_home() == user_home / ".openajar"
    (user_home / ".agentos").mkdir()  # legacy-name
    assert core_config.local_home() == user_home / ".agentos"  # legacy-name
    from openajar.local.paths import default_home
    assert default_home() == user_home / ".agentos"  # legacy-name
    (user_home / ".openajar").mkdir()
    assert core_config.local_home() == user_home / ".openajar"


def test_the_config_folder_is_the_old_one_as_it_is_when_only_that_one_exists(user_home):
    paths = Paths(user_home)
    assert paths.config_dir == user_home / ".config" / "openajar"
    (user_home / ".config" / "agentos" / "connections").mkdir(parents=True)  # legacy-name
    assert paths.connections_dir == user_home / ".config" / "agentos" / "connections"  # legacy-name


def test_a_home_made_before_the_rename_opens_with_its_environments(user_home, monkeypatch):
    """A v0.3.0-shaped home: node.toml and environments.db under ~/.agentos are found without moving them."""
    from openajar.core.config import home_from_env
    from openajar.environments.store import EnvironmentStore
    from openajar.local import home as local_home

    old = user_home / ".agentos"  # legacy-name
    local_home.ensure(old, 7410)
    EnvironmentStore(old / "environments.db")
    monkeypatch.delenv("OPENAJAR_HOME", raising=False)
    monkeypatch.delenv("AGENTOS_HOME", raising=False)  # legacy-name
    monkeypatch.setattr(core_config, "DEFAULT_HOME", user_home / "no-server-node")
    assert home_from_env() == old and local_home.read(old).mcp_port == 7410


# ---- the command


def test_the_old_command_runs_openajar_and_says_so_once_on_stderr():
    exe = Path(sys.executable).with_name("agentos")  # legacy-name
    if not exe.exists():
        pytest.skip("the venv has no console scripts (run uv sync)")
    proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True, check=True)
    import openajar
    assert proc.stdout.strip().endswith(openajar.__version__)
    assert proc.stderr.count("now called openajar") == 1


def test_pyproject_keeps_the_line_a_0_3_0_upgrader_looks_for():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'name = "agentos"' in text and 'name = "openajar"' in text  # legacy-name
    from openajar.ops import upgrade
    assert upgrade.PROJECT_RE.search('name = "agentos"\n') and upgrade.PROJECT_RE.search('name = "openajar"\n')  # legacy-name


# ---- the service unit


class FakeLaunchctl:
    def __init__(self, loaded: set[str]):
        self.calls: list[list[str]] = []
        self.loaded = set(loaded)

    def __call__(self, cmd):
        self.calls.append(cmd)
        verb, target = cmd[1], cmd[-1]
        label = target.rsplit("/", 1)[-1].removesuffix(".plist")
        if verb == "bootstrap":
            self.loaded.add(label)
        elif verb == "bootout":
            ok = label in self.loaded
            self.loaded.discard(label)
            return subprocess.CompletedProcess(cmd, 0 if ok else 3, "", "")
        elif verb == "print":
            if label not in self.loaded:
                return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
            return subprocess.CompletedProcess(cmd, 0, "\tstate = running\n\tpid = 4242\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class Ports:
    def __init__(self, fake: FakeLaunchctl):
        self.fake = fake

    def __call__(self, port: int) -> bool:  # 7410 is held while the old job runs
        return port == 7410 and service.LEGACY_LABEL in self.fake.loaded


def test_start_replaces_the_old_launchd_job_on_the_same_port(tmp_path, user_home):
    agents = user_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    old = agents / "dev.agentos.local.plist"  # legacy-name
    old.write_bytes(plistlib.dumps({"Label": service.LEGACY_LABEL}))
    home = tmp_path / "home"
    from openajar.local import home as local_home
    local_home.ensure(home, 7410)
    fake = FakeLaunchctl({service.LEGACY_LABEL})
    mgr = service.Launchd(Paths(user_home), fake, uid=501)
    deps = service.Deps(manager=mgr, busy=Ports(fake), answers=lambda port: service.LABEL in fake.loaded,
                        sleep=lambda s: None, wait_s=1)
    state = service.start(home, deps=deps)
    assert state.running and state.port == 7410
    assert not old.exists() and service.LEGACY_LABEL not in fake.loaded
    assert ["launchctl", "bootout", f"gui/501/{service.LEGACY_LABEL}"] in fake.calls
    assert (agents / "dev.openajar.local.plist").exists()


def test_start_replaces_the_old_systemd_unit(tmp_path, user_home):
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        out = "active\n" if "is-active" in cmd else "777\n" if "show" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    units = user_home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "agentos-local.service").write_text("[Service]\n")  # legacy-name
    mgr = service.Systemd(Paths(user_home), run)
    deps = service.Deps(manager=mgr, busy=lambda port: False, answers=lambda port: True, sleep=lambda s: None, wait_s=1)
    assert service.start(tmp_path / "home", deps=deps).running
    assert not (units / "agentos-local.service").exists()  # legacy-name
    assert ["systemctl", "--user", "disable", "--now", service.LEGACY_UNIT] in calls
    assert (units / "openajar-local.service").exists()


# ---- MCP entries and skills


def fake_claude(tmp_path: Path, entries: dict[str, str]) -> str:
    """A claude whose `mcp get` answers for `entries` and whose `mcp remove` logs and forgets one."""
    state = tmp_path / "claude-state"
    state.mkdir()
    for name, text in entries.items():
        (state / name).write_text(text)
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(f"""#!/bin/sh
echo "$@" >> {tmp_path}/claude-calls.txt
if [ "$2" = get ]; then [ -f "{state}/$3" ] && cat "{state}/$3" && exit 0; exit 1; fi
if [ "$2" = remove ]; then rm -f "{state}/$5"; exit 0; fi
exit 0
""")
    exe.chmod(0o755)
    return str(exe)


def test_the_old_claude_entry_goes_only_when_it_is_ours(tmp_path, user_home):
    exe = fake_claude(tmp_path, {
        "agentos-local": "  headersHelper: ~/.config/agentos/bin/agentos-mcp-headers local\n",  # legacy-name
        "agentos-mine": "  URL: https://example.test/mcp\n",  # legacy-name
    })
    claude = ClaudeCode(exe, user_home)
    assert claude.unregister_legacy("agentos-local")  # legacy-name
    assert not claude.unregister_legacy("agentos-mine")  # legacy-name: the owner's own entry stays
    assert not claude.unregister_legacy("agentos-none")  # legacy-name
    assert (tmp_path / "claude-state" / "agentos-mine").exists()  # legacy-name


def test_the_old_claude_entry_is_known_by_the_helper_in_claude_json(tmp_path, user_home):
    """`claude mcp get` prints no headersHelper: the helper is read from ~/.claude.json, or the URL tells."""
    exe = fake_claude(tmp_path, {
        "agentos-local": "agentos-local:\n  Type: http\n  URL: http://127.0.0.1:7410/mcp\n",  # legacy-name
        "agentos-there": "agentos-there:\n  Type: http\n  URL: http://127.0.0.1:7999/mcp\n",  # legacy-name
        "agentos-mine": "agentos-mine:\n  Type: http\n  URL: https://example.test/mcp\n",  # legacy-name
    })
    (user_home / ".claude.json").write_text(json.dumps({"mcpServers": {"agentos-local": {  # legacy-name
        "type": "http", "url": "http://127.0.0.1:7410/mcp",
        "headersHelper": "~/.config/agentos/bin/agentos-mcp-headers local"},  # legacy-name
        "agentos-there": {"type": "http", "url": "http://127.0.0.1:7999/mcp"},  # legacy-name
        "agentos-mine": {"type": "http", "url": "https://example.test/mcp"}}}))  # legacy-name
    claude = ClaudeCode(exe, user_home)
    assert claude.unregister_legacy("agentos-local")  # legacy-name
    assert claude.unregister_legacy("agentos-there", "http://localhost:7999/mcp")  # legacy-name: our node's URL
    assert not claude.unregister_legacy("agentos-mine", "http://localhost:7999/mcp")  # legacy-name
    assert (tmp_path / "claude-state" / "agentos-mine").exists()  # legacy-name


def test_an_old_entry_is_ours_by_its_helper_our_url_or_a_dead_address_on_this_computer():
    """The rule for agentos-* entries (field run 2026-11-21: agentos-agentvm on a dead :7400 stayed behind)."""
    from openajar.client.claude import legacy_ours

    dead, alive = (lambda url: False), (lambda url: True)
    helper = {"type": "http", "url": "https://far.test/mcp", "headersHelper": "~/x/agentos-mcp-headers a"}
    assert legacy_ours(helper, answers=alive)  # our old helper, wherever it points
    assert legacy_ours({"url": "http://127.0.0.1:7410/mcp"}, "http://localhost:7410/mcp", answers=alive)
    assert legacy_ours({"url": "http://localhost:7400/mcp"}, answers=dead)
    assert not legacy_ours({"url": "http://localhost:7400/mcp"}, answers=alive)  # it answers: maybe the owner's
    assert not legacy_ours({"url": "https://far.test/mcp"}, answers=dead)  # not on this computer
    assert not legacy_ours({"url": "http://localhost:7400/other"}, answers=dead)  # not an MCP address of ours
    assert not legacy_ours({"type": "stdio", "command": "agentos"}, answers=dead)


def test_profiles_are_the_default_the_named_one_and_each_claude_folder_with_a_config(tmp_path, user_home):
    from openajar.client.claude import profiles

    for name, config in ((".claude", True), (".claude_work", True), (".claude-empty", False), (".claudex", True)):
        (user_home / name).mkdir()
        if config:
            (user_home / name / ".claude.json").write_text("{}")
    (user_home / ".claude-file").write_text("not a folder")
    named = tmp_path / "elsewhere"
    found = profiles(user_home, {"CLAUDE_CONFIG_DIR": str(named)})
    assert [p.config_dir for p in found] == [None, named, user_home / ".claude", user_home / ".claude_work",
                                             user_home / ".claudex"]
    assert found[0].config_file == user_home / ".claude.json" and found[0].skills_dir == user_home / ".claude/skills"
    assert found[1].command("claude mcp list") == f"CLAUDE_CONFIG_DIR={named} claude mcp list"
    again = profiles(user_home, {"CLAUDE_CONFIG_DIR": str(user_home / ".claude_work")})  # named and found: once
    assert [p.config_dir for p in again].count(user_home / ".claude_work") == 1


def test_the_old_codex_table_goes_with_its_comment_and_the_rest_stays(user_home):
    path = user_home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "x"\n\n'
                    "# added by `agentos connect`; `agentos connect --remove NAME` removes it\n"  # legacy-name
                    '[mcp_servers.agentos-local]\ncommand = "/old/agentos"\nargs = ["mcp-bridge", "local"]\n\n'  # legacy-name
                    '[mcp_servers.agentos-mine]\ncommand = "mine"\n')  # legacy-name
    codex = CodexConfig(user_home)
    assert codex.unregister_legacy("agentos-local")  # legacy-name
    assert not codex.unregister_legacy("agentos-mine")  # legacy-name
    text = path.read_text()
    assert "mcp-bridge" not in text and "connect --remove" not in text and "agentos-mine" in text  # legacy-name


def _skill(name: str, product: str) -> str:
    return f"---\nname: {name}\ndescription: How to use {product}. Ships with {product}.\n---\nbody\n"


def test_installing_the_skills_removes_the_old_ones_we_wrote_and_keeps_the_owners(user_home):
    dest = skills.skills_dir(user_home)
    for name in skills.LEGACY_NAMES[:3]:
        (dest / name).mkdir(parents=True)
        (dest / name / "SKILL.md").write_text(_skill(name, skills.LEGACY_PRODUCT))
    own = dest / skills.LEGACY_NAMES[3]
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: something-else\n---\nmine\n")
    skills.install(user_home)
    assert all((dest / n / "SKILL.md").is_file() for n in skills.NAMES)
    assert not any((dest / n).exists() for n in skills.LEGACY_NAMES[:3]) and own.exists()


def test_the_service_brings_old_skills_over_under_the_new_names(user_home):
    dest = skills.skills_dir(user_home)
    name = skills.LEGACY_NAMES[0]
    (dest / name).mkdir(parents=True)
    (dest / name / "SKILL.md").write_text(_skill(name, skills.LEGACY_PRODUCT))
    assert skills.refresh(user_home) is not None
    assert (dest / skills.NAMES[0] / "SKILL.md").is_file() and not (dest / name).exists()


# ---- what stays under the old name


def test_backups_named_before_the_rename_are_still_read():
    assert backup.parse_name("agentos-node-1-20261104T040000Z.tar.gz.age")[0] == "node-1"  # legacy-name
    assert backup.parse_name("openajar-node-1-20261104T040000Z.tar.gz.age")[0] == "node-1"


def test_the_names_images_saves_and_signatures_keep():
    from openajar.environments import saves
    from openajar.node import viewer_tokens
    from openajar.vms import docker, models

    assert models.VmSettings().docker_image == "agentos-env:latest"  # legacy-name
    assert docker.IMAGE_LABEL == "org.agentos.environment" and docker.TYPE_LABEL == "agentos-server-type"  # legacy-name
    assert saves.MAGIC == b"AGENTOS-SAVE-1\n" and saves.SAVES_KEY_REF == "agentos/environments/saves-key"  # legacy-name
    assert viewer_tokens.TOKEN_CONTEXT == b"agentos-view-v2\n"  # legacy-name


def test_design_tokens_read_the_old_keys_too(tmp_path):
    from openajar.studio.tokens import load

    assert 'for key in ("agentos", "com.agentos", "openajar", "com.openajar")' in Path(load.__file__).read_text()  # legacy-name


# ---- the whole upgrade, from a home shaped as 0.3.0 left it


def _old_install(tmp_path: Path, user_home: Path, fake: FakeLaunchctl) -> dict:
    """What 0.3.0 left on a laptop (legacy-name throughout): ~/.agentos with its node, an open environment on a
    Hetzner machine and that machine's record, the home id the provider's labels carry, ~/.config/agentos with the
    connection, its token and the old headers helper, the agentos-local entries in Claude Code and Codex, the old
    skills, and the launchd job dev.agentos.local loaded and holding port 7410."""
    from datetime import UTC, datetime, timedelta

    from openajar.client.choices import Tools
    from openajar.client.ops import Client
    from openajar.environments.models import Environment
    from openajar.environments.store import EnvironmentStore
    from openajar.local import home as local_home
    from openajar.local.register import register_clients
    from openajar.vms.models import VmRecord
    from openajar.vms.store import VmStore

    old = user_home / ".agentos"  # legacy-name
    config = user_home / ".config" / "agentos"  # legacy-name
    config.mkdir(parents=True)
    (user_home / ".codex").mkdir()
    local_home.ensure(old, 7410)
    now = datetime.now(UTC)
    EnvironmentStore(old / "environments.db").put(Environment(
        id="e7k2", title="Shop page", state="ready", grant_id="owner", binding="grant:owner", vm_id="vm_abc",
        keep="save", created_at=now, expires_at=now + timedelta(hours=2)))
    VmStore(old / "vms.db").put(VmRecord(
        id="vm_abc", lane_id="env/e7k2", node="local", profile="default", server_type="cx22", location="nbg1",
        image="ubuntu-24.04", state="ready", created_at=now, expires_at=now + timedelta(hours=2),
        environment="e7k2"))
    (old / "vms").mkdir(exist_ok=True)
    (old / "vms" / "home-id").write_text("f00dfeed1234\n")

    exe = fake_claude(tmp_path, {})
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(claude=exe), out=lambda _: None)
    register_clients(old, 7410, client=client)  # the grant, its token file and the connection record
    grant_id = client.records.get("local").agent_grant_id
    # ... as 0.3.0 named them
    (config / "bin" / "openajar-mcp-headers").rename(config / "bin" / "agentos-mcp-headers")  # legacy-name
    codex = user_home / ".codex" / "config.toml"
    codex.write_text(codex.read_text().replace("openajar-local", "agentos-local")  # legacy-name
                     .replace("openajar connect", "agentos connect"))  # legacy-name
    dest = skills.skills_dir(user_home)
    for new, legacy in zip(skills.NAMES, skills.LEGACY_NAMES, strict=True):
        (dest / new).rename(dest / legacy)
        md = dest / legacy / "SKILL.md"
        md.write_text(md.read_text().replace("openajar", skills.LEGACY_PRODUCT))
    (tmp_path / "claude-state" / "agentos-local").write_text(  # legacy-name
        "  URL: http://127.0.0.1:7410/mcp\n  headersHelper: ~/.config/agentos/bin/agentos-mcp-headers local\n")  # legacy-name
    agents = user_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{service.LEGACY_LABEL}.plist").write_bytes(plistlib.dumps({"Label": service.LEGACY_LABEL}))
    fake.loaded.add(service.LEGACY_LABEL)
    return {"old": old, "config": config, "exe": exe, "grant": grant_id, "codex": codex, "agents": agents}


def test_an_upgrade_from_a_0_3_0_home_ends_with_one_service_on_the_old_data(tmp_path, user_home, monkeypatch):
    from openajar.client.choices import Tools
    from openajar.client.ops import Client
    from openajar.environments.store import EnvironmentStore
    from openajar.local import home as local_home
    from openajar.local.paths import default_home
    from openajar.local.register import register_clients
    from openajar.vms.store import VmStore

    fake = FakeLaunchctl(set())
    was = _old_install(tmp_path, user_home, fake)
    monkeypatch.delenv("OPENAJAR_HOME", raising=False)

    # `openajar local start`, as the installer runs it after the upgrade
    home = default_home()
    mgr = service.Launchd(Paths(user_home), fake, uid=501)
    deps = service.Deps(manager=mgr, busy=Ports(fake), answers=lambda port: service.LABEL in fake.loaded,
                        sleep=lambda s: None, wait_s=1)
    state = service.start(home, deps=deps)
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(claude=was["exe"]), out=lambda _: None)
    lines = "\n".join(register_clients(home, state.port, client=client))

    # one service, on the old home and its port, and the old job gone
    assert home == was["old"] and not (user_home / ".openajar").exists()
    assert fake.loaded == {service.LABEL} and state.running and state.port == 7410
    assert not (was["agents"] / f"{service.LEGACY_LABEL}.plist").exists()
    plist = plistlib.loads((was["agents"] / f"{service.LABEL}.plist").read_bytes())
    assert plist["ProgramArguments"][-1] == str(was["old"])
    assert plist["EnvironmentVariables"]["OPENAJAR_HOME"] == str(was["old"])
    # the old data: the environment, its machine and the provider labels' home id, the grant and its token
    assert [e.id for e in EnvironmentStore(home / "environments.db").open()] == ["e7k2"]
    assert VmStore(home / "vms.db").get("vm_abc").environment == "e7k2"
    assert (home / "vms" / "home-id").read_text().strip() == "f00dfeed1234"
    assert local_home.read(home).mcp_port == 7410
    assert f"Agent grant {was['grant']} kept" in lines and client.records.get("local").agent_grant_id == was["grant"]
    # the MCP entries and the skills under the new names, the old ones gone
    calls = (tmp_path / "claude-calls.txt").read_text()
    assert "add-json --scope user openajar-local" in calls and "remove --scope user agentos-local" in calls  # legacy-name
    codex = was["codex"].read_text()
    assert "openajar-local" in codex and "agentos-local" not in codex and "agentos connect" not in codex  # legacy-name
    assert codex.count("# added by `openajar connect`") == 1
    dest = skills.skills_dir(user_home)
    assert all((dest / n / "SKILL.md").is_file() for n in skills.NAMES)
    assert not any((dest / n).exists() for n in skills.LEGACY_NAMES)
    # the client's folder stays where it was, with the helper under its new name
    assert Paths(user_home).config_dir == was["config"]
    assert (was["config"] / "bin" / "openajar-mcp-headers").exists()


def test_a_token_in_the_keychain_under_the_old_service_is_read_and_the_helper_looks_there_too(tmp_path):
    """A fake `security` (never this computer's Keychain): it holds one item, under the old service name."""
    from openajar.client import tokens

    items = tmp_path / "items"
    items.mkdir()
    (items / "agentos.local").write_text("aos_" + "x" * 40)  # legacy-name
    exe = tmp_path / "security"
    exe.write_text(f"""#!/bin/sh
case "$1" in
  find-generic-password) f="{items}/$3.$5"; [ -f "$f" ] && cat "$f" && exit 0; exit 44 ;;
  delete-generic-password) f="{items}/$3.$5"; [ -f "$f" ] && rm "$f" && exit 0; exit 44 ;;
esac
exit 1
""")
    exe.chmod(0o755)
    store = tokens.KeychainStore(str(exe))
    assert store.get("local") == "aos_" + "x" * 40
    assert store.delete("local") and not (items / "agentos.local").exists()  # legacy-name
    script = tokens.helper_script(Paths(tmp_path), str(exe))
    assert f"-s {tokens.SERVICE} " in script and f"-s {tokens.LEGACY_SERVICE} " in script


# ---- the whole upgrade, from a home shaped as 0.4.0 left it


def _install_0_4_0(tmp_path: Path, user_home: Path, fake: FakeLaunchctl) -> dict:
    """What 0.4.0 left on top of 0.3.0's shape: its grant already has secret on env/* lanes (0.4.0's
    connect gave it), the web app it installed under the home (web/app and web.json at 0.4.0), an
    environments.toml that saves when idle on a limited network, a prepared machine of a repository with
    .agentos/environment.toml, and the replay of a closed environment's browser."""
    import json
    from datetime import UTC, datetime

    from openajar.environments.prepared import Prepared, PreparedStore

    was = _old_install(tmp_path, user_home, fake)
    old = was["old"]
    (old / "web" / "app").mkdir(parents=True)
    (old / "web" / "app" / "index.html").write_text("<!doctype html><title>agentos</title>")
    (old / "web" / "web.json").write_text(json.dumps({"version": "0.4.0", "sha256": "0" * 64, "wanted": True}))
    (old / "environments.toml").write_text('default_keep = "save"\nnetwork = "limited"\nallow_hosts = ["api.example.com"]\n')
    PreparedStore(old).put(Prepared(key="k" * 64, repo="github.com/me/shop", state="ready", image="agentos-eval-x:prep",
                                    provider="docker", started_at=datetime.now(UTC), built_at=datetime.now(UTC)))
    replay = old / "environments" / "e7k2" / "replay"
    replay.mkdir(parents=True)
    (replay / "index.jsonl").write_text(json.dumps({"id": "f0123456789", "at": datetime.now(UTC).isoformat()}) + "\n")
    return was


def test_an_upgrade_from_a_0_4_0_home_keeps_its_grant_web_app_settings_and_records(tmp_path, user_home, monkeypatch):
    from openajar.client.choices import Tools
    from openajar.client.ops import Client
    from openajar.core import webapp
    from openajar.environments.models import load_settings
    from openajar.environments.prepared import PreparedStore
    from openajar.environments.replay_store import ReplayStore
    from openajar.local.paths import default_home
    from openajar.local.register import register_clients

    fake = FakeLaunchctl(set())
    was = _install_0_4_0(tmp_path, user_home, fake)
    monkeypatch.delenv("OPENAJAR_HOME", raising=False)
    home = default_home()
    mgr = service.Launchd(Paths(user_home), fake, uid=501)
    deps = service.Deps(manager=mgr, busy=Ports(fake), answers=lambda port: service.LABEL in fake.loaded,
                        sleep=lambda s: None, wait_s=1)
    state = service.start(home, deps=deps)
    client = Client(paths=Paths(user_home), platform="linux", tools=Tools(claude=was["exe"]), out=lambda _: None)
    lines = "\n".join(register_clients(home, state.port, client=client))

    # one service on the old home and port, as from 0.3.0
    assert home == was["old"] and fake.loaded == {service.LABEL} and state.port == 7410
    # 0.4.0's grant already has secret: kept, so no connect is needed (from 0.3.0 one is issued instead)
    assert f"Agent grant {was['grant']} kept" in lines
    # the web app 0.4.0 installed is served until the installer's `openajar web update` brings 0.5.0's
    status = webapp.status(home, version="0.5.0")
    assert status.installed == "0.4.0" and status.mismatched and status.wanted is True
    assert webapp.built(webapp.installed_dir(home))
    # what 0.4.0 stored is read as it is
    settings = load_settings(home)
    assert settings.default_keep == "save" and settings.network == "limited" and "api.example.com" in settings.allow_hosts
    assert [p.repo for p in PreparedStore(home).all()] == ["github.com/me/shop"]
    assert [f["id"] for f in ReplayStore(home).frames("e7k2")] == ["f0123456789"]
# legacy-name: end
