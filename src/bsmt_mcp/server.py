"""MCP server exposing Beat Saber modding/build tools over stdio."""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, assembly, beatsaber, buildtools, manifest, ops, templates
from .errors import BsmtError
from .fileops import replace_in_file as _replace_in_file
from .fileops import set_action_output as _set_action_output
from .gitinfo import git_info
from .project import (
    ProjectInfo,
    ensure_manifest_embedded,
    load_project,
    remove_references,
    resolve_project_game_dir,
    set_game_properties,
)
from .project import (
    copy_game_references as _copy_game_references,
)
from .toolchain import evaluate_properties
from .util import find_project_file

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
BUILD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

INSTRUCTIONS = """\
Beat Saber modding build tools for AI agents — a replacement for the
BeatSaberModdingTools Visual Studio extension and BeatSaberModdingTools.Tasks.

Use this server when building or packaging a Beat Saber PC mod (BSIPA) without
Visual Studio. It detects game installs, fixes BeatSaberDir, manages game
references, validates/generates manifest.json, runs MSBuild or dotnet, creates
BeatMods-style release zips, and deploys to Plugins (or IPA/Pending).

Recommended workflow:
1. `detect_game` — find the install and its game version.
2. `inspect_project` — see references, BeatSaberDir, manifest and BSMT status.
3. `set_game_directory` — write BeatSaberDir/GameDirectory if unresolved.
4. `add_game_references` — add missing game DLLs (presets: core, bsipa, bsml, sira).
5. `generate_manifest` — merge/fix manifest.json and embed it in the assembly.
   Use `set_version` when bumping the version (manifest + AssemblyInfo together).
6. `build_mod` with package=true and deploy=true — build, zip and copy to the game.

Notes:
- Paths may be absolute or relative to the server's working directory.
- `build_mod` returns ok=false with diagnostics instead of hiding build errors.
- When BeatSaberModdingTools.Tasks is present the server suppresses its
  copy/zip side effects and performs them itself, so behavior is identical
  whether or not the NuGet package is installed.
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fail(message: str, hint: str | None = None) -> ToolError:
    return ToolError(f"{message}\nHint: {hint}" if hint else message)


def _manifest_path_for(info: ProjectInfo, path: str | None) -> Path | None:
    if path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = info.directory / candidate
        return candidate
    return info.manifest_path()


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_server() -> MCPServer:
    server = MCPServer(
        name="beat-saber-modding-tools",
        title="Beat Saber Modding Tools (no Visual Studio)",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def tool(name: str, description: str, annotations: ToolAnnotations):
        """Register a tool and turn anticipated BsmtError failures into ToolError."""

        def decorator(function):
            @functools.wraps(function)
            def wrapper(*args, **kwargs):
                try:
                    return function(*args, **kwargs)
                except (BsmtError, ValueError) as exc:
                    raise ToolError(str(exc)) from exc

            return server.tool(name=name, description=description, annotations=annotations)(wrapper)

        return decorator

    # -- discovery ---------------------------------------------------------

    @tool(
        name="detect_game",
        description=(
            "Detect Beat Saber installations (Steam, Oculus, BS Manager, BEATSABER_DIR) and their "
            "game versions. Pass game_dir to validate one specific install."
        ),
        annotations=READ_ONLY,
    )
    def detect_game(
        game_dir: str | None = None,
        include_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if game_dir:
            install = beatsaber.ensure_install(game_dir)
            installs = beatsaber.detect_installs()
            return {
                "ok": True,
                "found": True,
                "install": install.to_dict(),
                "installs": [item.summary() for item in installs],
                "game_running": beatsaber.is_game_running(),
                "reference_dirs": beatsaber.describe_reference_dirs(install),
            }
        installs = beatsaber.detect_installs(include_paths)
        if not installs:
            return {
                "ok": True,
                "found": False,
                "installs": [],
                "game_running": beatsaber.is_game_running(),
                "hint": "Pass game_dir, set BEATSABER_DIR, or launch the game once through Steam/BS Manager.",
            }
        chosen, _candidates, reason = beatsaber.resolve_install(installs=installs)
        return {
            "ok": True,
            "found": True,
            "install": chosen.to_dict(),
            "selection_reason": reason,
            "installs": [item.summary() for item in installs],
            "game_running": beatsaber.is_game_running(),
            "reference_dirs": beatsaber.describe_reference_dirs(chosen),
        }

    @tool(
        name="build_environment",
        description="Report the detected MSBuild, dotnet, git and Beat Saber installations plus setup recommendations.",
        annotations=READ_ONLY,
    )
    def build_environment() -> dict[str, Any]:
        data = buildtools.build_environment()
        data["ok"] = True
        return data

    @tool(
        name="game_process_status",
        description="Check whether Beat Saber.exe is currently running (deploy writes to IPA/Pending when it is).",
        annotations=READ_ONLY,
    )
    def game_process_status() -> dict[str, Any]:
        running = beatsaber.is_game_running()
        return {
            "ok": True,
            "game_running": running,
            "deploy_target": "IPA/Pending" if running else "Plugins",
        }

    @tool(
        name="inspect_project",
        description=(
            "Inspect a .csproj: SDK style, target frameworks, references, BeatSaberDir resolution, manifest, "
            "assembly version, BSMT.Tasks presence and git state. Use this before changing anything."
        ),
        annotations=READ_ONLY,
    )
    def inspect_project(project: str, configuration: str = "Debug") -> dict[str, Any]:
        return ops.inspect_project_data(project, configuration)

    @tool(
        name="list_game_references",
        description=(
            "List DLLs in the game's Managed/Libs/Plugins folders with versions and whether the project "
            "already references them. filter: all, managed, libs, plugins, unity, system, ipa, or a name substring."
        ),
        annotations=READ_ONLY,
    )
    def list_game_references(
        project: str | None = None,
        game_dir: str | None = None,
        filter: str | None = None,
        only_missing: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        return ops.list_references_data(project, game_dir, filter, only_missing, limit)

    # -- project setup -----------------------------------------------------

    @tool(
        name="add_game_references",
        description=(
            "Add game DLL references to a project with HintPath rooted at $(BeatSaberDir). "
            "Use names=['Main','IPA.Loader'] or presets=['core','bsipa','bsml','sira','harmony',"
            "'newtonsoft','zenject','all-managed','all-libs','all-plugins','all']."
        ),
        annotations=WRITE,
    )
    def add_game_references_tool(
        project: str,
        names: list[str] | None = None,
        presets: list[str] | None = None,
        game_dir: str | None = None,
        hint_root: str = "$(BeatSaberDir)",
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return ops.add_references_data(project, names, presets, game_dir, hint_root, force, dry_run)

    @tool(
        name="remove_game_references",
        description="Remove game DLL references from a project (only_game_references avoids removing framework/NuGet references).",
        annotations=DESTRUCTIVE,
    )
    def remove_game_references_tool(
        project: str,
        names: list[str],
        only_game_references: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        info = load_project(project)
        result = remove_references(
            info,
            names,
            only_game_references=only_game_references,
            dry_run=dry_run,
        )
        result.update({"ok": True, "project": str(info.path)})
        return result

    @tool(
        name="set_game_directory",
        description=(
            "Write BeatSaberDir and GameDirectory into the project, its .csproj.user, or Directory.Build.props. "
            "target=auto updates files that already define the property and otherwise creates a .csproj.user."
        ),
        annotations=WRITE,
    )
    def set_game_directory_tool(
        project: str,
        game_dir: str | None = None,
        target: Literal["auto", "user", "project", "props"] = "auto",
        set_reference_path: bool = False,
    ) -> dict[str, Any]:
        return ops.set_game_directory_data(project, game_dir, target, set_reference_path)

    @tool(
        name="copy_game_references",
        description=(
            "Copy game Managed/Libs/Plugins DLLs into a local Refs folder (default: the parent of the project "
            "folder) and point BeatSaberDir at it. Enables offline/CI builds without the game installed."
        ),
        annotations=WRITE,
    )
    def copy_game_references_tool(
        project: str,
        destination: str | None = None,
        game_dir: str | None = None,
        force: bool = False,
        set_beat_saber_dir: bool = True,
    ) -> dict[str, Any]:
        info = load_project(project)
        install, source, _candidates = ops.resolve_install(info, game_dir)
        target = Path(destination) if destination else info.directory.parent / "Refs"
        if not target.is_absolute():
            target = info.directory / target
        result = _copy_game_references(install.path, target, force=force)
        result.update(
            {
                "ok": True,
                "project": str(info.path),
                "game_dir": str(install.path),
                "game_source": source,
            }
        )
        if set_beat_saber_dir:
            changes = set_game_properties(info, game_dir=target, set_game_directory=False)
            result["beat_saber_dir"] = str(target)
            result["project_changes"] = changes
        return result

    @tool(
        name="list_templates",
        description=(
            "List project templates: the built-in SDK style plus the official BSIPA templates "
            "(bare, core, disableable) vendored from UnityModdingTools.Templates.BeatSaber."
        ),
        annotations=READ_ONLY,
    )
    def list_templates_tool(templates_dir: str | None = None) -> dict[str, Any]:
        return templates.list_templates(templates_dir)

    @tool(
        name="create_project",
        description=(
            "Scaffold a BSIPA plugin project. template='sdk' (default) is an SDK-style project that builds "
            "without Visual Studio; template='bare'/'core'/'disableable' instantiates the official Visual Studio "
            "templates from UnityModdingTools.Templates.BeatSaber (vendored, or a clone via templates_dir)."
        ),
        annotations=WRITE,
    )
    def create_project_tool(
        path: str,
        name: str | None = None,
        mod_id: str | None = None,
        author: str | None = None,
        description: str = "",
        version: str = "0.1.0",
        game_version: str | None = None,
        game_dir: str | None = None,
        target_framework: str = "net472",
        template: str = "sdk",
        templates_dir: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return templates.create_project(
            path,
            name=name,
            mod_id=mod_id,
            author=author,
            description=description,
            version=version,
            game_version=game_version,
            game_dir=game_dir,
            target_framework=target_framework,
            template=template,
            templates_dir=templates_dir,
            force=force,
        )

    # -- manifest ----------------------------------------------------------

    @tool(
        name="read_manifest",
        description="Read and validate a BSIPA manifest.json (project manifest by default).",
        annotations=READ_ONLY,
    )
    def read_manifest(project: str | None = None, path: str | None = None) -> dict[str, Any]:
        if not path:
            if not project:
                raise _fail("Pass project or path.", "Example: read_manifest(project='.')")
            info = load_project(project)
            manifest_path = info.manifest_path()
            if manifest_path is None:
                raise _fail(
                    f"No manifest.json found in {info.directory}.",
                    "Create one with generate_manifest, or pass path explicitly.",
                )
            data = manifest.read_manifest_info(manifest_path)
            data["embedded"] = info.manifest_resource.to_dict() if info.manifest_resource else None
            data["project"] = str(info.path)
            data["ok"] = True
            return data
        data = manifest.read_manifest_info(path)
        data["ok"] = True
        return data

    @tool(
        name="generate_manifest",
        description=(
            "Merge fields into manifest.json, validate against the BSIPA schema, write it, and (by default) "
            "make sure it is embedded as <AssemblyName>.manifest.json. Existing fields are preserved."
        ),
        annotations=WRITE,
    )
    def generate_manifest_tool(
        project: str,
        mod_id: str | None = None,
        name: str | None = None,
        author: str | None = None,
        version: str | None = None,
        game_version: str | None = None,
        description: str | None = None,
        depends_on: dict[str, str] | None = None,
        links: dict[str, str] | None = None,
        files: list[str] | None = None,
        load_before: list[str] | None = None,
        load_after: list[str] | None = None,
        icon: str | None = None,
        features: dict[str, Any] | None = None,
        misc: dict[str, Any] | None = None,
        plugin_hint: str | None = None,
        updates: dict[str, Any] | None = None,
        target: str | None = None,
        base_manifest: str | None = None,
        embed: bool = True,
        requires_bsipa: bool = True,
    ) -> dict[str, Any]:
        info = load_project(project)
        base_path = Path(base_manifest) if base_manifest else info.manifest_path()
        if base_path and not base_path.is_absolute():
            base_path = info.directory / base_path
        target_path = Path(target) if target else (base_path or info.directory / "manifest.json")
        if not target_path.is_absolute():
            target_path = info.directory / target_path
        payload: dict[str, Any] = {}
        for key, value in (
            ("id", mod_id),
            ("name", name),
            ("author", author),
            ("version", version),
            ("gameVersion", game_version),
            ("description", description),
            ("dependsOn", depends_on),
            ("files", files),
            ("loadBefore", load_before),
            ("loadAfter", load_after),
            ("icon", icon),
            ("features", features),
            ("links", links),
        ):
            if value is not None:
                payload[key] = value
        if misc is not None or plugin_hint is not None:
            merged_misc = dict(misc or {})
            if plugin_hint is not None:
                merged_misc["plugin-hint"] = plugin_hint
            payload["misc"] = merged_misc
        if updates:
            payload.update(updates)
        warnings: list[str] = []
        if not base_path or not base_path.is_file():
            if game_version is None:
                try:
                    install, _source, _ = resolve_project_game_dir(info)
                    detected = manifest.game_version_from_install(install.version)
                    if detected:
                        payload["gameVersion"] = detected
                        warnings.append(f"Used detected game version {detected}.")
                except BsmtError:
                    pass
            if depends_on is None and requires_bsipa:
                payload["dependsOn"] = {"BSIPA": "^4.0.0"}
                warnings.append("Added dependsOn BSIPA ^4.0.0 because the manifest is new.")
        result = manifest.generate_manifest(
            base_manifest=base_path if base_path and base_path.is_file() else None,
            target_path=target_path,
            updates=payload,
            requires_bsipa=requires_bsipa,
        )
        result["ok"] = True
        result["project"] = str(info.path)
        result["warnings"] = warnings + result.get("validation", {}).get("warnings", [])
        result["embed"] = None
        if embed:
            try:
                relative = target_path.resolve().relative_to(info.directory.resolve()).as_posix()
            except ValueError:
                result["embed"] = {
                    "changed": False,
                    "reason": "Manifest was written outside the project directory; not embedded.",
                }
                result["warnings"].append("Manifest was written outside the project; it was not embedded.")
            else:
                info = load_project(info.path)
                result["embed"] = ensure_manifest_embedded(info, manifest_rel=relative)
        check = assembly.compare_project_versions(load_project(info.path))
        if check.get("checked") and not check.get("match"):
            result["warnings"].append(
                f"Manifest version {check.get('manifest_version')} does not match assembly version "
                f"{check.get('assembly_version')}; call set_version to update both."
            )
        return result

    @tool(
        name="validate_manifest",
        description="Validate a manifest.json against the BSIPA metadata schema and BSIPA dependency rules.",
        annotations=READ_ONLY,
    )
    def validate_manifest(
        project: str | None = None,
        path: str | None = None,
        requires_bsipa: bool = True,
    ) -> dict[str, Any]:
        if path:
            manifest_path = Path(path)
        elif project:
            info = load_project(project)
            manifest_path = info.manifest_path()
            if manifest_path is None:
                raise _fail(f"No manifest.json found in {info.directory}.")
        else:
            raise _fail("Pass project or path.")
        data = manifest.read_manifest_info(manifest_path, requires_bsipa=requires_bsipa)
        data["ok"] = True
        return data

    # -- version / git -----------------------------------------------------

    @tool(
        name="get_assembly_info",
        description="Read the assembly version from Properties/AssemblyInfo.cs or SDK project properties and compare it with the manifest.",
        annotations=READ_ONLY,
    )
    def get_assembly_info(project: str, configuration: str = "Debug") -> dict[str, Any]:
        info = load_project(project)
        evaluated, error = evaluate_properties(
            info.path,
            ["Version", "AssemblyVersion", "FileVersion", "VersionPrefix", "VersionSuffix", "AssemblyName"],
            configuration=configuration,
        )
        data = assembly.read_assembly_info(info, evaluated)
        comparison = assembly.compare_project_versions(info, evaluated=evaluated)
        return {
            "ok": True,
            "project": str(info.path),
            "assembly": data,
            "manifest_comparison": comparison,
            "evaluated": evaluated,
            "evaluation_error": error,
        }

    @tool(
        name="set_version",
        description=(
            "Set the manifest version and the AssemblyInfo/csproj version together. Use this for version bumps: "
            "BeatSaberModdingTools.Tasks fails Release builds when the manifest and assembly versions differ."
        ),
        annotations=WRITE,
    )
    def set_version_tool(
        project: str,
        version: str,
        update_manifest: bool = True,
        update_assembly: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        info = load_project(project)
        return assembly.set_project_version(
            info,
            version,
            update_manifest=update_manifest,
            update_assembly=update_assembly,
            dry_run=dry_run,
        )

    @tool(
        name="compare_versions",
        description="Compare a manifest SemVer version with an assembly version using BSMT rules (1.2.3 == 1.2.3.0, prerelease labels ignored).",
        annotations=READ_ONLY,
    )
    def compare_versions(manifest_version: str, assembly_version: str) -> dict[str, Any]:
        data = assembly.compare_versions(manifest_version, assembly_version)
        data["ok"] = True
        return data

    @tool(
        name="get_git_info",
        description="Return commit hash, branch, modified state, origin URL and GitHub owner for the project repository.",
        annotations=READ_ONLY,
    )
    def get_git_info(project: str, hash_length: int = 7) -> dict[str, Any]:
        info = find_project_file(project)
        data = git_info(info.parent, hash_length=hash_length)
        data["ok"] = True
        return data

    # -- build / release ---------------------------------------------------

    @tool(
        name="build_mod",
        description=(
            "Build a mod with dotnet (SDK-style) or MSBuild.exe (legacy). Returns diagnostics, target path and "
            "timings. Set package=true to also create the BeatMods zip and deploy=true to copy into the game."
        ),
        annotations=BUILD,
    )
    def build_mod_tool(
        project: str,
        configuration: str = "Debug",
        target: Literal["Build", "Rebuild", "Clean"] = "Build",
        restore: bool = True,
        no_incremental: bool = False,
        prefer: Literal["auto", "msbuild", "dotnet"] = "auto",
        timeout: float = 900,
        properties: dict[str, str] | None = None,
        package: bool = False,
        deploy: bool = False,
        game_dir: str | None = None,
    ) -> dict[str, Any]:
        options = buildtools.BuildOptions(
            configuration=configuration,
            target=target,
            restore=restore,
            no_incremental=no_incremental,
            prefer=prefer,
            timeout=timeout,
            extra_properties=properties or {},
        )
        result = buildtools.build_mod(project, options=options)
        response: dict[str, Any] = {"ok": result["ok"], "build": result, "package": None, "deploy": None}
        if result["ok"] and package:
            try:
                response["package"] = buildtools.package_mod(project, configuration=configuration)
            except BsmtError as exc:
                response["ok"] = False
                response["package_error"] = str(exc)
        if result["ok"] and deploy:
            try:
                response["deploy"] = buildtools.deploy_mod(
                    project, configuration=configuration, game_dir=game_dir
                )
            except BsmtError as exc:
                response["ok"] = False
                response["deploy_error"] = str(exc)
        return response

    @tool(
        name="package_mod",
        description=(
            "Create a BeatMods-compatible release zip from an existing build (Plugins/... at the archive root). "
            "Honors OutputCopy items; otherwise copies Plugins/<AssemblyName>.dll and .pdb."
        ),
        annotations=WRITE,
    )
    def package_mod_tool(
        project: str,
        configuration: str = "Debug",
        artifact_dir: str | None = None,
        zip_dir: str | None = None,
        extra_files: list[dict[str, str]] | None = None,
        copy_pdb: bool = True,
        include_manifest: bool = False,
    ) -> dict[str, Any]:
        return buildtools.package_mod(
            project,
            configuration=configuration,
            artifact_dir=artifact_dir,
            zip_dir=zip_dir,
            extra_files=extra_files,
            copy_pdb=copy_pdb,
            include_manifest=include_manifest,
        )

    @tool(
        name="deploy_mod",
        description=(
            "Copy the built mod into the game: Plugins normally, or IPA/Pending/Plugins while Beat Saber is running "
            "(BSIPA moves pending files on next launch)."
        ),
        annotations=DESTRUCTIVE,
    )
    def deploy_mod_tool(
        project: str,
        configuration: str = "Debug",
        game_dir: str | None = None,
        copy_pdb: bool = True,
    ) -> dict[str, Any]:
        return buildtools.deploy_mod(
            project,
            configuration=configuration,
            game_dir=game_dir,
            copy_pdb=copy_pdb,
        )

    # -- utility -----------------------------------------------------------

    @tool(
        name="replace_in_file",
        description="Replace text in a file (literal or regular expression), mirroring BSMT's ReplaceInFile task.",
        annotations=WRITE,
    )
    def replace_in_file_tool(
        file: str,
        pattern: str,
        substitute: str,
        use_regex: bool = False,
        multiline: bool = False,
        singleline: bool = False,
        escape_backslash: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return _replace_in_file(
            file,
            pattern,
            substitute,
            use_regex=use_regex,
            multiline=multiline,
            singleline=singleline,
            escape_backslash=escape_backslash,
            dry_run=dry_run,
        )

    @tool(
        name="set_github_output",
        description="Append NAME=VALUE to a GitHub Actions output file (GITHUB_OUTPUT or output_path).",
        annotations=WRITE,
    )
    def set_github_output(name: str, value: str, output_path: str | None = None) -> dict[str, Any]:
        return _set_action_output(name, value, output_path)

    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        create_server().run("stdio")


if __name__ == "__main__":
    main()

