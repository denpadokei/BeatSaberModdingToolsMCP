"""AssemblyInfo parsing and version comparison (BSMT CompareVersions semantics)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BsmtError, ManifestError
from .manifest import is_semver, strip_version_label
from .project import ProjectInfo

_ASSEMBLY_VERSION_RE = re.compile(
    r"\[\s*assembly\s*:\s*(?:global::)?(?:System\.Reflection\.)?AssemblyVersion(?:Attribute)?\s*\(\s*\"([^\"]+)\"\s*\)"
)
_ASSEMBLY_FILE_VERSION_RE = re.compile(
    r"\[\s*assembly\s*:\s*(?:global::)?(?:System\.Reflection\.)?AssemblyFileVersion(?:Attribute)?\s*\(\s*\"([^\"]+)\"\s*\)"
)
_ASSEMBLY_INFO_VERSION_RE = re.compile(
    r"\[\s*assembly\s*:\s*(?:global::)?(?:System\.Reflection\.)?AssemblyInformationalVersion(?:Attribute)?\s*\(\s*\"([^\"]+)\"\s*\)"
)


@dataclass
class AssemblyInfo:
    path: Path | None
    assembly_version: str | None = None
    file_version: str | None = None
    informational_version: str | None = None
    source: str = "none"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "assembly_version": self.assembly_version,
            "file_version": self.file_version,
            "informational_version": self.informational_version,
            "source": self.source,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def parse_assembly_info(path: str | Path) -> AssemblyInfo:
    file = Path(path)
    info = AssemblyInfo(path=file, source="AssemblyInfo.cs")
    try:
        text = file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        info.errors.append(f"Could not read {file}: {exc}")
        return info
    match = _ASSEMBLY_VERSION_RE.search(text)
    if match:
        info.assembly_version = match.group(1).strip()
    else:
        info.errors.append(f"AssemblyVersion was not found in {file}.")
    match = _ASSEMBLY_FILE_VERSION_RE.search(text)
    if match:
        info.file_version = match.group(1).strip()
    match = _ASSEMBLY_INFO_VERSION_RE.search(text)
    if match:
        info.informational_version = match.group(1).strip()
    if info.assembly_version and info.file_version and info.assembly_version != info.file_version:
        info.warnings.append(
            f"AssemblyVersion ({info.assembly_version}) does not match AssemblyFileVersion ({info.file_version})."
        )
    return info


def _pad_version(value: str, minimum: int = 3) -> list[int]:
    try:
        parts = [int(part) for part in value.split(".")]
    except ValueError as exc:
        raise BsmtError(f"Could not parse version: {value!r}") from exc
    while len(parts) < minimum:
        parts.append(0)
    return parts


def compare_versions(manifest_version: str, assembly_version: str) -> dict[str, Any]:
    """Compare a manifest SemVer with an assembly version (BSMT rules)."""

    try:
        assembly_parts = _pad_version(assembly_version, 3)
        base_manifest = strip_version_label(manifest_version)
        manifest_parts = _pad_version(base_manifest, 3)
    except (BsmtError, ManifestError) as exc:
        return {
            "match": False,
            "assembly_version": assembly_version,
            "manifest_version": manifest_version,
            "error": str(exc),
        }

    if len(manifest_parts) > len(assembly_parts):
        return {
            "match": False,
            "assembly_version": assembly_version,
            "manifest_version": manifest_version,
            "error": "Manifest version has more components than the assembly version.",
        }
    if len(assembly_parts) > 4:
        return {
            "match": False,
            "assembly_version": assembly_version,
            "manifest_version": manifest_version,
            "error": "AssemblyVersion cannot have more than four components.",
        }

    if len(assembly_parts) > 3:
        same_first_three = assembly_parts[:3] == manifest_parts and assembly_parts[3] == 0
        same_shifted = assembly_parts[1:] == manifest_parts and assembly_parts[0] == 0
        matched = same_first_three or same_shifted
    else:
        matched = assembly_parts == manifest_parts
    return {
        "match": matched,
        "assembly_version": assembly_version,
        "manifest_version": manifest_version,
        "base_manifest_version": base_manifest,
        "assembly_parts": assembly_parts,
        "manifest_parts": manifest_parts,
    }


def _effective_assembly_version(version: str | None) -> str | None:
    if not version:
        return None
    base = version
    if is_semver(version):
        base = strip_version_label(version)
    parts = base.split(".")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) == 3:
        parts.append("0")
    return ".".join(parts)


def read_assembly_info(
    project: ProjectInfo,
    *,
    evaluated: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Read the assembly version from AssemblyInfo.cs or SDK project properties."""

    assembly_info_path = project.directory / "Properties" / "AssemblyInfo.cs"
    info = AssemblyInfo(path=None, source="project")
    if assembly_info_path.is_file():
        info = parse_assembly_info(assembly_info_path)
    else:
        props = dict(project.properties_to_evaluated(evaluated))
        version = props.get("Version") or ""
        assembly_version = props.get("AssemblyVersion") or ""
        file_version = props.get("FileVersion") or ""
        prefix = props.get("VersionPrefix") or ""
        suffix = props.get("VersionSuffix") or ""
        if not version and prefix:
            version = f"{prefix}-{suffix}" if suffix else prefix
        effective = assembly_version or _effective_assembly_version(version)
        info = AssemblyInfo(
            path=None,
            assembly_version=effective,
            file_version=file_version or effective,
            informational_version=version or None,
            source="project-properties",
        )
        if not effective:
            info.warnings.append(
                "No Properties/AssemblyInfo.cs and no Version/AssemblyVersion property was found."
            )
    return info.to_dict()


def compare_project_versions(
    project: ProjectInfo,
    *,
    manifest_version: str | None = None,
    evaluated: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare the project's assembly version with its manifest version."""

    assembly = read_assembly_info(project, evaluated=evaluated)
    assembly_version = assembly.get("assembly_version")
    if not manifest_version:
        manifest_path = project.manifest_path()
        if manifest_path:
            import json

            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    manifest_version = data.get("version")
            except (OSError, ValueError):
                manifest_version = None
    if not manifest_version:
        return {
            "checked": False,
            "reason": "No manifest version was found.",
            "assembly": assembly,
        }
    if not assembly_version:
        return {
            "checked": False,
            "reason": "No assembly version could be determined.",
            "manifest_version": manifest_version,
            "assembly": assembly,
        }
    comparison = compare_versions(str(manifest_version), str(assembly_version))
    return {
        "checked": True,
        "match": comparison.get("match", False),
        "manifest_version": manifest_version,
        "assembly_version": assembly_version,
        "comparison": comparison,
        "assembly": assembly,
    }
