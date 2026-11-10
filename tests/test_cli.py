from __future__ import annotations

import json

from bsmt_mcp.cli import main


def test_compare_cli(capsys) -> None:
    code = main(["compare", "1.2.3", "1.2.3.0"])
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert data["match"] is True


def test_doctor_cli(capsys) -> None:
    code = main(["doctor"])
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert "msbuild" in data


def test_inspect_cli(tmp_path, capsys) -> None:
    project = tmp_path / "TestMod.csproj"
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    code = main(["inspect", str(project)])
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert data["project"]["name"] == "TestMod"
