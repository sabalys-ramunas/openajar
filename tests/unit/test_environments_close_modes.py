# ruff: noqa: F811 - `node` is test_environments_saves's fixture, imported by its name
"""environment_close's guard and a file whose only change is its mode (chmod +x by a setup script): it
still refuses, and says which files changed only their mode, so the agent can discard with confidence.
GIT_STATE runs for real on test_environments_saves's machine folders."""

from __future__ import annotations

import pytest
from test_environments_saves import disk, git, node, opened  # noqa: F401 - the fixture

from openajar.environments.manager import EnvironmentManager, unsaved_fix
from openajar.environments.probes import parse_git_state
from openajar.policy.wrap import ToolCallError


def test_git_state_names_the_files_whose_only_change_is_their_mode():
    repos = parse_git_state("repo=/work/a\nbranch=main\nahead=0\ndirty_count=2\ndirty= M setup.sh\ndirty= M app.js\n"
                            "samecontent=setup.sh\nmodechange=setup.sh\nmodechange=app.js\n")
    assert repos[0]["mode_only"] == ["setup.sh"] and "_modechange" not in repos[0]  # app.js changed content too
    unsaved = EnvironmentManager.unsaved_of(repos)
    assert unsaved["mode_only"] == ["setup.sh"]
    words = EnvironmentManager.unsaved_words(unsaved)
    assert words == "changed or new files: app.js; mode change only: setup.sh"
    assert "discard=true when the human agreed" in unsaved_fix(unsaved)

    only = EnvironmentManager.unsaved_of(parse_git_state(
        "repo=/work/a\ndirty_count=1\ndirty= M scripts/setup.sh\nsamecontent=scripts/setup.sh\n"
        "modechange=scripts/setup.sh\n"))
    assert EnvironmentManager.unsaved_words(only) == "only file modes changed, no content: scripts/setup.sh"
    assert unsaved_fix(only).startswith("Nothing but file modes (chmod) differ from the commit: close with discard=true")
    # a new empty file has no lines either, but no mode change: it is content
    empty = parse_git_state("repo=/work/a\ndirty_count=1\ndirty=A  empty.txt\nsamecontent=empty.txt\n")
    assert "mode_only" not in empty[0]


async def test_close_says_a_chmod_is_only_a_mode_change(node):
    out = await opened(node, keep="none")
    work = disk(node, out["id"]) / "work" / "shop"
    (work / "app.js").chmod(0o755)
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_close")
    assert e.value.code == "unsaved_work"
    assert "only file modes changed, no content: app.js" in str(e.value)
    assert "Nothing but file modes (chmod)" in str(e.value) and "—" not in str(e.value)

    (work / "old.txt").write_text("changed\n")
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_close")
    assert "changed or new files: old.txt; mode change only: app.js" in str(e.value)
    assert "Nothing but file modes" not in str(e.value)

    git(work, "checkout", "--", "old.txt")
    closed = await node["call"]("environment_close", discard=True)
    assert closed["machine"] == "deleted"
