from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from bsmt_mcp import buildtools
from bsmt_mcp.project import load_project
from bsmt_mcp.util import safe_relative_path

CSPROJ = """<?xml version="1.0" encoding="utf-8"?>
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net472</TargetFramework>
    <AssemblyName>TestMod</AssemblyName>
  </PropertyGroup>
  {extra}
</Project>
"""


def test_safe_relative_path() -> None:
    assert safe_relative_path("Plugins/Test.dll").as_posix() == "Plugins/Test.dll"
    assert safe_relative_path("Plugins\\Test.dll").as_posix() == "Plugins/Test.dll"
    for bad in ("../evil.dll", "/etc/passwd", "C:\\Windows\\evil.dll", ""):
        with pytest.raises(ValueError):
            safe_relative_path(bad)


def test_prepare_artifact_default(tmp_path) -> None:
    (tmp_path / "TestMod.csproj").write_text(CSPROJ.format(extra=""), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "id": "TestMod",
                "name": "TestMod",
                "author": "x",
                "version": "1.0.0",
                "description": "x",
                "gameVersion": "1.44.1",
                "dependsOn": {"BSIPA": "^4.0.0"},
            }
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin" / "Debug"
    bin_dir.mkdir(parents=True)
    dll = bin_dir / "TestMod.dll"
    dll.write_bytes(b"MZ fake dll")
    (bin_dir / "TestMod.pdb").write_bytes(b"pdb")
    evaluated = {"TargetPath": str(dll), "TargetDir": str(bin_dir), "AssemblyName": "TestMod"}
    result = buildtools.prepare_artifact(tmp_path, evaluated=evaluated)
    artifact = Path(result["artifact_dir"])
    assert (artifact / "Plugins" / "TestMod.dll").is_file()
    assert (artifact / "Plugins" / "TestMod.pdb").is_file()
    assert result["missing"] == []


def test_prepare_artifact_output_copy(tmp_path) -> None:
    extra = """<ItemGroup>
    <OutputCopy Include="$(OutputPath)TestMod.dll" OutputPath="Plugins\\TestMod.dll" />
    <OutputCopy Include="Assets\\sound.wav" OutputPath="UserData\\TestMod\\sound.wav" />
  </ItemGroup>"""
    (tmp_path / "TestMod.csproj").write_text(CSPROJ.format(extra=extra), encoding="utf-8")
    bin_dir = tmp_path / "bin" / "Debug"
    bin_dir.mkdir(parents=True)
    dll = bin_dir / "TestMod.dll"
    dll.write_bytes(b"MZ")
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "sound.wav").write_bytes(b"wav")
    evaluated = {"TargetPath": str(dll), "TargetDir": str(bin_dir), "OutputPath": "bin\\Debug\\", "AssemblyName": "TestMod"}
    result = buildtools.prepare_artifact(tmp_path, evaluated=evaluated)
    artifact = Path(result["artifact_dir"])
    assert (artifact / "Plugins" / "TestMod.dll").is_file()
    assert (artifact / "UserData" / "TestMod" / "sound.wav").is_file()


def test_create_zip_deterministic(tmp_path) -> None:
    source = tmp_path / "Artifact"
    (source / "Plugins").mkdir(parents=True)
    (source / "Plugins" / "Test.dll").write_bytes(b"123")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    buildtools.create_zip(source, first)
    buildtools.create_zip(source, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["Plugins/Test.dll"]
        assert archive.infolist()[0].date_time == (1980, 1, 1, 0, 0, 0)


def test_package_mod(tmp_path, monkeypatch) -> None:
    (tmp_path / "TestMod.csproj").write_text(CSPROJ.format(extra=""), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "id": "TestMod",
                "name": "TestMod",
                "author": "x",
                "version": "1.2.3",
                "description": "x",
                "gameVersion": "1.44.1",
                "dependsOn": {"BSIPA": "^4.0.0"},
            }
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin" / "Debug"
    bin_dir.mkdir(parents=True)
    dll = bin_dir / "TestMod.dll"
    dll.write_bytes(b"MZ")
    evaluated = {"TargetPath": str(dll), "TargetDir": str(bin_dir), "AssemblyName": "TestMod"}
    monkeypatch.setattr(buildtools, "_evaluate_outputs", lambda info, configuration: (evaluated, None))
    monkeypatch.setattr(buildtools, "git_info", lambda directory, hash_length=7: {"commit_hash": "local"})
    result = buildtools.package_mod(tmp_path, configuration="Debug")
    zip_path = Path(result["zip_path"])
    assert zip_path.is_file()
    assert result["artifact_name"] == "TestMod-1.2.3-bs1.44.1"
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["Plugins/TestMod.dll"]
    assert load_project(tmp_path).assembly_name == "TestMod"
