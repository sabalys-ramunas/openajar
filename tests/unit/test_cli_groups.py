"""The task-first `openajar` top level (openajar.cli_groups, unification stage C1).

- Every command path recorded before the regrouping (tests/fixtures/cli/old_tree.json, made by
  scripts/cli_tree.py) still resolves to the same function with the same options.
- `openajar --help` shows the 14 groups and fits on one screen; the old names are hidden.
- Each top-level group's --help matches tests/fixtures/cli/help/<group>.txt, at 80 columns.
  After a meant change: OPENAJAR_UPDATE_HELP=1 uv run pytest tests/unit/test_cli_groups.py
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Annotated

import pytest
import typer
import typer.rich_utils
from typer.testing import CliRunner

from openajar import cli_groups
from openajar.cli import app

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "cli"
OLD = json.loads((FIXTURES / "old_tree.json").read_text())
runner = CliRunner()


def _tree_module():
    spec = importlib.util.spec_from_file_location("cli_tree", ROOT / "scripts" / "cli_tree.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TREE = _tree_module().tree()
WRAPPED = {"connect", "status", "open"}  # the old command, plus one option (cli_groups.with_flag)


def test_every_old_path_still_resolves_to_the_same_command():
    assert len(OLD) > 350
    broken = []
    for path, old in OLD.items():
        new = TREE.get(path)
        if new is None or new["kind"] != old["kind"] or new["target"] != old["target"]:
            broken.append((path, old, new))
        elif path in WRAPPED:
            if not set(old["params"]) < set(new["params"]):
                broken.append((path, old, new))
        elif not set(old["params"]) <= set(new["params"]):  # a new option is fine; a lost one breaks a script
            broken.append((path, old, new))
    assert not broken, f"{len(OLD) - len(broken)}/{len(OLD)} old paths resolve; broken: {broken[:5]}"


def test_old_top_level_names_are_hidden_and_each_has_a_new_home():
    from typer.main import get_command

    root = get_command(app)
    visible = [name for name, cmd in root.commands.items() if not cmd.hidden]
    assert visible == [name for name, _, _ in cli_groups.VISIBLE]
    for name in (p for p in OLD if " " not in p):
        assert name in visible or name in cli_groups.HOME, f"{name} has no place in the new layout"
        if name in cli_groups.HOME:
            assert root.commands[name].hidden, name
    for name, home in cli_groups.HOME.items():
        assert home in visible, (name, home)


@pytest.mark.parametrize(("old", "new"), [
    ("errors busy", "dev errors busy"),
    ("version", "help version"),
    ("freeze lane --help", "lane freeze --help"),
    ("grants --help", "access grants --help"),
    ("memory --help", "workspace memory --help"),
    ("runs --help", "routines runs --help"),
    ("config example", "node config example"),
])
def test_an_alias_prints_exactly_what_its_new_path_prints(old, new):
    first, second = runner.invoke(app, old.split()), runner.invoke(app, new.split())
    assert first.exit_code == second.exit_code == 0, (first.output, second.output)
    def body(text: str) -> list[str]:  # the Usage line names the path it was called by
        return [line for line in text.splitlines() if not line.strip().startswith("Usage:")]

    assert body(first.output) == body(second.output)


def _help(args: list[str]) -> str:
    result = runner.invoke(app, [*args, "--help"], prog_name="openajar")
    assert result.exit_code == 0, result.output
    return "\n".join(line.rstrip() for line in result.output.splitlines()) + "\n"


def test_top_level_help_fits_on_one_screen_and_starts_with_the_task(monkeypatch):
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 80)
    text = _help([])
    # 40, then `environments` (docs/design/environments.md), `local`, `web`, `mcp`, and `call` and `tools` (an agent
    # whose session has not loaded the MCP tools finds its way in from a shell; field run 2026-11-21)
    assert len(text.splitlines()) <= 46
    start = text.split("─ Start here ")[1].split("╰")[0]
    assert "│ call " in start and "│ tools " in start
    assert "openajar setup" in text.split("Options")[0]
    assert "—" not in text


@pytest.mark.parametrize("group", ["", *[name for name, _, _ in cli_groups.VISIBLE]])
def test_golden_help_per_group(group, monkeypatch):
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 80)
    text = _help([group] if group else [])
    assert "—" not in text
    golden = FIXTURES / "help" / f"{group or 'openajar'}.txt"
    if os.environ.get("OPENAJAR_UPDATE_HELP"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text)
    assert text == golden.read_text(), f"{group} --help changed; OPENAJAR_UPDATE_HELP=1 records it"


def test_help_lists_the_everyday_commands_and_they_exist():
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0 and "openajar up" in result.output and "—" not in result.output
    for line in result.output.splitlines():
        if line.startswith("  openajar "):
            words = [w for w in line.split("  ")[1].split()[1:] if not w.startswith(("<", "-")) and w.islower()]
            path = " ".join(words)
            assert path in TREE, f"`openajar help` names {path!r}, which is no command"


# ---- the wrapped commands ----


CALLS: list = []


def _original(lane: Annotated[str, typer.Argument()], wake: Annotated[bool, typer.Option("--wake")] = False):
    CALLS.append(("original", lane, wake))


def test_with_flag_forwards_every_option_and_keeps_the_rest_required():
    calls = CALLS
    calls.clear()
    toy = typer.Typer()
    flag = Annotated[bool, typer.Option("--terminal")]
    toy.command()(cli_groups.with_flag(_original, "terminal", flag, lambda kw: calls.append(("flag", kw)),
                                       optional=("lane",), missing="name a lane"))
    assert runner.invoke(toy, ["a/b", "--wake"]).exit_code == 0
    assert runner.invoke(toy, ["--terminal"]).exit_code == 0
    missing = runner.invoke(toy, [])
    assert missing.exit_code == 2 and "name a lane" in missing.output
    assert calls == [("original", "a/b", True), ("flag", {"lane": None, "wake": False})]


def test_open_terminal_is_the_dashboard_and_status_check_is_doctor(monkeypatch):
    from openajar.ops import cli as ops_cli
    from openajar.tui import cli as tui_cli

    seen = []
    monkeypatch.setattr(tui_cli, "dash", lambda **kw: seen.append(("dash", kw["node"])))
    monkeypatch.setattr(ops_cli, "doctor_cmd", lambda **kw: seen.append(("doctor", kw["offline"])))
    assert runner.invoke(app, ["open", "--terminal", "--node", "box"]).exit_code == 0
    assert runner.invoke(app, ["status", "--check"]).exit_code == 0
    assert seen == [("dash", "box"), ("doctor", False)]
    missing = runner.invoke(app, ["open"])
    assert missing.exit_code == 2 and "--terminal" in missing.output


def test_connect_by_hand_prints_the_steps_for_this_box(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "node"))
    monkeypatch.delenv("OPENAJAR_CONTROLLER_HOME", raising=False)
    none = runner.invoke(app, ["connect", "--by-hand"])
    assert none.exit_code == 1 and "no node.toml" in none.output
    assert runner.invoke(app, ["node", "init", "--mcp-port", "7690"]).exit_code == 0
    node = runner.invoke(app, ["connect", "--by-hand", "box"])
    assert node.exit_code == 0, node.output
    assert "ssh -N -L 7690:localhost:7690 box" in node.output and "openajar access grants issue" in node.output
    assert "claude mcp add" in node.output and "—" not in node.output
    assert runner.invoke(app, ["controller", "init", "--port", "7691"]).exit_code == 0
    ctl = runner.invoke(app, ["connect", "--by-hand", "box"])
    assert ctl.exit_code == 0, ctl.output
    assert "ssh -N -L 7691:localhost:7691 box" in ctl.output and "http://localhost:7691/mcp" in ctl.output
