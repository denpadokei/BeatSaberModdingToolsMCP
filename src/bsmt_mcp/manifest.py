"""BSIPA manifest generation, merging and validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import __version__
from .errors import ManifestError
from .util import atomic_write_text

SCHEMA_URL = "https://raw.githubusercontent.com/bsmg/BSIPA-MetadataFileSchema/master/Schema.json"

SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
SEMVER_RE = re.compile(SEMVER_PATTERN)
VERRANGE_RE = re.compile(
    r"^(?:[~\^*]=?)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
    r"(?:-(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?)?$"
)
URL_RE = re.compile(r"^https?://[^\s]+$")
ID_RE = re.compile(r"^[^\s]+$")
TYPENAME_RE = re.compile(r"^(?:((?!\d)\w+(?:\.(?!\d)\w+)*)\.)?((?!\d)\w+)$")

REQUIRED_FIELDS = ("name", "id", "version", "gameVersion", "description", "author")
KNOWN_FIELDS = {
    "$schema",
    "name",
    "id",
    "version",
    "gameVersion",
    "description",
    "author",
    "dependsOn",
    "conflictsWith",
    "loadBefore",
    "loadAfter",
    "features",
    "icon",
    "files",
    "links",
    "misc",
}


def is_semver(value: Any) -> bool:
    return isinstance(value, str) and bool(SEMVER_RE.match(value))


def is_version_range(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value.strip() == "":
        return False
    return bool(VERRANGE_RE.match(value.strip()))


def strip_version_label(version: str) -> str:
    """Return ``major.minor.patch`` from a SemVer string (BSMT behaviour)."""

    if not isinstance(version, str) or not SEMVER_RE.match(version):
        raise ManifestError(
            f"Not a valid SemVer version: {version!r}",
            hint="Use a SemVer version such as 1.2.3 or 1.2.3-beta.1.",
        )
    match = SEMVER_RE.match(version)
    assert match is not None
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def game_version_from_install(version: str | None) -> str | None:
    """``1.44.1_20239`` (BS Manager) -> ``1.44.1``."""

    if not version:
        return None
    match = re.match(r"^(\d+\.\d+\.\d+)", version)
    return match.group(1) if match else None


def load_manifest(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    try:
        raw = file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManifestError(f"Could not read manifest: {file}", hint=str(exc)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ManifestError(f"Manifest is not valid JSON: {file}", hint=str(exc)) from exc
    if not isinstance(data, dict):
        raise ManifestError(f"Manifest must be a JSON object: {file}")
    return data


def validate_manifest(
    manifest: Any,
    *,
    requires_bsipa: bool = True,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate a BSIPA manifest against the published schema's important rules."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        return {"valid": False, "errors": ["Manifest root must be a JSON object."], "warnings": []}

    for field in REQUIRED_FIELDS:
        value = manifest.get(field)
        if field == "gameVersion" and not requires_bsipa and value in (None, ""):
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"Required property '{field}' is missing or empty.")
    for field in ("name", "id", "author", "version", "gameVersion"):
        value = manifest.get(field)
        if value is not None and not isinstance(value, str):
            errors.append(f"Property '{field}' must be a string.")
    version = manifest.get("version")
    if isinstance(version, str) and version and not is_semver(version):
        errors.append(f"'version' is not valid SemVer: {version!r}")
    game_version = manifest.get("gameVersion")
    if isinstance(game_version, str) and game_version and not is_semver(game_version):
        errors.append(f"'gameVersion' is not valid SemVer: {game_version!r}")
    mod_id = manifest.get("id")
    if isinstance(mod_id, str) and mod_id and not ID_RE.match(mod_id):
        errors.append("'id' must not contain whitespace.")

    description = manifest.get("description")
    if description is not None:
        if isinstance(description, list):
            if not all(isinstance(item, str) for item in description):
                errors.append("'description' array entries must be strings.")
            elif not any(item.strip() for item in description):
                errors.append("'description' must not be empty.")
        elif isinstance(description, str):
            if not description.strip():
                errors.append("'description' must not be empty.")
        else:
            errors.append("'description' must be a string or an array of strings.")

    depends_on = manifest.get("dependsOn")
    if depends_on is not None:
        if not isinstance(depends_on, dict):
            errors.append("'dependsOn' must be an object mapping mod ids to version ranges.")
        else:
            for key, value in depends_on.items():
                if not isinstance(key, str) or not key.strip() or not ID_RE.match(key):
                    errors.append(f"Invalid dependency id: {key!r}")
                if not is_version_range(value):
                    errors.append(f"Dependency '{key}' has an invalid version range: {value!r}")
            if requires_bsipa and not any(key.lower() == "bsipa" for key in depends_on):
                errors.append("'dependsOn' must include BSIPA for a BSIPA plugin.")
    elif requires_bsipa:
        errors.append("'dependsOn' is required and must include BSIPA.")

    conflicts = manifest.get("conflictsWith")
    if conflicts is not None:
        if not isinstance(conflicts, dict):
            errors.append("'conflictsWith' must be an object.")
        else:
            for key, value in conflicts.items():
                if not isinstance(key, str) or not ID_RE.match(key):
                    errors.append(f"Invalid conflictsWith id: {key!r}")
                if not is_version_range(value):
                    errors.append(f"conflictsWith '{key}' has an invalid version range: {value!r}")

    for field in ("files", "loadBefore", "loadAfter"):
        value = manifest.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"'{field}' must be an array of strings.")
        elif len(set(value)) != len(value):
            warnings.append(f"'{field}' contains duplicate entries.")

    links = manifest.get("links")
    if links is not None:
        if not isinstance(links, dict):
            errors.append("'links' must be an object.")
        else:
            for key in ("project-home", "project-source", "donate"):
                value = links.get(key)
                if value is not None and (not isinstance(value, str) or not URL_RE.match(value)):
                    errors.append(f"'links.{key}' must be an absolute http(s) URL.")
    icon = manifest.get("icon")
    if icon is not None and (not isinstance(icon, str) or not icon.lower().endswith(".png")):
        warnings.append("'icon' should be a .png resource path.")

    misc = manifest.get("misc")
    if misc is not None:
        if not isinstance(misc, dict):
            errors.append("'misc' must be an object.")
        elif "plugin-hint" in misc and not TYPENAME_RE.match(str(misc["plugin-hint"])):
            warnings.append("'misc.plugin-hint' does not look like a fully qualified type name.")
    features = manifest.get("features")
    if features is not None and not isinstance(features, dict):
        errors.append("'features' must be an object.")

    for key in manifest:
        if key not in KNOWN_FIELDS:
            warnings.append(f"Unknown top-level property '{key}' (BSIPA will ignore it).")

    if base_dir is not None:
        for entry in manifest.get("files") or []:
            candidate = base_dir / str(entry).replace("\\", "/")
            if not candidate.exists():
                warnings.append(f"File listed in 'files' does not exist: {entry}")

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _normalize_inputs(updates: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(updates)
    links = result.get("links")
    if isinstance(links, dict):
        result["links"] = {key: value for key, value in links.items() if value}
    for key in ("files", "loadBefore", "loadAfter"):
        if isinstance(result.get(key), list):
            seen: list[str] = []
            for item in result[key]:
                if isinstance(item, str) and item not in seen:
                    seen.append(item)
            result[key] = seen
    if isinstance(result.get("dependsOn"), list):
        converted: dict[str, str] = {}
        for item in result["dependsOn"]:
            if isinstance(item, dict) and "id" in item:
                converted[str(item["id"])] = str(item.get("version") or "")
            elif isinstance(item, str):
                converted[item] = ""
        result["dependsOn"] = converted
    return result


def _clean_empty(manifest: dict[str, Any]) -> None:
    for key in list(manifest):
        value = manifest[key]
        if value is None or isinstance(value, str) and value == "" or isinstance(value, dict) and not value or isinstance(value, list) and not value:
            manifest.pop(key)


def generate_manifest(
    *,
    base_manifest: str | Path | None = None,
    target_path: str | Path,
    updates: dict[str, Any] | None = None,
    requires_bsipa: bool = True,
    include_generated_by: bool = True,
) -> dict[str, Any]:
    """Merge ``updates`` into a base manifest, validate it, and write the result."""

    base: dict[str, Any] = {}
    if base_manifest:
        base_file = Path(base_manifest)
        if base_file.is_file():
            base = load_manifest(base_file)
    payload = _normalize_inputs(updates or {})
    if "links" in payload:
        payload["links"] = {key: value for key, value in payload["links"].items() if value}
    # BSMT's GenerateManifest appends item arrays to the base manifest instead of
    # replacing them (Files, LoadBefore, LoadAfter).
    for key in ("files", "loadBefore", "loadAfter"):
        base_values = base.get(key)
        new_values = payload.get(key)
        if isinstance(base_values, list) and isinstance(new_values, list):
            combined = list(base_values) + [value for value in new_values if value not in base_values]
            payload[key] = combined
    manifest = _deep_merge(base, payload)
    if include_generated_by:
        misc = manifest.get("misc")
        if not isinstance(misc, dict):
            misc = {}
        misc.setdefault("generated-by", f"bsmt-mcp/{__version__}")
        manifest["misc"] = misc
    manifest.setdefault("$schema", SCHEMA_URL)
    _clean_empty(manifest)
    validation = validate_manifest(manifest, requires_bsipa=requires_bsipa)
    if validation["errors"]:
        raise ManifestError(
            "Generated manifest is invalid: " + " ".join(validation["errors"]),
            hint="Fill in the missing fields or pass them explicitly (for example depends_on={'BSIPA': '^4.3.0'}).",
            details=validation,
        )
    target = Path(target_path)
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(target, text)
    version = manifest.get("version")
    return {
        "path": str(target),
        "manifest": manifest,
        "version": version,
        "base_version": strip_version_label(version) if isinstance(version, str) else None,
        "game_version": manifest.get("gameVersion"),
        "validation": validation,
    }


def read_manifest_info(path: str | Path, *, requires_bsipa: bool = True) -> dict[str, Any]:
    file = Path(path)
    manifest = load_manifest(file)
    validation = validate_manifest(manifest, requires_bsipa=requires_bsipa, base_dir=file.parent)
    version = manifest.get("version")
    base_version = None
    if isinstance(version, str) and is_semver(version):
        base_version = strip_version_label(version)
    data = file.read_bytes()
    return {
        "path": str(file),
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "manifest": manifest,
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "author": manifest.get("author"),
        "version": version,
        "base_version": base_version,
        "game_version": manifest.get("gameVersion"),
        "depends_on": manifest.get("dependsOn"),
        "validation": validation,
    }
