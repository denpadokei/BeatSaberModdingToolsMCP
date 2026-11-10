"""Discovery of MSBuild / dotnet and MSBuild property evaluation."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import ProcessResult, run_process

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")

_tool_cache: dict[str, BuildTool | None] = {}
_msbuilds_cache: list[BuildTool] | None = None


@dataclass(frozen=True)
class BuildTool:
    kind: str  # "msbuild" or "dotnet"
    path: str
    version: str | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "version": self.version, "source": self.source}


def _version_of(command: list[str]) -> str | None:
    result = run_process(command, timeout=30)
    if not result.output:
        return None
    match = _VERSION_RE.search(result.output)
    return match.group(1) if match else None


def _vswhere_paths() -> list[Path]:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
    ]
    return [path for path in candidates if path.is_file()]


def _msbuild_from_vswhere() -> list[BuildTool]:
    tools: list[BuildTool] = []
    for vswhere in _vswhere_paths():
        result = run_process(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.Component.MSBuild",
                "-find",
                r"MSBuild\**\Bin\MSBuild.exe",
            ],
            timeout=30,
        )
        for line in result.output.splitlines():
            line = line.strip()
            if line.lower().endswith("msbuild.exe") and Path(line).is_file():
                tools.append(BuildTool("msbuild", line, _version_of([line, "-version", "-nologo"]), "vswhere"))
    return tools


def _msbuild_from_known_paths() -> list[BuildTool]:
    tools: list[BuildTool] = []
    roots = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft Visual Studio",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio",
    ]
    patterns = [
        "*/*/MSBuild/Current/Bin/MSBuild.exe",
        "*/*/MSBuild/*/Bin/MSBuild.exe",
        "*/MSBuild/Current/Bin/MSBuild.exe",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.is_file():
                    tools.append(BuildTool("msbuild", str(path), _version_of([str(path), "-version", "-nologo"]), "known-path"))
    return tools


def list_msbuilds(*, refresh: bool = False) -> list[BuildTool]:
    """Return every MSBuild.exe found on this machine."""

    global _msbuilds_cache
    if _msbuilds_cache is not None and not refresh:
        return list(_msbuilds_cache)
    tools: list[BuildTool] = []
    env_path = os.environ.get("MSBUILD_EXE_PATH")
    if env_path and Path(env_path).is_file():
        tools.append(BuildTool("msbuild", env_path, _version_of([env_path, "-version", "-nologo"]), "MSBUILD_EXE_PATH"))
    which = shutil.which("msbuild") or shutil.which("MSBuild.exe")
    if which:
        tools.append(BuildTool("msbuild", which, _version_of([which, "-version", "-nologo"]), "PATH"))
    tools.extend(_msbuild_from_vswhere())
    tools.extend(_msbuild_from_known_paths())
    deduped: list[BuildTool] = []
    seen: set[str] = set()
    for tool in tools:
        key = os.path.normcase(os.path.realpath(tool.path))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(tool)
    deduped.sort(key=lambda item: tuple(int(x) for x in (item.version or "0").split(".") if x.isdigit()), reverse=True)
    _msbuilds_cache = deduped
    return list(deduped)


def find_dotnet() -> BuildTool | None:
    if "dotnet" in _tool_cache:
        return _tool_cache["dotnet"]
    candidates = [shutil.which("dotnet")]
    candidates.append(str(Path(os.environ.get("DOTNET_ROOT", r"C:\Program Files\dotnet")) / "dotnet.exe"))
    tool: BuildTool | None = None
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            tool = BuildTool("dotnet", candidate, _version_of([candidate, "--version"]), "PATH")
            break
    _tool_cache["dotnet"] = tool
    return tool


def find_msbuild() -> BuildTool | None:
    tools = list_msbuilds()
    return tools[0] if tools else None


def get_build_tool(*, sdk_style: bool = True, prefer: str = "auto") -> BuildTool:
    """Choose the tool used for building.

    ``prefer`` may be ``auto``, ``msbuild`` or ``dotnet``. Auto uses the .NET SDK
    for SDK-style projects (works without Visual Studio) and MSBuild.exe for
    legacy projects, which need the full .NET Framework targets.
    """

    if prefer == "msbuild":
        tool = find_msbuild()
        if tool is None:
            raise RuntimeError(
                "MSBuild.exe was not found. Install Visual Studio Build Tools (.NET desktop development) "
                "or pass prefer='dotnet' for an SDK-style project."
            )
        return tool
    if prefer == "dotnet":
        tool = find_dotnet()
        if tool is None:
            raise RuntimeError("dotnet was not found. Install the .NET SDK, or pass prefer='msbuild'.")
        return tool
    if sdk_style:
        dotnet = find_dotnet()
        if dotnet is not None:
            return dotnet
        msbuild = find_msbuild()
        if msbuild is not None:
            return msbuild
    else:
        msbuild = find_msbuild()
        if msbuild is not None:
            return msbuild
        dotnet = find_dotnet()
        if dotnet is not None:
            return dotnet
    raise RuntimeError(
        "Neither MSBuild.exe nor dotnet was found. Install Visual Studio Build Tools or the .NET SDK."
    )


def get_evaluator() -> BuildTool | None:
    """Return the most capable tool for property evaluation (MSBuild first)."""

    msbuild = find_msbuild()
    if msbuild is not None and _supports_get_property(msbuild):
        return msbuild
    dotnet = find_dotnet()
    if dotnet is not None:
        return dotnet
    return msbuild


def _supports_get_property(tool: BuildTool) -> bool:
    version = tool.version or "0"
    try:
        parts = [int(part) for part in version.split(".")[:2]]
    except ValueError:
        return True
    return tuple(parts) >= (17, 8)


def _evaluator_args(tool: BuildTool, project: Path, properties: list[str], configuration: str) -> list[str]:
    common = [
        str(project),
        "-nologo",
        "-v:q",
        f"-p:Configuration={configuration}",
        "-p:Platform=AnyCPU",
    ]
    if tool.kind == "dotnet":
        common.insert(0, "msbuild")
    for prop in properties:
        common.append(f"-getProperty:{prop}")
    return [tool.path, *common]


def evaluate_properties(
    project: Path,
    properties: list[str],
    *,
    configuration: str = "Debug",
    timeout: float = 90,
) -> tuple[dict[str, str] | None, str | None]:
    """Evaluate MSBuild properties without building the project.

    Returns ``(properties, error)``. Uses ``-getProperty`` (MSBuild 17.8+) so
    properties coming from Directory.Build.props, .csproj.user and imports are
    resolved exactly as the build sees them.
    """

    tool = get_evaluator()
    if tool is None:
        return None, "MSBuild/dotnet was not found."
    result: ProcessResult = run_process(
        _evaluator_args(tool, project, properties, configuration),
        cwd=project.parent,
        timeout=timeout,
    )
    if result.timed_out:
        return None, f"Property evaluation timed out after {timeout:g}s."
    stdout = result.output.strip()
    if result.returncode != 0:
        return None, f"Property evaluation failed (exit {result.returncode}): {stdout[-1000:]}"
    if not stdout:
        return None, "Property evaluation produced no output."
    if stdout.startswith("{"):
        try:
            data = json.loads(stdout)
        except ValueError as exc:
            return None, f"Could not parse MSBuild JSON output: {exc}"
        values = data.get("Properties") or {}
        if isinstance(values, dict):
            return {str(key): ("" if value is None else str(value)) for key, value in values.items()}, None
        return None, "MSBuild JSON output did not contain a Properties object."
    if len(properties) == 1:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        return ({properties[0]: lines[-1]} if lines else None), None
    return None, f"Unexpected MSBuild output: {stdout[-1000:]}"
