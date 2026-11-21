#!/usr/bin/env python3
"""The version every published copy of openajar carries must be the same, and the release tag's.

pyproject.toml (PyPI), src/openajar/__init__.py (the CLI's --version), integrations/npm/package.json
(npm) and server.json (the MCP Registry: its own version and each package's). With --tag vX.Y.Z it
also checks they all say X.Y.Z. Exit 0 when they agree, else 1 with one line per difference.

    python3 scripts/check_release_versions.py              # do the files agree?
    python3 scripts/check_release_versions.py --tag v0.5.2 # and are they the tag's?

The publish workflow (.github/workflows/publish.yml) runs it before anything is published, and
tests/unit/test_release_versions.py runs it with the unit tests. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def versions(root: Path = ROOT) -> dict[str, str | None]:
    """{where: version} for every place a release's version is written."""
    found: dict[str, str | None] = {}
    found["pyproject.toml"] = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    init = re.search(r'^__version__ = "([^"]+)"$', (root / "src/openajar/__init__.py").read_text(), re.MULTILINE)
    found["src/openajar/__init__.py"] = init.group(1) if init else None
    found["integrations/npm/package.json"] = json.loads((root / "integrations/npm/package.json").read_text()).get("version")
    server = json.loads((root / "server.json").read_text())
    found["server.json version"] = server.get("version")
    for package in server.get("packages") or []:
        found[f"server.json {package.get('registryType')} package"] = package.get("version")
    return found


def problems(root: Path = ROOT, tag: str | None = None) -> list[str]:
    found = versions(root)
    want = tag.removeprefix("v") if tag else found["pyproject.toml"]
    if tag is not None and not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        return [f"{tag} is not a vX.Y.Z tag"]
    return [f"{where} says {got}, not {want}" for where, got in found.items() if got != want]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="the release tag, vX.Y.Z")
    args = parser.parse_args(argv)
    wrong = problems(tag=args.tag)
    for line in wrong:
        print(f"error: {line}", file=sys.stderr)
    if not wrong:
        print(f"every copy says {versions()['pyproject.toml']}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
