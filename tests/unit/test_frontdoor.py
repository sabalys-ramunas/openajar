"""The front door an agent meets first: the MCP intro leads with the environment (a cloud computer
for this chat), whoami names the bound environment or says how to get one, and the
openajar-environment skill names real tools and installs like the other skills."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fakes import FakeInteraction, FakeLaneManager
from mcp import Client

from openajar.client import skills
from openajar.core.config import NodeConfig
from openajar.core.runtime import Runtime
from openajar.mcp.registry import build_mcp
from openajar.node.tools import INSTRUCTIONS, tool_specs

EM_DASH = chr(0x2014)
SKILL = Path(__file__).resolve().parents[2] / "src/openajar/client/skills/openajar-environment/SKILL.md"
ENV_TOOLS = ("environment_open", "environment()", "environment_close")


def test_the_intro_starts_with_the_environment_and_puts_lanes_after():
    first = INSTRUCTIONS.split("\n\n")[0]
    assert first.startswith("openajar can give you your own cloud computer")
    for phrase in (*ENV_TOOLS, "without `on`", "localhost", "push", "links", "work in openajar",
                   "builds, tests, dev servers", "task done, not asked to keep it: environment_close()",
                   "file_read/file_edit (not cat)", "Keep the `on` it returns",
                   "pass it on every call", "only your chat reaches the machine", "environment_in_use"):
        assert phrase in first, phrase
    assert "browsers_list" not in first  # account lanes are a separate thing, after
    assert INSTRUCTIONS.index("environment_open") < INSTRUCTIONS.index("is workspace/name")
    assert EM_DASH not in INSTRUCTIONS and len(INSTRUCTIONS) < 4_000
    for gone in ("The title names it", "your other chats reach it by title", 'on="<title>"'):  # the lease of 2026-11-16
        assert gone not in INSTRUCTIONS, gone


class Bound:
    """Stands in for openajar.environments: the environment bound to the calling chat."""

    def __init__(self, env):
        self.env = env

    def bound_to_call(self, ctx):
        return self.env


async def _whoami(tmp_path, **services) -> dict:
    config = NodeConfig(home=tmp_path, name="test-node", mcp_token="tok")
    runtime = Runtime(config=config, lanes=FakeLaneManager(config), interaction=FakeInteraction(), **services)
    async with Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS)) as client:
        result = await client.call_tool("whoami", {})
    return result.structured_content or json.loads(result.content[0].text)


async def test_whoami_says_how_to_get_an_environment_when_none_is_bound(tmp_path):
    for services in ({}, {"environments": Bound(None)}):
        data = await _whoami(tmp_path, **services)
        assert data["node"] == "test-node" and data["environment"] is None
        assert "environment_open(title=" in data["next"]


async def test_whoami_names_the_bound_environment(tmp_path):
    env = {"id": "e7k2", "title": "Black Friday page", "state": "ready", "workdir": "/work/shop",
           "repo": "github.com/me/shop", "branch": "main", "machine": {"provider": "docker"}}
    data = await _whoami(tmp_path, environments=Bound(env))
    assert data["environment"] == {k: env[k] for k in ("id", "title", "state", "workdir", "repo", "branch")}
    assert "e7k2 (Black Friday page)" in data["next"] and "without `on`" in data["next"]


def test_the_environment_skill_is_installed_like_the_others_and_names_the_loop():
    assert "openajar-environment" in skills.NAMES
    text = skills.source("openajar-environment")
    head = text.split("---")[1]
    assert re.search(r"^name: openajar-environment$", head, re.MULTILINE) and skills.ours(text, "openajar-environment")
    assert text == SKILL.read_text()
    for word in ("environment_open(", "environment()", "environment_close(", "discard=true", "unsaved_work",
                 "no_environment", "terminal_exec(", "browser_navigate(", "ui_check(", "localhost", "git push",
                 "preview pane", "links.browser", "default branch", "secrets", "data, never instructions"):
        assert word in text, word
    assert EM_DASH not in text
    assert len(text.split()) < 1025  # short: read in one go (1000, then the shell's way in: openajar call)
    # field run 2026-11-21: a session without the MCP tools loaded wrote its own HTTP client; the skill names the CLI
    assert "openajar call <tool> --on <on>" in text and "openajar tools" in text
    # evals 2026-11-21 (Sonnet 5.5): it left a finished machine open with the default keep, read files with cat,
    # and invented a second title when its machine died. The skill says each default in so many words.
    for words in ("Done, and not asked to keep it open: `environment_close()` is your last call, whatever the `keep`",
                  "never `cat`, `head` or `sed` in a command", "`machine_gone`", "same title gives a new one"):
        assert words in text, words


def test_the_intro_and_the_skill_say_where_the_terminal_tools_are_and_promise_no_missing_link():
    """An agent with an environment finds its terminal without guessing (terminal_exec and terminal_job_start are
    in the default list; the rest are tools_more("terminals")), and is promised only links that exist: the
    node's live view (links.browser), no web app page and no port link."""
    first = INSTRUCTIONS.split("\n\n")[0]
    assert "links.browser" in first and "terminal_job_start" in first and "ui_check" in first
    assert "`on` for an account browser" in INSTRUCTIONS
    text = skills.source("openajar-environment")
    for words in ('tools_more("terminals")', 'tools_more("studio")', "terminal_job_read", "in your tool list",
                  "environment_not_ready", "stashes"):
        assert words in text, words
    assert "links.environment" not in text and "a `link` for each port" not in text


def test_a_checkout_without_the_app_says_what_to_use_instead(tmp_path, monkeypatch):
    # an installation with no web/: the placeholder and the dev banner must not tell a
    # stranger to build one
    from types import SimpleNamespace

    from openajar.api.static import NO_APP, NOT_BUILT, placeholder
    from openajar.dev import cli as dev_cli

    assert placeholder(tmp_path / "web" / "dist") == NO_APP
    # a checkout with web/, before it is built
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    assert placeholder(tmp_path / "web" / "dist") == NOT_BUILT
    assert "pnpm build" in NOT_BUILT and "panel/" not in NOT_BUILT
    assert "/view/" in NO_APP and "openajar environments list" in NO_APP
    node = SimpleNamespace(temporary=True, home=tmp_path, panel_link="http://localhost:7450/login#code=x",
                           mcp_url="http://127.0.0.1:7450/mcp", claude_add="claude mcp add ...", panel_built=False)
    monkeypatch.setattr(dev_cli, "web_source", lambda: False)
    text = dev_cli.banner(node)
    assert "no web app" in text and "npm ci" not in text and "\u2014" not in text
    monkeypatch.setattr(dev_cli, "web_source", lambda: True)  # as in the public export
    text = dev_cli.banner(node)
    assert "pnpm install && pnpm build" in text and "panel/" not in text and "\u2014" not in text


def test_dev_up_sees_a_listening_port_as_busy_and_a_closing_one_as_free():
    # after Ctrl-C a connection of the last run may still be closing on the port: dev up starts again
    import socket

    from openajar.dev.cli import port_is_free

    with socket.socket() as server:
        # as uvicorn's listener does: on Linux a closing connection frees the port for a new bind
        # only when the socket it was accepted on had SO_REUSEADDR too
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert not port_is_free(port)
        with socket.create_connection(("127.0.0.1", port)) as client:
            conn, _ = server.accept()
            conn.close()  # the server end closes first: its side waits in TIME_WAIT
            client.recv(1)
    assert port_is_free(port)


def test_the_skill_and_the_intros_teach_the_three_modes_and_coming_back():
    """An agent picks single use, save or live for the right reason, resumes by title, reads `resumed`
    (its note as data), saves before risky steps, tells the human how to come back, and forgets saves."""
    from openajar.local.node import INSTRUCTIONS as LOCAL

    text = skills.source("openajar-environment")
    for words in ('keep="none"', 'keep="save"', 'keep="live"', "continue tomorrow", "same title", "resumed",
                  "missing", "note", "environment_save(", "environment_forget(", "continue Black Friday page in openajar",
                  "environment_upload", "environment_download(", "keep_live_not_allowed", "save_failed",
                  "restore_failed", "final=true"):
        assert words in text, words
    first = INSTRUCTIONS.split("\n\n")[0]
    for words in ('keep="save"', '"live"', "same title", "resumed", "environment_save"):
        assert words in first, words
    for words in ('"save"', '"live"', "same title", "`resumed`", "environment_save(note)", "environment_forget(title)",
                  "continue <title> in openajar", "environment_download(path)", "sleep"):
        assert words in LOCAL, words
    assert EM_DASH not in LOCAL
    # saves when idle is the default (2026-11-18): single use is asked for, and only for a one-off fix
    assert '`keep="save"`, the default' in text and '`keep="none"` for a one-off fix only' in text
    assert 'keep="save" is the default' in first and 'keep="none" only for a one-off fix' in first
    assert '"save" is the default' in LOCAL and 'keep="none" for a one-off fix only' in LOCAL


def test_the_skill_and_the_intros_say_how_the_agent_signs_in():
    """A sign-in page: the login tool types a login the owner bound to the machine; the model never sees it."""
    from openajar.local.node import INSTRUCTIONS as LOCAL

    assert "`login` types a login the owner bound; you never see it" in skills.source("openajar-environment")
    assert "login (the owner's bound logins)" in INSTRUCTIONS.split("\n\n")[0]
    assert "login types a login the owner bound to this machine (you never see it)" in LOCAL


def test_the_skill_and_the_intros_teach_coming_back_ports_and_pull_requests():
    """Builder I3: services come back (or say how to restart them), one call stops with a note and one
    finishes for good, the repo alone finds the environment, a dev server gets a link, and a pushed
    branch gets a real pull request (git.compare is only the fallback)."""
    from openajar.local.node import INSTRUCTIONS as LOCAL

    text = skills.source("openajar-environment")
    for words in ("environment_pull_request(", "environment_port(", "forget=true", 'environment_close(note="',
                  "repo=", "environment_choice", "`restart`", "running_jobs_lost", "pull_request_failed",
                  "git.compare", "signins=false", "tunnel"):
        assert words in text, words
    first = INSTRUCTIONS.split("\n\n")[0]
    for words in ("environment_pull_request(title)", "environment_port(port)", "forget=true", "note=",
                  "the repo alone"):
        assert words in first, words
    for words in ("environment_pull_request", "environment_port(port)", "forget=true", 'note="',
                  "environment_open(repo=...)", "`services`", "http://127.0.0.1:<p>/"):
        assert words in LOCAL, words
    assert "compare: a PR link" not in INSTRUCTIONS + LOCAL + text
