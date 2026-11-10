from __future__ import annotations

import json
import re
from pathlib import Path

from bsmt_mcp import manifest, templates
from bsmt_mcp.errors import BsmtError
from bsmt_mcp.project import load_project


def test_sanitize_identifier() -> None:
    assert templates.sanitize_identifier("My Plugin") == "My_Plugin"
    assert templates.sanitize_identifier("123Mod") == "_123Mod"
    assert templates.sanitize_identifier("!!!") == "BeatSaberPlugin"


def test_list_templates() -> None:
    data = templates.list_templates()
    keys = {entry["key"] for entry in data["templates"]}
    assert {"sdk", "bare", "core", "disableable"} <= keys
    assert {"harmony-patch", "bsml-file", "bsml-viewcontroller", "monobehaviour"} <= keys
    assert data["ok"] is True


def test_add_harmony_item_to_sdk_project(tmp_path) -> None:
    target = tmp_path / "SdkMod"
    templates.create_project(target, name="SdkMod", author="Tester", game_version="1.44.1")
    result = templates.add_item_template(
        target,
        template="harmony-patch",
        name="MyPatch",
        directory="Patches",
    )
    assert result["ok"] is True
    patch = target / "Patches" / "MyPatch.cs"
    assert patch.is_file()
    text = patch.read_text(encoding="utf-8")
    assert "namespace SdkMod.Patches" in text
    assert "class MyPatch" in text
    assert "$" not in text
    assert result["project_changes"]["implicit"] == ["Patches/MyPatch.cs"]
    project_text = (target / "SdkMod.csproj").read_text(encoding="utf-8")
    assert "Patches\\MyPatch.cs" not in project_text


def test_add_bsml_view_to_legacy_project(tmp_path) -> None:
    target = tmp_path / "LegacyMod"
    templates.create_project(
        target,
        name="LegacyMod",
        author="Tester",
        game_version="1.44.1",
        template="core",
    )
    result = templates.add_item_template(
        target,
        template="bsml-viewcontroller",
        name="SettingsView",
        directory="UI",
    )
    assert result["ok"] is True
    assert (target / "UI" / "SettingsView.cs").is_file()
    assert (target / "UI" / "SettingsView.bsml").is_file()
    view_text = (target / "UI" / "SettingsView.cs").read_text(encoding="utf-8")
    assert "namespace LegacyMod.UI" in view_text
    assert 'ViewDefinition("LegacyMod.UI.SettingsView.bsml")' in view_text
    project_text = (target / "LegacyMod.csproj").read_text(encoding="utf-8")
    assert '<Compile Include="UI\\SettingsView.cs"' in project_text
    assert '<EmbeddedResource Include="UI\\SettingsView.bsml"' in project_text
    assert "<DependentUpon>SettingsView.cs</DependentUpon>" in project_text
    assert "project_changes" in result


def test_add_item_template_duplicate_requires_force(tmp_path) -> None:
    target = tmp_path / "Mod"
    templates.create_project(target, name="Mod", author="Tester", game_version="1.44.1")
    templates.add_item_template(target, template="monobehaviour", name="Runner")
    try:
        templates.add_item_template(target, template="monobehaviour", name="Runner")
    except BsmtError as exc:
        assert "already exists" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected duplicate error")
    result = templates.add_item_template(target, template="monobehaviour", name="Runner", force=True)
    assert result["ok"] is True


def test_create_project_from_official_core_template(tmp_path) -> None:
    target = tmp_path / "Core Mod"
    result = templates.create_project(
        target,
        name="Core Mod",
        author="Tester",
        description="Official core template",
        version="0.2.0",
        game_version="1.44.1",
        template="core",
    )
    assert result["ok"] is True
    assert result["template"] == "core"
    assert result["project_style"] == "legacy"
    project = Path(result["project"])
    info = load_project(project)
    assert info.sdk_style is False
    assert info.assembly_name == "Core_Mod"
    assert "Core_ModController.cs" in {path.name for path in target.iterdir()}
    assert (target / "Configuration" / "PluginConfig.cs").is_file()
    assert (target / "Directory.Build.props").is_file()
    assert (target / "manifest.json").is_file()
    data = manifest.read_manifest_info(target / "manifest.json")
    assert data["validation"]["valid"] is True
    assert data["game_version"] == "1.44.1"
    assert data["base_version"] == "0.2.0"
    assembly_info = (target / "Properties" / "AssemblyInfo.cs").read_text(encoding="utf-8")
    assert 'AssemblyVersion("0.2.0.0")' in assembly_info
    for path in target.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".cs", ".csproj", ".json", ".props", ".user", ".md"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not re.search(r"\$[A-Za-z0-9_]+\$", text), f"unresolved template parameter in {path}"


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
