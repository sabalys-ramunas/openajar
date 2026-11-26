"""Per-lane files: the inbox agents fill and the outbox the lane's downloads land in.

    OPENAJAR_HOME/lanes/<ws>/<name>/files/
      inbox/<id>/<name>        browser_file_put; the only files browser_upload may attach
      outbox/<id>/<name>       every download the lane's browser makes
      <box>/<id>/.meta.json    type (sniffed from the bytes), size, sha256, status

Directories are 700 and files 600. Ids are random (`in_...`, `out_...`), and every content
lookup goes through `LaneFiles.path()`, which refuses symlinks, hard links and anything that
resolves outside its box, so neither a crafted name nor a planted link can point an upload at
another file on the node. Names are sanitized (last path component only, no control or bidi
characters, no leading dots); for the inbox the extension follows the sniffed type, because
sites and the browser judge a file by its extension.

Types come from magic bytes, never from the name: images, videos, PDF, CSV and plain text may
enter the inbox. Downloads are not restricted (an export can be a zip), only sniffed and capped.

URL fetches (`put_url`) go only to https on public hosts, through the lane's proxy when it has
one and never around it. Without a proxy every address a name resolves to must be public and
the connection goes to exactly those addresses (no DNS rebinding); with a proxy, names are not
resolved on the node (that would leak the lookup outside the lane's network), and literal
private addresses and private names are refused.
"""

from __future__ import annotations

import asyncio
import email.message
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel

from openajar import __version__
from openajar.core.errors import EngineError, InvalidRequest, NotFound
from openajar.core.models import ProxyConfig, utcnow

log = logging.getLogger("openajar.files")

MB = 1024 * 1024
INBOX, OUTBOX = "inbox", "outbox"
BOXES = (INBOX, OUTBOX)
META = ".meta.json"
BROWSER_COPY = ".browser-copy"  # a download streamed from a browser that runs elsewhere, before it takes its name
SNIFF_BYTES = 64 * 1024
EARLY_SNIFF_BYTES = 8 * 1024
ID_RE = re.compile(r"^(in|out)_[0-9a-f]{12}$")
MAX_NAME_BYTES = 150
STALE_TMP_SECONDS = 3600
USER_AGENT = f"openajar/{__version__} (browser_file_put)"


@dataclass(frozen=True)
class FilesConfig:
    max_bytes: int = 200 * MB            # one browser_file_put
    download_max_bytes: int = 200 * MB   # one download
    download_stall_seconds: float = 60.0  # a download that stops growing this long is cancelled as partial
    inline_image_bytes: int = 3_500_000  # browser_file_get returns smaller png/jpeg/gif/webp inline
    inline_text_bytes: int = 256 * 1024  # and smaller txt/csv as text
    fetch_timeout: float = 300.0         # a whole browser_file_put url fetch
    allowed_kinds: frozenset[str] = frozenset({"image", "video", "pdf", "csv", "txt"})

    @classmethod
    def from_node(cls, config: Any) -> FilesConfig:
        """Limits from node.toml once NodeConfig carries them (files_max_mb, downloads_max_mb)."""
        out = cls()
        for key, attr in (("files_max_mb", "max_bytes"), ("downloads_max_mb", "download_max_bytes")):
            value = getattr(config, key, None)
            if isinstance(value, int | float) and value > 0:
                out = replace(out, **{attr: int(value * MB)})
        return out


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < MB:
        return f"{n / 1024:.1f} KB"
    return f"{n / MB:.1f} MB"


# ---- types from magic bytes -------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FileType:
    kind: str                   # image | video | pdf | csv | txt | archive | other
    mime: str
    exts: tuple[str, ...] = ()  # the first one is used when a name needs an extension


PNG = FileType("image", "image/png", ("png",))
JPEG = FileType("image", "image/jpeg", ("jpg", "jpeg"))
GIF = FileType("image", "image/gif", ("gif",))
WEBP = FileType("image", "image/webp", ("webp",))
TIFF = FileType("image", "image/tiff", ("tif", "tiff"))
HEIC = FileType("image", "image/heic", ("heic", "heif"))
HEIF = FileType("image", "image/heif", ("heif", "heic"))
AVIF = FileType("image", "image/avif", ("avif",))
MP4 = FileType("video", "video/mp4", ("mp4", "m4v"))
MOV = FileType("video", "video/quicktime", ("mov", "qt"))
THREE_GP = FileType("video", "video/3gpp", ("3gp", "3g2"))
WEBM = FileType("video", "video/webm", ("webm",))
MKV = FileType("video", "video/x-matroska", ("mkv",))
AVI = FileType("video", "video/x-msvideo", ("avi",))
PDF = FileType("pdf", "application/pdf", ("pdf",))
CSV = FileType("csv", "text/csv", ("csv",))
TXT = FileType("txt", "text/plain", ("txt",))
ZIP = FileType("archive", "application/zip", ("zip",))
OTHER = FileType("other", "application/octet-stream")

INLINE_IMAGES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
_HEIF_BRANDS = {b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}
_MP4_BRANDS = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso8", b"mp41", b"mp42", b"avc1",
               b"dash", b"M4V ", b"M4VH", b"M4VP", b"mmp4", b"f4v ", b"MSNV", b"XAVC", b"mp71"}


def _iso_bmff(head: bytes) -> FileType | None:
    """ISO base media (mp4, mov, 3gp, heic, avif): the ftyp box names a major brand and the
    compatible ones."""
    size = int.from_bytes(head[0:4], "big")
    end = min(len(head), size if 16 <= size <= 4096 else 64)
    major = head[8:12]
    brands = [major] + [head[i:i + 4] for i in range(16, end - 3, 4)]
    if major in _AUDIO_BRANDS:
        return None
    if any(b in _AVIF_BRANDS for b in brands):
        return AVIF
    if any(b in _HEIC_BRANDS for b in brands):
        return HEIC
    if any(b in _HEIF_BRANDS for b in brands):
        return HEIF
    if major == b"qt  ":
        return MOV
    if major.startswith((b"3gp", b"3g2")):
        return THREE_GP
    if any(b in _MP4_BRANDS for b in brands):
        return MP4
    if b"qt  " in brands:
        return MOV
    return None


def _is_text(head: bytes) -> bool:
    """UTF-8 (or ASCII) text without NULs and with almost no control characters."""
    data = head.removeprefix(b"\xef\xbb\xbf")
    if not data.strip() or b"\x00" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        # a character cut by the end of the sample is fine; anything else is not text
        if e.reason != "unexpected end of data" or len(data) - e.start > 3:
            return False
        text = data[:e.start].decode("utf-8")
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f")
    return controls * 100 <= len(text)


_MARKUP_RE = re.compile(rb"^\s*(<!doctype\s+html|<html|<head|<body|<svg|<\?xml|<script)", re.IGNORECASE)


def is_markup(head: bytes) -> bool:
    """A web page or SVG (text, but never something to upload as a txt file by accident)."""
    return bool(_MARKUP_RE.match(head.removeprefix(b"\xef\xbb\xbf")[:512]))


def _ext(name: str) -> str:
    stem, dot, ext = (name or "").rpartition(".")
    return ext.lower() if dot and stem else ""


def sniff(head: bytes, name: str = "") -> FileType | None:
    """The type of a file from its first bytes. `name` only decides csv versus plain text."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return AVI
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF
    if head.startswith(b"%PDF-"):
        return PDF
    if head[4:8] == b"ftyp":
        return _iso_bmff(head)
    if head[4:8] in (b"moov", b"mdat", b"wide"):
        return MOV
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        doc = head[:64]
        return WEBM if b"webm" in doc else MKV if b"matroska" in doc else None
    if head.startswith(b"PK\x03\x04"):
        return ZIP
    if _is_text(head):
        return CSV if _ext(name) == "csv" else TXT
    return None


# ---- names ------------------------------------------------------------------------------

_UNSAFE_CHARS = set('<>:"|?*`$')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _limit(name: str) -> str:
    if len(name.encode()) <= MAX_NAME_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 16:
        stem, dot, ext = name, "", ""
    tail = dot + ext
    while stem and len((stem + tail).encode()) > MAX_NAME_BYTES:
        stem = stem[:-1]
    return (stem.rstrip(" .") or "file") + tail


def sanitize_name(raw: str | None, default: str = "file") -> str:
    """A safe file name: the last path component (either separator), without control, format
    (bidi overrides, zero width) or shell-hostile characters, no leading dots, at most 150 bytes."""
    text = unicodedata.normalize("NFC", str(raw or ""))
    text = re.split(r"[\\/]", text)[-1]
    chars = []
    for ch in text:
        if ch.isspace():
            chars.append(" ")
            continue
        if unicodedata.category(ch)[0] == "C":  # control, format (bidi, zero width), surrogate, unassigned
            continue
        chars.append("_" if ch in _UNSAFE_CHARS else ch)
    name = " ".join("".join(chars).split()).strip(" .")
    if not name:
        return default
    if name.split(".", 1)[0].strip().lower() in _RESERVED:
        name = "_" + name
    return _limit(name)


def fit_extension(name: str, ftype: FileType) -> str:
    """Make the extension say what the bytes are: keep a matching one, otherwise replace a short
    extension-looking suffix (or append) with the type's own."""
    if not ftype.exts:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem:
        if ext.lower() in ftype.exts:
            return name
        if re.fullmatch(r"[A-Za-z0-9]{1,5}", ext):
            name = stem
    return _limit(f"{name}.{ftype.exts[0]}")


def name_from_url(url: str) -> str:
    path = urlsplit(url).path
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def display_url(url: str | None) -> str | None:
    """A URL safe to store and show: no credentials, query or fragment (signed links carry
    tokens there). blob: and data: URLs are reduced to their scheme."""
    if not url:
        return None
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in ("blob", "data"):
        return f"{scheme}:"
    if scheme not in ("http", "https"):
        return f"{scheme}:" if scheme else None
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((scheme, netloc, parts.path, "", ""))


def disposition_filename(value: str | None) -> str | None:
    """The file name in a Content-Disposition header (plain or RFC 2231 encoded)."""
    if not value:
        return None
    msg = email.message.Message()
    msg["content-disposition"] = value
    try:
        name = msg.get_filename()
    except Exception:  # noqa: BLE001 - a malformed header simply has no usable name
        return None
    return str(name) if name else None


# ---- records ------------------------------------------------------------------------------

class FileStatus(StrEnum):
    COMPLETE = "complete"
    DOWNLOADING = "downloading"
    PARTIAL = "partial"  # stopped part way (the lane closed, the node restarted, the connection broke)
    FAILED = "failed"    # cancelled, refused or over the size limit; nothing usable was kept


class StoredFile(BaseModel):
    id: str
    box: str
    name: str
    status: FileStatus = FileStatus.COMPLETE
    size: int = 0
    kind: str | None = None
    mime: str | None = None
    sha256: str | None = None
    source: str = "put"           # put (content), url (fetched), download (the lane's browser)
    url: str | None = None        # display form only (display_url)
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    machine_path: str | None = None  # a download of an environment's browser: the copy on its machine, too
    owner_pid: int | None = None  # the daemon process writing a download; internal

    def public(self) -> dict[str, Any]:
        out = self.model_dump(mode="json", exclude={"owner_pid"}, exclude_none=True)
        out["size_text"] = human_size(self.size)
        if self.machine_path and self.status is not FileStatus.DOWNLOADING:
            out["to_human"] = f'environment_download(path="{self.machine_path}") gives the human a link to it'
        return out


# Downloads in progress, by (outbox root, file id) -> cancel(); filled by the page tracker.
_ACTIVE_DOWNLOADS: dict[tuple[str, str], Callable[[], Awaitable[Any]]] = {}


def register_download(root: Path, file_id: str, cancel: Callable[[], Awaitable[Any]]) -> None:
    _ACTIVE_DOWNLOADS[(str(root), file_id)] = cancel


def unregister_download(root: Path, file_id: str) -> None:
    _ACTIVE_DOWNLOADS.pop((str(root), file_id), None)


def _refuse(message: str) -> InvalidRequest:
    return InvalidRequest(message)


def _shown(path: Path, root: Path) -> str:
    """A lane folder as an error names it: from the lane's files/ down, never the node's own path."""
    try:
        rel = path.relative_to(root.parent)
    except ValueError:
        return path.name
    return rel.as_posix()


def _private_dir(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise _refuse(f"the browser's {_shown(path, root)} is a symlink; refusing to use it for browser files")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise _refuse(f"the browser's {_shown(path, root)} is not a directory")
    path.chmod(0o700)


def _open_private(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


class LaneFiles:
    """The files of one lane. Cheap to construct; touches the disk only when used."""

    def __init__(self, root: Path, config: FilesConfig | None = None):
        self.root = Path(root)
        self.config = config or FilesConfig()

    # ---- layout --------------------------------------------------------------------------

    def ensure(self) -> None:
        if not self.root.parent.is_dir():
            raise NotFound("this browser has no folder on the node; tell the owner (`openajar status --check` "
                           "shows what is wrong)")
        for d in (self.root, self.root / INBOX, self.root / OUTBOX):
            _private_dir(d, self.root)
        self._sweep_tmp()

    def _sweep_tmp(self) -> None:
        cutoff = time.time() - STALE_TMP_SECONDS
        for box in BOXES:
            with os.scandir(self.root / box) as entries:
                for entry in entries:
                    if entry.name.startswith(".tmp-") and entry.is_dir(follow_symlinks=False) \
                            and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        shutil.rmtree(entry.path, ignore_errors=True)

    def _box_root(self, box: str) -> Path:
        """The box directory, resolved, after checking that neither it nor files/ is a symlink."""
        for d in (self.root, self.root / box):
            try:
                st = os.lstat(d)
            except FileNotFoundError:
                raise NotFound(f"no {box} yet: nothing has been stored in it") from None
            if not stat.S_ISDIR(st.st_mode):
                raise _refuse(f"the browser's {_shown(d, self.root)} is a symlink or not a directory; refusing to "
                              "read browser files through it")
        return (self.root / box).resolve(strict=True)

    @staticmethod
    def box_of(file_id: str) -> str:
        if not isinstance(file_id, str) or not ID_RE.match(file_id):
            raise InvalidRequest(f"not a file id: {str(file_id)[:40]!r} (ids look like in_3f9a0c1b2d4e; see browser_files)")
        return INBOX if file_id.startswith("in_") else OUTBOX

    # ---- metadata ------------------------------------------------------------------------

    def _read_meta(self, box: str, file_id: str) -> StoredFile | None:
        path = self.root / box / file_id / META
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb") as f:
                fd = -1
                raw = f.read(64 * 1024)
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            meta = StoredFile.model_validate(json.loads(raw))
        except ValueError:
            log.warning("ignoring unreadable file metadata %s", path)
            return None
        if meta.id != file_id or meta.box != box or meta.name != sanitize_name(meta.name):
            log.warning("ignoring file metadata that does not match its place: %s", path)
            return None
        return meta

    def _write_meta(self, entry_dir: Path, meta: StoredFile) -> None:
        tmp = entry_dir / f".meta.{secrets.token_hex(4)}.tmp"
        fd = _open_private(tmp)
        with os.fdopen(fd, "w") as f:
            f.write(meta.model_dump_json(indent=1))
        os.replace(tmp, entry_dir / META)

    def _live(self, meta: StoredFile) -> StoredFile:
        """A download marked in progress by another process (one that died) is partial."""
        if meta.status is FileStatus.DOWNLOADING and meta.owner_pid != os.getpid():
            return meta.model_copy(update={
                "status": FileStatus.PARTIAL,
                "error": meta.error or "interrupted: the node restarted before the download finished"})
        return meta

    # ---- reads -----------------------------------------------------------------------------

    def list(self, box: str | None = None) -> list[StoredFile]:
        out: list[StoredFile] = []
        for b in (box,) if box else BOXES:
            d = self.root / b
            if d.is_symlink() or not d.is_dir() or self.root.is_symlink():
                continue
            with os.scandir(d) as entries:
                for entry in entries:
                    if not ID_RE.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                        continue
                    meta = self._read_meta(b, entry.name)
                    if meta is not None:
                        out.append(self._live(meta))
        return sorted(out, key=lambda f: (f.created_at, f.id))

    def get(self, file_id: str) -> StoredFile:
        box = self.box_of(file_id)
        meta = None
        entry = self.root / box / file_id
        if not entry.is_symlink() and entry.is_dir():
            meta = self._read_meta(box, file_id)
        if meta is None:
            raise NotFound(f"no file {file_id} in this browser; see browser_files")
        return self._live(meta)

    def path(self, file_id: str, *, box: str | None = None) -> Path:
        """The content of a complete file, resolved, after refusing symlinks, hard links, and
        anything that is not a regular file inside its own box."""
        meta = self.get(file_id)
        if box is not None and meta.box != box:
            raise InvalidRequest(f"{file_id} is in the {meta.box}; only files in the browser's {box} can be used "
                                 "here. Put it with browser_file_put first.")
        if meta.status is not FileStatus.COMPLETE:
            raise InvalidRequest(f"{file_id} is {meta.status.value}"
                                 + (f" ({meta.error})" if meta.error else "") + "; only complete files can be used")
        root = self._box_root(meta.box)
        entry = root / meta.id
        content = entry / meta.name
        try:
            entry_st = os.lstat(entry)
            st = os.lstat(content)
        except FileNotFoundError:
            raise NotFound(f"the content of {file_id} is missing") from None
        if not stat.S_ISDIR(entry_st.st_mode):
            raise _refuse(f"{file_id}: its directory is a symlink; refusing")
        if not stat.S_ISREG(st.st_mode):
            raise _refuse(f"{file_id}: {meta.name} is a symlink or not a regular file; refusing")
        if st.st_nlink != 1:
            raise _refuse(f"{file_id}: {meta.name} has other hard links; refusing")
        real = content.resolve(strict=True)
        if not real.is_relative_to(root) or real.parent != entry:
            raise _refuse(f"{file_id}: {meta.name} resolves outside the {meta.box}; refusing")
        return real

    def read(self, file_id: str, limit: int) -> bytes:
        path = self.path(file_id)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return f.read(limit)

    # ---- inbox writes ----------------------------------------------------------------------

    def allowed_type(self, head: bytes, name: str) -> FileType:
        if is_markup(head):
            raise InvalidRequest("that is a web page or SVG markup, not a file the inbox takes (images, videos, "
                                 "PDF, CSV or plain text)")
        ftype = sniff(head, name)
        if ftype is None or ftype.kind not in self.config.allowed_kinds:
            what = ftype.mime if ftype else "an unrecognized type"
            allowed = ", ".join(sorted(self.config.allowed_kinds))
            raise InvalidRequest(f"the file is {what}; the inbox takes only {allowed} (judged by the bytes, "
                                 "not the name)")
        return ftype

    def _check_size(self, size: int, cap: int) -> None:
        if size > cap:
            raise InvalidRequest(f"the file is larger than the {human_size(cap)} limit")

    def _new_entry(self, box: str) -> tuple[str, Path]:
        self.ensure()
        prefix = "in" if box == INBOX else "out"
        for _ in range(5):
            file_id = f"{prefix}_{secrets.token_hex(6)}"
            if not (self.root / box / file_id).exists():
                break
        tmp = self.root / box / f".tmp-{file_id}"
        os.mkdir(tmp, 0o700)
        return file_id, tmp

    def _commit(self, box: str, file_id: str, tmp: Path, meta: StoredFile) -> StoredFile:
        self._write_meta(tmp, meta)
        os.rename(tmp, self.root / box / file_id)
        log.info("stored %s %s (%s, %s)", file_id, meta.name, meta.mime, human_size(meta.size))
        return meta

    def put_bytes(self, name: str, data: bytes, *, source: str = "put", url: str | None = None) -> StoredFile:
        if not data:
            raise InvalidRequest("the file is empty")
        self._check_size(len(data), self.config.max_bytes)
        ftype = self.allowed_type(data[:SNIFF_BYTES], name)
        final = fit_extension(sanitize_name(name), ftype)
        file_id, tmp = self._new_entry(INBOX)
        try:
            with os.fdopen(_open_private(tmp / final), "wb") as f:
                f.write(data)
            meta = StoredFile(id=file_id, box=INBOX, name=final, size=len(data), kind=ftype.kind, mime=ftype.mime,
                              sha256=hashlib.sha256(data).hexdigest(), source=source, url=display_url(url),
                              created_at=utcnow(), finished_at=utcnow())
            return self._commit(INBOX, file_id, tmp, meta)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    async def put_url(self, name: str | None, url: str, *, proxy: ProxyConfig | None = None,
                      policy: FetchPolicy | None = None) -> StoredFile:
        """Fetch a URL into the inbox (see the module docstring for what is refused)."""
        policy = policy or FetchPolicy()
        target = check_fetch_url(url, policy)
        file_id, tmp = await asyncio.to_thread(self._new_entry, INBOX)
        part = tmp / ".incoming"
        try:
            sink = _Sink(part, self.config.max_bytes,
                         lambda head: self.allowed_type(head, name or name_from_url(target)))
            try:
                result = await asyncio.wait_for(fetch_url(target, sink, proxy=proxy, policy=policy),
                                                self.config.fetch_timeout)
            except TimeoutError:
                raise EngineError(f"fetching {display_url(target)} did not finish within "
                                  f"{self.config.fetch_timeout:g} s") from None
            finally:
                sink.close()
            if sink.size == 0:
                raise InvalidRequest(f"{display_url(result.url)} returned an empty file")
            base = (name or "").strip() or result.filename or name_from_url(result.url) or "file"
            ftype = self.allowed_type(sink.head, base)
            final = fit_extension(sanitize_name(base), ftype)
            os.rename(part, tmp / final)
            meta = StoredFile(id=file_id, box=INBOX, name=final, size=sink.size, kind=ftype.kind, mime=ftype.mime,
                              sha256=sink.sha256, source="url", url=display_url(result.url), created_at=utcnow(),
                              finished_at=utcnow())
            return await asyncio.to_thread(self._commit, INBOX, file_id, tmp, meta)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    # ---- outbox (downloads) ----------------------------------------------------------------

    def begin_download(self, suggested_name: str | None, url: str | None) -> StoredFile:
        file_id, tmp = self._new_entry(OUTBOX)
        meta = StoredFile(id=file_id, box=OUTBOX, name=sanitize_name(suggested_name, default="download"),
                          status=FileStatus.DOWNLOADING, source="download", url=display_url(url),
                          created_at=utcnow(), owner_pid=os.getpid())
        return self._commit(OUTBOX, file_id, tmp, meta)

    def _entry_dir(self, meta: StoredFile) -> Path:
        entry = self._box_root(meta.box) / meta.id
        if not stat.S_ISDIR(os.lstat(entry).st_mode):
            raise _refuse(f"{meta.id}: its directory is a symlink; refusing")
        return entry

    def browser_copy(self, file_id: str) -> Path:
        """Where a browser that runs elsewhere (an environment's, on its machine) streams a finished
        download to: a private name inside the file's own outbox folder, which finish_download takes."""
        return self._entry_dir(self.get(file_id)) / BROWSER_COPY

    def finish_download(self, file_id: str, src: Path, *, expected_size: int | None = None,
                        machine_path: str | None = None) -> StoredFile:
        """Copy a finished download (the browser's temporary file) into the outbox. With the size the
        server announced, a shorter file is kept as partial, never as complete. A `src` that is the
        file's browser_copy is moved into place, not copied, and is gone afterwards either way."""
        meta = self.get(file_id)
        entry = self._entry_dir(meta)
        update = {"machine_path": machine_path} if machine_path else {}
        try:
            return self._finish(meta, entry, Path(src), expected_size, update)
        finally:
            if Path(src) == entry / BROWSER_COPY:
                Path(src).unlink(missing_ok=True)

    def _finish(self, meta: StoredFile, entry: Path, src: Path, expected_size: int | None,
                update: dict[str, Any]) -> StoredFile:
        file_id = meta.id
        size = os.stat(src).st_size
        if size > self.config.download_max_bytes:
            return self.fail_download(file_id, FileStatus.FAILED,
                                      f"larger than the {human_size(self.config.download_max_bytes)} download limit",
                                      size=size, **update)
        incoming = entry / ".incoming"
        digest = hashlib.sha256()
        head = b""
        written = 0
        in_place = src == entry / BROWSER_COPY and not src.is_symlink()  # in its own folder: moved, not copied
        try:
            with open(src, "rb") as source, (open(os.devnull, "wb") if in_place
                                             else os.fdopen(_open_private(incoming), "wb")) as out:
                while chunk := source.read(MB):
                    head = head or chunk[:SNIFF_BYTES]
                    digest.update(chunk)
                    out.write(chunk)
                    written += len(chunk)
                    if written > self.config.download_max_bytes:
                        raise InvalidRequest("over the download limit")
            if in_place:
                os.chmod(src, 0o600)
            os.replace(src if in_place else incoming, entry / meta.name)
            ftype = sniff(head, meta.name) or OTHER
        except BaseException as e:
            incoming.unlink(missing_ok=True)
            if isinstance(e, InvalidRequest):
                return self.fail_download(file_id, FileStatus.FAILED, f"larger than the "
                                          f"{human_size(self.config.download_max_bytes)} download limit", size=written,
                                          **update)
            raise
        short = expected_size is not None and written < expected_size
        done = meta.model_copy(update={
            "status": FileStatus.PARTIAL if short else FileStatus.COMPLETE, "size": written, "kind": ftype.kind,
            "mime": ftype.mime, "sha256": digest.hexdigest(), "finished_at": utcnow(), "owner_pid": None,
            "error": f"the server announced {expected_size} bytes but only {written} arrived" if short else None,
            **update})
        self._write_meta(entry, done)
        log.info("download %s %s: %s (%s)", file_id, done.status.value, meta.name, human_size(written))
        return done

    def fail_download(self, file_id: str, status: FileStatus, error: str, *, size: int = 0,
                      machine_path: str | None = None) -> StoredFile:
        meta = self.get(file_id)
        entry = self._entry_dir(meta)
        for leftover in (entry / ".incoming", entry / BROWSER_COPY, entry / meta.name):
            if leftover.is_file() and not leftover.is_symlink():
                leftover.unlink()
        failed = meta.model_copy(update={"status": status, "error": error, "size": size, "finished_at": utcnow(),
                                         "owner_pid": None, **({"machine_path": machine_path} if machine_path else {})})
        self._write_meta(entry, failed)
        log.info("download %s %s: %s", file_id, status.value, error)
        return failed

    # ---- delete ------------------------------------------------------------------------------

    async def delete(self, file_id: str) -> StoredFile:
        meta = self.get(file_id)
        cancel = _ACTIVE_DOWNLOADS.get((str(self.root), file_id))
        if meta.status is FileStatus.DOWNLOADING and cancel is not None:
            try:
                await cancel()
            except Exception as e:  # noqa: BLE001 - the download may have finished meanwhile
                log.debug("cancel of %s: %s", file_id, e)
            for _ in range(50):
                if (str(self.root), file_id) not in _ACTIVE_DOWNLOADS:
                    break
                await asyncio.sleep(0.1)
        entry = self.root / meta.box / meta.id
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_dir():
            await asyncio.to_thread(shutil.rmtree, entry)
        log.info("deleted %s %s", file_id, meta.name)
        return meta


class _Sink:
    """Where a fetch writes: a private file, with the size cap and an early type check."""

    def __init__(self, path: Path, max_bytes: int, check: Callable[[bytes], Any]):
        self._file = os.fdopen(_open_private(path), "wb")
        self.max_bytes = max_bytes
        self._check = check
        self._checked = False
        self._hash = hashlib.sha256()
        self.size = 0
        self.head = b""

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise InvalidRequest(f"the file is larger than the {human_size(self.max_bytes)} limit")
        if len(self.head) < SNIFF_BYTES:
            self.head += chunk[:SNIFF_BYTES - len(self.head)]
        if not self._checked and len(self.head) >= EARLY_SNIFF_BYTES:
            self._check(self.head)  # refuse a wrong type before downloading all of it
            self._checked = True
        self._hash.update(chunk)
        self._file.write(chunk)

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


# ---- URL fetch ------------------------------------------------------------------------------

@dataclass(frozen=True)
class FetchPolicy:
    https_only: bool = True
    allow_private: bool = False  # only for local test fixtures
    max_redirects: int = 5
    connect_timeout: float = 15.0
    read_timeout: float = 30.0


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str | None
    filename: str | None


_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp",
                     ".localdomain")
_REDIRECTS = {301, 302, 303, 307, 308}


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address of a literal host, including legacy IPv4 spellings (2130706433, 0x7f.1, 127.1)."""
    value = host.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        pass
    if not re.fullmatch(r"[0-9a-fA-Fx.]+", value) or not value[0].isdigit():
        return None
    try:
        return ipaddress.ip_address(socket.inet_aton(value))
    except (OSError, ValueError):
        return None


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable unicast only. Addresses that embed an IPv4 one (mapped, 6to4, Teredo,
    NAT64) are judged by the IPv4 inside them."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in ipaddress.IPv6Network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            if not is_public_ip(embedded):
                return False
            if ip.ipv4_mapped:
                return True
    return ip.is_global and not ip.is_multicast


def is_private_host(host: str) -> bool:
    """A name or literal address on the node or a private network. Names are not resolved."""
    host = (host or "").strip().rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return True
    ip = _literal_ip(host)
    if ip is not None:
        return not is_public_ip(ip)
    return "." not in host  # a single label resolves through the node's own search domains


def check_fetch_url(url: str, policy: FetchPolicy | None = None) -> str:
    """https (or http when the policy allows), no credentials, not a private host."""
    policy = policy or FetchPolicy()
    value = (url or "").strip()
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise InvalidRequest(f"malformed URL {value[:200]!r}") from None
    schemes = ("https",) if policy.https_only else ("https", "http")
    if parts.scheme.lower() not in schemes or not parts.hostname:
        raise InvalidRequest(f"browser_file_put fetches only {' or '.join(schemes)} URLs, not {value[:120]!r}")
    if parts.username is not None or parts.password is not None:
        raise InvalidRequest("URLs with a user name or password are not allowed")
    if not policy.allow_private and is_private_host(parts.hostname):
        raise InvalidRequest(f"refusing {parts.hostname}: it is the node itself or a private network")
    return value


def ascii_url(url: str) -> str:
    """The URL as ASCII: an IDNA host and percent-encoded path and query (credentials are
    already refused)."""
    if url.isascii():
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidRequest(f"malformed host name {host[:80]!r}") from None
    netloc = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if parts.port else "")
    safe = "/%:@!$&'()*+,;=-._~?"
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe=safe), quote(parts.query, safe=safe), ""))


async def resolve_public(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, if all of them are public; refuse otherwise."""
    ip = _literal_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise InvalidRequest(f"refusing {host}: it is the node itself or a private network")
        return [str(ip)]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise EngineError(f"cannot resolve {host}: {e.strerror or e}") from None
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if not is_public_ip(ipaddress.ip_address(address)):
            raise InvalidRequest(f"refusing {host}: it resolves to a private address ({address})")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise EngineError(f"cannot resolve {host}")
    return addresses


def _network_backend(policy: FetchPolicy, trusted: frozenset[str]) -> Any:
    """An httpcore network backend that connects only to public addresses it resolved itself
    (so a name cannot resolve to something else between the check and the connect), except for
    the hosts in `trusted` (the lane's own proxy, which the owner configured)."""
    import httpcore

    class GuardedBackend(httpcore.AsyncNetworkBackend):
        def __init__(self) -> None:
            self._inner = httpcore.AnyIOBackend()

        async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                              local_address: str | None = None, socket_options: Any = None) -> Any:
            kwargs = {"timeout": timeout, "local_address": local_address, "socket_options": socket_options}
            if host in trusted or policy.allow_private:
                return await self._inner.connect_tcp(host, port, **kwargs)
            last: Exception | None = None
            for address in await resolve_public(host, port):
                try:
                    return await self._inner.connect_tcp(address, port, **kwargs)
                except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                    last = e
            assert last is not None
            raise last

        async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                      socket_options: Any = None) -> Any:
            raise InvalidRequest("unix sockets are not allowed")

        async def sleep(self, seconds: float) -> None:
            await self._inner.sleep(seconds)

    return GuardedBackend()


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception as e:  # noqa: BLE001 - the system store is the fallback
        log.debug("certifi not used: %s", e)
    return ctx


async def fetch_url(url: str, sink: Any, *, proxy: ProxyConfig | None = None,
                    policy: FetchPolicy | None = None) -> FetchResult:
    """GET `url` into `sink.write(chunk)`, following redirects, each hop checked again."""
    import httpcore

    policy = policy or FetchPolicy()
    ssl_ctx = _ssl_context()
    if proxy is not None:
        auth = (proxy.username, proxy.password or "") if proxy.username else None
        pool: Any = httpcore.AsyncHTTPProxy(proxy_url=proxy.server, proxy_auth=auth, ssl_context=ssl_ctx,
                                            network_backend=_network_backend(policy, frozenset({proxy.host})),
                                            retries=0)
    else:
        pool = httpcore.AsyncConnectionPool(ssl_context=ssl_ctx, network_backend=_network_backend(policy, frozenset()),
                                            retries=0)
    timeouts = {"connect": policy.connect_timeout, "read": policy.read_timeout, "write": policy.read_timeout,
                "pool": policy.connect_timeout}
    current = url
    async with pool:
        for _ in range(policy.max_redirects + 1):
            current = ascii_url(check_fetch_url(current, policy))
            headers = [(b"Host", urlsplit(current).netloc.encode()), (b"User-Agent", USER_AGENT.encode()),
                       (b"Accept", b"*/*"), (b"Accept-Encoding", b"identity")]
            try:
                async with pool.stream("GET", current, headers=headers, extensions={"timeout": timeouts}) as resp:
                    found = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in resp.headers}
                    if resp.status in _REDIRECTS:
                        location = found.get("location")
                        if not location:
                            raise EngineError(f"{display_url(current)} redirected without a location")
                        current = urljoin(current, location.strip())
                        continue
                    if resp.status != 200:
                        raise InvalidRequest(f"{display_url(current)} answered HTTP {resp.status}")
                    length = found.get("content-length", "")
                    cap = getattr(sink, "max_bytes", None)
                    if cap is not None and length.isdigit() and int(length) > cap:
                        raise InvalidRequest(f"the file is {human_size(int(length))}, over the {human_size(cap)} limit")
                    async for chunk in resp.aiter_stream():
                        sink.write(chunk)
                    return FetchResult(url=current, status=resp.status, content_type=found.get("content-type"),
                                       filename=disposition_filename(found.get("content-disposition")))
            except (InvalidRequest, EngineError):
                raise
            except httpcore.ProxyError as e:
                raise EngineError(f"the browser's proxy refused the fetch of {display_url(current)}: {e}") from None
            except httpcore.TimeoutException:
                raise EngineError(f"fetching {display_url(current)} timed out") from None
            except (httpcore.NetworkError, httpcore.ProtocolError, OSError) as e:
                raise EngineError(f"fetching {display_url(current)} failed: {str(e) or type(e).__name__}") from None
    raise InvalidRequest(f"too many redirects (more than {policy.max_redirects}) from {display_url(url)}")


class NodeFiles:
    """The runtime's `files` service: resolves a file id from a lane's inbox to its content path
    (for flows that upload a file an agent put with browser_file_put). Every check of LaneFiles.path
    applies: complete files only, no symlinks or hard links, nothing outside the inbox."""

    def __init__(self, config: Any):
        self.config = config

    def lane(self, lane_id: str) -> LaneFiles:
        return LaneFiles(self.config.lane_dir(lane_id) / "files", FilesConfig.from_node(self.config))

    def resolve(self, lane_id: str, ref: str) -> Path:
        return self.lane(lane_id).path(ref, box=INBOX)
