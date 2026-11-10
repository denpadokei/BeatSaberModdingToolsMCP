"""MSBuild project inspection and editing (references, BeatSaberDir, ...)."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from . import beatsaber
from .beatsaber import REFERENCE_DIRS, BeatSaberInstall, GameReference
from .errors import BsmtError, ProjectError
from .toolchain import evaluate_properties
from .util import atomic_write_bytes, find_project_file

NS_MSBUILD = "http://schemas.microsoft.com/developer/msbuild/2003"

GAME_REFERENCE_MARKERS = (
    "$(BeatSaberDir)",
    "$(GameReferences)",
    "$(BeatSaberReferences)",
    "$(LocalRefsDir)",
    "$(ReferencePath)",
    "Beat Saber_Data",
    "\\Libs\\",
    "\\Plugins\\",
    "/Libs/",
    "/Plugins/",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ReferenceInfo:
    include: str
    name: str
    hint_path: str | None
    private: str | None
    specific_version: str | None
    element: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "include": self.include,
            "hint_path": self.hint_path,
            "private": self.private,
            "specific_version": self.specific_version,
        }

    @property
    def is_game_reference(self) -> bool:
        hint = self.hint_path or ""
        return any(marker.lower() in hint.lower() for marker in GAME_REFERENCE_MARKERS)


@dataclass
class ManifestResource:
    include: str
    logical_name: str | None
    element: Any

    def to_dict(self) -> dict[str, Any]:
        return {"include": self.include, "logical_name": self.logical_name}


@dataclass
class OutputCopyItem:
    include: str
    output_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"include": self.include, "output_path": self.output_path}


@dataclass
class ProjectInfo:
    path: Path
    directory: Path
    tree: Any
    namespace: str | None
    sdk_style: bool
    assembly_name: str
    root_namespace: str
    target_frameworks: list[str]
    configurations: list[str]
    references: list[ReferenceInfo] = field(default_factory=list)
    package_references: list[dict[str, str]] = field(default_factory=list)
    project_references: list[str] = field(default_factory=list)
    embedded_manifests: list[ManifestResource] = field(default_factory=list)
    output_copy: list[OutputCopyItem] = field(default_factory=list)
    properties: dict[str, list[str]] = field(default_factory=dict)
    bsmt: dict[str, Any] = field(default_factory=dict)
    user_file: Path | None = None
    props_file: Path | None = None
    targets_file: Path | None = None
    _original_bytes: bytes | None = None

    # -- convenience -----------------------------------------------------
    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def is_bsipa(self) -> bool:
        return any(ref.name.lower() == "ipa.loader" for ref in self.references)

    @property
    def manifest_resource(self) -> ManifestResource | None:
        if not self.embedded_manifests:
            return None
        for item in self.embedded_manifests:
            if item.include.replace("\\", "/").strip("./").lower() == "manifest.json":
                return item
        return self.embedded_manifests[0]

    def manifest_path(self) -> Path | None:
        resource = self.manifest_resource
        if resource is not None:
            candidate = (self.directory / resource.include.replace("\\", "/")).resolve()
            if candidate.is_file():
                return candidate
        fallback = self.directory / "manifest.json"
        return fallback if fallback.is_file() else None

    def get_property(self, name: str, default: str = "") -> str:
        values = self.properties.get(name, [])
        if not values:
            return default
        return values[-1]

    def reference_names(self) -> set[str]:
        return {ref.name.lower() for ref in self.references}

    def properties_to_evaluated(self, evaluated: dict[str, str] | None = None) -> dict[str, str]:
        props = {name: values[-1] for name, values in self.properties.items() if values}
        props.update(
            {
                "ProjectDir": str(self.directory) + os.sep,
                "MSBuildProjectDirectory": str(self.directory),
                "AssemblyName": self.assembly_name,
                "ProjectName": self.assembly_name,
            }
        )
        if evaluated:
            props.update({key: value for key, value in evaluated.items() if value is not None})
        return props

    def to_dict(self, *, include_references: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "project": str(self.path),
            "directory": str(self.directory),
            "name": self.name,
            "sdk_style": self.sdk_style,
            "assembly_name": self.assembly_name,
            "root_namespace": self.root_namespace,
            "target_frameworks": self.target_frameworks,
            "configurations": self.configurations,
            "is_bsipa": self.is_bsipa,
            "reference_count": len(self.references),
            "package_references": self.package_references,
            "project_references": self.project_references,
            "bsmt": self.bsmt,
            "user_file": str(self.user_file) if self.user_file and self.user_file.is_file() else None,
            "directory_build_props": str(self.props_file) if self.props_file else None,
            "directory_build_targets": str(self.targets_file) if self.targets_file else None,
            "manifest": {
                "path": str(self.manifest_path()) if self.manifest_path() else None,
                "embedded": self.manifest_resource.to_dict() if self.manifest_resource else None,
            },
            "output_copy": [item.to_dict() for item in self.output_copy],
        }
        if include_references:
            data["references"] = [ref.to_dict() for ref in self.references]
        return data

    def save(self) -> None:
        _save_tree(self.tree, self.path)


# ---------------------------------------------------------------------------
# Loading / parsing
# ---------------------------------------------------------------------------


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _child_text(element: Any, local_name: str) -> str | None:
    for child in element:
        if _local(child.tag) == local_name:
            return child.text or ""
    return None


def _parse_tree(path: Path) -> tuple[Any, bytes | None]:
    original = path.read_bytes() if path.is_file() else None
    parser = etree.XMLParser(
        remove_blank_text=False,
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        strip_cdata=False,
    )
    if original is None or not original.strip():
        root = etree.Element("Project")
        return etree.ElementTree(root), None
    try:
        tree = etree.parse(str(path), parser)
    except (etree.XMLSyntaxError, OSError) as exc:
        raise ProjectError(
            f"Could not parse project XML: {path}",
            hint=f"{exc}. Fix the XML (or the file encoding) and try again.",
        ) from exc
    return tree, original


def _save_tree(tree: Any, path: Path, original: bytes | None = None) -> None:
    root = tree.getroot()
    if original is None:
        try:
            original = path.read_bytes()
        except OSError:
            original = None
    body = etree.tostring(root, encoding="unicode", pretty_print=False).rstrip()
    declaration = '<?xml version="1.0" encoding="utf-8"?>'
    has_bom = False
    if original:
        has_bom = original.startswith(b"\xef\xbb\xbf")
        try:
            text = original.decode("utf-8-sig", errors="replace")
        except Exception:
            text = ""
        match = re.match(r"<\?xml[^>]*\?>", text.lstrip("\ufeff"))
        if match:
            declaration = match.group(0)
    payload = (declaration + "\n" + body + "\n").encode("utf-8")
    if has_bom:
        payload = b"\xef\xbb\xbf" + payload
    atomic_write_bytes(path, payload)


def _is_sdk_style(root: Any) -> bool:
    if root.get("Sdk"):
        return True
    for element in root.iter():
        local = _local(element.tag)
        if local == "Sdk":
            return True
        if local == "Import" and "Microsoft.NET.Sdk" in (element.get("Project") or ""):
            return True
    return False


def _iter_direct(root: Any, name: str):
    for child in root:
        if _local(child.tag) == name:
            yield child


def _collect_properties(root: Any) -> dict[str, list[str]]:
    properties: dict[str, list[str]] = {}
    for group in _iter_direct(root, "PropertyGroup"):
        for element in group:
            if not isinstance(element.tag, str):
                continue
            name = _local(element.tag)
            value = element.text or ""
            if element.get("Condition"):
                # Keep conditional values available but rank them below unconditional ones.
                value = value
            properties.setdefault(name, []).append(value)
    return properties


def _pick_property(properties: dict[str, list[str]], name: str, default: str = "") -> str:
    values = properties.get(name)
    if not values:
        return default
    return values[-1]


def _normalize_tfm(value: str) -> str:
    value = value.strip()
    if value.startswith("v") and value.count(".") >= 2:
        parts = value[1:].split(".")
        if len(parts) >= 2:
            return f"net{parts[0]}{parts[1]}{parts[2] if len(parts) > 2 else ''}"
    return value


def _target_frameworks(properties: dict[str, list[str]]) -> list[str]:
    frameworks: list[str] = []
    multi = _pick_property(properties, "TargetFrameworks")
    single = _pick_property(properties, "TargetFramework")
    legacy = _pick_property(properties, "TargetFrameworkVersion")
    if multi:
        for item in re.split(r"[;,]", multi):
            item = item.strip()
            if item:
                frameworks.append(_normalize_tfm(item))
    if single and single not in frameworks:
        frameworks.append(_normalize_tfm(single))
    if legacy and not frameworks:
        frameworks.append(_normalize_tfm(legacy))
    return frameworks or ["unknown"]


def _configurations(properties: dict[str, list[str]], root: Any) -> list[str]:
    found: list[str] = []
    explicit = _pick_property(properties, "Configurations")
    if explicit:
        found.extend(item.strip() for item in explicit.split(";") if item.strip())
    for element in root.iter():
        condition = element.get("Condition") or ""
        for match in re.finditer(r"'\$\(Configuration\)\s*\|\s*\$\(Platform\)'\s*==\s*'([^'|]+)", condition):
            value = match.group(1).strip()
            if value and value not in found:
                found.append(value)
        for match in re.finditer(r"'\$\(Configuration\)'\s*==\s*'([^']+)'", condition):
            value = match.group(1).strip()
            if value and value not in found:
                found.append(value)
    if not found:
        found = ["Debug", "Release"]
    return found


def _parse_references(root: Any) -> list[ReferenceInfo]:
    references: list[ReferenceInfo] = []
    for element in root.iter():
        if _local(element.tag) != "Reference":
            continue
        include = element.get("Include")
        if not include:
            continue
        name = include.split(",", 1)[0].strip()
        references.append(
            ReferenceInfo(
                include=include,
                name=name,
                hint_path=_child_text(element, "HintPath"),
                private=_child_text(element, "Private"),
                specific_version=_child_text(element, "SpecificVersion"),
                element=element,
            )
        )
    return references


def _parse_package_references(root: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for element in root.iter():
        if _local(element.tag) != "PackageReference":
            continue
        include = element.get("Include") or ""
        if not include:
            continue
        result.append(
            {
                "include": include,
                "version": _child_text(element, "Version") or element.get("Version") or "",
                "private_assets": _child_text(element, "PrivateAssets") or "",
            }
        )
    return result


def _parse_project_references(root: Any) -> list[str]:
    result: list[str] = []
    for element in root.iter():
        if _local(element.tag) != "ProjectReference":
            continue
        include = element.get("Include")
        if include:
            result.append(include)
    return result


def _parse_manifests(root: Any) -> list[ManifestResource]:
    manifests: list[ManifestResource] = []
    for element in root.iter():
        if _local(element.tag) != "EmbeddedResource":
            continue
        include = element.get("Include") or ""
        if not include.replace("\\", "/").lower().endswith("manifest.json"):
            continue
        manifests.append(
            ManifestResource(
                include=include,
                logical_name=_child_text(element, "LogicalName"),
                element=element,
            )
        )
    return manifests


def _parse_output_copy(root: Any) -> list[OutputCopyItem]:
    result: list[OutputCopyItem] = []
    for element in root.iter():
        if _local(element.tag) != "OutputCopy":
            continue
        include = element.get("Include") or ""
        if not include:
            continue
        output_path = element.get("OutputPath") or _child_text(element, "OutputPath")
        result.append(OutputCopyItem(include=include, output_path=output_path))
    return result


def nearest_props_file(project_dir: Path) -> Path | None:
    current = project_dir.resolve()
    while True:
        candidate = current / "Directory.Build.props"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def nearest_targets_file(project_dir: Path) -> Path | None:
    current = project_dir.resolve()
    while True:
        candidate = current / "Directory.Build.targets"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def _detect_bsmt(info: ProjectInfo, package_refs: list[dict[str, str]]) -> dict[str, Any]:
    package = next(
        (item for item in package_refs if item["include"].lower() == "beatsabermoddingtools.tasks"),
        None,
    )
    import_value = _pick_property(info.properties, "ImportBSMTTargets", "").strip().lower()
    using_tasks = any(
        "BeatSaberModdingTools" in (element.get("TaskName") or "")
        for element in info.tree.getroot().iter()
        if _local(element.tag) == "UsingTask"
    )
    directory_targets = False
    if info.targets_file and info.targets_file.is_file():
        try:
            directory_targets = "BeatSaberModdingTools" in info.targets_file.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            directory_targets = False
    present = bool(package or using_tasks or directory_targets)
    if import_value in {"false", "0", "off"}:
        enabled = False
    elif import_value in {"true", "1", "on"}:
        enabled = True
    else:
        enabled = present
    return {
        "present": present,
        "enabled": enabled,
        "package_version": package["version"] if package else None,
        "import_targets_property": import_value or None,
        "using_tasks": using_tasks,
        "directory_build_targets": str(info.targets_file) if directory_targets else None,
    }


def _merge_properties(base: dict[str, list[str]], overlay: dict[str, list[str]]) -> dict[str, list[str]]:
    merged = {name: list(values) for name, values in base.items()}
    for name, values in overlay.items():
        merged[name] = list(values)
    return merged


def load_project(path: str | os.PathLike[str] | None) -> ProjectInfo:
    project_path = find_project_file(path)
    tree, original = _parse_tree(project_path)
    root = tree.getroot()
    namespace = root.nsmap.get(None)
    props_file = nearest_props_file(project_path.parent)
    targets_file = nearest_targets_file(project_path.parent)
    user_file = project_path.with_name(project_path.name + ".user")

    # MSBuild import order: Directory.Build.props -> project -> .csproj.user.
    properties: dict[str, list[str]] = {}
    for extra in (props_file, user_file):
        if extra and extra.is_file():
            try:
                extra_tree, _ = _parse_tree(extra)
                properties = _merge_properties(properties, _collect_properties(extra_tree.getroot()))
            except BsmtError:
                pass
    properties = _merge_properties(properties, _collect_properties(root))

    assembly_name = _pick_property(properties, "AssemblyName") or project_path.stem
    root_namespace = _pick_property(properties, "RootNamespace") or assembly_name
    info = ProjectInfo(
        path=project_path,
        directory=project_path.parent,
        tree=tree,
        namespace=namespace,
        sdk_style=_is_sdk_style(root),
        assembly_name=assembly_name,
        root_namespace=root_namespace,
        target_frameworks=_target_frameworks(properties),
        configurations=_configurations(properties, root),
        properties=properties,
        _original_bytes=original,
    )
    info.references = _parse_references(root)
    info.package_references = _parse_package_references(root)
    info.project_references = _parse_project_references(root)
    info.embedded_manifests = _parse_manifests(root)
    info.output_copy = _parse_output_copy(root)
    info.user_file = user_file
    info.props_file = props_file
    info.targets_file = targets_file
    info.bsmt = _detect_bsmt(info, info.package_references)
    return info


# ---------------------------------------------------------------------------
# XML editing helpers
# ---------------------------------------------------------------------------


def _qualified(info: ProjectInfo, tag: str) -> str:
    return f"{{{info.namespace}}}{tag}" if info.namespace else tag


def _indent_width(root: Any) -> str:
    for child in root:
        tail = child.tail or ""
        match = re.search(r"\n([ \t]+)\S", tail)
        if match:
            return match.group(1)
        if tail.startswith("\n"):
            return "  "
    return "  "


def _child_indent(parent: Any, *, fallback: str = "  ") -> str:
    """Indentation used by ``parent``'s children."""

    for child in parent:
        tail = child.tail or ""
        match = re.search(r"\n([ \t]+)\S", tail)
        if match:
            return match.group(1)
        if tail.startswith("\n"):
            return fallback
    return fallback


def _ensure_root_text(root: Any, indent: str) -> None:
    if not root.text or not root.text.strip():
        root.text = "\n" + indent


def _layout_element(element: Any, element_indent: str) -> None:
    """Give ``element``'s element children and closing tag consistent indentation."""

    children = [child for child in element if isinstance(child.tag, str)]
    if not children:
        return
    child_indent = element_indent + "  "
    element.text = "\n" + child_indent
    for index, child in enumerate(children):
        child.tail = "\n" + (element_indent if index == len(children) - 1 else child_indent)
        _layout_element(child, child_indent)


def _create_item_group(info: ProjectInfo, items: list[Any]) -> Any:
    """Create a properly indented ItemGroup and insert it into the project."""

    root = info.tree.getroot()
    indent = _indent_width(root)
    _ensure_root_text(root, indent)
    indent + "  "
    group = etree.Element(_qualified(info, "ItemGroup"))
    for item in items:
        group.append(item)
    _layout_element(group, indent)
    # Insert before the first Import/Target so explicit imports stay at the
    # bottom; otherwise append at the end of the project.
    insert_at = None
    for index, child in enumerate(root):
        if _local(child.tag) in {"Import", "Target"}:
            insert_at = index
            break
    if insert_at is None:
        element_children = [child for child in root if isinstance(child.tag, str)]
        if element_children:
            element_children[-1].tail = "\n" + indent
        group.tail = "\n"
        root.append(group)
    else:
        group.tail = "\n" + indent
        root.insert(insert_at, group)
    return group


def _handle_duplicate_reference(info: ProjectInfo, ref: ReferenceInfo, new_hint: str, force: bool) -> str:
    if not force:
        return "skipped"
    changed = False
    if ref.hint_path != new_hint:
        for child in ref.element:
            if _local(child.tag) == "HintPath":
                child.text = new_hint
                changed = True
                break
        else:
            hint = etree.SubElement(ref.element, "HintPath")
            hint.text = new_hint
            hint.tail = ref.element[-1].tail if len(ref.element) > 1 else None
            changed = True
    for tag, value in (("Private", "False"), ("SpecificVersion", "False")):
        current = _child_text(ref.element, tag)
        if current is None:
            element = etree.SubElement(ref.element, tag)
            element.text = value
            changed = True
        elif current.strip().lower() not in {"false", "0"}:
            for child in ref.element:
                if _local(child.tag) == tag:
                    child.text = value
                    changed = True
                    break
    return "updated" if changed else "skipped"


# ---------------------------------------------------------------------------
# Reference management
# ---------------------------------------------------------------------------


def add_references(
    info: ProjectInfo,
    references: Iterable[GameReference],
    *,
    hint_root: str = "$(BeatSaberDir)",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Add ``<Reference>`` items with HintPaths rooted at ``hint_root``."""

    root = info.tree.getroot()
    indent = _indent_width(root)
    reference_indent = indent + "  "
    metadata_indent = reference_indent + "  "
    hint_root = hint_root.rstrip("\\/")
    existing = {ref.name.lower(): ref for ref in info.references}
    seen = set(existing)
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    new_elements: list[Any] = []
    for reference in references:
        relative = reference.relative_dir.replace("/", "\\")
        hint = f"{hint_root}\\{relative}\\{reference.name}.dll"
        key = reference.name.lower()
        if key in seen:
            if key in existing:
                outcome = _handle_duplicate_reference(info, existing[key], hint, force)
                if outcome == "updated":
                    updated.append(reference.name)
                elif outcome == "skipped":
                    skipped.append(reference.name)
            else:
                skipped.append(reference.name)
            continue
        element = etree.Element(_qualified(info, "Reference"))
        element.set("Include", reference.name)
        hint_element = etree.SubElement(element, _qualified(info, "HintPath"))
        hint_element.text = hint
        hint_element.tail = "\n" + metadata_indent
        private = etree.SubElement(element, _qualified(info, "Private"))
        private.text = "False"
        private.tail = "\n" + metadata_indent
        specific = etree.SubElement(element, _qualified(info, "SpecificVersion"))
        specific.text = "False"
        specific.tail = "\n" + reference_indent
        new_elements.append(element)
        added.append(reference.name)
        seen.add(key)
    if new_elements:
        _create_item_group(info, new_elements)
    if not dry_run and (added or updated):
        info.save()
        info.references = _parse_references(root)
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "hint_root": hint_root,
        "dry_run": dry_run,
        "changed": bool(added or updated),
    }


def add_project_items(
    info: ProjectInfo,
    items: Iterable[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Add Compile/EmbeddedResource/None items for newly created item-template files."""

    root = info.tree.getroot()
    existing: set[str] = set()
    for element in root.iter():
        if _local(element.tag) not in {"Compile", "EmbeddedResource", "None", "Content"}:
            continue
        include = element.get("Include")
        if include:
            existing.add(include.replace("\\", "/").lstrip("./").lower())

    added: list[str] = []
    implicit: list[str] = []
    skipped: list[str] = []
    new_elements: list[Any] = []
    sdk = info.sdk_style
    for item in items:
        path = Path(str(item["path"]))
        if not path.is_absolute():
            path = info.directory / path
        try:
            relative = path.resolve().relative_to(info.directory.resolve())
        except ValueError as exc:
            raise BsmtError(f"Item template wrote outside the project: {path}") from exc
        include_posix = relative.as_posix()
        include_key = include_posix.lower()
        item_type = str(item.get("item_type") or ("Compile" if relative.suffix.lower() == ".cs" else "None"))
        if sdk and item_type.lower() in {"compile", "none", "content"}:
            implicit.append(include_posix)
            continue
        if sdk and item_type.lower() == "embeddedresource" and relative.suffix.lower() == ".resx":
            implicit.append(include_posix)
            continue
        if include_key in existing:
            skipped.append(include_posix)
            continue
        element = etree.Element(_qualified(info, item_type))
        element.set("Include", str(relative))
        dependent = item.get("dependent_upon")
        if dependent:
            child = etree.Element(_qualified(info, "DependentUpon"))
            child.text = str(dependent)
            element.append(child)
        new_elements.append(element)
        existing.add(include_key)
        added.append(include_posix)

    if new_elements and not dry_run:
        _create_item_group(info, new_elements)
        info.save()
    return {
        "added": added,
        "implicit": implicit,
        "skipped": skipped,
        "changed": bool(new_elements),
        "dry_run": dry_run,
    }


def remove_references(
    info: ProjectInfo,
    names: Iterable[str],
    *,
    only_game_references: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    wanted = {str(name).strip().split(",", 1)[0].lower() for name in names if str(name).strip()}
    if wanted:
        for name in list(wanted):
            if name.endswith(".dll"):
                wanted.add(name[:-4])
    removed: list[str] = []
    kept: list[str] = []
    for reference in info.references:
        if reference.name.lower() not in wanted:
            continue
        if only_game_references and not reference.is_game_reference:
            kept.append(reference.name)
            continue
        parent = reference.element.getparent()
        if parent is None:
            continue
        parent.remove(reference.element)
        removed.append(reference.name)
        if len(parent) == 0:
            grandparent = parent.getparent()
            if grandparent is not None:
                grandparent.remove(parent)
    if removed and not dry_run:
        info.save()
        info.references = _parse_references(info.tree.getroot())
    return {
        "removed": sorted(set(removed)),
        "kept_non_game_references": sorted(set(kept)),
        "dry_run": dry_run,
        "changed": bool(removed),
    }


def _append_child(element: Any, child: Any, element_indent: str) -> None:
    """Append a child and keep the XML indentation readable."""

    children = [item for item in element if isinstance(item.tag, str)]
    child_indent = element_indent + "  "
    if not children:
        element.text = "\n" + child_indent
    else:
        children[-1].tail = "\n" + child_indent
    child.tail = "\n" + element_indent
    element.append(child)


def _append_property_element(namespace: str | None, group: Any, name: str, value: str, group_indent: str) -> None:
    qname = f"{{{namespace}}}{name}" if namespace else name
    element = etree.Element(qname)
    element.text = value
    _append_child(group, element, group_indent)


def _set_property_in_tree(info: ProjectInfo, tree: Any, name: str, value: str) -> tuple[str | None, bool]:
    root = tree.getroot()
    namespace = root.nsmap.get(None)
    indent = _indent_width(root)
    target_group = None
    old_value: str | None = None
    for group in _iter_direct(root, "PropertyGroup"):
        for element in group:
            if isinstance(element.tag, str) and _local(element.tag) == name:
                old_value = element.text or ""
                # Template placeholders often contain a comment child; replace it.
                for child in list(element):
                    element.remove(child)
                element.text = value
                return old_value, True
        if target_group is None:
            target_group = group
    _ensure_root_text(root, indent)
    if target_group is None:
        qname = f"{{{namespace}}}PropertyGroup" if namespace else "PropertyGroup"
        target_group = etree.SubElement(root, qname)
        target_group.tail = "\n"
    _append_property_element(namespace, target_group, name, value, indent)
    return None, True


def _new_user_tree(info: ProjectInfo) -> Any:
    root = etree.Element("Project")
    root.text = "\n  "
    group = etree.SubElement(root, "PropertyGroup")
    group.tail = "\n"
    return etree.ElementTree(root)


def _file_has_property(path: Path, name: str) -> bool:
    try:
        tree, _ = _parse_tree(path)
    except BsmtError:
        return False
    for group in _iter_direct(tree.getroot(), "PropertyGroup"):
        for element in group:
            if isinstance(element.tag, str) and _local(element.tag) == name:
                return True
    return False


def set_game_properties(
    info: ProjectInfo,
    *,
    game_dir: Path,
    target: str = "auto",
    set_game_directory: bool = True,
    set_reference_path: bool = False,
) -> dict[str, Any]:
    """Write BeatSaberDir (and optionally GameDirectory / ReferencePath)."""

    game_dir = game_dir.resolve()
    values: dict[str, str] = {"BeatSaberDir": str(game_dir)}
    if set_game_directory:
        values["GameDirectory"] = str(game_dir)
    if set_reference_path:
        values["ReferencePath"] = ";".join(str(game_dir / relative) for relative in REFERENCE_DIRS)
    return set_property_values(info, values, target=target)


def set_property_values(
    info: ProjectInfo,
    values: dict[str, str],
    *,
    target: str = "auto",
) -> dict[str, Any]:
    """Set one or more MSBuild properties in the project / user file / props file."""

    if target not in {"auto", "user", "project", "props"}:
        raise BsmtError(f"Unknown target: {target}", hint="Use auto, user, project or props.")

    candidates: list[Path] = []
    if target == "auto":
        for path in (info.path, info.user_file, info.props_file):
            if path and path.is_file() and any(_file_has_property(path, name) for name in values):
                candidates.append(path)
        if not candidates:
            candidates = [info.user_file]
    elif target == "user":
        candidates = [info.user_file]
    elif target == "project":
        candidates = [info.path]
    else:
        candidates = [info.props_file or (info.directory / "Directory.Build.props")]

    changes: list[dict[str, Any]] = []
    written: list[str] = []
    for path in candidates:
        if path is None:
            continue
        created = not path.is_file()
        if path.is_file():
            tree, original = _parse_tree(path)
        else:
            tree, original = _new_user_tree(info), None
        file_changed = False
        for name, value in values.items():
            old, changed = _set_property_in_tree(info, tree, name, value)
            if changed:
                changes.append(
                    {
                        "file": str(path),
                        "property": name,
                        "old": old,
                        "new": value,
                        "created": created,
                    }
                )
                file_changed = True
        if file_changed:
            _save_tree(tree, path, original)
            written.append(str(path))
    if info.path in [Path(item) for item in written]:
        info.properties = _collect_properties(info.tree.getroot())
    return {"changes": changes, "files": written, "values": values}


def ensure_manifest_embedded(
    info: ProjectInfo,
    *,
    manifest_rel: str = "manifest.json",
    logical_name: str = "$(AssemblyName).manifest.json",
    force: bool = False,
) -> dict[str, Any]:
    """Make sure the manifest becomes ``<AssemblyName>.manifest.json`` in the DLL.

    Projects using BeatSaberModdingTools.Tasks normally let the package generate
    and embed the manifest. In that case we leave the project alone to avoid a
    duplicate resource name.
    """

    if info.bsmt.get("enabled") and not force:
        return {
            "changed": False,
            "managed_by": "BeatSaberModdingTools.Tasks",
            "reason": "ImportBSMTTargets is enabled; the package generates and embeds the manifest.",
        }
    root = info.tree.getroot()
    existing = info.manifest_resource
    root_namespace = info.root_namespace
    if existing is not None:
        current_logical = existing.logical_name
        if current_logical:
            return {"changed": False, "managed_by": "project", "logical_name": current_logical}
        if root_namespace == info.assembly_name and not force:
            return {
                "changed": False,
                "managed_by": "project",
                "logical_name": f"{info.assembly_name}.manifest.json",
                "reason": "Default embedded resource name already matches the assembly name.",
            }
        for child in existing.element:
            if _local(child.tag) == "LogicalName":
                child.text = logical_name
                break
        else:
            parent = existing.element.getparent()
            element_indent = _child_indent(parent, fallback="  ") if parent is not None else "  "
            element = etree.Element(_qualified(info, "LogicalName"))
            element.text = logical_name
            _append_child(existing.element, element, element_indent)
        info.save()
        return {"changed": True, "managed_by": "project", "logical_name": logical_name}

    element = etree.Element(_qualified(info, "EmbeddedResource"))
    element.set("Include", manifest_rel.replace("/", "\\"))
    logical = etree.SubElement(element, _qualified(info, "LogicalName"))
    logical.text = logical_name
    logical.tail = "\n"
    _create_item_group(info, [element])
    info.save()
    info.embedded_manifests = _parse_manifests(root)
    return {"changed": True, "managed_by": "bsmt-mcp", "logical_name": logical_name, "include": manifest_rel}


# ---------------------------------------------------------------------------
# Reference copies (offline / CI builds)
# ---------------------------------------------------------------------------


def copy_game_references(
    source_dir: Path,
    destination: Path,
    *,
    force: bool = False,
    relative_dirs: Iterable[Path] = REFERENCE_DIRS,
) -> dict[str, Any]:
    """Copy game DLLs into a local References tree, preserving folder layout."""

    destination = destination.resolve()
    source_dir = source_dir.resolve()
    if destination == source_dir or source_dir in destination.parents:
        raise BsmtError(
            "The reference copy destination must be outside the game folder.",
            hint="Use a folder such as <project>/../Refs.",
        )
    copied: list[str] = []
    skipped: list[str] = []
    total_bytes = 0
    for relative in relative_dirs:
        source = source_dir / relative
        if not source.is_dir():
            continue
        for file in sorted(source.glob("*.dll")):
            if not file.is_file():
                continue
            target = destination / relative / file.name
            if target.is_file() and not force:
                try:
                    if target.stat().st_size == file.stat().st_size:
                        skipped.append(str(relative / file.name))
                        continue
                except OSError:
                    pass
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            copied.append(str(relative / file.name))
            total_bytes += file.stat().st_size
    return {
        "destination": str(destination),
        "copied": copied[:50],
        "copied_count": len(copied),
        "copied_truncated": len(copied) > 50,
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
        "skipped_truncated": len(skipped) > 50,
        "bytes": total_bytes,
    }


# ---------------------------------------------------------------------------
# Property resolution fallback
# ---------------------------------------------------------------------------


def expand_msbuild_value(value: str, properties: dict[str, str], project_dir: Path) -> str:
    """Expand the small subset of MSBuild properties needed for paths."""

    text = value
    for _ in range(10):
        replaced = False

        def substitute(match: re.Match[str]) -> str:
            nonlocal replaced
            name = match.group(1)
            replaced = True
            if name in {"ProjectDir", "MSBuildProjectDirectory"}:
                return str(project_dir) + os.sep
            if name in {"MSBuildThisFileDirectory"}:
                return str(project_dir) + os.sep
            if name in properties:
                return properties[name]
            return os.environ.get(name, "")

        new_text = re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_.-]*)\)", substitute, text)
        text = new_text
        if not replaced:
            break
    return text


def raw_property(info: ProjectInfo, name: str) -> str | None:
    values = info.properties.get(name)
    if not values:
        return None
    return values[-1]


def resolve_project_game_dir(
    info: ProjectInfo,
    *,
    explicit: str | None = None,
    configuration: str = "Debug",
    installs: list[BeatSaberInstall] | None = None,
) -> tuple[BeatSaberInstall, str, list[BeatSaberInstall]]:
    """Resolve the game folder using explicit input, MSBuild properties, then detection."""

    if explicit:
        return beatsaber.ensure_install(explicit), "explicit argument", installs or []

    evaluated, error = evaluate_properties(
        info.path,
        ["BeatSaberDir", "GameDirectory", "TargetPath", "TargetDir", "AssemblyName", "Version"],
        configuration=configuration,
    )
    candidates = installs if installs is not None else beatsaber.detect_installs()
    if evaluated:
        for name in ("GameDirectory", "BeatSaberDir"):
            value = (evaluated.get(name) or "").strip()
            if value and beatsaber.is_beat_saber_dir(value):
                return beatsaber.ensure_install(value), f"project property {name}", candidates

    # Fallback for machines without MSBuild: expand the raw values ourselves.
    fallback_props = {
        name: values[-1]
        for name, values in info.properties.items()
        if values and values[-1]
    }
    for name in ("GameDirectory", "BeatSaberDir"):
        raw = raw_property(info, name)
        if not raw:
            continue
        expanded = expand_msbuild_value(raw, fallback_props, info.directory)
        if expanded and beatsaber.is_beat_saber_dir(expanded):
            return beatsaber.ensure_install(expanded), f"project property {name} (raw)", candidates

    manifest_version = None
    manifest_path = info.manifest_path()
    if manifest_path:
        try:
            import json

            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if isinstance(manifest, dict):
                manifest_version = manifest.get("gameVersion")
        except (OSError, ValueError):
            manifest_version = None

    install, candidates, reason = beatsaber.resolve_install(
        None,
        manifest_game_version=manifest_version,
        installs=candidates,
    )
    return install, f"auto-detected ({reason})", candidates


# ---------------------------------------------------------------------------
# Output copy expansion
# ---------------------------------------------------------------------------


def expand_output_copy(
    info: ProjectInfo,
    item: OutputCopyItem,
    evaluated: dict[str, str] | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve an ``OutputCopy`` item to ``(source, destination)``.

    Supports the MSBuild properties commonly used by BSMT templates. Wildcards
    are handled by the caller through :func:`glob_output_copy`.
    """

    props = dict(info.properties_to_evaluated(evaluated))
    include = expand_msbuild_value(item.include, props, info.directory)
    destination = item.output_path or Path(include).name
    destination = expand_msbuild_value(destination, props, info.directory)
    if any(char in include for char in "*?"):
        return None, destination
    source = Path(include)
    if not source.is_absolute():
        source = info.directory / source
    return source, destination


def glob_output_copy(
    info: ProjectInfo,
    item: OutputCopyItem,
    evaluated: dict[str, str] | None = None,
) -> list[tuple[Path, str]]:
    props = dict(info.properties_to_evaluated(evaluated))
    include = expand_msbuild_value(item.include, props, info.directory)
    if not any(char in include for char in "*?"):
        source, destination = expand_output_copy(info, item, evaluated)
        return [(source, destination)] if source else []
    base = Path(include)
    if not base.is_absolute():
        base = info.directory / base
    matches: list[tuple[Path, str]] = []
    for match in sorted(base.parent.glob(base.name)):
        if not match.is_file():
            continue
        destination = item.output_path or match.name
        if item.output_path and any(char in item.output_path for char in "*?"):
            # Only filename-level substitution is supported for wildcards.
            destination = item.output_path.replace("*", match.stem)
        destination = expand_msbuild_value(destination, props, info.directory)
        matches.append((match, destination))
    return matches
