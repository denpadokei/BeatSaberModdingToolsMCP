from __future__ import annotations

import pytest

from bsmt_mcp import assembly, manifest


@pytest.mark.parametrize(
    ("assembly_version", "manifest_version", "match"),
    [
        ("1.2.3", "1.2.3", True),
        ("1.2.3.0", "1.2.3", True),
        ("0.1.2.3", "1.2.3", True),
        ("1.2.3.4", "1.2.3", False),
        ("1.2.3.0", "1.2.3-beta.1", True),
        ("1.2.4.0", "1.2.3", False),
        ("1.2", "1.2.0", True),
    ],
)
def test_compare_versions(assembly_version: str, manifest_version: str, match: bool) -> None:
    result = assembly.compare_versions(manifest_version, assembly_version)
    assert result["match"] is match


def test_strip_version_label() -> None:
    assert manifest.strip_version_label("1.2.3-beta.1+build") == "1.2.3"
    assert manifest.strip_version_label("1.2.3") == "1.2.3"


def test_strip_version_label_invalid() -> None:
    with pytest.raises(manifest.ManifestError):
        manifest.strip_version_label("1.2")


def test_game_version_from_install() -> None:
    assert manifest.game_version_from_install("1.44.1_20239") == "1.44.1"
    assert manifest.game_version_from_install("1.44.1") == "1.44.1"
    assert manifest.game_version_from_install(None) is None


def test_parse_assembly_info(tmp_path) -> None:
    path = tmp_path / "AssemblyInfo.cs"
    path.write_text(
        """
using System.Reflection;
[assembly: AssemblyVersion("1.2.3.4")]
[assembly: AssemblyFileVersion("1.2.3.4")]
[assembly: AssemblyInformationalVersion("1.2.3-beta")]
""",
        encoding="utf-8",
    )
    info = assembly.parse_assembly_info(path)
    assert info.assembly_version == "1.2.3.4"
    assert info.file_version == "1.2.3.4"
    assert info.informational_version == "1.2.3-beta"
    assert info.errors == []


def test_parse_assembly_info_mismatch(tmp_path) -> None:
    path = tmp_path / "AssemblyInfo.cs"
    path.write_text(
        '[assembly: AssemblyVersion("1.2.3.4")]\n[assembly: AssemblyFileVersion("1.2.3.5")]\n',
        encoding="utf-8",
    )
    info = assembly.parse_assembly_info(path)
    assert info.assembly_version == "1.2.3.4"
    assert info.warnings
