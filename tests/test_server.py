from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bsmt_mcp.server import create_server


def run(coro):
    return asyncio.run(coro)


def test_tool_catalog() -> None:
    async def check():
        server = create_server()
        tools = await server.list_tools()
        names = {tool.name for tool in tools}
        assert len(tools) == 21
        assert {
            "detect_game",
            "inspect_project",
            "add_game_references",
            "set_game_directory",
            "generate_manifest",
            "set_version",
            "build_mod",
            "package_mod",
            "deploy_mod",
        } <= names
        return tools

    tools = run(check())
    by_name = {tool.name: tool for tool in tools}
    assert by_name["detect_game"].annotations.read_only_hint is True
    assert by_name["build_mod"].annotations.open_world_hint is True


def test_compare_versions_tool() -> None:
    async def check():
        server = create_server()
        return await server.call_tool(
            "compare_versions",
            {"manifest_version": "1.2.3-beta", "assembly_version": "1.2.3.0"},
        )

    result = run(check())
    assert result.is_error is False
    assert result.structured_content["match"] is True


def test_validate_manifest_without_arguments_is_tool_error() -> None:
    async def check():
        server = create_server()
        return await server.call_tool("validate_manifest", {})

    with pytest.raises(ToolError):
        run(check())


def test_anticipated_bsmterror_becomes_tool_error() -> None:
    async def check():
        server = create_server()
        return await server.call_tool("inspect_project", {"project": "does-not-exist"})

    with pytest.raises(ToolError) as exc_info:
        run(check())
    assert "Project path does not exist" in str(exc_info.value)


def test_detect_game_tool() -> None:
    async def check():
        server = create_server()
        return await server.call_tool("detect_game", {})

    result = run(check())
    assert result.is_error is False
    data = result.structured_content
    assert data["ok"] is True
    assert isinstance(data["installs"], list)


def test_inspect_project_tool(tmp_path, monkeypatch) -> None:
    project = tmp_path / "TestMod.csproj"
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net472</TargetFramework>
    <AssemblyName>TestMod</AssemblyName>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
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
    monkeypatch.setattr("bsmt_mcp.ops.evaluate_properties", lambda *args, **kwargs: ({}, None))

    async def check():
        server = create_server()
        return await server.call_tool("inspect_project", {"project": str(project)})

    result = run(check())
    assert result.is_error is False
    data = result.structured_content
    assert data["project"]["assembly_name"] == "TestMod"
    assert data["manifest"]["id"] == "TestMod"
