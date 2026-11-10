"""Shared operations used by both the MCP server and the CLI.

Keeping the logic here means the CLI and the agent-facing tools cannot drift.
"""

from __future__ import annotations

from typing import Any

from . import assembly, beatsaber, manifest, templates
from .beatsaber import BeatSaberInstall, GameReference
from .errors import BsmtError
from .gitinfo import git_info
from .project import (
    add_references,
    load_project,
    resolve_project_game_dir,
    set_game_properties,
)
from .toolchain import evaluate_properties


def resolve_install(
    info,
    game_dir: str | None,
    configuration: str = "Debug",
) -> tuple[BeatSaberInstall, str, list[BeatSaberInstall]]:
    return resolve_project_game_dir(info, explicit=game_dir, configuration=configuration)


def preset_names(install: BeatSaberInstall, presets: list[str] | None) -> tuple[list[str], list[str]]:
    table = {
        "bsipa": ["IPA.Loader"],
        "core": templates.CORE_REFERENCES,
        "bsml": ["BSML"],
        "sira": ["SiraUtil"],
        "harmony": ["0Harmony"],
        "newtonsoft": ["Newtonsoft.Json"],
        "zenject": ["Zenject", "Zenject-usage"],
    }
    names: list[str] = []
    warnings: list[str] = []
    all_references = beatsaber.available_references(install, include_versions=False)
    for preset in presets or []:
        key = preset.strip().lower()
        if key in table:
            names.extend(table[key])
        elif key in {"all-managed", "managed"}:
            names.extend(ref.name for ref in all_references if ref.relative_dir == str(beatsaber.MANAGED_REL))
        elif key in {"all-libs", "libs"}:
            names.extend(ref.name for ref in all_references if ref.relative_dir == str(beatsaber.LIBS_REL))
        elif key in {"all-plugins", "plugins"}:
            names.extend(ref.name for ref in all_references if ref.relative_dir == str(beatsaber.PLUGINS_REL))
        elif key == "all":
            names.extend(ref.name for ref in all_references)
        else:
            warnings.append(f"Unknown preset ignored: {preset}")
    return names, warnings


def reference_filter(reference: GameReference, filter_value: str | None) -> bool:
    if not filter_value:
        return True
    value = filter_value.strip().lower()
    relative = reference.relative_dir.lower()
    if value in {"managed", "game"}:
        return relative.startswith("beat saber_data")
    if value == "libs":
        return relative == "libs"
    if value == "plugins":
        return relative == "plugins"
    if value == "unity":
        return reference.name.lower().startswith("unity")
    if value == "system":
        return reference.name.lower().startswith("system")
    if value == "ipa":
        return reference.name.lower().startswith("ipa.")
    return value in reference.name.lower()


def project_summary(info, evaluated: dict[str, str] | None) -> dict[str, Any]:
    data = info.to_dict()
    data["evaluated"] = {
        key: value
        for key, value in (evaluated or {}).items()
        if key
        in {
            "TargetPath",
            "TargetDir",
            "AssemblyName",
            "TargetFramework",
            "Version",
            "GameVersion",
            "BeatSaberDir",
            "GameDirectory",
        }
    }
    return data


def inspect_project_data(project: str, configuration: str = "Debug") -> dict[str, Any]:
    info = load_project(project)
    evaluated, eval_error = evaluate_properties(
        info.path,
        [
            "TargetPath",
            "TargetDir",
            "AssemblyName",
            "TargetFramework",
            "Version",
            "GameVersion",
            "BeatSaberDir",
            "GameDirectory",
        ],
        configuration=configuration,
    )
    game: dict[str, Any] | None = None
    game_error: str | None = None
    candidates: list[BeatSaberInstall] = []
    try:
        install, source, candidates = resolve_install(info, None, configuration)
        game = {"install": install.to_dict(), "source": source}
    except BsmtError as exc:
        game_error = str(exc)
    manifest_info = None
    manifest_path = info.manifest_path()
    if manifest_path:
        try:
            manifest_info = manifest.read_manifest_info(manifest_path)
        except BsmtError as exc:
            manifest_info = {"path": str(manifest_path), "error": str(exc)}
    assembly_info = assembly.read_assembly_info(info, evaluated=evaluated)
    version_check = assembly.compare_project_versions(info, evaluated=evaluated)
    warnings: list[str] = []
    if not info.references:
        warnings.append("The project has no assembly references. Run add_game_references with preset='core'.")
    if game is None and any(ref.is_game_reference for ref in info.references):
        warnings.append("Game references exist but BeatSaberDir/GameDirectory did not resolve to a game install.")
    if manifest_path is None:
        warnings.append("No manifest.json was found. Create one with generate_manifest.")
    elif info.manifest_resource is None and not info.bsmt.get("enabled"):
        warnings.append("manifest.json is not embedded in the assembly. Run generate_manifest with embed=true.")
    if version_check.get("checked") and not version_check.get("match"):
        warnings.append("The assembly version does not match the manifest version.")
    return {
        "ok": True,
        "project": project_summary(info, evaluated),
        "game": game,
        "game_candidates": [item.to_dict() for item in candidates],
        "game_error": game_error,
        "manifest": manifest_info,
        "assembly": assembly_info,
        "version_check": version_check,
        "git": git_info(info.directory),
        "evaluated": evaluated,
        "evaluation_error": eval_error,
        "warnings": warnings,
    }


def list_references_data(
    project: str | None = None,
    game_dir: str | None = None,
    filter_value: str | None = None,
    only_missing: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    info = load_project(project) if project else None
    if info is not None:
        install, source, _candidates = resolve_install(info, game_dir)
    else:
        if game_dir:
            install = beatsaber.ensure_install(game_dir)
            source = "explicit"
        else:
            install = beatsaber.resolve_install()[0]
            source = "auto-detected"
    existing = {ref.name.lower() for ref in info.references} if info else set()
    rows: list[dict[str, Any]] = []
    for reference in beatsaber.available_references(install):
        referenced = reference.name.lower() in existing
        if only_missing and referenced:
            continue
        if filter_value and filter_value.strip().lower() == "referenced":
            if not referenced:
                continue
        elif not reference_filter(reference, filter_value):
            continue
        row = reference.to_dict()
        row["referenced"] = referenced
        rows.append(row)
    return {
        "ok": True,
        "game_dir": str(install.path),
        "game_source": source,
        "version": install.version,
        "matches": len(rows),
        "truncated": len(rows) > limit,
        "references": rows[: max(0, limit)],
    }


def add_references_data(
    project: str,
    names: list[str] | None = None,
    presets: list[str] | None = None,
    game_dir: str | None = None,
    hint_root: str = "$(BeatSaberDir)",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    info = load_project(project)
    install, source, _candidates = resolve_install(info, game_dir)
    preset_names_list, warnings = preset_names(install, presets)
    wanted = [*(names or []), *preset_names_list]
    if not wanted:
        raise BsmtError("No references requested.", hint="Pass names=[...] or presets=[...].")
    references, missing = beatsaber.find_references(install, wanted)
    result = add_references(info, references, hint_root=hint_root, force=force, dry_run=dry_run)
    if result["changed"] and not dry_run:
        updated = load_project(info.path)
        result["reference_count"] = len(updated.references)
        existing = {ref.name.lower() for ref in updated.references}
        result["remaining_missing"] = sorted({name for name in missing if name.lower() not in existing})
    else:
        result["reference_count"] = len(info.references)
    result.update(
        {
            "ok": True,
            "project": str(info.path),
            "game_dir": str(install.path),
            "game_source": source,
            "missing": sorted(set(missing)),
            "warnings": warnings,
            "requested": sorted(set(wanted)),
        }
    )
    return result


def set_game_directory_data(
    project: str,
    game_dir: str | None = None,
    target: str = "auto",
    set_reference_path: bool = False,
) -> dict[str, Any]:
    info = load_project(project)
    install, source, _candidates = resolve_install(info, game_dir)
    result = set_game_properties(
        info,
        game_dir=install.path,
        target=target,
        set_reference_path=set_reference_path,
    )
    result.update(
        {
            "ok": True,
            "project": str(info.path),
            "game_dir": str(install.path),
            "game_source": source,
            "game_version": install.version,
        }
    )
    return result
