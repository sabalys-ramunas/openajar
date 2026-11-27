"""Downloads of a lane's browser (openajar.node.camoufox.pages.PageTracker): a browser the node launched
hands its file over with path(); an environment's browser, reached with connect(), has no path() and
is streamed with save_as(), kept on its machine as a plain file too (openajar.environments.vmdownloads),
watched for size and stalls there, and what it saved there by itself is picked up (audit U3-01, U3-06)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from testkit.timing import until

from openajar.environments import vmbrowser_ctl as ctl
from openajar.environments import vmdownloads
from openajar.environments.vmbrowser import DOWNLOAD_PREFS, browser_options
from openajar.node.camoufox import pages
from openajar.node.camoufox.pages import PageTracker, download_note
from openajar.node.files import FilesConfig, FileStatus, LaneFiles

PDF = b"%PDF-1.7\n" + b"x" * 300
ARTIFACTS = "/tmp/playwright-artifacts-AbC123"


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.handlers: dict[str, list] = {}

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.handlers: dict[str, list] = {}
        self.url = "https://example.test/files"
        context.pages.append(self)

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event: str, arg: Any) -> None:
        for fn in list(self.handlers.get(event, [])):
            fn(arg)

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return ""


class Download:
    """What Playwright's Download offers on a connect()ed browser: save_as, and no path()."""

    def __init__(self, name: str, data: bytes, *, artifact: str | None, fail: str | None = None) -> None:
        self.suggested_filename = name
        self.url = f"https://example.test/download/{name}"
        self.data = data
        self.fail = fail
        self.finished = asyncio.Event()
        self.cancelled = self.deleted = False
        self.saved_to: list[Path] = []
        self._impl_obj = SimpleNamespace(_artifact=SimpleNamespace(absolute_path=artifact))

    def finish(self) -> None:
        self.finished.set()

    async def failure(self) -> str | None:
        await self.finished.wait()
        return "canceled" if self.cancelled else self.fail

    async def save_as(self, path: Any) -> None:
        self.saved_to.append(Path(path))
        Path(path).write_bytes(self.data)

    async def cancel(self) -> None:
        self.cancelled = True
        self.finished.set()

    async def delete(self) -> None:
        self.deleted = True


class LocalDownload(Download):
    """A browser the node launched: path() is its file here."""

    def __init__(self, name: str, data: bytes, *, artifact: Path) -> None:
        super().__init__(name, data, artifact=str(artifact))
        artifact.write_bytes(data)
        self.artifact = artifact

    async def path(self) -> str:
        return str(self.artifact)

    async def save_as(self, path: Any) -> None:
        raise AssertionError("a browser on the node is never asked for save_as")


class Machine:
    folder = vmdownloads.DOWNLOADS_ON_VM

    def __init__(self) -> None:
        self.kept: list[tuple[str, str]] = []
        self.sizes: list[int | None] = []
        self.asked_size = 0
        self.saved: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.keep_fails = False
        self.windows: list[dict[str, Any]] = []

    async def browser_windows(self) -> list[dict[str, Any]]:
        return self.windows

    async def keep(self, src: str, name: str) -> str:
        if self.keep_fails:
            raise RuntimeError("the machine did not answer")
        self.kept.append((src, name))
        return f"{self.folder}/{name}"

    async def size(self, src: str) -> int | None:
        self.asked_size += 1
        return self.sizes.pop(0) if len(self.sizes) > 1 else (self.sizes[0] if self.sizes else None)

    async def saves(self, known: set[str]) -> list[dict[str, Any]]:
        return [f for f in self.saved if f["name"] not in known]

    async def fetch(self, name: str, dest: Path, max_bytes: int) -> None:
        dest.write_bytes(self.files[name])


def tracker(tmp_path: Path, machine: Any = None, **limits: Any) -> tuple[PageTracker, FakePage, LaneFiles]:
    (tmp_path / "lane").mkdir(parents=True, exist_ok=True)
    files = LaneFiles(tmp_path / "lane" / "files", FilesConfig(**limits))
    context = FakeContext()
    page = FakePage(context)
    return PageTracker(context, files=files, machine=machine), page, files


async def finished(files: LaneFiles) -> Any:
    await until(lambda: files.list() and files.list()[0].status is not FileStatus.DOWNLOADING)
    return files.list()[0]


# ---- a connect()ed browser -------------------------------------------------------------------

async def test_a_connected_download_is_streamed_with_save_as_and_kept_on_the_machine(tmp_path):
    machine = Machine()
    t, page, files = tracker(tmp_path, machine)
    mark = t.mark(saves=True)
    download = Download("report.pdf", PDF, artifact=f"{ARTIFACTS}/0f3c")
    page.emit("download", download)
    download.finish()
    meta = await finished(files)
    assert meta.status is FileStatus.COMPLETE and meta.size == len(PDF) and meta.kind == "pdf"
    assert files.path(meta.id).read_bytes() == PDF
    assert meta.machine_path == "/work/downloads/report.pdf"
    assert machine.kept == [(f"{ARTIFACTS}/0f3c", "report.pdf")]  # the browser's own file on the machine
    assert download.deleted
    assert not any(p.name.startswith(".") and p.name != ".meta.json" for p in files.path(meta.id).parent.iterdir())
    public = meta.public()
    assert public["machine_path"] == "/work/downloads/report.pdf"
    assert 'environment_download(path="/work/downloads/report.pdf")' in public["to_human"]
    notes = await t.notes_since(mark)
    assert notes == [(f"a download started: report.pdf; it finished (309 bytes): {meta.id} in browser_files and on "
                     "the machine at /work/downloads/report.pdf")]


async def test_a_connected_download_that_fails_says_why(tmp_path):
    t, page, files = tracker(tmp_path, Machine())
    mark = t.mark()
    download = Download("big.zip", b"", artifact=f"{ARTIFACTS}/1", fail="net::ERR_CONNECTION_RESET")
    page.emit("download", download)
    download.finish()
    meta = await finished(files)
    assert meta.status is FileStatus.FAILED
    (note,) = await t.notes_since(mark)
    assert note.startswith("the download failed: the download did not finish: net::ERR_CONNECTION_RESET (big.zip")


async def test_a_download_the_machine_could_not_keep_is_still_in_the_outbox(tmp_path):
    machine = Machine()
    machine.keep_fails = True
    _t, page, files = tracker(tmp_path, machine)
    download = Download("notes.txt", b"hello\n", artifact=f"{ARTIFACTS}/2")
    page.emit("download", download)
    download.finish()
    meta = await finished(files)
    assert meta.status is FileStatus.COMPLETE and meta.machine_path is None
    assert files.path(meta.id).read_bytes() == b"hello\n"


async def test_the_size_cap_holds_while_a_connected_download_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "REMOTE_POLL", 0.01)
    machine = Machine()
    machine.sizes = [50, 5000]
    _t, page, files = tracker(tmp_path, machine, download_max_bytes=1000)
    download = Download("huge.iso", b"", artifact=f"{ARTIFACTS}/3")
    page.emit("download", download)  # never finishes by itself: the watch on the machine cancels it
    meta = await finished(files)
    assert download.cancelled
    assert meta.status is FileStatus.FAILED and "larger than the 1000 bytes download limit" in meta.error
    assert meta.size == 5000 and download.saved_to == []


async def test_a_connected_download_that_stops_growing_is_cancelled_as_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "REMOTE_POLL", 0.01)
    machine = Machine()
    machine.sizes = [120]
    _t, page, files = tracker(tmp_path, machine, download_stall_seconds=0.2)
    download = Download("stuck.bin", b"", artifact=f"{ARTIFACTS}/4")
    page.emit("download", download)
    meta = await finished(files)
    assert download.cancelled
    assert meta.status is FileStatus.PARTIAL and meta.error.startswith("stalled") and meta.size == 120


async def test_a_machine_that_does_not_answer_is_no_stall(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "REMOTE_POLL", 0.01)
    machine = Machine()
    machine.sizes = [None]
    _t, page, files = tracker(tmp_path, machine, download_stall_seconds=0.05)
    download = Download("slow.csv", b"a,b\n1,2\n", artifact=f"{ARTIFACTS}/5")
    page.emit("download", download)
    await until(lambda: machine.asked_size >= 20)  # well past the stall time, with no size known
    assert not download.cancelled
    download.finish()
    assert (await finished(files)).status is FileStatus.COMPLETE


async def test_a_short_connected_download_never_asks_the_machine_its_size(tmp_path):
    machine = Machine()
    _t, page, files = tracker(tmp_path, machine)
    download = Download("a.txt", b"a\n", artifact=f"{ARTIFACTS}/6")
    page.emit("download", download)
    download.finish()
    await finished(files)
    assert machine.asked_size == 0


# ---- what the browser saved on its machine by itself (the PDF viewer's Save) ----------------

async def test_a_file_the_browser_saved_itself_is_brought_to_the_outbox_once(tmp_path):
    machine = Machine()
    machine.saved = [{"name": "1412.6980v9.pdf", "size": len(PDF)}]
    machine.files = {"1412.6980v9.pdf": PDF}
    t, _page, files = tracker(tmp_path, machine)
    mark = t.mark(saves=True)
    (note,) = await t.notes_since(mark)
    (meta,) = files.list()
    assert meta.status is FileStatus.COMPLETE and files.path(meta.id).read_bytes() == PDF
    assert meta.machine_path == "/work/downloads/1412.6980v9.pdf" and meta.url is None
    assert note.startswith("a download started: 1412.6980v9.pdf; it finished")
    assert await t.notes_since(t.mark(saves=True)) == []  # the next click finds nothing new
    await pages.pick_up_saves(files.root)                  # nor does browser_files
    assert len(files.list()) == 1


async def test_a_save_over_the_limit_stays_on_the_machine_only(tmp_path):
    machine = Machine()
    machine.saved = [{"name": "dump.tar", "size": 10_000}]
    t, _page, files = tracker(tmp_path, machine, download_max_bytes=1000)
    (note,) = await t.notes_since(t.mark(saves=True))
    (meta,) = files.list()
    assert meta.status is FileStatus.FAILED and meta.machine_path == "/work/downloads/dump.tar"
    assert note.startswith("the download failed: larger than the 1000 bytes download limit")
    assert "on the machine at /work/downloads/dump.tar" in note


async def test_a_name_the_node_kept_is_never_taken_for_a_save(tmp_path):
    machine = Machine()
    t, page, files = tracker(tmp_path, machine)
    download = Download("r.pdf", PDF, artifact=f"{ARTIFACTS}/7")
    page.emit("download", download)
    download.finish()
    await finished(files)
    machine.saved = [{"name": "r.pdf", "size": len(PDF)}]  # the copy download_keep made
    assert await t.notes_since(t.mark(saves=True)) == []
    assert len(files.list()) == 1


# ---- a browser the node launched: as before ------------------------------------------------

async def test_a_local_download_still_comes_from_path_and_never_from_a_machine(tmp_path):
    t, page, files = tracker(tmp_path)
    assert t.mark(saves=True)[1] is False  # no machine: a click never looks for saves
    mark = t.mark()
    download = LocalDownload("export.csv", b"a,b\n1,2\n", artifact=tmp_path / "artifact-1")
    page.emit("download", download)
    download.finish()
    meta = await finished(files)
    assert meta.status is FileStatus.COMPLETE and meta.machine_path is None and meta.kind == "csv"
    assert files.path(meta.id).read_bytes() == b"a,b\n1,2\n"
    assert "to_human" not in meta.public() and "machine_path" not in meta.public()
    assert await t.notes_since(mark) == [download_note(meta)]
    assert download.deleted


async def test_browser_files_with_no_browser_on_a_machine_asks_nothing(tmp_path):
    _t, _page, files = tracker(tmp_path)
    await pages.pick_up_saves(files.root)  # a local browser: returns at once
    await pages.pick_up_saves(tmp_path / "nowhere")


# ---- the machine's side (vmbrowser_ctl) --------------------------------------------------------

@pytest.fixture
def machine_fs(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    art = temp / "playwright-artifacts-XyZ"
    art.mkdir(parents=True)
    downloads = tmp_path / "work" / "downloads"
    monkeypatch.setattr(ctl, "DOWNLOADS", str(downloads))
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    return art, downloads


def said(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def keep(capsys, src: Path | str, name: str) -> dict:
    try:
        ctl.download_keep({"src": str(src), "name": name})
    except Exception as e:  # noqa: BLE001 - main() turns it into {"ok": false}
        return {"ok": False, "error": str(e)}
    return said(capsys)


@pytest.mark.parametrize("hostile, expect", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\boot.ini", "boot.ini"),
    ("/root/.ssh/authorized_keys", "authorized_keys"),
    (".bashrc", "bashrc"),
    ("..", "download"),
    ("a\x00b\nc.txt", "abc.txt"),
    ("", "download"),
])
def test_a_hostile_name_lands_in_the_folder_only(machine_fs, capsys, hostile, expect):
    art, downloads = machine_fs
    src = art / "f1"
    src.write_bytes(b"data")
    out = keep(capsys, src, hostile)
    assert out["ok"] and out["path"] == f"{downloads}/{expect}"
    assert sorted(p.name for p in downloads.iterdir()) == [expect]
    assert (downloads / expect).read_bytes() == b"data"


def test_a_taken_name_is_never_replaced(machine_fs, capsys):
    art, downloads = machine_fs
    downloads.mkdir(parents=True)
    (downloads / "report.pdf").write_bytes(b"the agent's own")
    for i in range(2):
        (art / f"d{i}").write_bytes(b"new %d" % i)
        keep(capsys, art / f"d{i}", "report.pdf")
    assert (downloads / "report.pdf").read_bytes() == b"the agent's own"
    assert (downloads / "report (1).pdf").read_bytes() == b"new 0"
    assert (downloads / "report (2).pdf").read_bytes() == b"new 1"


def test_a_long_name_is_cut_to_150_bytes_keeping_its_extension(machine_fs, capsys):
    art, _downloads = machine_fs
    (art / "f").write_bytes(b"x")
    out = keep(capsys, art / "f", "é" * 200 + ".pdf")
    name = Path(out["path"]).name
    assert len(name.encode()) <= 150 and name.endswith(".pdf")


def test_only_a_file_of_the_browser_is_kept(machine_fs, capsys, tmp_path):
    art, downloads = machine_fs
    secret = tmp_path / "secret"
    secret.write_bytes(b"not a download")
    assert not keep(capsys, secret, "x")["ok"]                       # outside the artifacts folder
    assert not keep(capsys, f"{art}/../../secret", "x")["ok"]        # a path that climbs out
    (art / "link").symlink_to(secret)
    assert not keep(capsys, art / "link", "x")["ok"]                 # a link in the folder
    assert not downloads.exists() or list(downloads.iterdir()) == []


def test_a_downloads_folder_that_is_a_link_is_refused(machine_fs, capsys, tmp_path):
    art, downloads = machine_fs
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    downloads.parent.mkdir(parents=True)
    downloads.symlink_to(elsewhere)
    (art / "f").write_bytes(b"x")
    assert not keep(capsys, art / "f", "a.txt")["ok"]
    assert list(elsewhere.iterdir()) == []


def test_a_running_downloads_size_counts_its_part_file(machine_fs, capsys):
    art, _downloads = machine_fs
    (art / "d").write_bytes(b"")
    (art / "d.part").write_bytes(b"x" * 70)
    ctl.download_size({"src": str(art / "d")})
    assert said(capsys) == {"ok": True, "size": 70}


def test_saves_are_complete_files_written_since_the_browser_started(machine_fs, capsys, monkeypatch):
    _art, downloads = machine_fs
    downloads.mkdir(parents=True)
    old = downloads / "old.pdf"
    old.write_bytes(b"from before")
    os.utime(old, (1000, 1000))
    (downloads / "paper.pdf").write_bytes(PDF)
    (downloads / "writing.pdf").write_bytes(b"")
    (downloads / "writing.pdf.part").write_bytes(b"half")
    (downloads / "kept.pdf").write_bytes(b"a download the node kept")
    monkeypatch.setattr(ctl, "_running", lambda name: 4242)
    monkeypatch.setattr(ctl, "_started_at", lambda pid: 2000.0)
    ctl.download_saves({"known": ["kept.pdf"]})
    assert said(capsys)["files"] == [{"name": "paper.pdf", "size": len(PDF)}]
    monkeypatch.setattr(ctl, "_running", lambda name: None)
    ctl.download_saves({"known": []})
    assert said(capsys)["files"] == []  # no browser running: nothing is its save


def test_a_fetched_save_must_be_one_regular_file(tmp_path):
    def archive(*members: tuple[str, bytes | None]) -> Path:
        path = tmp_path / f"a{len(list(tmp_path.iterdir()))}.tar"
        with tarfile.open(path, "w") as tf:
            for name, data in members:
                info = tarfile.TarInfo(name)
                if data is None:
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
                    tf.addfile(info)
                else:
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        return path

    vmdownloads._extract_one(archive(("./paper.pdf", PDF)), "paper.pdf", tmp_path / "ok", 10_000)
    assert (tmp_path / "ok").read_bytes() == PDF
    for bad in (archive(("./paper.pdf", None)), archive(("./paper.pdf", PDF), ("./x", b"x")),
                archive(("./other.pdf", PDF))):
        with pytest.raises(Exception, match="not a regular file"):
            vmdownloads._extract_one(bad, "paper.pdf", tmp_path / "no", 10_000)
    with pytest.raises(Exception, match="limit"):
        vmdownloads._extract_one(archive(("./paper.pdf", PDF)), "paper.pdf", tmp_path / "no", 10)
    assert not (tmp_path / "no").exists()


# ---- the environment browser's prefs -------------------------------------------------------------

def test_the_environment_browser_saves_without_asking_into_the_machines_folder():
    from openajar.core.models import LaneSpec

    prefs = browser_options(LaneSpec(id="env/e1-browser"))["firefox_user_prefs"]
    assert {k: prefs[k] for k in DOWNLOAD_PREFS} == {
        "browser.download.useDownloadDir": True, "browser.download.folderList": 2,
        "browser.download.dir": "/work/downloads", "browser.download.always_ask_before_handling_new_types": False,
        "browser.download.alwaysOpenPanel": False, "pdfjs.disabled": False}
    assert ctl.DOWNLOADS == vmdownloads.DOWNLOADS_ON_VM


def test_only_a_click_in_the_pdf_viewer_or_a_save_key_looks_on_the_machine(tmp_path):
    t, page, _files = tracker(tmp_path, Machine())
    assert not t.may_save(page, None)  # an ordinary page: a click costs no command on the machine
    pdf = "https://example.test/paper.pdf"
    response = SimpleNamespace(url=pdf, headers={"content-type": "application/pdf; qs=1"},
                               request=SimpleNamespace(is_navigation_request=lambda: True))
    for fn in t._ctx.handlers["response"]:
        fn(response)
    page.url = pdf
    assert t.may_save(page, None)
    assert t.may_save(page, "Control+s") and t.may_save(page, "Meta+S") and t.may_save(page, "ControlOrMeta+s")
    assert not t.may_save(page, "s") and not t.may_save(page, "Control+Shift+a") and not t.may_save(page, "Enter")
    local, page2, _ = tracker(tmp_path / "local")
    assert not local.may_save(page2, "Control+s")


# ---- a window of the browser's own (the PDF viewer's Save As) -------------------------------------

BROWSER_WINDOW = {"id": 6291477, "title": "dummy.pdf", "app": "camoufox", "kind": "browser", "x": 72, "y": 100,
                  "w": 1786, "h": 900}
SAVE_AS = {"id": 6291786, "title": "Save As - dummy.pdf", "app": "camoufox", "kind": "browser", "x": 403, "y": 131,
           "w": 1126, "h": 859, "front": True}
TERMINAL = {"id": 10485763, "title": "commands", "app": "Lxterminal", "kind": "agent_terminal", "x": 104, "y": 540,
            "w": 710, "h": 425}


def test_a_browser_window_that_shows_no_tab_is_named():
    assert pages.outside_windows([SAVE_AS, BROWSER_WINDOW, TERMINAL], {"dummy.pdf"}) == [SAVE_AS]
    assert pages.outside_windows([BROWSER_WINDOW, TERMINAL], {"dummy.pdf"}) == []
    popup = {**SAVE_AS, "title": "Sign in"}  # a popup window shows a tab: not a dialog
    assert pages.outside_windows([popup, BROWSER_WINDOW], {"dummy.pdf", "Sign in"}) == []
    assert pages.outside_windows([{**SAVE_AS, "minimized": True}, BROWSER_WINDOW], set()) == []


async def test_a_save_click_that_saved_nothing_names_the_open_window(tmp_path):
    machine = Machine()
    machine.windows = [SAVE_AS, BROWSER_WINDOW, TERMINAL]
    t, page, _files = tracker(tmp_path, machine)
    page.title = lambda: asyncio.sleep(0, "dummy.pdf")  # type: ignore[method-assign]
    (note,) = await t.notes_since(t.mark(saves=True))
    assert note.startswith("the browser opened a window of its own over the page: 'Save As - dummy.pdf' (window "
                           "6291786)")
    assert '"keys": "Return", "window": 6291786' in note and "/work/downloads" in note
    assert await t.notes_since(t.mark(saves=False)) == []  # an ordinary click never asks the screen
    machine.windows = [BROWSER_WINDOW]
    assert await t.notes_since(t.mark(saves=True)) == []
    assert await t.window_notes({"dummy.pdf"}) == []


async def test_a_local_browser_never_reads_a_screen(tmp_path):
    t, _page, _files = tracker(tmp_path)
    assert await t.window_notes() == []
