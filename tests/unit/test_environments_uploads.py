"""Uploads into an environment (openajar.environments.uploads): environment_upload's one-use link, the
viewer's PUT route, the archive checks, the overlay on the machine, and the record of it.

The machine is the fake of test_environments; its /work is a folder here, and put_archive runs this
computer's tar on it. The archive is made by the very pack command the tool gives, in a real git
checkout, and sent to the real viewer route."""

from __future__ import annotations

import asyncio
import io
import subprocess
import tarfile
from urllib.parse import urlsplit

import httpx
import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)

from openajar.env.execs import in_bash
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.models import ExecResult
from openajar.env.paths import EnvSettings as TermSettings
from openajar.env.sandbox import DirectLauncher
from openajar.environments import uploads
from openajar.environments.uploads import tool_specs as upload_specs
from openajar.node.viewer_app import build_viewer_app
from openajar.node.viewer_tokens import ViewTokens
from openajar.policy.wrap import ToolCallError, bind_request_token, wrap_tool

GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test"}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Machine:
    """put_archive on a folder here that stands for the machine's /work."""

    def __init__(self, root):
        self.root = root
        self.calls: list[tuple[str, str, bool]] = []

    def local(self, dest: str):
        assert dest == "/work" or dest.startswith("/work/"), dest
        return self.root / dest.removeprefix("/work").lstrip("/")

    async def put_archive(self, lane_id, dest, archive, *, gzip, timeout_s):
        self.calls.append((lane_id, dest, gzip))
        here = self.local(dest)
        here.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.to_thread(subprocess.run, ["tar", "-x", "--no-same-owner", "-f", "-",
                                                        *(["-z"] if gzip else [])],
                                       stdin=archive, cwd=here, capture_output=True)
        return ExecResult(exit_code=proc.returncode, stdout="", stderr=proc.stderr.decode(), duration_s=0.01)


@pytest.fixture
def up(node, tmp_path, monkeypatch):  # noqa: F811
    clock = Clock()
    tokens = ViewTokens(tmp_path / "viewer.key", clock=clock)
    rt = node["rt"]
    rt.register("viewer_links", tokens)
    machine = Machine(tmp_path / "machine" / "work")
    machine.root.mkdir(parents=True)
    monkeypatch.setattr(node["envs"], "put_archive", machine.put_archive)
    [spec] = upload_specs(rt)
    wrapped = wrap_tool(spec, rt)

    async def link(*, session="chat-1", **kw):
        with bind_request_token(node["token"], session=session):
            return await wrapped(**kw)

    app = build_viewer_app(node["lanes"], rt.get("config"), tokens=tokens, environments=lambda: node["mgr"])

    async def send(url: str, body: bytes, method: str = "PUT"):
        parts = urlsplit(url)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
            return await c.request(method, f"{parts.path}?{parts.query}", content=body)

    return {"link": link, "send": send, "machine": machine, "clock": clock, "tokens": tokens}


def git(*args: str, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**GIT_ENV, "HOME": str(cwd)}, capture_output=True,
                          text=True, check=True).stdout


def pack(checkout) -> bytes:
    """The tool's own pack command, run in a checkout (or a plain folder): the bytes of the archive, which
    it writes to the temp folder, never into the folder it packs."""
    tmp = checkout.parent / f"tmp-{checkout.name}"
    tmp.mkdir(exist_ok=True)
    subprocess.run(["sh", "-c", uploads.PACK], cwd=checkout,
                   env={**GIT_ENV, "HOME": str(checkout), "TMPDIR": str(tmp)}, check=True, capture_output=True)
    assert not (checkout / "openajar-work.tgz").exists()
    return (tmp / "openajar-work.tgz").read_bytes()


def monorepo(root):
    """A small pnpm monorepo with a commit, then uncommitted work: a changed file, a new one, and what git
    ignores (node_modules, .env)."""
    root.mkdir()
    git("init", "-q", "-b", "main", str(root), cwd=root.parent)
    (root / "package.json").write_text('{"name": "mono", "private": true}\n')
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - packages/*\n")
    (root / "packages" / "web").mkdir(parents=True)
    (root / "packages" / "web" / "package.json").write_text('{"name": "web"}\n')
    (root / ".gitignore").write_text("node_modules/\n.env\nwork.tgz\n")
    git("add", ".", cwd=root)
    git("commit", "-qm", "first", cwd=root)
    (root / "packages" / "web" / "package.json").write_text('{"name": "web", "version": "2.0.0"}\n')  # uncommitted
    (root / "packages" / "web" / "index.ts").write_text("export const sale = true\n")  # new, not added
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (root / ".env").write_text("SECRET=1\n")
    return root


def tar_of(*members: tuple[tarfile.TarInfo, bytes | None], gz: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tf:
        for info, data in members:
            if data is not None:
                info.size = len(data)
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def member(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info


def leftovers(n, env_id: str) -> list[str]:
    folder = n["mgr"].home / "environments" / env_id / "uploads"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ------------------------------------------------------------------ the real case
async def test_local_code_goes_onto_an_environment_opened_without_a_repo_and_the_loop_runs(node, up, tmp_path):  # noqa: F811
    """environment_open(title) with no repo, environment_upload(), curl the tarball, terminal_exec("pnpm install")
    in the workdir, environment() shows it, close."""
    out = await opened(node)
    assert out["state"] == "ready" and out["workdir"] == "/work"
    link = await up["link"]()
    assert link.get("status") != "approval_pending", link  # a link in its own environment never waits
    assert link["dest"] == "/work" and link["pack"] == uploads.PACK
    assert link["url"].startswith(f"http://localhost:{node['rt'].get('config').mcp_port}/view/env/{out['id']}/upload?t=")
    assert link["send"] == f'curl --fail-with-body -T "${{TMPDIR:-/tmp}}/openajar-work.tgz" "{link["url"]}"'

    body = pack(monorepo(tmp_path / "mono"))
    res = await up["send"](link["url"], body)
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["dest"] == "/work" and got["bytes"] == len(body) and got["files"] == 5
    assert set(got["paths"]) == {".gitignore", "package.json", "pnpm-workspace.yaml", "packages/web/package.json",
                                 "packages/web/index.ts"}
    work = up["machine"].root
    assert (work / "packages" / "web" / "package.json").read_text().count("2.0.0") == 1  # uncommitted work came
    assert (work / "packages" / "web" / "index.ts").is_file()
    assert not (work / "node_modules").exists() and not (work / ".env").exists()  # what git ignores stayed home
    assert up["machine"].calls == [(f"env/{out['id']}", "/work", True)]
    assert leftovers(node, out["id"]) == []  # the temp file is gone

    ran = await node["call"]("terminal_exec", cmd="pnpm install", cwd="/work")
    assert ran["exit_code"] == 0 and (in_bash("cd '/work' 2>/dev/null || { echo openajar-no-such-folder >&2; exit 97; }\npnpm install"), node["grant"].id) in node["machine"].ran
    state = await node["call"]("environment")
    tools = [c["tool"] for c in state["calls"]]
    assert tools[:3] == ["terminal_exec", "upload", "environment_upload"], tools  # newest first
    upload_call = next(c for c in state["calls"] if c["tool"] == "upload")
    assert upload_call["ok"] and "5 files" in upload_call["summary"] and "/work" in upload_call["summary"]

    traces = node["rt"].get("traces")
    if getattr(traces, "deferred", False):
        await traces.settled()
    [row], _ = traces.query(tool="environment.upload")
    assert row.status == "ok" and row.lane == f"env/{out['id']}" and row.grant_id == node["grant"].id
    assert row.args["files"] == 5 and row.args["dest"] == "/work" and row.args["sha256"] == got["sha256"] and len(got["sha256"]) == 71

    closed = await node["call"]("environment_close", discard=True)
    assert closed["state"] == "closed"


async def test_a_plain_folder_with_no_git_packs_too_and_an_empty_one_is_refused(node, up, tmp_path):  # noqa: F811
    """T3: in a folder that is no git repository the pack command made an empty archive. It now packs the
    folder (without node_modules, .env, .venv), and an archive with no files is refused with the reason."""
    await opened(node)
    calc = tmp_path / "calc"
    (calc / "node_modules" / "x").mkdir(parents=True)
    (calc / "node_modules" / "x" / "i.js").write_text("1\n")
    (calc / "tests").mkdir()
    (calc / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (calc / "tests" / "test_calc.py").write_text("from calc import add\n")
    (calc / "pyproject.toml").write_text("[project]\nname = 'calc'\n")
    (calc / ".env").write_text("SECRET=1\n")
    link = await up["link"]()
    res = await up["send"](link["url"], pack(calc))
    assert res.status_code == 200, res.text
    got = res.json()
    assert sorted(got["paths"]) == ["calc.py", "pyproject.toml", "tests/test_calc.py"] and got["files"] == 3
    assert 'terminal_exec(cmd="uv sync", cwd="/work")' in got["next"] and "environment_download" in got["next"]
    work = up["machine"].root
    assert (work / "calc.py").is_file() and not (work / ".env").exists() and not (work / "node_modules").exists()

    empty = tmp_path / "empty"
    (empty / "only-a-folder").mkdir(parents=True)
    link = await up["link"]()
    res = await up["send"](link["url"], pack(empty))
    assert res.status_code == 400 and "holds no files" in res.json()["error"]["message"]


async def test_uncommitted_files_overlay_the_clone_and_nothing_else_is_deleted(node, up, tmp_path):  # noqa: F811
    out = await opened(node, repo="example.test/me/shop")
    assert out["workdir"] == "/work/shop"
    clone = up["machine"].root / "shop"  # what the clone holds on the machine
    (clone / "src").mkdir(parents=True)
    (clone / "src" / "app.ts").write_text("old\n")
    (clone / "README.md").write_text("kept\n")
    (clone / "dist").mkdir()
    (clone / "dist" / "built.js").write_text("built on the machine\n")

    mine = tmp_path / "shop"
    mine.mkdir()
    git("init", "-q", "-b", "main", str(mine), cwd=tmp_path)
    (mine / "src").mkdir()
    (mine / "src" / "app.ts").write_text("old\n")
    (mine / ".gitignore").write_text("dist/\nwork.tgz\n")
    git("add", ".", cwd=mine)
    git("commit", "-qm", "one", cwd=mine)
    (mine / "src" / "app.ts").write_text("new, not committed\n")
    (mine / "src" / "cart.ts").write_text("export {}\n")
    (mine / "dist").mkdir()
    (mine / "dist" / "built.js").write_text("my local build\n")

    link = await up["link"]()
    assert link["dest"] == "/work/shop"
    res = await up["send"](link["url"], pack(mine), method="POST")
    assert res.status_code == 200, res.text
    assert (clone / "src" / "app.ts").read_text() == "new, not committed\n"
    assert (clone / "src" / "cart.ts").read_text() == "export {}\n"
    assert (clone / "README.md").read_text() == "kept\n"  # not in the archive: not deleted
    assert (clone / "dist" / "built.js").read_text() == "built on the machine\n"  # ignored at home: not sent

    sub = await up["link"](dest="packages/web")
    assert sub["dest"] == "/work/shop/packages/web"
    for bad in ("/etc", "../../etc", "/work/../root"):
        with pytest.raises(ToolCallError, match="outside /work"):
            await up["link"](dest=bad)


# ------------------------------------------------------------------ the link
async def test_a_link_is_spent_on_first_use_and_a_tampered_or_expired_one_is_refused(node, up, tmp_path):  # noqa: F811
    out = await opened(node)
    body = tar_of((member("a.txt"), b"a\n"))
    first = await up["link"]()
    res = await up["send"](first["url"].replace("?t=", "?t=x"), body)
    assert res.status_code == 403 and res.json()["error"]["code"] == "denied"
    assert (await up["send"](first["url"], body)).status_code == 200
    again = await up["send"](first["url"], body)
    assert again.status_code == 403 and "used already" in again.json()["error"]["message"]

    other = await up["link"]()
    wrong_env = other["url"].replace(f"/env/{out['id']}/", "/env/eother1234/")
    assert (await up["send"](wrong_env, body)).status_code == 403
    proof_like = other["url"].replace(f"/env/{out['id']}/", f"/env/{out['id']}-browser/")
    assert (await up["send"](proof_like, body)).status_code == 403
    up["clock"].now += uploads.UPLOAD_LINK_S + 1
    assert (await up["send"](other["url"], body)).status_code == 403  # expired
    view = up["tokens"].issue(f"env/{out['id']}-browser", "control").token  # a live view link is no upload link
    path = urlsplit(other["url"]).path
    assert (await up["send"](f"http://localhost{path}?t={view}", body)).status_code == 403
    assert len(up["machine"].calls) == 1


async def test_the_link_dies_when_the_environment_closes(node, up):  # noqa: F811
    await opened(node)
    link = await up["link"]()
    await node["call"]("environment_close", discard=True)
    res = await up["send"](link["url"], tar_of((member("a.txt"), b"a\n")))
    assert res.status_code == 403 and "closed" in res.json()["error"]["message"]
    assert up["machine"].calls == []
    with pytest.raises(ToolCallError, match="no_environment"):
        await up["link"]()


async def test_a_link_needs_the_chats_own_environment(node, up):  # noqa: F811
    with pytest.raises(ToolCallError, match="no_environment"):
        await up["link"]()
    await opened(node)
    with pytest.raises(ToolCallError, match="environment_upload puts files"):
        await up["link"](on="dev/other")


# ------------------------------------------------------------------ the archive
def _symlink(name, target):
    return member(name, tarfile.SYMTYPE, target), None


def _hardlink(name, target):
    return member(name, tarfile.LNKTYPE, target), None


@pytest.mark.parametrize(("members", "why"), [
    ([(member("/etc/cron.d/x"), b"x")], "absolute path"),
    ([(member("../outside.txt"), b"x")], ".. in its path"),
    ([(member("a/../../outside.txt"), b"x")], ".. in its path"),
    ([_symlink("escape", "../../etc")], "refused"),
    ([_symlink("escape", "/etc/passwd")], "refused"),
    ([_hardlink("hard", "/etc/passwd")], "refused"),
    ([(member("pipe", tarfile.FIFOTYPE), None)], "special file"),
    ([(member("dev", tarfile.CHRTYPE), None)], "special file"),
    ([(member("blk", tarfile.BLKTYPE), None)], "special file"),
])
async def test_a_bad_member_refuses_the_whole_archive_before_the_machine_sees_it(node, up, members, why):  # noqa: F811
    out = await opened(node)
    link = await up["link"]()
    body = tar_of((member("fine.txt"), b"fine\n"), *members)
    res = await up["send"](link["url"], body)
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "invalid" and why in res.json()["error"]["message"]
    assert up["machine"].calls == [] and leftovers(node, out["id"]) == []
    assert not (up["machine"].root / "fine.txt").exists()
    state = await node["call"]("environment")
    failed = next(c for c in state["calls"] if c["tool"] == "upload")
    assert not failed["ok"] and failed["error"] == "invalid"


async def test_links_inside_the_folder_and_a_plain_tar_are_fine(node, up):  # noqa: F811
    await opened(node)
    link = await up["link"]()
    body = tar_of((member("pkg/real.txt"), b"real\n"), _symlink("pkg/alias.txt", "real.txt"),
                  _hardlink("pkg/hard.txt", "pkg/real.txt"), gz=False)
    res = await up["send"](link["url"], body)
    assert res.status_code == 200, res.text
    work = up["machine"].root
    assert (work / "pkg" / "alias.txt").read_text() == "real\n" and (work / "pkg" / "hard.txt").read_text() == "real\n"
    assert up["machine"].calls[0][2] is False  # no -z for a plain tar


async def test_too_big_empty_or_not_a_tarball_is_refused_and_leaves_nothing(node, up, monkeypatch):  # noqa: F811
    out = await opened(node)
    monkeypatch.setattr(uploads, "UPLOAD_BYTES_MAX", 1000)
    big = await up["link"]()
    res = await up["send"](big["url"], b"x" * 5000)
    assert res.status_code == 413 and res.json()["error"]["code"] == "too_large"
    for body, words in ((b"", "send the archive"), (b"hello, not a tar at all" * 3, "not a .tar.gz")):
        link = await up["link"]()
        res = await up["send"](link["url"], body)
        assert res.status_code == 400 and words in res.json()["error"]["message"], res.text
    monkeypatch.setattr(uploads, "UPLOAD_BYTES_MAX", 1 << 20)
    monkeypatch.setattr(uploads, "UNPACKED_BYTES_MAX", 10)
    bomb = await up["link"]()
    res = await up["send"](bomb["url"], tar_of((member("big.bin"), b"\0" * 1000)))
    assert res.status_code == 400 and "unpacks to more than" in res.json()["error"]["message"]
    assert up["machine"].calls == [] and leftovers(node, out["id"]) == []


async def test_a_failed_extract_on_the_machine_says_why_and_cleans_up(node, up, monkeypatch):  # noqa: F811
    out = await opened(node)

    async def broken(lane_id, dest, archive, *, gzip, timeout_s):
        return ExecResult(exit_code=2, stdout="", stderr="tar: No space left on device", duration_s=0.01)

    monkeypatch.setattr(node["envs"], "put_archive", broken)
    link = await up["link"]()
    res = await up["send"](link["url"], tar_of((member("a.txt"), b"a\n")))
    assert res.status_code == 502 and "No space left" in res.json()["error"]["message"]
    assert leftovers(node, out["id"]) == []


# ------------------------------------------------------------------ the machine's side
async def test_put_archive_extracts_into_the_folder_over_the_lanes_transport(tmp_path):
    """EnvManager.put_archive streams the archive into a workload's tar (here a local lane; on a VM lane the
    same workload runs over SSH): the archive's files replace theirs, the rest stays."""
    envs = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0, install_proxy_port=0))
    envs.create("ws/bench", preset="workbench", node_secrets_ok=True)
    try:
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.txt").write_text("old\n")
        (dest / "keep.txt").write_text("kept\n")
        archive = tmp_path / "w.tgz"
        archive.write_bytes(tar_of((member("a.txt"), b"new\n"), (member("sub/b.txt"), b"b\n")))
        with archive.open("rb") as f:
            res = await envs.put_archive("ws/bench", str(dest / "deeper"), f, gzip=True, timeout_s=30)
        assert res.exit_code == 0, res.stderr
        assert (dest / "deeper" / "a.txt").read_text() == "new\n" and (dest / "deeper" / "sub" / "b.txt").is_file()
        with archive.open("rb") as f:
            res = await envs.put_archive("ws/bench", str(dest), f, gzip=True, timeout_s=30)
        assert res.exit_code == 0 and (dest / "a.txt").read_text() == "new\n"
        assert (dest / "keep.txt").read_text() == "kept\n"
    finally:
        await envs.shutdown()
