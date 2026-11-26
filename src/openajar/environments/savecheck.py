"""A save's file part as it arrives from the machine, checked on the node before anything may rely on it
(openajar.environments.saving): only the payload names SAVE writes, every sum matches, the tarball holds only
saved folders, each bundle is one, and every path git lists as changed is in it.

The last check is the 0.7.0 audit's S1-02: SAVE packed `git ls-files -m -o`, which leaves out what was staged
with git add, so a rescue said "saved (0 files)" and deleted the machine. SAVE now packs what differs from HEAD
plus the untracked files, and writes each repository's `git status` paths (r<n>.status: `c<path>` for one on the
disk, `d<path>` for one that is not); `unheld` names each one on the disk that the tarball does not hold, and a
save that has one is not called whole.
"""

from __future__ import annotations

import re
import tarfile
from pathlib import Path
from typing import BinaryIO

from openajar.environments.errors import SaveFailed
from openajar.environments.saves import MANIFEST, SaveCorrupt, check_bundle, check_files_tar, sha256_file

PAYLOAD_RE = re.compile(r"^(files\.tgz|r\d{1,3}\.(bundle|deleted|status))$")
DELETED_KEPT = 5000
UNHELD_SHOWN = 5


def unpack(archive: Path, into: Path) -> None:
    """The staging folder's tar from the machine, into the save's temp folder: only the payload names
    SAVE writes, plain files, nothing else."""
    try:
        with tarfile.open(archive, "r:*") as tf:
            for m in tf:
                name = m.name.removeprefix("./")
                if m.isdir() and name in ("", "."):
                    continue
                if not m.isreg() or not PAYLOAD_RE.match(name):
                    raise SaveFailed(f"the machine's save folder holds {name[:80]!r}, which a save never writes")
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(into / name, "wb") as out:
                    _copy(src, out)
    except tarfile.TarError as e:
        raise SaveFailed(f"the save did not arrive whole from the machine: {e}") from None
    finally:
        archive.unlink(missing_ok=True)


def _copy(src: BinaryIO, dst: BinaryIO) -> None:
    dst.writelines(iter(lambda: src.read(1 << 20), b""))


def verify(tmp: Path, found: dict, roots: list[str]) -> int:
    """Every file SAVE summed arrived with the same sha256, nothing else arrived, the tarball lists and
    holds only saved folders, and each bundle is one. Returns the tarball's file count; its member names
    go to found["names"]."""
    have = {p.name for p in tmp.iterdir() if p.is_file() and p.name != MANIFEST}
    if have != set(found["sums"]):
        raise SaveFailed("the save did not arrive whole from the machine (its files differ from what it wrote)")
    for name, want in found["sums"].items():
        if sha256_file(tmp / name) != want:
            raise SaveFailed(f"{name} arrived damaged from the machine")
    names: set[str] = set()
    try:
        n = check_files_tar(tmp / "files.tgz", roots, names)
        for name in have:
            if name.endswith(".bundle"):
                check_bundle(tmp / name)
    except SaveCorrupt as e:
        raise SaveFailed(e.message) from None
    found["names"] = names
    return n


def deleted(tmp: Path, found: dict) -> dict[str, list[str]]:
    """Each repository's deleted paths (from r<n>.deleted, which then leaves the payload)."""
    out: dict[str, list[str]] = {}
    for name in found["repos"]:
        f = tmp / f"{name}.deleted"
        if f.exists():
            paths = [p for p in f.read_bytes().decode(errors="replace").split("\0") if p]
            if paths:
                out[name] = paths[:DELETED_KEPT]
            f.unlink()
    return out


def unheld(tmp: Path, found: dict) -> list[str]:
    """The paths git status lists in each repository that are on the machine's disk and not in the file part
    (from r<n>.status, which then leaves the payload), as /work/... paths. [] when it holds them all."""
    names: set[str] = found.get("names") or set()
    out: list[str] = []
    for name, repo in found["repos"].items():
        f = tmp / f"{name}.status"
        if not f.exists():
            continue
        root = str(repo["dir"]).strip("/")
        for entry in f.read_bytes().decode(errors="replace").split("\0"):
            path = f"{root}/{entry[1:]}".rstrip("/")
            if entry[:1] == "c" and path not in names:
                out.append("/" + path)
        f.unlink()
    return out


def unheld_words(paths: list[str]) -> str:
    """save_failed's words for a file part that misses changed files."""
    shown = ", ".join(paths[:UNHELD_SHOWN]) + (f" and {len(paths) - UNHELD_SHOWN} more" if len(paths) > UNHELD_SHOWN
                                               else "")
    return (f"git lists {len(paths)} changed file{'s' if len(paths) != 1 else ''} that the save does not hold "
            f"({shown}), so it is not called whole.")
