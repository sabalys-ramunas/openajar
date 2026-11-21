"""scripts/check_release_versions.py: PyPI, npm and the MCP Registry get the same version, the tag's."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_release_versions", ROOT / "scripts/check_release_versions.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_every_published_copy_has_the_same_version():
    found = check.versions()
    assert len(found) >= 6, found  # pyproject, __init__, npm, server.json and its two packages
    assert check.problems() == [], check.problems()
    assert check.problems(tag=f"v{found['pyproject.toml']}") == []


def test_a_copy_that_differs_or_a_wrong_tag_fails(tmp_path):
    for rel in ("pyproject.toml", "src/openajar/__init__.py", "integrations/npm/package.json", "server.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    npm = tmp_path / "integrations/npm/package.json"
    data = json.loads(npm.read_text())
    data["version"] = "9.9.9"
    npm.write_text(json.dumps(data))
    assert check.problems(tmp_path) == [f"integrations/npm/package.json says 9.9.9, not {check.versions()['pyproject.toml']}"]
    assert any("server.json npm package says" in p for p in check.problems(tmp_path, tag="v9.9.9"))
    assert check.problems(tmp_path, tag="9.9.9") == ["9.9.9 is not a vX.Y.Z tag"]


def test_the_registry_entries_name_the_same_server():
    server = json.loads((ROOT / "server.json").read_text())
    npm = json.loads((ROOT / "integrations/npm/package.json").read_text())
    assert server["name"] == npm["mcpName"] == "com.openajar/openajar"
    assert len(server["description"]) <= 100
    assert f"<!-- mcp-name: {server['name']} -->" in (ROOT / "README.md").read_text()  # PyPI's long description
    assert {p["registryType"]: p["identifier"] for p in server["packages"]} == {"pypi": "openajar", "npm": npm["name"]}
