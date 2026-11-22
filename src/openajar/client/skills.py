"""The Claude Code skills that come with openajar (openajar-environment, openajar-lanes, openajar-offload,
openajar-studio).

They ship inside the package (`openajar/client/skills/<name>/SKILL.md`), so an installed
`openajar` with no checkout has them. `openajar connect` copies them into Claude Code's skills
folder, `$CLAUDE_CONFIG_DIR/skills` or `~/.claude/skills`, when Claude Code is here; the last
`openajar connect --remove` takes them out again. A skill folder is ours only when its SKILL.md
says `name: <the skill>`; anything else in the folder is left alone.
"""

from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path

NAMES = ("openajar-environment", "openajar-lanes", "openajar-offload", "openajar-studio")
# legacy-name: the same skills before the rename; installing the new ones removes these where we wrote them
LEGACY_NAMES = ("agentos-environment", "agentos-lanes", "agentos-offload", "agentos-studio")  # legacy-name
LEGACY_PRODUCT = "agentos"  # legacy-name: what an old SKILL.md's frontmatter says ships it


def skills_dir(home: Path) -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env).expanduser() if env else home / ".claude") / "skills"


def source(name: str) -> str:
    return (resources.files("openajar.client") / "skills" / name / "SKILL.md").read_text()


def state(home: Path, dest: Path | None = None) -> str:
    """"installed", "outdated" or "missing", for every skill together, in `dest` (default: skills_dir)."""
    dest = dest or skills_dir(home)
    found = [(dest / n / "SKILL.md") for n in NAMES]
    if not all(p.is_file() for p in found):
        return "missing"
    same = all(p.read_text() == source(n) for p, n in zip(found, NAMES, strict=True))
    return "installed" if same else "outdated"


def ours(text: str, name: str, product: str = "openajar") -> bool:
    """A SKILL.md openajar wrote: its frontmatter names the skill and says openajar ships it."""
    head = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
    return bool(re.search(rf"^name: {re.escape(name)}\s*$", head, re.MULTILINE)) and product in head.lower()


def remove_legacy(home: Path, dest: Path | None = None) -> list[str]:
    """The skills' folders from before the rename, where an older version wrote them (one the owner wrote
    under such a name stays). Returns the names removed."""
    removed = []
    for name in LEGACY_NAMES:
        target = (dest or skills_dir(home)) / name / "SKILL.md"
        if target.is_file() and ours(target.read_text(), name, LEGACY_PRODUCT):
            target.unlink()
            try:
                target.parent.rmdir()
            except OSError:
                pass  # something else of the owner's is in it
            removed.append(name)
    return removed


def install(home: Path, dest: Path | None = None) -> str:
    """Copy or update every skill into `dest` (default: skills_dir; connect passes each Claude Code
    profile's). Returns "installed", "updated", "already installed", or says which ones it kept because
    someone else wrote them."""
    dest = dest or skills_dir(home)
    before = state(home, dest)
    remove_legacy(home, dest)
    if before == "installed":
        return "already installed"
    kept = []
    for name in NAMES:
        target = dest / name / "SKILL.md"
        if target.is_file() and target.read_text() != source(name) and not ours(target.read_text(), name):
            kept.append(name)  # the owner's own file under our name: never overwritten
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source(name))
    if kept:
        return f"kept your own {', '.join(kept)}"
    return "updated" if before == "outdated" else "installed"


def refresh(home: Path) -> str | None:
    """After an update: the installed openajar skills brought up to this openajar's copies (install, which
    keeps a file someone else wrote under our name). Only where openajar installed them before (one of
    ours is there): a person who never installed them, or removed them, gets none. None: nothing to do.
    The local service runs it when it starts (openajar.local.cli serve)."""
    dest = skills_dir(home)
    present = [n for n in NAMES if (dest / n / "SKILL.md").is_file() and ours((dest / n / "SKILL.md").read_text(), n)]
    present += [n for n in LEGACY_NAMES  # an install from before the rename: its skills come over under the new names
                if (dest / n / "SKILL.md").is_file() and ours((dest / n / "SKILL.md").read_text(), n, LEGACY_PRODUCT)]
    if not present or state(home) == "installed":
        return None
    return install(home)


def remove(home: Path, dest: Path | None = None) -> list[str]:
    dest = dest or skills_dir(home)
    removed = remove_legacy(home, dest)
    for name in NAMES:
        target = dest / name / "SKILL.md"
        if target.is_file() and ours(target.read_text(), name):
            target.unlink()
            try:
                target.parent.rmdir()
            except OSError:
                pass  # something else of the owner's is in it
            removed.append(name)
    return removed
