"""Small, dependency-light helpers used across the package."""

from __future__ import annotations

import contextlib
import json
import locale
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ProjectError

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------------------------------------------------------------------------
# Process execution
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    """Result of a captured child process."""

    args: list[str]
    returncode: int
    output: str
    duration_ms: int
    timed_out: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.args,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
        }


def decode_console(data: bytes | None) -> str:
    """Decode console output from Windows tools.

    MSBuild on a Japanese Windows installation writes CP932, while the .NET CLI
    usually writes UTF-8. Try the most likely encodings instead of trusting the
    process locale, which is frequently wrong when output is redirected.
    """

    if not data:
        return ""
    encodings = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in {"utf-8", "utf8"}:
        encodings.append(preferred)
    encodings.extend(["cp932", "latin-1"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _kill_process_tree(pid: int) -> None:
    try:
        import psutil

        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            with contextlib.suppress(Exception):
                child.kill()
        with contextlib.suppress(Exception):
            parent.kill()
    except Exception:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=20)


def run_process(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = 600,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> ProcessResult:
    """Run a command, capture merged stdout/stderr, and never raise on failure."""

    command = [str(part) for part in args]
    merged_env = os.environ.copy()
    merged_env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    merged_env.setdefault("DOTNET_NOLOGO", "1")
    merged_env.setdefault("MSBUILDDISABLENODEREUSE", "1")
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=merged_env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    timed_out = False
    try:
        raw, _ = proc.communicate(stdin.encode("utf-8") if stdin is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc.pid)
        try:
            raw, _ = proc.communicate(timeout=20)
        except Exception:
            raw = b""
    duration_ms = int((time.monotonic() - started) * 1000)
    output = strip_ansi(decode_console(raw))
    return ProcessResult(command, proc.returncode, output, duration_ms, timed_out)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def tail_lines(text: str, limit: int = 80) -> str:
    """Return at most ``limit`` trailing lines."""

    if limit <= 0:
        return ""
    lines = text.rstrip("\r\n").splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[-limit:])


# ---------------------------------------------------------------------------
# Files and paths
# ---------------------------------------------------------------------------


def expand_path(value: str | os.PathLike[str]) -> Path:
    text = os.path.expandvars(str(value))
    text = os.path.expanduser(text)
    return Path(text)


def find_project_file(path: str | os.PathLike[str] | None) -> Path:
    """Resolve a project directory or .csproj path to an existing .csproj file."""

    if path is None or str(path).strip() == "":
        path = "."
    candidate = expand_path(path)
    if candidate.is_file():
        if candidate.suffix.lower() != ".csproj":
            raise ProjectError(
                f"Not a C# project file: {candidate}",
                hint="Pass a .csproj file or the directory that contains it.",
            )
        return candidate.resolve()
    if candidate.is_dir():
        projects = sorted(p for p in candidate.glob("*.csproj") if p.is_file())
        if not projects:
            raise ProjectError(
                f"No .csproj file found in {candidate}",
                hint="Pass the project file explicitly (for example, -project MyMod/MyMod.csproj).",
            )
        if len(projects) > 1:
            names = ", ".join(p.name for p in projects)
            raise ProjectError(
                f"Multiple .csproj files found in {candidate}: {names}",
                hint="Pass the exact .csproj file to use.",
            )
        return projects[0].resolve()
    raise ProjectError(
        f"Project path does not exist: {candidate}",
        hint="Check the path, or pass the directory that contains the .csproj file.",
    )


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        if temp.exists():
            with contextlib.suppress(OSError):
                temp.unlink()
    return target


def atomic_write_text(path: str | os.PathLike[str], text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def read_text_auto(path: str | os.PathLike[str]) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def is_within(path: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def safe_relative_path(value: str) -> Path:
    """Validate a destination path from untrusted input (zip entries, copies)."""

    text = value.replace("\\", "/").strip()
    if not text:
        raise ValueError("Destination path cannot be empty.")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"Destination path must be relative: {value!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"Destination path must not contain '..': {value!r}")
    return Path(*parts) if parts else Path(".")


def copy_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> Path:
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def remove_tree(path: str | os.PathLike[str]) -> None:
    """Remove a directory tree even on OneDrive/read-only/ReparsePoint folders.

    ``shutil.rmtree`` fails on Windows when a directory has the read-only
    attribute or is a cloud-sync reparse point. Clear the attribute, retry, and
    finally fall back to ``cmd /c rd`` which handles junctions.
    """

    target = Path(path)
    if not target.exists():
        return

    def onerror(func, failed_path, _exc_info) -> None:  # type: ignore[no-untyped-def]
        failed = Path(failed_path)
        with contextlib.suppress(OSError):
            os.chmod(failed, stat.S_IWRITE | stat.S_IREAD)
        try:
            func(failed_path)
            return
        except OSError:
            pass
        if os.name == "nt":
            if failed.is_dir():
                subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(failed)], capture_output=True, timeout=120)
            else:
                subprocess.run(["cmd", "/c", "del", "/f", "/q", str(failed)], capture_output=True, timeout=120)
        else:
            raise

    shutil.rmtree(target, onerror=onerror)


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def path_key(path: str | os.PathLike[str]) -> str:
    """Normalized key for comparing paths on the current OS."""

    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def unique_paths(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for item in paths:
        path = Path(item)
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def json_dumps(data: Any, *, pretty: bool = True) -> str:
    return json.dumps(data, indent=2 if pretty else None, ensure_ascii=False, default=str)


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def ok(**values: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True}
    result.update(values)
    return result


def truncate_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40] + "\n... (output truncated) ..."
