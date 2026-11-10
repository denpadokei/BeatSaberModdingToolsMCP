from __future__ import annotations

import json

from bsmt_mcp import manifest, templates
from bsmt_mcp.errors import BsmtError
from bsmt_mcp.project import load_project


def test_sanitize_identifier() -> None:
    assert templates.sanitize_identifier("My Plugin") == "My_Plugin"
    assert templates.sanitize_identifier("123Mod") == "_123Mod"
    assert templates.sanitize_identifier("!!!") == "BeatSaberPlugin"


def test_create_project_without_game(tmp_path) -> None:
    target = tmp_path / "My Mod"
    result = templates.create_project(
        target,
        name="My Mod",
        author="Tester",
        version="1.2.3",
        game_version="1.44.1",
    )
    assert result["ok"] is True
    assert result["assembly_name"] == "My_Mod"
    project = load_project(target)
    assert project.sdk_style is True
    assert project.assembly_name == "My_Mod"
    assert any(pkg["include"] == "BeatSaberModdingTools.Tasks" for pkg in project.package_references)
    assert project.manifest_resource is not None
    data = manifest.read_manifest_info(project.manifest_path())
    assert data["validation"]["valid"] is True
    assert data["game_version"] == "1.44.1"
    assert data["base_version"] == "1.2.3"
    assert "author" in json.loads(project.manifest_path().read_text(encoding="utf-8"))
    assert project.user_file is not None and not project.user_file.exists()


def test_create_project_refuses_non_empty(tmp_path) -> None:
    target = tmp_path / "mod"
    target.mkdir()
    (target / "existing.txt").write_text("x", encoding="utf-8")
    try:
        templates.create_project(target, name="Mod", game_version="1.44.1")
    except BsmtError as exc:
        assert "not empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected ProjectError")
