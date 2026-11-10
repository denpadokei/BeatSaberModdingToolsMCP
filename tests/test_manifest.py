from __future__ import annotations

import json

import pytest

from bsmt_mcp import manifest

BASE = {
    "$schema": manifest.SCHEMA_URL,
    "id": "MyMod",
    "name": "My Mod",
    "author": "Tester",
    "version": "1.2.3",
    "description": "A test mod.",
    "gameVersion": "1.44.1",
    "dependsOn": {"BSIPA": "^4.3.0"},
}


def test_validate_valid() -> None:
    result = manifest.validate_manifest(BASE)
    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_missing_fields() -> None:
    result = manifest.validate_manifest({"id": "x"})
    assert result["valid"] is False
    assert any("name" in error for error in result["errors"])
    assert any("BSIPA" in error for error in result["errors"])


def test_validate_bad_semver() -> None:
    data = dict(BASE, version="1.2")
    result = manifest.validate_manifest(data)
    assert not result["valid"]
    assert any("SemVer" in error for error in result["errors"])


def test_validate_bad_dependency_range() -> None:
    data = dict(BASE, dependsOn={"BSIPA": "not-a-version"})
    result = manifest.validate_manifest(data)
    assert not result["valid"]
    assert any("version range" in error for error in result["errors"])


def test_validate_unknown_property_warning() -> None:
    data = dict(BASE, custom="value")
    result = manifest.validate_manifest(data)
    assert result["valid"]
    assert any("custom" in warning for warning in result["warnings"])


def test_generate_manifest_roundtrip(tmp_path) -> None:
    base_path = tmp_path / "manifest.json"
    base_path.write_text(json.dumps(BASE), encoding="utf-8")
    result = manifest.generate_manifest(
        base_manifest=base_path,
        target_path=base_path,
        updates={"version": "2.0.0-beta.1", "links": {"project-source": "https://github.com/example/mod"}},
    )
    data = json.loads(base_path.read_text(encoding="utf-8"))
    assert data["version"] == "2.0.0-beta.1"
    assert data["links"]["project-source"].endswith("/mod")
    assert data["misc"]["generated-by"].startswith("bsmt-mcp/")
    assert result["base_version"] == "2.0.0"


def test_generate_manifest_requires_description(tmp_path) -> None:
    with pytest.raises(manifest.ManifestError):
        manifest.generate_manifest(
            target_path=tmp_path / "manifest.json",
            updates={
                "id": "x",
                "name": "x",
                "author": "x",
                "version": "1.0.0",
                "gameVersion": "1.44.1",
                "dependsOn": {"BSIPA": "^4.0.0"},
            },
        )


def test_read_manifest_info(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(BASE), encoding="utf-8")
    info = manifest.read_manifest_info(path)
    assert info["id"] == "MyMod"
    assert info["base_version"] == "1.2.3"
    assert info["validation"]["valid"]
