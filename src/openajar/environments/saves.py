"""Saves of environments on the node: where they live, what a manifest says, how their files are
sealed, and how many are kept (openajar.environments.saving makes and restores them).

A save is a folder `$OPENAJAR_HOME/environments/saves/<family>/<title>/<save id>/` (folders 0700,
files 0600):

- `manifest.json`: what the save is (title, repository, branch, HEAD, when, the snapshot's id and
  size, the services, the resume note), readable without the vault, so the node lists saves while
  its vault is sealed;
- the file part, sealed with the node's saves key (`files.tgz`: changed and new files that git does
  not ignore; `r<n>.bundle`: each repository's commits and stashes no remote has; `r<n>.json`: its
  branch, HEAD and deleted paths). The key is 32 random bytes the vault holds as the machine secret
  SAVES_KEY_REF, so a copy of the folder is unreadable without the vault.

A save is written to a temp folder (`.tmp/`), verified, and only then moved into place, so a folder
under a title is always a whole save. Retention: keep_last saves of a title (an autosave only until
a newer save exists), max_saves_gb of file parts on the node, and saved_days.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import shutil
import struct
import tarfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, Field

from openajar.core import fs
from openajar.core.errors import InvalidRequest, NotFound

log = logging.getLogger("openajar.environments")

SAVES_DIR = ("environments", "saves")
MANIFEST = "manifest.json"
SAVES_KEY_REF = "agentos/environments/saves-key"
SAVE_ID_RE = re.compile(r"^s[a-z0-9]{10}$")
_ID_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"
CHUNK = 1 << 20
MAGIC = b"AGENTOS-SAVE-1\n"
NOTE_MAX = 4000
SaveKind = Literal["save", "autosave", "rescue"]


def new_save_id() -> str:
    return "s" + "".join(secrets.choice(_ID_CHARS) for _ in range(10))


def check_save_id(save_id: str) -> str:
    if not isinstance(save_id, str) or not SAVE_ID_RE.match(save_id):
        raise InvalidRequest(f"not a save id: {str(save_id)[:40]!r} (a save id is s and 10 letters or digits)")
    return save_id


def _slug(text: str, n: int = 40) -> str:
    """A folder name for a family or a title: its readable part, then a hash of the whole (two titles
    that read the same stay apart)."""
    readable = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "x"
    return f"{readable}-{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def title_key(title: str) -> str:
    return " ".join(title.split()).lower()


class Snapshot(BaseModel):
    id: str
    gb: float | None = None
    provider: str = ""
    server_type: str = ""
    location: str = ""


class Paused(BaseModel):
    """A save that kept the machine itself, paused with its memory (openajar.environments.pausing): its
    VM record and the provider's server, and what pausing it costs a month (an estimate)."""
    vm_id: str
    provider_id: str
    provider: str = ""
    server_type: str = ""
    location: str = ""
    eur_month: float | None = None


class RepoSave(BaseModel):
    dir: str                                 # /work/shop
    branch: str | None = None
    head: str | None = None
    bundle: str | None = None                # r1.bundle, when it has commits no remote has
    commits: int = 0                         # commits in the bundle
    stashes: list[str] = Field(default_factory=list)  # stash commits, newest first
    stash_messages: list[str] = Field(default_factory=list)  # and their messages


class Service(BaseModel):
    cmd: str
    cwd: str | None = None
    port: int | None = None


class SaveManifest(BaseModel):
    id: str
    kind: SaveKind = "save"
    reason: str = ""
    family: str
    title: str
    env_id: str
    agent: str = "an agent"
    member: str | None = None
    grant_id: str = ""
    repo: str | None = None
    ref: str | None = None
    branch: str | None = None
    head: str | None = None
    workdir: str = "/work"
    keep: str = "save"
    saved_at: datetime
    snapshot: Snapshot | None = None
    paused: Paused | None = None             # the machine paused with its memory, in place of a snapshot
    server_type: str = ""
    size: str | None = None
    location: str = ""
    browser: str = "node"
    files: int = 0                           # files in the file part (files.tgz)
    deleted: int = 0                         # paths the work tree deleted
    commits: int = 0                         # commits the bundles carry
    bytes: int = 0                           # the file part on disk
    repos: list[RepoSave] = Field(default_factory=list)
    keep_paths: list[str] = Field(default_factory=list)
    services: list[Service] = Field(default_factory=list)
    containers: list[str] = Field(default_factory=list)   # docker containers that ran (started again on restore)
    own_branches: list[str] = Field(default_factory=list)
    own_heads: dict[str, str] = Field(default_factory=dict)
    pushed: list[dict] = Field(default_factory=list)
    proofs: list[str] = Field(default_factory=list)       # the proofs' titles
    summary: str = ""                        # the automatic part of the resume note
    agent_note: str | None = None            # the agent's own words (untrusted)
    encrypted: bool = True
    checksums: dict[str, str] = Field(default_factory=dict)  # payload file: sha256 of its plain bytes
    warned_at: datetime | None = None        # the owner was told it is about to be removed (saved_days)
    signins: int = 0                         # cookies of the browser's sign-ins in the payload (browser.json)
    keep_signins: bool = True                # the environment saves its browser's sign-ins (its opt-out)
    browser_proxy: str | None = None         # the owner's proxy its browser exited through (a label, never a credential)
    fingerprint: bool = False                # the payload holds its browser's pinned device (fingerprint.json, vmpin)
    browser_exit_ip: str | None = None       # the address sites saw through it at the last launch before the save

    @property
    def title_key(self) -> str:
        return title_key(self.title)


# ---------------------------------------------------------------- sealing
class Cipher:
    """ChaCha20-Poly1305 (openajar.vault.crypto) over a file in CHUNK pieces: each piece's associated
    data names the save, the file, its index and whether it is the last, so pieces cannot be moved,
    reordered or cut off without the open failing."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("a saves key is 32 bytes")
        self._key = key

    def _aad(self, save_id: str, name: str, i: int, last: bool) -> bytes:
        return f"{save_id}/{name}/{i}/{int(last)}".encode()

    def seal(self, save_id: str, name: str, src: BinaryIO, dst: BinaryIO) -> None:
        from openajar.vault.crypto import seal

        dst.write(MAGIC)
        i = 0
        chunk = src.read(CHUNK)
        while True:
            nxt = src.read(CHUNK)
            last = not nxt
            blob = seal(self._key, chunk, self._aad(save_id, name, i, last))
            dst.write(struct.pack(">I", len(blob)) + blob)
            if last:
                return
            chunk, i = nxt, i + 1

    def open(self, save_id: str, name: str, src: BinaryIO, dst: BinaryIO) -> None:
        from openajar.vault.crypto import DecryptError, unseal

        if src.read(len(MAGIC)) != MAGIC:
            raise SaveCorrupt(f"{name} is not a sealed save file")
        i = 0
        while True:
            head = src.read(4)
            if len(head) != 4:
                raise SaveCorrupt(f"{name} is cut short")
            blob = src.read(struct.unpack(">I", head)[0])
            last = _at_end(src)
            try:
                dst.write(unseal(self._key, blob, self._aad(save_id, name, i, last)))
            except DecryptError:
                raise SaveCorrupt(f"{name} does not open with this node's saves key (changed, or from "
                                  "another node)") from None
            if last:
                return
            i += 1


def _at_end(f: BinaryIO) -> bool:
    pos = f.tell()
    end = f.read(1) == b""
    f.seek(pos)
    return end


class SaveCorrupt(InvalidRequest):
    """A save's file does not open or does not match its checksum."""


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- the store
class SaveStore:
    """The saves folder of one node. Every method is synchronous file work (callers run it in a thread
    when the files may be large)."""

    def __init__(self, home: Path):
        self.root = Path(home).joinpath(*SAVES_DIR)

    def _mkdir(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        return path

    def tmp(self) -> Path:
        """A new temp folder for a save being made (moved into place by commit, else removed)."""
        base = self._mkdir(self.root / ".tmp")
        return self._mkdir(base / secrets.token_hex(6))

    def folder(self, family: str, title: str, save_id: str) -> Path:
        return self.root / _slug(family) / _slug(title_key(title)) / check_save_id(save_id)

    def commit(self, tmp: Path, manifest: SaveManifest) -> Path:
        """Write the manifest and move the verified save into place."""
        fs.write_private(tmp / MANIFEST, manifest.model_dump_json(indent=1))
        dest = self.folder(manifest.family, manifest.title, manifest.id)
        self._mkdir(dest.parent)
        os.rename(tmp, dest)
        fs.fsync_dir(dest.parent)
        return dest

    def write_manifest(self, manifest: SaveManifest) -> None:
        fs.write_private(self.folder(manifest.family, manifest.title, manifest.id) / MANIFEST,
                         manifest.model_dump_json(indent=1))

    def discard(self, tmp: Path) -> None:
        shutil.rmtree(tmp, ignore_errors=True)

    def _manifests(self) -> Iterator[tuple[SaveManifest, Path]]:
        if not self.root.is_dir():
            return
        for path in self.root.glob(f"*/*/s*/{MANIFEST}"):
            if path.parts[-4] == ".tmp":
                continue
            try:
                yield SaveManifest.model_validate_json(path.read_text()), path.parent
            except (OSError, ValueError) as e:
                log.warning("a save at %s does not read: %s", path.parent, e)

    def all(self, family: str | None = None) -> list[SaveManifest]:
        """Every save (of one family), newest first."""
        out = [m for m, _ in self._manifests() if family is None or m.family == family]
        return sorted(out, key=lambda m: m.saved_at, reverse=True)

    def of_title(self, family: str, title: str) -> list[SaveManifest]:
        key = title_key(title)
        return [m for m in self.all(family) if m.title_key == key]

    def latest(self, family: str, title: str) -> SaveManifest | None:
        saves = self.of_title(family, title)
        return saves[0] if saves else None

    def find(self, save_id: str) -> tuple[SaveManifest, Path]:
        check_save_id(save_id)
        for m, path in self._manifests():
            if m.id == save_id:
                return m, path
        raise NotFound(f"no save {save_id}")

    def sharing(self, m: SaveManifest) -> list[str]:
        """The other saves that name m's disk snapshot (an import on this node shares the original's)."""
        if m.snapshot is None:
            return []
        return [o.id for o in self.all() if o.id != m.id and o.snapshot is not None and o.snapshot.id == m.snapshot.id]

    def move(self, save_id: str, *, family: str, title: str | None = None) -> SaveManifest:
        """Give a save to another grant family, under another title when one is given (openajar.environments.adopt):
        its folder moves first, then its manifest says so, so a move that stops halfway is found and finished by
        the next one (the payload's sealing names the save id, never its family or title)."""
        m, path = self.find(save_id)
        moved = m.model_copy(update={"family": family, "title": title or m.title})
        dest = self.folder(moved.family, moved.title, moved.id)
        if dest != path:
            self._mkdir(dest.parent)
            os.rename(path, dest)
            fs.fsync_dir(dest.parent)
            for parent in (path.parent, path.parent.parent):  # an empty title, then an empty family
                try:
                    parent.rmdir()
                except OSError:
                    break
        self.write_manifest(moved)
        return moved

    def remove(self, save_id: str) -> SaveManifest:
        m, path = self.find(save_id)
        shutil.rmtree(path, ignore_errors=True)
        for parent in (path.parent, path.parent.parent):  # an empty title, then an empty family
            try:
                parent.rmdir()
            except OSError:
                break
        return m

    def total_bytes(self, *, but: set[str] | None = None) -> int:
        return sum(m.bytes for m in self.all() if m.id not in (but or set()))

    def sweep_tmp(self, older_than_s: float = 6 * 3600) -> int:
        """Temp folders a save that died halfway left (the node stopped while it saved)."""
        import time

        base = self.root / ".tmp"
        n = 0
        for d in base.glob("*") if base.is_dir() else []:
            try:
                if time.time() - d.stat().st_mtime > older_than_s:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                continue
        return n

    @staticmethod
    def pruned(saves: list[SaveManifest], keep_last: int) -> list[SaveManifest]:
        """Which saves of one title retention removes (they are newest first): every autosave older
        than the newest save, every autosave but the newest, and the full saves past keep_last."""
        drop: list[SaveManifest] = []
        full = [m for m in saves if m.kind != "autosave"]
        autos = [m for m in saves if m.kind == "autosave"]
        newest_full = full[0].saved_at if full else None
        for i, m in enumerate(autos):
            if i > 0 or (newest_full is not None and m.saved_at <= newest_full):
                drop.append(m)
        drop += full[keep_last:]
        return drop


# ---------------------------------------------------------------- the payload
def payload_names(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir() if p.name != MANIFEST and p.is_file())


def seal_payload(path: Path, save_id: str, key: bytes | None) -> dict[str, str]:
    """Seal every payload file of a save folder in place (key None: leave them as they are). Returns
    the sha256 of each plain file, checked again when a restore opens it."""
    sums: dict[str, str] = {}
    cipher = Cipher(key) if key is not None else None
    for name in payload_names(path):
        f = path / name
        sums[name] = sha256_file(f)
        os.chmod(f, 0o600)
        if cipher is None:
            continue
        sealed = path / f".{name}.sealed"
        with f.open("rb") as src, open(sealed, "wb", opener=_private) as dst:
            cipher.seal(save_id, name, src, dst)
        os.replace(sealed, f)
    return sums


def _private(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_CREAT, 0o600)


def open_payload(manifest: SaveManifest, path: Path, out: Path, key: bytes | None) -> dict[str, Path]:
    """The plain payload files of a save, in `out` (a folder of the caller's): each checked against the
    manifest's sha256. Raises SaveCorrupt."""
    if manifest.encrypted and key is None:
        raise SaveCorrupt("this save is sealed, and the vault gives no saves key (is it sealed? `openajar vault "
                          "unseal`)")
    cipher = Cipher(key) if manifest.encrypted and key is not None else None
    files: dict[str, Path] = {}
    for name, want in manifest.checksums.items():
        src, dst = path / name, out / name
        if not src.is_file():
            raise SaveCorrupt(f"the save has no {name}")
        if cipher is None:
            shutil.copyfile(src, dst)
        else:
            with src.open("rb") as fin, open(dst, "wb", opener=_private) as fout:
                cipher.open(manifest.id, name, fin, fout)
        if sha256_file(dst) != want:
            raise SaveCorrupt(f"{name} does not match its checksum")
        files[name] = dst
    return files


def check_files_tar(path: Path, roots: list[str]) -> int:
    """The file part's tarball: it opens, every member is a plain file, link or folder under one of
    `roots` (work/, a keep_paths folder) with no `..`. Returns how many files it holds."""
    n = 0
    with tarfile.open(path, "r:*") as tf:
        for m in tf:
            name = m.name.removeprefix("./")
            if name.startswith("/") or ".." in name.split("/") or not any(
                    name == r or name.startswith(r + "/") for r in roots):
                raise SaveCorrupt(f"the file part holds {name[:120]!r}, outside the saved folders")
            if not (m.isreg() or m.isdir() or m.issym()):
                raise SaveCorrupt(f"the file part holds {name[:120]!r}, which is not a plain file")
            if m.issym() and (m.linkname.startswith("/") or ".." in m.linkname.split("/")):
                raise SaveCorrupt(f"the file part holds a link that leaves its folder: {name[:120]!r}")
            n += m.isreg() or m.issym()
    return n


def check_bundle(path: Path) -> None:
    """A git bundle's header (the machine ran `git bundle verify` against its repository)."""
    with path.open("rb") as f:
        head = f.read(20)
    if not head.startswith((b"# v2 git bundle", b"# v3 git bundle")):
        raise SaveCorrupt(f"{path.name} is not a git bundle")


# ---------------------------------------------------------------- export and import
def export_save(store: SaveStore, save_id: str, dest: Path, key: bytes | None) -> SaveManifest:
    """One file with the save, its payload opened (plain), for another node: a tar of manifest.json
    and the payload, 0600. The snapshot stays at the provider; the importing node uses it only when it
    rents from the same account, else it restores from the files."""
    import tempfile

    manifest, path = store.find(save_id)
    with tempfile.TemporaryDirectory(prefix="openajar-save-") as t:
        out = Path(t)
        files = open_payload(manifest, path, out, key)
        plain = manifest.model_copy(update={"encrypted": False})
        (out / MANIFEST).write_text(plain.model_dump_json(indent=1))
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w") as tf:
            tf.add(out / MANIFEST, arcname=MANIFEST)
            for name, p in files.items():
                tf.add(p, arcname=name)
    return manifest


def import_save(store: SaveStore, src: Path, key: bytes | None, *, family: str | None = None) -> SaveManifest:
    """A file from export_save, as a save of this node (a new id; sealed with this node's key)."""
    tmp = store.tmp()
    try:
        with tarfile.open(src, "r") as tf:
            names = []
            for m in tf:
                if not m.isreg() or "/" in m.name or m.name.startswith(".") or len(m.name) > 80:
                    raise SaveCorrupt(f"{src.name} is not a save export (it holds {m.name[:80]!r})")
                names.append(m.name)
            if MANIFEST not in names:
                raise SaveCorrupt(f"{src.name} is not a save export (no manifest)")
            tf.extractall(tmp, filter="data")
        manifest = SaveManifest.model_validate_json((tmp / MANIFEST).read_text())
        (tmp / MANIFEST).unlink()
        for name, want in manifest.checksums.items():
            if not (tmp / name).is_file() or sha256_file(tmp / name) != want:
                raise SaveCorrupt(f"{name} does not match its checksum")
        sid = new_save_id()
        sums = seal_payload(tmp, sid, key)
        manifest = manifest.model_copy(update={"id": sid, "family": family or manifest.family, "checksums": sums,
                                               "encrypted": key is not None, "warned_at": None})
        store.commit(tmp, manifest)
        return manifest
    except BaseException:
        store.discard(tmp)
        raise


def snapshot_eur_month(snapshot: Any, eur_per_gb_month: float) -> float:
    """What a save's snapshot costs a month: its size at the rate, and nothing where nothing is rented (an
    image in Docker on this computer or on the person's own machine)."""
    from openajar.vms.providers import FREE

    gb = getattr(snapshot, "gb", None) if snapshot is not None else None
    if not gb or getattr(snapshot, "provider", "") in FREE:
        return 0.0
    return round(gb * eur_per_gb_month, 4)


def public(m: SaveManifest, eur_per_gb_month: float) -> dict[str, Any]:
    """A save as environment() and the web data show it."""
    from openajar.core import clock

    gb = m.snapshot.gb if m.snapshot is not None else None
    cost = snapshot_eur_month(m.snapshot, eur_per_gb_month)
    if m.paused is not None:  # the machine itself, paused: what its provider says that costs
        cost = m.paused.eur_month or 0.0
    return {"id": m.id, "title": m.title, "repo": m.repo, "branch": m.branch, "head": (m.head or "")[:12] or None,
            "saved_at": clock.iso_or_none(m.saved_at, z=True), "kind": m.kind,
            "snapshot_gb": gb, "files_bytes": m.bytes, "paused": m.paused is not None,
            "cost_eur_month": cost,
            "note": (m.agent_note or "")[:300] or None, "agent": m.agent}

