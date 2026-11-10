"""Small file editing helpers (BSMT ReplaceInFile equivalent)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .errors import BsmtError
from .util import atomic_write_bytes, atomic_write_text


def replace_in_file(
    file: str,
    pattern: str,
    substitute: str,
    *,
    use_regex: bool = False,
    multiline: bool = False,
    singleline: bool = False,
    escape_backslash: bool = False,
    dry_run: bool = False,
    count: int = 0,
) -> dict[str, Any]:
    """Replace text in a file, mirroring BeatSaberModdingTools.Tasks.ReplaceInFile.

    ``count=0`` replaces every occurrence.
    """

    path = Path(file)
    if not path.is_file():
        raise BsmtError(f"File not found: {path}")
    raw = path.read_bytes()
    encoding = "utf-8"
    text = ""
    candidates = ("utf-8-sig", "utf-8", "cp932", "latin-1") if raw.startswith(b"\xef\xbb\xbf") else ("utf-8", "cp932", "latin-1")
    for candidate in candidates:
        try:
            text = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if "\x00" in text:
        raise BsmtError(f"Refusing to edit a binary file: {path}")
    if use_regex:
        flags = 0
        if multiline:
            flags |= re.MULTILINE
        if singleline:
            flags |= re.DOTALL
        replacement = substitute.replace("\\", "\\\\") if escape_backslash else substitute
        try:
            new_text, occurrences = re.subn(pattern, replacement, text, count=count, flags=flags)
        except re.error as exc:
            raise BsmtError(f"Invalid regular expression: {exc}") from exc
    else:
        occurrences = text.count(pattern)
        if count:
            new_text = text.replace(pattern, substitute, count)
            occurrences = min(occurrences, count)
        else:
            new_text = text.replace(pattern, substitute)
    changed = new_text != text
    if changed and not dry_run:
        if encoding == "utf-8-sig":
            atomic_write_bytes(path, b"\xef\xbb\xbf" + new_text.encode("utf-8"))
        elif encoding == "utf-8":
            atomic_write_text(path, new_text, encoding="utf-8")
        else:
            atomic_write_text(path, new_text, encoding=encoding)
    return {
        "ok": True,
        "file": str(path),
        "occurrences": occurrences,
        "changed": changed,
        "dry_run": dry_run,
        "old_size": len(text.encode("utf-8")),
        "new_size": len(new_text.encode("utf-8")),
    }


def set_action_output(name: str, value: str, output_path: str | None = None) -> dict[str, Any]:
    """Write ``NAME=VALUE`` to a GitHub Actions output file (BSMT SetActionOutput)."""

    if not name or not name.strip():
        raise BsmtError("Output name cannot be empty.")
    target = output_path or os.environ.get("GITHUB_OUTPUT")
    if not target:
        raise BsmtError(
            "GITHUB_OUTPUT is not set.",
            hint="Pass output_path explicitly or run this inside a GitHub Actions step.",
        )
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{name}={value}\n")
    return {"ok": True, "path": str(path), "name": name, "value": value}
