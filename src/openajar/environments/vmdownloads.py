"""The downloads of an environment's browser that runs on its machine (openajar.environments.vmbrowser).

The node drives that browser with Playwright's `connect()`, so a download's file is on the machine, not
here: Playwright will not hand out its path (it does on a browser the node launched itself) and streams it
with `save_as` instead. A download ends up in two places:

- the lane's outbox on the node, as every browser's download does (browser_files lists it; the record has
  its status, size, type and sha256): streamed once over the connection, by the node's PageTracker
  (openajar.node.camoufox.pages);
- a plain file on the machine, in DOWNLOADS (/work/downloads), for the agent's terminal and for
  environment_download, which takes files under /work. It is the browser's own file on the machine,
  hard-linked (or copied, on another file system) by vmbrowser_ctl's download_keep, so the bytes never
  cross the network a second time. The machine takes only a regular file directly in a Playwright
  artifacts folder of its temp folder as the source, makes the name safe again itself (its last part, no
  control characters, no leading dots, 150 bytes), and creates the new name with O_EXCL in a folder it
  opened without following a link, adding " (1)", " (2)" ... when the name is taken: nothing is replaced,
  and nothing lands outside the folder.

The browser's prefs (vmbrowser.DOWNLOAD_PREFS) point its own saves at the same folder. What Firefox saves
by itself, never as a download Playwright sees (the PDF viewer's Save button, Ctrl+S), still opens its Save
As window: Firefox asks there whatever the prefs say (contentAreaUtils.js promiseTargetFile turns
useDownloadDir off for a save that may ask, and the viewer's Save is one). The window opens on that folder,
so Enter saves the file there. `browser_windows` reads the screen's windows (what screen_snapshot reads, on
the desktop) so the tracker can name such a window in the click's result and in browser_tabs; `saves` finds
a file saved there (complete, written since the browser started, a name the tracker has not seen) and
`fetch` brings it into the outbox.

`MachineDownloads` is what the tracker calls; it holds no state of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tarfile
from pathlib import Path
from typing import Any

from openajar.core.errors import EngineError

log = logging.getLogger("openajar.environments")

DOWNLOADS_ON_VM = "/work/downloads"  # vmbrowser_ctl.DOWNLOADS
KEEP_TIMEOUT_S = 60.0     # a copy, when a link cannot be made: 200 MB on a slow disk
SIZE_TIMEOUT_S = 15.0
SAVES_TIMEOUT_S = 20.0
FETCH_TIMEOUT_S = 600.0
TAR_SLACK = 64 * 1024     # a tar of one file: its header and padding


class MachineDownloads:
    """The machine side of one environment browser's downloads, for openajar.node.camoufox.pages.PageTracker."""

    folder = DOWNLOADS_ON_VM

    def __init__(self, vm: Any, env: Any):
        self.vm = vm
        self.env = env

    async def keep(self, src: str, name: str) -> str:
        """The finished download `src` (the browser's own file on the machine) as a file in the folder,
        named after `name`. Returns its path on the machine."""
        out = await self.vm.ctl(self.env, "download_keep", src=src, name=name, timeout_s=KEEP_TIMEOUT_S)
        path = out.get("path")
        if not isinstance(path, str) or not path.startswith(self.folder + "/"):
            raise EngineError("the machine did not say where it kept the download")
        return path

    async def size(self, src: str) -> int | None:
        """How many bytes of a running download are on the machine (None: it cannot tell)."""
        out = await self.vm.ctl(self.env, "download_size", src=src, timeout_s=SIZE_TIMEOUT_S)
        value = out.get("size")
        return value if type(value) is int and value >= 0 else None

    async def saves(self, known: set[str]) -> list[dict[str, Any]]:
        """Complete files in the folder, written since the browser started, whose names are not in `known`."""
        out = await self.vm.ctl(self.env, "download_saves", known=sorted(known), timeout_s=SAVES_TIMEOUT_S)
        return [f for f in out.get("files") or [] if isinstance(f, dict) and _plain(f.get("name"))
                and type(f.get("size")) is int]

    async def browser_windows(self) -> list[dict[str, Any]]:
        """The screen's windows (screen_ctl look, no picture): [] on a screen with no desktop."""
        from openajar.environments import screen

        body = json.dumps({"script": screen.ctl_script(), "action_kind": "look", "image": False})
        res = await self.vm._exec(self.env, screen.ctl_command(), stdin=body, timeout_s=screen.LOOK_S)
        line = next((ln for ln in reversed((res.stdout or "").splitlines()) if ln.startswith("{")), "")
        try:
            out = json.loads(line) if line else {}
        except ValueError:
            return []
        return screen.clean_windows(out.get("windows")) if isinstance(out, dict) and out.get("ok") else []

    async def fetch(self, name: str, dest: Path, max_bytes: int) -> None:
        """The folder's file `name` into `dest` on the node (new, 0600), over the environment's SSH as a
        tar of that one file. Anything but one regular file of that name is refused, so a link in the
        folder never brings another file of the machine."""
        if not _plain(name):
            raise EngineError(f"{name!r} is not a plain file name")
        envs = self.vm.svc.envs()
        archive = dest.with_name(dest.name + ".tar")
        try:
            await envs.get_archive(self.env.terminal_lane, self.folder, archive, paths=[name], gzip=False,
                                   timeout_s=FETCH_TIMEOUT_S, max_bytes=max_bytes + TAR_SLACK)
            await asyncio.to_thread(_extract_one, archive, name, dest, max_bytes)
        finally:
            with contextlib.suppress(OSError):
                archive.unlink()


def _plain(name: Any) -> bool:
    return isinstance(name, str) and bool(name) and "/" not in name and name not in (".", "..") \
        and "\x00" not in name


def _extract_one(archive: Path, name: str, dest: Path, max_bytes: int) -> None:
    with tarfile.open(archive, "r:") as tf:
        members = tf.getmembers()
        if len(members) != 1 or members[0].name not in (name, f"./{name}") or not members[0].isreg():
            raise EngineError(f"{name} on the machine is not a regular file")
        member = members[0]
        if member.size > max_bytes:
            raise EngineError("over the download limit")
        source = tf.extractfile(member)
        if source is None:
            raise EngineError(f"{name} on the machine could not be read")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with source, os.fdopen(fd, "wb") as out:
            while chunk := source.read(1 << 20):
                out.write(chunk)
