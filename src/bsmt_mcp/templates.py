"""Scaffolding for a minimal BSIPA plugin project (BSMT template equivalent)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import beatsaber
from .errors import BsmtError, GameNotFoundError, ProjectError
from .manifest import SCHEMA_URL
from .project import add_references, load_project, set_game_properties
from .util import atomic_write_text

CORE_REFERENCES = [
    "IPA.Loader",
    "Main",
    "HMLib",
    "HMUI",
    "UnityEngine",
    "UnityEngine.CoreModule",
    "UnityEngine.UI",
    "UnityEngine.UIModule",
    "UnityEngine.UIElementsModule",
    "Unity.TextMeshPro",
]


def sanitize_identifier(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "BeatSaberPlugin"
    if value[0].isdigit():
        value = "_" + value
    return value


def _csproj_xml(identifier: str, framework: str, has_game: bool) -> str:
    reference_block = ""
    if not has_game:
        reference_block = """
  <ItemGroup>
    <!-- No game references were generated yet. See the README for the refs add command. -->
  </ItemGroup>
"""
    return f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>{framework}</TargetFramework>
    <LangVersion>latest</LangVersion>
    <AssemblyName>{identifier}</AssemblyName>
    <RootNamespace>{identifier}</RootNamespace>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <AppendTargetFrameworkToOutputPath>false</AppendTargetFrameworkToOutputPath>
    <CopyLocalLockFileAssemblies>false</CopyLocalLockFileAssemblies>
    <Nullable>disable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <EmbeddedResource Include="manifest.json" />
  </ItemGroup>
  <ItemGroup>
    <PackageReference Include="BeatSaberModdingTools.Tasks" Version="2.0.0-beta4">
      <PrivateAssets>all</PrivateAssets>
      <IncludeAssets>runtime; build; native; contentfiles; analyzers; buildtransitive</IncludeAssets>
    </PackageReference>
  </ItemGroup>{reference_block}
</Project>
"""


def _plugin_cs(identifier: str) -> str:
    return f"""using IPA;
using IPALogger = IPA.Logging.Logger;

namespace {identifier}
{{
    [Plugin(RuntimeOptions.SingleStartInit)]
    public class Plugin
    {{
        internal static Plugin Instance {{ get; private set; }}
        internal static IPALogger Log {{ get; private set; }}

        [Init]
        public Plugin(IPALogger logger)
        {{
            Instance = this;
            Log = logger;
            Log.Info("Plugin initialized.");
        }}

        [OnStart]
        public void OnApplicationStart()
        {{
            Log.Info("OnApplicationStart");
        }}

        [OnExit]
        public void OnApplicationQuit()
        {{
            Log.Info("OnApplicationQuit");
        }}
    }}
}}
"""


def _assembly_info(identifier: str, version: str) -> str:
    assembly_version = version if version.count(".") >= 3 else f"{version}.0"
    if assembly_version.count(".") == 2:
        assembly_version += ".0"
    return f"""using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyTitle("{identifier}")]
[assembly: AssemblyDescription("")]
[assembly: AssemblyConfiguration("")]
[assembly: AssemblyCompany("")]
[assembly: AssemblyProduct("{identifier}")]
[assembly: AssemblyCopyright("")]
[assembly: AssemblyTrademark("")]
[assembly: AssemblyCulture("")]
[assembly: ComVisible(false)]
[assembly: Guid("{_stable_guid(identifier)}")]

[assembly: AssemblyVersion("{assembly_version}")]
[assembly: AssemblyFileVersion("{assembly_version}")]
"""


def _stable_guid(seed: str) -> str:
    import hashlib
    import uuid

    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest))


def _manifest_json(*, mod_id: str, name: str, author: str, version: str, game_version: str, description: str) -> str:
    import json

    manifest = {
        "$schema": SCHEMA_URL,
        "id": mod_id,
        "name": name,
        "author": author,
        "version": version,
        "description": description,
        "gameVersion": game_version,
        "dependsOn": {"BSIPA": "^4.0.0"},
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def _readme(name: str) -> str:
    return f"""# {name}

A Beat Saber BSIPA plugin created with `bsmt` (BeatSaberModdingToolsMCP).

## Build

```powershell
# using the CLI
bsmt build --project . --configuration Release --package

# or with uvx straight from git
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt build --project . -c Release --package
```

The release zip is written to `bin/Release/zip/`.

## MCP

Add the server to your MCP client:

```json
{{
  "mcpServers": {{
    "beat-saber-modding": {{
      "command": "uvx",
      "args": ["--from", "git+https://github.com/denpadokei/BeatSaberModdingToolsMCP", "bsmt-mcp"]
    }}
  }}
}}
```

Then ask the agent to call `inspect_project`, `set_game_directory`, `add_references`,
`generate_manifest`, and `build_mod` (with `package` and `deploy` enabled).
"""


def _gitignore() -> str:
    return """bin/
obj/
.vs/
*.user
Refs/
*.zip
"""


def create_project(
    path: str | os.PathLike[str],
    *,
    name: str | None = None,
    mod_id: str | None = None,
    author: str | None = None,
    description: str = "",
    version: str = "0.1.0",
    game_version: str | None = None,
    game_dir: str | os.PathLike[str] | None = None,
    target_framework: str = "net472",
    force: bool = False,
) -> dict[str, Any]:
    """Create a minimal BSIPA plugin project that builds without Visual Studio."""

    directory = Path(path).expanduser()
    if directory.exists() and not directory.is_dir():
        raise ProjectError(f"Path exists but is not a directory: {directory}")
    if directory.exists() and any(directory.iterdir()) and not force:
        raise ProjectError(
            f"Directory is not empty: {directory}",
            hint="Pass an empty directory, or set force=True to write the template files anyway.",
        )
    directory.mkdir(parents=True, exist_ok=True)
    display_name = name or directory.name
    identifier = sanitize_identifier(display_name)
    mod_identifier = mod_id or identifier
    if not description.strip():
        description = f"{display_name} plugin for Beat Saber."
    if re.search(r"\s", mod_identifier):
        raise BsmtError(
            f"Mod id must not contain whitespace: {mod_identifier!r}",
            hint="Use an id such as 'MyPlugin' or 'my-plugin'.",
        )

    warnings: list[str] = []
    install = None
    if game_dir or game_version is None:
        try:
            install, _ = beatsaber.require_game(game_dir)
        except GameNotFoundError as exc:
            if game_dir:
                raise exc
    if game_version is None:
        if install and install.version:
            game_version = beatsaber.detect_game_version(install.path)[0]
        if not game_version:
            game_version = "0.0.0"
            warnings.append(
                "No Beat Saber install was detected, so gameVersion is a placeholder (0.0.0). "
                "Set it before publishing."
            )
    if author is None:
        author = _git_author(directory) or "unknown"
        if author == "unknown":
            warnings.append("Set the 'author' field in manifest.json before publishing.")

    files: dict[str, str] = {
        f"{identifier}.csproj": _csproj_xml(identifier, target_framework, bool(install)),
        "Plugin.cs": _plugin_cs(identifier),
        "Properties/AssemblyInfo.cs": _assembly_info(identifier, version),
        "manifest.json": _manifest_json(
            mod_id=mod_identifier,
            name=display_name,
            author=author,
            version=version,
            game_version=game_version,
            description=description,
        ),
        "Directory.Build.props": """<?xml version="1.0" encoding="utf-8"?>
<!-- Project properties used by BeatSaberModdingTools.Tasks. -->
<Project>
  <PropertyGroup>
    <ImportBSMTTargets>True</ImportBSMTTargets>
    <BSMTProjectType>BSIPA</BSMTProjectType>
  </PropertyGroup>
</Project>
""",
        ".gitignore": _gitignore(),
        "README.md": _readme(display_name),
    }
    written: list[str] = []
    for relative, content in files.items():
        target = directory / relative
        atomic_write_text(target, content)
        written.append(str(target))

    result: dict[str, Any] = {
        "ok": True,
        "path": str(directory),
        "name": display_name,
        "assembly_name": identifier,
        "mod_id": mod_identifier,
        "game_version": game_version,
        "files": written,
        "warnings": warnings,
        "references_added": [],
        "game_dir": str(install.path) if install else None,
    }
    if install is not None:
        info = load_project(directory / f"{identifier}.csproj")
        refs, missing = beatsaber.find_references(install, CORE_REFERENCES)
        if missing:
            warnings.append(f"Game DLLs not found: {', '.join(sorted(missing))}")
        added = add_references(info, refs)
        result["references_added"] = added["added"]
        set_game_properties(info, game_dir=install.path)
        result["user_file"] = str(info.user_file)
        result["warnings"] = warnings
    else:
        warnings.append(
            "No game references were added (no game_dir/game_version detection). "
            "Pass game_dir or run add_game_references later."
        )
        result["warnings"] = warnings
    result["next_steps"] = [
        "Edit manifest.json (author, description, gameVersion).",
        "Add game references with add_references if the project has none.",
        "Run build_mod with package=True (and deploy=True to copy into the game).",
    ]
    return result


def _git_author(directory: Path) -> str | None:
    import shutil
    import subprocess

    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(directory), "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        value = result.stdout.strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None
