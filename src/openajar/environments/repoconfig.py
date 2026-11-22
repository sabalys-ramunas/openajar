"""A repository's own word on how its machine is set up: `.openajar/environment.toml`.

    setup = "pnpm install --frozen-lockfile"     # once, on a fresh machine: its result is the prepared machine
    refresh = "pnpm install --prefer-offline"    # on each open from a prepared machine, after the checkout
    start = "pnpm dev --host 0.0.0.0"            # the dev server, started as a job on each open
    port = 3000                                  # where it listens (needed with start)
    ready_timeout_s = 120                        # how long the open waits for it to listen
    inputs = ["pnpm-lock.yaml"]                  # what makes a prepared machine stale (default: the lockfiles)
    env_files = ["apps/api/.env"]                # the local env files `openajar environments env push` sends

The node reads it before it rents anything (openajar.environments.preparing): from its own mirror of a
declared repository, else from the host's raw files for a public one on GitHub, else from the checkout
on the machine once it is cloned. An absent file changes nothing: the open works as it always did.

The key of a prepared machine (prepared_key) is a sha256 of what decides its contents: the repository,
the image the build starts from, setup and refresh, each input file by its git blob id (a hash of its
content, the same on the node's mirror, on the host and in a checkout) and SETUP_VERSION.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import tomllib
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CONFIG_PATH = ".openajar/environment.toml"
# legacy-name: where 0.4.0 read it, in repositories that already have it: read when the new one is not there
LEGACY_CONFIG_PATH = ".agentos/environment.toml"  # legacy-name
CONFIG_PATHS = (CONFIG_PATH, LEGACY_CONFIG_PATH)
CONFIG_MAX_BYTES = 16 * 1024
COMMAND_MAX = 4000
INPUTS_MAX = 50
# The lockfiles and manifests that decide what `setup` installs, when the file names no inputs: those
# of them the repository has (requirements*.txt is a pattern).
DEFAULT_INPUTS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb", "uv.lock",
                  "poetry.lock", "requirements*.txt", "Pipfile.lock", "Gemfile.lock", "go.sum", "Cargo.lock",
                  "composer.lock")
# Bump when a build puts something else on a prepared machine: every key changes, so every repository
# builds a new one, and the old ones go with the retention rules.
SETUP_VERSION = 1
KEYS = ("setup", "refresh", "start", "port", "ready_timeout_s", "inputs", "env_files")


class ConfigError(ValueError):
    """The file is there but is not one openajar can use: its words name the key and the fix."""


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setup: str | None = None
    refresh: str | None = None
    start: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    ready_timeout_s: int = Field(default=120, ge=5, le=1800)
    inputs: list[str] | None = None
    # Only names files: the person's own `env push` reads and sends them, never the node or an open.
    env_files: list[str] | None = None

    @field_validator("setup", "refresh", "start")
    @classmethod
    def _command(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("is empty; leave the key out instead")
        if len(v) > COMMAND_MAX or "\x00" in v:
            raise ValueError(f"is a shell command of at most {COMMAND_MAX} characters")
        return v

    @field_validator("inputs", "env_files")
    @classmethod
    def _inputs(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > INPUTS_MAX:
            raise ValueError(f"names at most {INPUTS_MAX} files")
        out = []
        for p in v:
            p = str(p).strip().removeprefix("./")
            if not p or p.startswith("/") or ".." in p.split("/") or "\x00" in p or len(p) > 300:
                raise ValueError(f"are paths in the repository, like pnpm-lock.yaml or web/package-lock.json: {p!r}")
            out.append(p)
        return out

    @model_validator(mode="after")
    def _start_needs_port(self) -> RepoConfig:
        if self.start and self.port is None:
            raise ValueError("start needs port: the port the dev server listens on, like port = 3000")
        if self.port is not None and not self.start:
            raise ValueError("port needs start: the command that starts the dev server")
        if self.refresh and not self.setup:
            raise ValueError("refresh needs setup: refresh catches a prepared machine up, and setup makes it")
        return self


def parse(text: str) -> RepoConfig:
    """The file's text as a RepoConfig, or ConfigError with words that name the bad key."""
    if len(text.encode()) > CONFIG_MAX_BYTES:
        raise ConfigError(f"{CONFIG_PATH} is larger than {CONFIG_MAX_BYTES // 1024} KB")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_PATH} is not valid TOML: {e}") from None
    try:
        return RepoConfig(**data)
    except ValidationError as e:
        raise ConfigError(_words(e)) from None


def _words(e: ValidationError) -> str:
    first = e.errors()[0]
    where = ".".join(str(p) for p in first.get("loc") or ()) or "the file"
    if first.get("type") == "extra_forbidden":
        return (f"{CONFIG_PATH} has a key openajar does not know: {where}. The keys are "
                f"{', '.join(KEYS)}")
    msg = str(first.get("msg") or "is not valid").removeprefix("Value error, ")
    if where == "the file":
        return f"{CONFIG_PATH}: {msg}"
    return f"{CONFIG_PATH}: {where} {msg}"


def blob_id(content: bytes) -> str:
    """git's id of a file with this content (`git hash-object`), so a file read from the host over
    HTTPS has the same id as the one `git ls-tree` gives in a mirror or a checkout."""
    return hashlib.sha1(b"blob %d\x00" % len(content) + content).hexdigest()  # git's own id, not a security hash


def wanted(inputs: list[str] | None, names: list[str]) -> list[str]:
    """The input files to key on: the configured ones, or the DEFAULT_INPUTS among `names` (the
    repository's top-level files)."""
    if inputs is not None:
        return list(dict.fromkeys(inputs))
    return sorted(n for n in names if any(fnmatch.fnmatchcase(n, pat) for pat in DEFAULT_INPUTS))


def prepared_key(repo: str, base_image: str, config: RepoConfig, inputs: dict[str, str | None],
                 ref: str | None = None) -> str:
    """The prepared machine's key: what decides what is on it. An input that is missing counts as missing.
    `ref`: what the build checks out. None (the default branch) is the one shared build every open of the
    repository may boot; a build of any other branch, tag or commit is that ref's own, because its setup ran
    that ref's scripts (anyone who pushes a branch decides them), so an open of another ref never boots it."""
    doc: dict = {"v": SETUP_VERSION, "repo": repo, "image": base_image, "setup": config.setup or "",
                 "refresh": config.refresh or "", "inputs": sorted((k, v or "") for k, v in inputs.items())}
    if ref:
        doc["ref"] = ref
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class ConfigRead:
    """What reading the file found. `state`: found, absent (the repository has none), invalid (it has
    one openajar cannot use: `why`), or unreadable (the node could not read it now: `why`)."""
    state: str
    config: RepoConfig | None = None
    commit: str | None = None
    inputs: dict[str, str | None] = field(default_factory=dict)  # path: git blob id (None: not in the repository)
    source: str = ""                     # mirror, github or checkout
    why: str | None = None


def from_text(text: str | None, *, source: str, commit: str | None = None) -> ConfigRead:
    """A ConfigRead for the file's text (None: the repository has no such file)."""
    if text is None:
        return ConfigRead("absent", source=source, commit=commit)
    try:
        return ConfigRead("found", config=parse(text), source=source, commit=commit)
    except ConfigError as e:
        return ConfigRead("invalid", source=source, commit=commit, why=str(e))


# On the machine, in the checkout (cwd): the file, base64 on one line after `config=`, or `config=-`,
# then the top-level file names (`name=`), for DEFAULT_INPUTS.
CHECKOUT_READ = (f"f={CONFIG_PATH}; [ -f \"$f\" ] || f={LEGACY_CONFIG_PATH}; "
                 f"if [ -f \"$f\" ]; then printf 'config=%s\\n' \"$(head -c {CONFIG_MAX_BYTES + 1} "
                 "\"$f\" | base64 -w0)\"; else echo config=-; fi; "
                 "git ls-tree --name-only HEAD 2>/dev/null | sed 's/^/name=/'; "
                 "printf 'commit=%s\\n' \"$(git rev-parse HEAD 2>/dev/null)\"")


def checkout_blobs_cmd(paths: list[str]) -> str:
    """`git ls-tree HEAD -- <paths>` in the checkout: one `<mode> blob <id>\\t<path>` line per file there."""
    import shlex

    return "git ls-tree HEAD -- " + " ".join(shlex.quote(p) for p in paths) if paths else "true"


def parse_ls_tree(text: str, paths: list[str]) -> dict[str, str | None]:
    """{path: blob id} from `git ls-tree` lines, None for a path it did not list."""
    found: dict[str, str | None] = dict.fromkeys(paths)
    for line in text.splitlines():
        meta, _, path = line.partition("\t")
        bits = meta.split()
        if len(bits) == 3 and bits[1] == "blob" and path in found:
            found[path] = bits[2]
    return found
