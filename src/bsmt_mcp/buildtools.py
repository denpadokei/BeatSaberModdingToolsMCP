"""Build, package and deploy Beat Saber mods without Visual Studio."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import assembly, beatsaber
from .beatsaber import BeatSaberInstall
from .errors import BuildError, DeployError
from .gitinfo import git_info
from .project import ProjectInfo, glob_output_copy, load_project, resolve_project_game_dir
from .toolchain import BuildTool, evaluate_properties, find_dotnet, get_build_tool, list_msbuilds
from .util import (
    remove_tree,
    run_process,
    safe_relative_path,
    tail_lines,
    truncate_text,
)

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DIAG_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<severity>error|warning)\s*"
    r"(?P<code>[A-Za-z]+[0-9]+)?\s*:?\s*(?P<message>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_DIAG_SIMPLE_RE = re.compile(
    r"^(?P<file>.+?)\s*:\s*(?P<severity>error|warning)\s+(?P<code>[A-Za-z]+[0-9]+)\s*:\s*(?P<message>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_DIAG_BARE_RE = re.compile(
    r"^(?P<severity>error|warning)\s+(?P<code>[A-Za-z]+[0-9]+)\s*:\s*(?P<message>.*)$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class BuildOptions:
    configuration: str = "Debug"
    target: str = "Build"
    restore: bool = True
    no_incremental: bool = False
    prefer: str = "auto"
    timeout: float = 900
    extra_properties: dict[str, str] = field(default_factory=dict)
    disable_bsmt_side_effects: bool = True


def _rel(project: ProjectInfo, path: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(project.directory))
    except (ValueError, OSError):
        return str(path)


def parse_diagnostics(output: str, *, project_dir: Path | None = None, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    claimed: list[tuple[int, int]] = []

    def add(match: re.Match[str]) -> None:
        severity = match.group("severity").lower()
        file = (match.groupdict().get("file") or "").strip()
        if project_dir and file:
            with contextlib.suppress(ValueError, OSError):
                file = str(Path(file).resolve().relative_to(project_dir.resolve()))
        code = (match.groupdict().get("code") or "").strip()
        message = (match.groupdict().get("message") or "").strip()
        key = (severity, file, code, message)
        if key in seen:
            return
        seen.add(key)
        entry = {
            "severity": severity,
            "file": file or None,
            "line": int(match.groupdict()["line"]) if match.groupdict().get("line") else None,
            "column": int(match.groupdict()["col"]) if match.groupdict().get("col") else None,
            "code": code or None,
            "message": message,
        }
        if severity == "error":
            if len(errors) < limit:
                errors.append(entry)
        elif len(warnings) < limit:
            warnings.append(entry)

    for regex in (_DIAG_RE, _DIAG_SIMPLE_RE, _DIAG_BARE_RE):
        for match in regex.finditer(output):
            if any(start <= match.start() < end for start, end in claimed):
                continue
            claimed.append((match.start(), match.end()))
            add(match)
    return {"errors": errors, "warnings": warnings}


def _command_for(
    tool: BuildTool,
    project: Path,
    options: BuildOptions,
) -> list[str]:
    if tool.kind == "dotnet":
        args = [tool.path, "build", str(project), "-nologo", "-v:minimal", "-c", options.configuration]
        if options.target and options.target.lower() != "build":
            args.append(f"-t:{options.target}")
        if options.no_incremental:
            args.append("--no-incremental")
        if not options.restore:
            args.append("--no-restore")
    else:
        args = [
            tool.path,
            str(project),
            "-nologo",
            "-v:minimal",
            "-m:1",
            "-nodeReuse:false",
            f"-p:Configuration={options.configuration}",
            "-p:Platform=AnyCPU",
            f"-t:{options.target or 'Build'}",
        ]
        if options.restore:
            args.append("-restore")
    if options.disable_bsmt_side_effects:
        args.extend(
            [
                "-p:DisableCopyToGame=True",
                "-p:DisableCopyToPlugins=True",
                "-p:DisableZipRelease=True",
            ]
        )
    for key, value in options.extra_properties.items():
        args.append(f"-p:{key}={value}")
    return args


def _guess_target_path(project: ProjectInfo, configuration: str) -> Path | None:
    bin_dir = project.directory / "bin" / configuration
    candidates: list[Path] = []
    for framework in project.target_frameworks:
        if framework and framework != "unknown":
            candidates.append(bin_dir / framework / f"{project.assembly_name}.dll")
    candidates.append(bin_dir / f"{project.assembly_name}.dll")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if bin_dir.is_dir():
        matches = sorted(bin_dir.rglob(f"{project.assembly_name}.dll"))
        if matches:
            return matches[0]
    return None


def _evaluate_outputs(project: ProjectInfo, configuration: str) -> tuple[dict[str, str], str | None]:
    evaluated, error = evaluate_properties(
        project.path,
        [
            "TargetPath",
            "TargetDir",
            "AssemblyName",
            "TargetFramework",
            "OutputPath",
            "Version",
            "GameVersion",
            "BeatSaberDir",
            "GameDirectory",
        ],
        configuration=configuration,
    )
    return evaluated or {}, error


def build_mod(
    project_path: str | os.PathLike[str],
    *,
    options: BuildOptions | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a mod project with dotnet or MSBuild and report diagnostics."""

    options = options or BuildOptions(**overrides)
    info = load_project(project_path)
    if options.configuration not in info.configurations and info.configurations:
        # Let MSBuild decide; an unknown configuration usually means a typo.
        known = ", ".join(info.configurations)
        raise BuildError(
            f"Unknown configuration '{options.configuration}' (project has: {known}).",
            hint="Pass one of the project's configurations, usually Debug or Release.",
        )
    try:
        tool = get_build_tool(sdk_style=info.sdk_style, prefer=options.prefer)
    except RuntimeError as exc:
        raise BuildError(str(exc), hint="Install Visual Studio Build Tools or the .NET SDK.") from exc

    command = _command_for(tool, info.path, options)
    preflight: list[str] = []
    game_refs = [ref for ref in info.references if ref.name.lower() != "ipa.loader" and ref.is_game_reference]
    if game_refs:
        evaluated, _ = evaluate_properties(info.path, ["BeatSaberDir", "GameDirectory"], configuration=options.configuration)
        values = [value for value in (evaluated or {}).values() if value and Path(value).is_dir()]
        if not values:
            try:
                install, source, _ = resolve_project_game_dir(info, configuration=options.configuration)
                preflight.append(
                    f"References use game DLLs, but BeatSaberDir/GameDirectory is not set in the project. "
                    f"Detected {install.path} ({source}); run set_game_directory to write it."
                )
            except Exception:
                preflight.append(
                    "References use game DLLs, but BeatSaberDir/GameDirectory is not set in the project. "
                    "Run set_game_directory or pass extra_properties={'BeatSaberDir': '<game folder>'}."
                )

    if info.manifest_path() and not info.manifest_resource and not info.bsmt.get("enabled"):
        preflight.append(
            "manifest.json exists but is not embedded in the assembly. BSIPA will load the plugin without "
            "metadata (and BeatMods may reject it). Run ensure_manifest_embedding or generate_manifest with embed=True."
        )
    version_check = assembly.compare_project_versions(info)
    if version_check.get("checked") and not version_check.get("match"):
        preflight.append(
            f"Manifest version {version_check.get('manifest_version')} does not match assembly version "
            f"{version_check.get('assembly_version')}; Release builds with BeatSaberModdingTools.Tasks fail "
            "with BSMT01. Run set_version or update AssemblyInfo.cs."
        )
    if (info.get_property("GenerateManifest") or "").strip().lower() == "true":
        preflight.append(
            "GenerateManifest=true: BSMT.Tasks generates the manifest from project properties, so the source "
            "manifest.json is not what gets embedded. Prefer setting the Plugin*/Description properties or "
            "set GenerateManifest=false to use the source manifest."
        )

    started = time.monotonic()
    result = run_process(command, cwd=info.directory, timeout=options.timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    diagnostics = parse_diagnostics(result.output, project_dir=info.directory)
    ok = result.ok
    evaluated, eval_error = _evaluate_outputs(info, options.configuration)
    target_path = evaluated.get("TargetPath")
    if target_path and not Path(target_path).is_file():
        target_path = None
    if not target_path:
        guessed = _guess_target_path(info, options.configuration)
        target_path = str(guessed) if guessed else None
    target_dir = evaluated.get("TargetDir")
    if not target_dir and target_path:
        target_dir = str(Path(target_path).parent)
    if not ok and diagnostics["errors"]:
        summary = diagnostics["errors"][0]
        message = f"{summary.get('code') or 'Build'}: {summary['message']}"
    elif not ok:
        message = "Build failed. See output_tail for the MSBuild output."
    else:
        message = None

    return {
        "ok": ok,
        "message": message,
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "duration_ms": duration_ms,
        "tool": tool.to_dict(),
        "command": command,
        "configuration": options.configuration,
        "target": options.target,
        "project": str(info.path),
        "assembly_name": evaluated.get("AssemblyName") or info.assembly_name,
        "target_framework": evaluated.get("TargetFramework") or (info.target_frameworks[0] if info.target_frameworks else None),
        "target_path": target_path,
        "target_dir": target_dir,
        "version": evaluated.get("Version"),
        "game_version": evaluated.get("GameVersion"),
        "diagnostics": diagnostics,
        "preflight": preflight,
        "evaluation_error": eval_error,
        "output_tail": truncate_text(tail_lines(result.output, 120), 12000),
    }


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def _artifact_dir(info: ProjectInfo, target_dir: Path, artifact_dir: str | Path | None) -> Path:
    if artifact_dir:
        result = Path(artifact_dir)
        if not result.is_absolute():
            result = info.directory / result
        return result.resolve()
    return (target_dir / "Artifact").resolve()


def _clean_artifact(info: ProjectInfo, directory: Path) -> None:
    dangerous = {Path(directory.anchor), info.directory.resolve(), Path.home().resolve()}
    if directory in dangerous or not directory.name:
        raise BuildError(f"Refusing to clean unsafe artifact directory: {directory}")
    if directory.exists():
        remove_tree(directory)
    directory.mkdir(parents=True, exist_ok=True)


def _copy_extra_files(info: ProjectInfo, artifact: Path, extra_files: Iterable[dict[str, str]] | None) -> tuple[list[str], list[str]]:
    copied: list[str] = []
    missing: list[str] = []
    for item in extra_files or []:
        source_value = str(item.get("source") or "").strip()
        destination_value = str(item.get("destination") or "").strip()
        if not source_value:
            missing.append("(empty source)")
            continue
        source = Path(source_value)
        if not source.is_absolute():
            source = info.directory / source
        if not source.is_file():
            missing.append(str(source))
            continue
        destination = safe_relative_path(destination_value or source.name)
        target = artifact / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(destination.as_posix())
    return copied, missing


def prepare_artifact(
    project_path: str | os.PathLike[str],
    *,
    configuration: str = "Debug",
    artifact_dir: str | Path | None = None,
    extra_files: Iterable[dict[str, str]] | None = None,
    copy_pdb: bool = True,
    clean: bool = True,
    evaluated: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Copy build outputs into a BSMT-style artifact folder (without zipping)."""

    info = load_project(project_path)
    if evaluated is None:
        evaluated, eval_error = _evaluate_outputs(info, configuration)
    else:
        eval_error = None
    assembly_name = evaluated.get("AssemblyName") or info.assembly_name
    target_path_value = evaluated.get("TargetPath")
    target_path = Path(target_path_value) if target_path_value else None
    if target_path is None or not target_path.is_file():
        target_path = _guess_target_path(info, configuration)
    if target_path is None or not target_path.is_file():
        raise BuildError(
            f"Build output for '{assembly_name}' was not found.",
            hint="Run build_mod first, or call build_mod with package=True.",
        )
    target_dir = Path(evaluated.get("TargetDir")) if evaluated.get("TargetDir") else target_path.parent
    artifact = _artifact_dir(info, target_dir, artifact_dir)
    if clean:
        _clean_artifact(info, artifact)
    else:
        artifact.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    missing: list[str] = []
    if info.output_copy:
        for item in info.output_copy:
            for source, destination in glob_output_copy(info, item, evaluated):
                if not source.is_file():
                    missing.append(str(source))
                    continue
                relative = safe_relative_path(destination)
                target = artifact / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(relative.as_posix())
    else:
        plugin_destination = Path("Plugins") / f"{assembly_name}.dll"
        target = artifact / plugin_destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_path, target)
        copied.append(plugin_destination.as_posix())
        pdb = target_path.with_suffix(".pdb")
        copy_pdb_property = (evaluated.get("CopyPDB") or info.get_property("CopyPDB") or "True").lower()
        if copy_pdb and copy_pdb_property not in {"false", "0"} and pdb.is_file():
            pdb_destination = Path("Plugins") / f"{assembly_name}.pdb"
            shutil.copy2(pdb, artifact / pdb_destination)
            copied.append(pdb_destination.as_posix())

    extra_copied, extra_missing = _copy_extra_files(info, artifact, extra_files)
    copied.extend(extra_copied)
    missing.extend(extra_missing)
    if not copied:
        raise BuildError(
            "The artifact folder is empty.",
            hint="Check the project's OutputCopy items and build output path.",
        )
    return {
        "project": str(info.path),
        "configuration": configuration,
        "artifact_dir": str(artifact),
        "files": sorted(copied),
        "missing": missing,
        "assembly_name": assembly_name,
        "target_path": str(target_path),
        "target_dir": str(target_dir),
        "evaluation_error": eval_error,
    }


def _artifact_name(
    info: ProjectInfo,
    evaluated: dict[str, str],
    configuration: str,
) -> tuple[str, dict[str, Any]]:
    assembly = evaluated.get("AssemblyName") or info.assembly_name
    manifest_version = None
    game_version = None
    manifest_path = info.manifest_path()
    if manifest_path:
        try:
            import json

            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if isinstance(manifest, dict):
                manifest_version = manifest.get("version")
                game_version = manifest.get("gameVersion")
        except (OSError, ValueError):
            pass
    version = manifest_version or evaluated.get("Version")
    base_version = None
    if isinstance(version, str):
        from .manifest import is_semver, strip_version_label

        base_version = strip_version_label(version) if is_semver(version) else version
    if not game_version:
        game_version = evaluated.get("GameVersion")
    commit = None
    try:
        git = git_info(info.directory)
        if git.get("commit_hash") and git["commit_hash"] != "local":
            commit = git["commit_hash"]
    except Exception:
        commit = None
    parts = [assembly]
    # BSMT uses the full PluginVersion (including prerelease labels) in ArtifactName.
    if isinstance(version, str) and version:
        parts.append(version)
    if game_version:
        parts.append(f"bs{game_version}")
    if commit:
        parts.append(str(commit))
    name = "-".join(parts)
    name = _INVALID_FILENAME.sub("-", name).strip(" .-") or assembly
    metadata = {
        "plugin_version": version,
        "base_version": base_version,
        "game_version": game_version,
        "commit_hash": commit,
    }
    return name, metadata


def create_zip(source_dir: Path, destination: Path, *, deterministic: bool = True) -> dict[str, Any]:
    """Zip the *contents* of a directory (BeatMods layout)."""

    if not source_dir.is_dir():
        raise BuildError(f"Artifact directory does not exist: {source_dir}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        files = sorted((p for p in source_dir.rglob("*") if p.is_file() and not p.is_symlink()), key=lambda p: p.as_posix())
        for file in files:
            relative = file.relative_to(source_dir).as_posix()
            safe_relative_path(relative)
            info = zipfile.ZipInfo(relative)
            info.compress_type = zipfile.ZIP_DEFLATED
            if deterministic:
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.external_attr = 0o644 << 16
            with file.open("rb") as handle:
                archive.writestr(info, handle.read())
            entries.append({"path": relative, "size": file.stat().st_size})
    if not entries:
        destination.unlink(missing_ok=True)
        raise BuildError(f"Cannot create an empty zip from {source_dir}")
    return {"zip_path": str(destination), "entries": entries, "size": destination.stat().st_size}


def package_mod(
    project_path: str | os.PathLike[str],
    *,
    configuration: str = "Debug",
    artifact_dir: str | Path | None = None,
    zip_dir: str | Path | None = None,
    extra_files: Iterable[dict[str, str]] | None = None,
    copy_pdb: bool = True,
    include_manifest: bool = False,
    deterministic: bool = True,
    clean: bool = True,
) -> dict[str, Any]:
    """Create a BeatMods-compatible zip (``Plugins/...`` at the archive root)."""

    info = load_project(project_path)
    evaluated, eval_error = _evaluate_outputs(info, configuration)
    artifact = prepare_artifact(
        info.path,
        configuration=configuration,
        artifact_dir=artifact_dir,
        extra_files=extra_files,
        copy_pdb=copy_pdb,
        clean=clean,
        evaluated=evaluated,
    )
    artifact_path = Path(artifact["artifact_dir"])
    if include_manifest:
        manifest = info.manifest_path()
        if manifest:
            shutil.copy2(manifest, artifact_path / "manifest.json")
            artifact["files"] = sorted({*artifact["files"], "manifest.json"})
    name, metadata = _artifact_name(info, evaluated, configuration)
    destination_dir = Path(zip_dir) if zip_dir else Path(artifact["target_dir"]) / "zip"
    if not destination_dir.is_absolute():
        destination_dir = info.directory / destination_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    for old in destination_dir.glob(f"{Path(artifact['assembly_name']).name}*.zip"):
        with contextlib.suppress(OSError):
            old.unlink()
    zip_path = destination_dir / f"{name}.zip"
    zipped = create_zip(artifact_path, zip_path, deterministic=deterministic)
    return {
        "ok": True,
        "project": str(info.path),
        "configuration": configuration,
        "artifact": artifact,
        "artifact_name": name,
        "artifact_metadata": metadata,
        "zip_path": zipped["zip_path"],
        "zip_size": zipped["size"],
        "zip_entries": zipped["entries"],
        "evaluation_error": eval_error,
    }


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


def deploy_mod(
    project_path: str | os.PathLike[str],
    *,
    configuration: str = "Debug",
    game_dir: str | os.PathLike[str] | None = None,
    artifact_dir: str | Path | None = None,
    copy_pdb: bool = True,
) -> dict[str, Any]:
    """Copy the artifact into ``Plugins`` (or ``IPA/Pending`` while the game runs)."""

    info = load_project(project_path)
    artifact = prepare_artifact(info.path, configuration=configuration, artifact_dir=artifact_dir, copy_pdb=copy_pdb, clean=True)
    installed, source, candidates = resolve_project_game_dir(info, explicit=str(game_dir) if game_dir else None, configuration=configuration)
    artifact_path = Path(artifact["artifact_dir"])
    game_running = beatsaber.is_game_running()
    destination_root = installed.path / "IPA" / "Pending" if game_running else installed.path
    copied: list[str] = []
    for relative in artifact["files"]:
        safe = safe_relative_path(relative)
        source_file = artifact_path / safe
        if not source_file.is_file():
            continue
        target = destination_root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        copied.append(safe.as_posix())
    if not copied:
        raise DeployError("Nothing was copied to the game folder.")
    return {
        "ok": True,
        "project": str(info.path),
        "configuration": configuration,
        "game_dir": str(installed.path),
        "game_source": source,
        "game_running": game_running,
        "destination": str(destination_root),
        "copied": copied,
        "restart_required": game_running,
        "candidates": [item.to_dict() for item in candidates],
    }


def build_and_release(
    project_path: str | os.PathLike[str],
    *,
    configuration: str = "Release",
    package: bool = True,
    deploy: bool = False,
    game_dir: str | os.PathLike[str] | None = None,
    **build_kwargs: Any,
) -> dict[str, Any]:
    """Build, then optionally package and deploy."""

    build = build_mod(project_path, configuration=configuration, **build_kwargs)
    result: dict[str, Any] = {"build": build, "package": None, "deploy": None}
    if not build["ok"]:
        return result
    if package:
        result["package"] = package_mod(project_path, configuration=configuration)
    if deploy:
        result["deploy"] = deploy_mod(project_path, configuration=configuration, game_dir=game_dir)
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Environment report
# ---------------------------------------------------------------------------


def build_environment() -> dict[str, Any]:
    installs = beatsaber.detect_installs()
    msbuilds = list_msbuilds()
    dotnet = find_dotnet()
    return {
        "os": os.name,
        "platform": os.sys.platform,
        "python": os.sys.version.split()[0],
        "msbuild": msbuilds[0].to_dict() if msbuilds else None,
        "msbuild_candidates": [tool.to_dict() for tool in msbuilds],
        "dotnet": dotnet.to_dict() if dotnet else None,
        "git": shutil.which("git"),
        "game_running": beatsaber.is_game_running(),
        "install_count": len(installs),
        "installs": [install.summary() for install in installs],
        "recommendation": _recommendation(msbuilds, dotnet, installs),
    }


def _recommendation(msbuilds: list[BuildTool], dotnet: BuildTool | None, installs: list[BeatSaberInstall]) -> str:
    parts: list[str] = []
    if not installs:
        parts.append("No Beat Saber install was detected; set game_dir explicitly or set BEATSABER_DIR.")
    if not msbuilds and not dotnet:
        parts.append("Install Visual Studio Build Tools (.NET desktop development) or the .NET SDK.")
    elif not msbuilds:
        parts.append("MSBuild.exe was not found; SDK-style projects can still build with dotnet.")
    return " ".join(parts) or "Ready."
