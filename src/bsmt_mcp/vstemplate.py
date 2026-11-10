"""Minimal Visual Studio `.vstemplate` expansion engine.

The official Beat Saber templates are VS templates with ``$safeprojectname$``
style parameters, conditional lines and ``ProjectItem`` target mappings. This
module implements just enough of the template engine to instantiate them on the
command line, without Visual Studio.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .errors import ProjectError
from .util import atomic_write_text, safe_relative_path

VST_NS = "http://schemas.microsoft.com/developer/vstemplate/2005"
_PARAM_RE = re.compile(r"\$([A-Za-z0-9_]+)\$")


@dataclass
class TemplateInfo:
    key: str
    name: str
    description: str
    default_name: str
    framework: str
    template_type: str
    path: Path
    folder: Path
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "default_name": self.default_name,
            "framework": self.framework,
            "type": self.template_type,
            "path": str(self.path),
            "source": self.source,
        }


@dataclass
class ExpandedItem:
    path: Path
    item_type: str | None
    dependent_upon: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "item_type": self.item_type,
            "dependent_upon": self.dependent_upon,
        }


def _local(element: Any) -> str:
    tag = element.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return str(tag)


def _child(element: Any, name: str) -> Any | None:
    for item in element:
        if _local(item) == name:
            return item
    return None


def _text(element: Any, name: str) -> str:
    child = _child(element, name)
    return (child.text or "").strip() if child is not None else ""


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return value or "template"


def _legacy_key(folder: str) -> str | None:
    lower = folder.lower()
    for key in ("bare", "core", "disableable"):
        if key in lower:
            return key
    return None


def parse_template(path: Path, *, source: str = "") -> TemplateInfo:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        raise ProjectError(f"Could not parse template: {path}", hint=str(exc)) from exc
    root = tree.getroot()
    if _local(root) != "VSTemplate":
        raise ProjectError(f"Not a Visual Studio template: {path}")
    template_type = root.get("Type", "Project")
    data = _child(root, "TemplateData")
    name = _text(data, "Name") if data is not None else path.stem
    description = _text(data, "Description") if data is not None else ""
    default_name = _text(data, "DefaultName") if data is not None else ""
    framework = _text(data, "RequiredFrameworkVersion") if data is not None else ""
    key = _legacy_key(path.parent.name) or _slug(name)
    return TemplateInfo(
        key=key,
        name=name or path.stem,
        description=description,
        default_name=default_name,
        framework=framework,
        template_type=template_type,
        path=path,
        folder=path.parent,
        source=source or str(path.parent),
    )


def discover_templates(root: Path, *, source: str = "", template_type: str = "Project") -> list[TemplateInfo]:
    """Find ``.vstemplate`` files below ``root`` (project templates by default)."""

    if not root.is_dir():
        raise ProjectError(f"Template directory does not exist: {root}")
    templates = [parse_template(path, source=source or str(root)) for path in sorted(root.rglob("*.vstemplate"))]
    if not template_type:
        return templates
    return [template for template in templates if template.template_type.lower() == template_type.lower()]


def resolve_templates_root(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolve a template repository directory.

    Order: explicit argument, ``BSMT_TEMPLATES_DIR``, the templates bundled with
    this package. Returns ``None`` only if the bundled data is missing.
    """

    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("BSMT_TEMPLATES_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
        raise ProjectError(
            f"Template directory does not exist: {candidate}",
            hint="Pass a clone of https://github.com/denpadokei/UnityModdingTools.Templates.BeatSaber.",
        )
    bundled = Path(__file__).resolve().parent / "template_data" / "official"
    return bundled if bundled.is_dir() else None


def find_template(templates: list[TemplateInfo], name: str) -> TemplateInfo:
    wanted = name.strip().lower()
    for template in templates:
        if wanted in {template.key.lower(), template.name.lower(), template.folder.name.lower()}:
            return template
    for template in templates:
        if wanted in template.key.lower() or wanted in template.name.lower() or wanted in template.folder.name.lower():
            return template
    available = ", ".join(template.key for template in templates)
    raise ProjectError(
        f"Unknown template: {name}. Available: {available}",
        hint="Pass template='sdk', 'bare', 'core' or 'disableable'.",
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _eval_condition(condition: str, parameters: dict[str, str]) -> bool:
    expanded = _PARAM_RE.sub(lambda match: parameters.get(match.group(1), ""), condition)
    match = re.search(r"(\d+(?:\.\d+)*)\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)", expanded)
    if not match:
        return True
    left, operator, right = _version_tuple(match.group(1)), match.group(2), _version_tuple(match.group(3))
    length = max(len(left), len(right))
    left = left + (0,) * (length - len(left))
    right = right + (0,) * (length - len(right))
    return {
        ">=": left >= right,
        "<=": left <= right,
        "==": left == right,
        "!=": left != right,
        ">": left > right,
        "<": left < right,
    }[operator]


def _process_conditionals(text: str, parameters: dict[str, str]) -> str:
    """Handle the simple ``$if$ ($targetframeworkversion$ >= 3.5)`` syntax."""

    output: list[str] = []
    skipping = 0
    for line in text.splitlines(keepends=True):
        if "$if$" in line:
            condition, _, remainder = line.partition("$if$")
            condition_text, separator, after = remainder.partition(")")
            keep = _eval_condition(condition_text, parameters)
            if keep:
                line = condition + (after if separator else "")
                line = line.replace("$endif$", "")
            else:
                skipping += 1
                continue
        if "$endif$" in line:
            if skipping:
                skipping -= 1
                continue
            line = line.replace("$endif$", "")
        if not skipping:
            output.append(line)
    return "".join(output)


def substitute(text: str, parameters: dict[str, str]) -> str:
    text = _process_conditionals(text, parameters)
    return _PARAM_RE.sub(lambda match: parameters.get(match.group(1), match.group(0)), text)


def build_parameters(
    *,
    project_name: str,
    identifier: str,
    target_framework_version: str,
    author: str = "",
    root_namespace: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    project_guid = str(uuid.uuid4()).upper()
    parameters = {
        "safeprojectname": identifier,
        "projectname": project_name,
        "rootnamespace": root_namespace or identifier,
        "guid1": "{" + project_guid + "}",
        "guid2": project_guid,
        "targetframeworkversion": target_framework_version,
        "registeredorganization": author,
        "year": str(datetime.now().year),
        "username": os.environ.get("USERNAME", ""),
        "machinename": os.environ.get("COMPUTERNAME", ""),
        "currenttime": datetime.now().isoformat(timespec="seconds"),
        "clrversion": "4.0",
    }
    if extra:
        parameters.update({key.lstrip("$").rstrip("$"): value for key, value in extra.items()})
    return parameters


def expand_template(template: TemplateInfo, destination: Path, parameters: dict[str, str]) -> list[Path]:
    """Write an instantiated project to ``destination`` and return its files."""
    try:
        tree = ET.parse(template.path)
    except (ET.ParseError, OSError) as exc:
        raise ProjectError(f"Could not parse template: {template.path}", hint=str(exc)) from exc
    root = tree.getroot()
    content = _child(root, "TemplateContent")
    project = _child(content, "Project") if content is not None else None
    if project is None:
        raise ProjectError(f"Template has no TemplateContent/Project element: {template.path}")

    mappings: list[tuple[Path, str, bool]] = []
    source_file = project.get("File")
    if not source_file:
        raise ProjectError(f"Template Project element has no File attribute: {template.path}")
    mappings.append(
        (
            template.folder / source_file,
            project.get("TargetFileName") or source_file,
            (project.get("ReplaceParameters") or "").lower() == "true",
        )
    )
    for item in project:
        if _local(item) != "ProjectItem":
            continue
        item_source = (item.text or "").strip()
        if not item_source:
            continue
        mappings.append(
            (
                template.folder / item_source,
                item.get("TargetFileName") or item_source,
                (item.get("ReplaceParameters") or "").lower() == "true",
            )
        )

    written: list[Path] = []
    for source, target, replace in mappings:
        if not source.is_file():
            raise ProjectError(
                f"Template references a missing file: {source}",
                hint="The template checkout may be incomplete; update or re-clone the template repository.",
            )
        target_name = substitute(target, parameters) if replace else target
        relative = safe_relative_path(target_name)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        if replace:
            text = source.read_text(encoding="utf-8-sig", errors="replace")
            atomic_write_text(output, substitute(text, parameters), encoding="utf-8")
        else:
            output.write_bytes(source.read_bytes())
        written.append(output)
    return written


def _infer_item_type(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix == ".cs":
        return "Compile"
    if suffix == ".bsml":
        return "EmbeddedResource"
    if suffix in {".resx", ".png", ".jpg", ".txt", ".json", ".xml"}:
        return "None"
    return None


def expand_item_template(
    template: TemplateInfo,
    destination: Path,
    parameters: dict[str, str],
    *,
    file_input_name: str,
    force: bool = False,
) -> list[ExpandedItem]:
    """Instantiate an ``Item`` template into ``destination``.

    Paths containing the ``Name.cs/Name.bsml`` nesting used by the BSML
    ViewController template are flattened to sibling files, which is what the
    BSML loader expects at runtime.
    """

    try:
        tree = ET.parse(template.path)
    except (ET.ParseError, OSError) as exc:
        raise ProjectError(f"Could not parse template: {template.path}", hint=str(exc)) from exc
    root = tree.getroot()
    content = _child(root, "TemplateContent")
    if content is None:
        raise ProjectError(f"Template has no TemplateContent element: {template.path}")

    expanded: list[ExpandedItem] = []
    for item in content:
        if _local(item) != "ProjectItem":
            continue
        item_source = (item.text or "").strip()
        if not item_source:
            continue
        source = template.folder / item_source
        if not source.is_file():
            raise ProjectError(
                f"Template references a missing file: {source}",
                hint="The template checkout may be incomplete; update or re-clone the template repository.",
            )
        replace = (item.get("ReplaceParameters") or "").lower() == "true"
        target = item.get("TargetFileName") or f"{file_input_name}{source.suffix}"
        target_name = substitute(target, parameters) if replace else target
        normalized = target_name.replace("\\", "/")
        # BSMLViewControllerTemplate targets "Name.cs/Name.bsml"; keep them siblings.
        nested = f"{file_input_name}.cs/{file_input_name}.bsml"
        if nested in normalized:
            normalized = normalized.replace(nested, f"{file_input_name}.bsml")
        relative = safe_relative_path(normalized)
        output = destination / relative
        if output.exists() and not force:
            raise ProjectError(
                f"Target file already exists: {output}",
                hint="Pass force=True to overwrite, or choose another name.",
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        if replace:
            text = source.read_text(encoding="utf-8-sig", errors="replace")
            atomic_write_text(output, substitute(text, parameters), encoding="utf-8")
        else:
            output.write_bytes(source.read_bytes())
        item_type = item.get("ItemType") or _infer_item_type(target_name)
        dependent = item.get("DependentUpon")
        if dependent and replace:
            dependent = substitute(dependent, parameters)
        expanded.append(ExpandedItem(path=output, item_type=item_type, dependent_upon=dependent))
    if not expanded:
        raise ProjectError(f"Template produced no files: {template.path}")
    return expanded
