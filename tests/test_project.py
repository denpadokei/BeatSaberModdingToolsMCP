from __future__ import annotations

from pathlib import Path

import pytest

from bsmt_mcp.beatsaber import GameReference
from bsmt_mcp.errors import BsmtError
from bsmt_mcp.project import (
    add_references,
    copy_game_references,
    ensure_manifest_embedded,
    load_project,
    remove_references,
    set_game_properties,
)

SDK_CSPROJ = """<?xml version="1.0" encoding="utf-8"?>
<Project Sdk="Microsoft.NET.Sdk">
  <!-- keep this comment -->
  <PropertyGroup>
    <TargetFramework>net472</TargetFramework>
    <AssemblyName>TestMod</AssemblyName>
    <RootNamespace>TestMod</RootNamespace>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="BeatSaberModdingTools.Tasks" Version="2.0.0-beta4" />
  </ItemGroup>
</Project>
"""

LEGACY_CSPROJ = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion>
    <AssemblyName>LegacyMod</AssemblyName>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="System" />
  </ItemGroup>
  <Import Project="$(MSBuildToolsPath)\\Microsoft.CSharp.targets" />
</Project>
"""


def write_project(tmp_path: Path, content: str = SDK_CSPROJ) -> Path:
    path = tmp_path / "TestMod.csproj"
    path.write_text(content, encoding="utf-8")
    return path


def test_add_and_remove_references(tmp_path) -> None:
    path = write_project(tmp_path)
    info = load_project(path)
    refs = [
        GameReference("Main", tmp_path / "Main.dll", "Beat Saber_Data\\Managed", 10),
        GameReference("IPA.Loader", tmp_path / "IPA.Loader.dll", "Beat Saber_Data\\Managed", 10),
    ]
    result = add_references(info, refs)
    assert result["added"] == ["Main", "IPA.Loader"]
    text = path.read_text(encoding="utf-8")
    assert "keep this comment" in text
    assert 'HintPath>$(BeatSaberDir)\\Beat Saber_Data\\Managed\\Main.dll<' in text
    reloaded = load_project(path)
    assert {ref.name for ref in reloaded.references} == {"Main", "IPA.Loader"}

    again = add_references(reloaded, refs)
    assert again["skipped"] == ["Main", "IPA.Loader"]
    assert again["changed"] is False

    removed = remove_references(reloaded, ["Main"])
    assert removed["removed"] == ["Main"]
    reloaded = load_project(path)
    assert {ref.name for ref in reloaded.references} == {"IPA.Loader"}


def test_remove_keeps_non_game_reference(tmp_path) -> None:
    content = SDK_CSPROJ.replace(
        "  </ItemGroup>\n</Project>",
        '    <Reference Include="Main" />\n  </ItemGroup>\n</Project>',
    )
    path = write_project(tmp_path, content)
    info = load_project(path)
    result = remove_references(info, ["Main"], only_game_references=True)
    assert result["removed"] == []
    assert result["kept_non_game_references"] == ["Main"]


def test_legacy_namespace_preserved(tmp_path) -> None:
    path = write_project(tmp_path, LEGACY_CSPROJ)
    info = load_project(path)
    assert info.namespace == "http://schemas.microsoft.com/developer/msbuild/2003"
    add_references(info, [GameReference("Main", tmp_path / "Main.dll", "Beat Saber_Data\\Managed", 1)])
    text = path.read_text(encoding="utf-8")
    assert 'xmlns="http://schemas.microsoft.com/developer/msbuild/2003"' in text
    assert "<Reference Include=\"Main\"" in text


def test_set_game_properties_creates_user_file(tmp_path) -> None:
    path = write_project(tmp_path)
    info = load_project(path)
    changes = set_game_properties(info, game_dir=tmp_path / "Game")
    assert changes["files"]
    user = Path(changes["files"][0])
    assert user.name.endswith(".csproj.user")
    text = user.read_text(encoding="utf-8")
    assert "<BeatSaberDir>" in text
    assert "<GameDirectory>" in text
    reloaded = load_project(path)
    assert reloaded.get_property("BeatSaberDir").endswith("Game")


def test_set_game_properties_updates_existing_project_property(tmp_path) -> None:
    content = SDK_CSPROJ.replace("<AssemblyName>TestMod</AssemblyName>", "<AssemblyName>TestMod</AssemblyName>\n    <BeatSaberDir>old</BeatSaberDir>")
    path = write_project(tmp_path, content)
    info = load_project(path)
    changes = set_game_properties(info, game_dir=tmp_path / "Game", target="project", set_game_directory=False)
    assert any(change["property"] == "BeatSaberDir" for change in changes["changes"])
    assert "old" not in path.read_text(encoding="utf-8")
    assert "GameDirectory" not in path.read_text(encoding="utf-8")


def test_ensure_manifest_embedded_adds_resource(tmp_path) -> None:
    content = SDK_CSPROJ.replace(
        "  </ItemGroup>\n</Project>",
        '  </ItemGroup>\n  <ItemGroup><None Include="manifest.json" /></ItemGroup>\n</Project>',
    )
    path = write_project(tmp_path, content)
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    info = load_project(path)
    info.bsmt = {"enabled": False}
    result = ensure_manifest_embedded(info)
    assert result["changed"] is True
    text = path.read_text(encoding="utf-8")
    assert "<EmbeddedResource" in text
    assert "LogicalName" in text


def test_ensure_manifest_embedded_skips_bsmt(tmp_path) -> None:
    path = write_project(tmp_path)
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    info = load_project(path)
    result = ensure_manifest_embedded(info)
    assert result["changed"] is False
    assert result["managed_by"] == "BeatSaberModdingTools.Tasks"


def test_copy_game_references(tmp_path) -> None:
    game = tmp_path / "Game"
    (game / "Beat Saber_Data" / "Managed").mkdir(parents=True)
    (game / "Beat Saber_Data" / "Managed" / "Main.dll").write_bytes(b"abc")
    (game / "Libs").mkdir()
    (game / "Libs" / "Newtonsoft.Json.dll").write_bytes(b"def")
    destination = tmp_path / "Refs"
    result = copy_game_references(game, destination)
    assert result["copied_count"] == 2
    assert (destination / "Beat Saber_Data" / "Managed" / "Main.dll").read_bytes() == b"abc"
    assert (destination / "Libs" / "Newtonsoft.Json.dll").read_bytes() == b"def"

    again = copy_game_references(game, destination)
    assert again["copied_count"] == 0
    assert again["skipped_count"] == 2


def test_copy_game_references_rejects_inside_game(tmp_path) -> None:
    game = tmp_path / "Game"
    game.mkdir()
    with pytest.raises(BsmtError):
        copy_game_references(game, game / "Refs")
