"""Beat Saber installation discovery and reference inventory.

The detection rules mirror the Visual Studio extension (Steam registry +
``libraryfolders.vdf``, Oculus libraries, BS Manager instances) and keep the
quirks fixed in the denpadokei fork (null-safe BS Manager handling and regex
based VDF parsing).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BsmtError, GameNotFoundError
from .util import expand_path, path_key, unique_paths

MANAGED_REL = Path("Beat Saber_Data") / "Managed"
LIBS_REL = Path("Libs")
PLUGINS_REL = Path("Plugins")
GAME_EXE = "Beat Saber.exe"

REFERENCE_DIRS: tuple[Path, ...] = (MANAGED_REL, LIBS_REL, PLUGINS_REL)

_ILLEGAL_VERSION_BYTES = {0, *range(1, 0x20)} | {ord(c) for c in '<>:"/\\|?*'}
_VERSION_MARKER = b"public.app-category.games"
_SEMVER_PREFIX_RE = re.compile(r"^\d+\.\d+\.\d+")

_file_version_cache: dict[tuple[str, int, int], str | None] = {}
_file_version_lock = threading.Lock()


@dataclass
class GameReference:
    """A DLL that a mod project can reference from a game installation."""

    name: str
    file: Path
    relative_dir: str
    size: int
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_dir": self.relative_dir,
            "path": str(self.file),
            "size": self.size,
            "version": self.version,
        }


@dataclass
class BeatSaberInstall:
    """A validated Beat Saber installation."""

    path: Path
    source: str
    version: str | None = None
    build: str | None = None
    version_source: str | None = None
    last_launched: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def exe(self) -> Path:
        return self.path / GAME_EXE

    @property
    def managed_dir(self) -> Path:
        return self.path / MANAGED_REL

    @property
    def libs_dir(self) -> Path:
        return self.path / LIBS_REL

    @property
    def plugins_dir(self) -> Path:
        return self.path / PLUGINS_REL

    @property
    def ipa_dir(self) -> Path:
        return self.path / "IPA"

    @property
    def ipa_installed(self) -> bool:
        return (self.path / "IPA.exe").is_file() or ((self.path / "winhttp.dll").is_file() and self.ipa_dir.is_dir())

    def plugin_count(self) -> int:
        try:
            return sum(1 for p in self.plugins_dir.glob("*.dll") if p.is_file())
        except OSError:
            return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source": self.source,
            "version": self.version,
            "build": self.build,
            "version_source": self.version_source,
            "last_launched": self.last_launched,
            "has_exe": self.exe.is_file(),
            "managed_dir": str(self.managed_dir) if self.managed_dir.is_dir() else None,
            "ipa_installed": self.ipa_installed,
            "plugin_count": self.plugin_count(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source": self.source,
            "version": self.version,
            "build": self.build,
            "last_launched": self.last_launched,
        }


# ---------------------------------------------------------------------------
# Validation and version detection
# ---------------------------------------------------------------------------


def is_beat_saber_dir(path: str | os.PathLike[str] | None) -> bool:
    if not path:
        return False
    try:
        root = Path(path)
        if not root.is_dir():
            return False
        has_exe = any(p.name.lower() == GAME_EXE.lower() and p.is_file() for p in root.iterdir())
        return has_exe and (root / MANAGED_REL).is_dir()
    except (OSError, ValueError):
        return False


def _valid_version_string(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value if _SEMVER_PREFIX_RE.match(value) else None


def version_from_version_file(game_dir: str | os.PathLike[str]) -> tuple[str | None, str | None]:
    """Read BS Manager's ``BeatSaberVersion.txt`` (``1.44.1_20239``)."""

    path = Path(game_dir) / "BeatSaberVersion.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    match = re.match(r"^v?(\d+\.\d+\.\d+(?:[-+][^\s]+)?)(?:_(\d+))?", text)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def version_from_globalgamemanagers(game_dir: str | os.PathLike[str]) -> str | None:
    """Extract the game version from Unity's ``globalgamemanagers``.

    The original BSMT/ModAssistant approach assumed the version started exactly
    136 bytes after ``public.app-category.games``. That offset no longer holds in
    newer Unity builds (BSMT returns nothing for 1.44.x), but the version string
    is still stored near the marker as ``major.minor.patch_build``. Search the
    surrounding block for that pattern, then fall back to the old offset for old
    game versions.
    """

    path = Path(game_dir) / "Beat Saber_Data" / "globalgamemanagers"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    index = data.find(_VERSION_MARKER)
    if index < 0:
        return None

    window = data[index : index + 8192]
    match = re.search(rb"(?<!\d)(\d{1,3}\.\d{1,3}\.\d{1,3})_(\d+)(?!\d)", window)
    if match:
        major_minor_patch = match.group(1).decode("ascii")
        if not major_minor_patch.startswith("0.0."):
            return f"{major_minor_patch}_{match.group(2).decode('ascii')}"

    # Legacy layout: version starts 136 bytes after the marker.
    start = index + 136
    end = start
    while end < len(data) and data[end] not in _ILLEGAL_VERSION_BYTES:
        end += 1
    if end > start:
        try:
            value = data[start:end].decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            value = data[start:end].decode("latin-1").strip()
        match = re.match(r"^\d+\.\d+\.\d+(?:_\d+)?$", value)
        if match and not value.startswith("0.0."):
            return value

    # Last resort: any ``x.y.z_build`` string in the file (Unity's own version
    # looks like 2022.3.33f1 and does not match because of the trailing letter).
    for match in re.finditer(rb"(?<!\d)(\d{1,3}\.\d{1,3}\.\d{1,3})_(\d+)(?!\d)", data):
        major_minor_patch = match.group(1).decode("ascii")
        if not major_minor_patch.startswith("0.0."):
            return f"{major_minor_patch}_{match.group(2).decode('ascii')}"
    return None


def version_from_folder_name(path: str | os.PathLike[str]) -> str | None:
    match = re.match(r"^(\d+\.\d+\.\d+)", Path(path).name)
    return match.group(1) if match else None


def detect_game_version(game_dir: str | os.PathLike[str]) -> tuple[str | None, str | None, str | None]:
    """Return ``(version, build, source)`` for a game directory.

    ``globalgamemanagers`` is the authoritative source because it ships with
    the game data; BS Manager's ``BeatSaberVersion.txt`` can go stale when the
    game is updated outside BS Manager, so it is only a fallback.
    """

    raw = version_from_globalgamemanagers(game_dir)
    if raw:
        match = re.match(r"^(\d+\.\d+\.\d+)(?:_(\d+))?$", raw)
        if match:
            return match.group(1), match.group(2), "globalgamemanagers"
        return raw, None, "globalgamemanagers"
    version, build = version_from_version_file(game_dir)
    if version:
        return version, build, "BeatSaberVersion.txt"
    version = version_from_folder_name(game_dir)
    if version:
        return version, None, "folder-name"
    return None, None, None


# ---------------------------------------------------------------------------
# Registry / Steam / Oculus / BS Manager
# ---------------------------------------------------------------------------


def _read_registry(hive: Any, subkey: str, name: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
    except OSError:
        return None


def _registry_paths() -> dict[str, list[str]]:
    """Collect candidate paths from the Windows registry."""

    result: dict[str, list[str]] = {"steam": [], "oculus": [], "bsmanager": []}
    try:
        import winreg
    except ImportError:
        return result

    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 620980"
    value = _read_registry(winreg.HKEY_LOCAL_MACHINE, uninstall, "InstallLocation")
    if value:
        result["steam"].append(value)

    steam_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam", "SteamPath"),
    ]
    for hive, subkey, name in steam_keys:
        value = _read_registry(hive, subkey, name)
        if value:
            result["steam"].append(value)

    oculus_lm = _read_registry(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\WOW6432Node\Oculus VR, LLC\Oculus\Config",
        "InitialAppLibrary",
    )
    if oculus_lm:
        result["oculus"].append(oculus_lm)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Oculus VR, LLC\Oculus\Libraries",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as libraries:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(libraries, index)
                except OSError:
                    break
                index += 1
                value = _read_registry(
                    winreg.HKEY_CURRENT_USER,
                    rf"SOFTWARE\Oculus VR, LLC\Oculus\Libraries\{name}",
                    "OriginalPath",
                )
                if value:
                    result["oculus"].append(value)
    except OSError:
        pass
    return result


def parse_steam_library_folders(text: str) -> list[str]:
    """Extract library paths from ``libraryfolders.vdf``.

    Handles both the old (``"1" { "path" ... }``) and the current
    (``"path" "..."`` anywhere in the file) layouts without a VDF dependency.
    """

    paths: list[str] = []
    for match in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text, flags=re.IGNORECASE):
        raw = match.group(1)
        raw = raw.replace(r"\\", "\\").replace(r"\"", '"')
        if raw:
            paths.append(raw)
    return paths


def _steam_library_roots() -> list[Path]:
    roots: list[Path] = []
    for value in _registry_paths()["steam"]:
        path = expand_path(value)
        if path.is_dir():
            roots.append(path)
    for fallback in (
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Steam",
    ):
        if fallback.is_dir():
            roots.append(fallback)
    libraries: list[Path] = []
    for root in unique_paths(roots):
        libraries.append(root)
        for vdf in (root / "config" / "libraryfolders.vdf", root / "steamapps" / "libraryfolders.vdf"):
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for value in parse_steam_library_folders(text):
                path = expand_path(value)
                if path.is_dir():
                    libraries.append(path)
    return unique_paths(libraries)


def _oculus_library_roots() -> list[Path]:
    roots: list[Path] = []
    for value in _registry_paths()["oculus"]:
        path = expand_path(value)
        if path.is_dir():
            roots.append(path)
    default = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Oculus"
    if default.is_dir():
        roots.append(default)
    return unique_paths(roots)


def _find_oculus_install(root: Path) -> Path | None:
    candidates = [root / "Software" / "hyperbolic-magnetism-beat-saber", root / "hyperbolic-magnetism-beat-saber"]
    software = root / "Software"
    if software.is_dir():
        try:
            for child in software.iterdir():
                if child.is_dir() and child.name.lower() == "hyperbolic-magnetism-beat-saber":
                    candidates.append(child)
        except OSError:
            pass
    for candidate in candidates:
        if is_beat_saber_dir(candidate):
            return candidate
    return None


def _bsmanager_instances() -> tuple[list[Path], Path | None]:
    """Return BS Manager instance directories and the last launched one."""

    appdata = Path(os.environ.get("APPDATA", ""))
    config_candidates = [
        appdata / "bs-manager" / "config.json",
        appdata / "BSManager" / "config.json",
        appdata / "bsmanager" / "config.json",
    ]
    instances: list[Path] = []
    last_launched: Path | None = None
    for config_path in config_candidates:
        try:
            raw = config_path.read_text(encoding="utf-8", errors="replace")
            config = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(config, dict):
            continue
        folder = config.get("installation-folder")
        if isinstance(folder, str) and folder.strip():
            instances_dir = expand_path(folder) / "BSManager" / "BSInstances"
            if instances_dir.is_dir():
                with contextlib.suppress(OSError):
                    instances.extend(p for p in instances_dir.iterdir() if p.is_dir())
        last = config.get("last-version-launched")
        if isinstance(last, dict) and isinstance(last.get("path"), str):
            candidate = expand_path(last["path"])
            if candidate.is_dir():
                last_launched = candidate
    return unique_paths(instances), last_launched


# ---------------------------------------------------------------------------
# Public detection API
# ---------------------------------------------------------------------------


def _split_explicit_paths(paths: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    for env_name in ("BSMT_GAME_DIR", "BEATSABER_DIR"):
        value = os.environ.get(env_name)
        if value:
            values.append(value)
    if paths:
        for item in paths:
            if not item:
                continue
            values.extend(part.strip() for part in str(item).split(";") if part.strip())
    return values


def detect_installs(
    explicit_paths: Iterable[str] | None = None,
    *,
    include_steam: bool = True,
    include_oculus: bool = True,
    include_bsmanager: bool = True,
) -> list[BeatSaberInstall]:
    """Find all Beat Saber installations known to this machine."""

    found: list[tuple[Path, str]] = []
    for raw in _split_explicit_paths(explicit_paths):
        path = expand_path(raw)
        if is_beat_saber_dir(path):
            found.append((path, "manual"))

    if include_steam:
        for root in _steam_library_roots():
            candidate = root / "steamapps" / "common" / "Beat Saber"
            if is_beat_saber_dir(candidate):
                found.append((candidate, "steam"))

    if include_oculus:
        for root in _oculus_library_roots():
            candidate = _find_oculus_install(root)
            if candidate:
                found.append((candidate, "oculus"))

    last_launched: Path | None = None
    if include_bsmanager:
        instances, last_launched = _bsmanager_instances()
        for candidate in instances:
            if is_beat_saber_dir(candidate):
                found.append((candidate, "bsmanager"))

    installs: list[BeatSaberInstall] = []
    seen: set[str] = set()
    for path, source in found:
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        version, build, version_source = detect_game_version(path)
        is_last = last_launched is not None and path_key(last_launched) == key
        installs.append(
            BeatSaberInstall(
                path=path.resolve(),
                source=source,
                version=version,
                build=build,
                version_source=version_source,
                last_launched=is_last,
            )
        )
    installs.sort(key=lambda item: (_version_sort_key(item.version), 0 if item.last_launched else 1), reverse=True)
    return installs


def _version_sort_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return (0,)
    parts = re.findall(r"\d+", version)[:3]
    return tuple(int(part) for part in parts) or (0,)


def _base_version(version: str | None) -> str | None:
    if not version:
        return None
    match = re.match(r"^(\d+\.\d+\.\d+)", version)
    return match.group(1) if match else None


def resolve_install(
    game_dir: str | os.PathLike[str] | None = None,
    *,
    manifest_game_version: str | None = None,
    installs: list[BeatSaberInstall] | None = None,
) -> tuple[BeatSaberInstall, list[BeatSaberInstall], str]:
    """Pick the best installation and explain why.

    Returns ``(chosen, candidates, reason)`` and raises :class:`GameNotFoundError`
    with the candidate list when nothing usable exists.
    """

    if game_dir:
        path = expand_path(game_dir)
        if is_beat_saber_dir(path):
            version, build, version_source = detect_game_version(path)
            return (
                BeatSaberInstall(
                    path=path.resolve(),
                    source="manual",
                    version=version,
                    build=build,
                    version_source=version_source,
                ),
                installs or [],
                "explicit",
            )
        raise GameNotFoundError(
            f"Not a Beat Saber installation: {path}",
            hint="The folder must contain 'Beat Saber.exe' and 'Beat Saber_Data/Managed'.",
        )

    candidates = installs if installs is not None else detect_installs()
    if not candidates:
        raise GameNotFoundError(
            "Could not find a Beat Saber installation.",
            hint=(
                "Pass game_dir explicitly, set the BEATSABER_DIR environment variable, "
                "or launch the game once through Steam/BS Manager so its install is discoverable."
            ),
            details={"searched": ["Steam", "Oculus", "BS Manager", "BEATSABER_DIR"]},
        )

    wanted = _base_version(manifest_game_version)
    if wanted:
        matching = [item for item in candidates if _base_version(item.version) == wanted]
        if matching:
            return matching[0], candidates, f"manifest-game-version ({wanted})"

    if len(candidates) == 1:
        return candidates[0], candidates, "only-install"

    last_launched = [item for item in candidates if item.last_launched]
    if last_launched:
        return last_launched[0], candidates, "bsmanager-last-launched"

    return candidates[0], candidates, "highest-version"


def is_game_running() -> bool:
    """Return True when ``Beat Saber.exe`` is running."""

    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a hard dependency
        return False
    wanted = {"beat saber.exe", "beat saber"}
    for process in psutil.process_iter(["name"]):
        try:
            name = (process.info.get("name") or "").lower()
        except (psutil.Error, OSError):
            continue
        if name in wanted:
            return True
    return False


def file_version(path: Path) -> str | None:
    """Return the Win32 file version of a DLL, with a small cache."""

    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _file_version_lock:
        if key in _file_version_cache:
            return _file_version_cache[key]
    value: str | None = None
    if os.name == "nt":
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(path), "\\")
            ms = int(info["FileVersionMS"])
            ls = int(info["FileVersionLS"])
            value = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:
            value = None
    with _file_version_lock:
        _file_version_cache[key] = value
    return value


def available_references(
    install: BeatSaberInstall,
    *,
    include_versions: bool = True,
) -> list[GameReference]:
    """Enumerate the DLLs the VS reference manager would show."""

    references: list[GameReference] = []
    for relative in REFERENCE_DIRS:
        directory = install.path / relative
        if not directory.is_dir():
            continue
        try:
            files = sorted((p for p in directory.glob("*.dll") if p.is_file()), key=lambda p: p.name.lower())
        except OSError:
            continue
        for file in files:
            references.append(
                GameReference(
                    name=file.stem,
                    file=file,
                    relative_dir=str(relative),
                    size=file.stat().st_size,
                    version=file_version(file) if include_versions else None,
                )
            )
    return references


def reference_map(install: BeatSaberInstall, *, include_versions: bool = True) -> dict[str, list[GameReference]]:
    result: dict[str, list[GameReference]] = {}
    for reference in available_references(install, include_versions=include_versions):
        result.setdefault(reference.name.lower(), []).append(reference)
    return result


def find_references(
    install: BeatSaberInstall,
    names: Iterable[str],
    *,
    relative_dir: str | None = None,
    include_versions: bool = True,
) -> tuple[list[GameReference], list[str]]:
    """Resolve user supplied DLL names to references.

    Returns ``(found, missing)``. Names are case-insensitive, may include the
    ``.dll`` extension, and may be qualified as ``Managed:Main`` or
    ``Plugins:BSML`` to disambiguate duplicates.
    """

    wanted: list[tuple[str, str | None]] = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        prefix = None
        if ":" in name:
            prefix, _, name = name.partition(":")
            prefix = prefix.strip().lower()
        if name.lower().endswith(".dll"):
            name = name[:-4]
        wanted.append((name.lower(), prefix))

    by_name = reference_map(install, include_versions=False)
    found: list[GameReference] = []
    missing: list[str] = []
    for name, prefix in wanted:
        matches = by_name.get(name, [])
        if prefix:
            matches = [item for item in matches if item.relative_dir.lower().startswith(prefix)]
        if relative_dir:
            matches = [item for item in matches if item.relative_dir == relative_dir]
        if not matches:
            missing.append(name)
            continue
        chosen = matches[0]
        if len(matches) > 1:
            preferred_order = {str(MANAGED_REL): 0, str(LIBS_REL): 1, str(PLUGINS_REL): 2}
            chosen = sorted(matches, key=lambda item: preferred_order.get(item.relative_dir, 9))[0]
        if include_versions:
            chosen = GameReference(
                name=chosen.name,
                file=chosen.file,
                relative_dir=chosen.relative_dir,
                size=chosen.size,
                version=file_version(chosen.file),
            )
        found.append(chosen)
    return found, missing


def describe_environment() -> dict[str, Any]:
    """Compact overview used by the MCP instructions and the CLI doctor."""

    installs = detect_installs()
    return {
        "game_running": is_game_running(),
        "install_count": len(installs),
        "installs": [item.to_dict() for item in installs],
    }


def ensure_install(game_dir: str | os.PathLike[str]) -> BeatSaberInstall:
    path = expand_path(game_dir)
    if not is_beat_saber_dir(path):
        raise GameNotFoundError(
            f"Not a Beat Saber installation: {path}",
            hint="Expected 'Beat Saber.exe' and 'Beat Saber_Data/Managed' in that folder.",
        )
    version, build, version_source = detect_game_version(path)
    return BeatSaberInstall(
        path=path.resolve(),
        source="manual",
        version=version,
        build=build,
        version_source=version_source,
    )


def require_game(
    game_dir: str | os.PathLike[str] | None = None,
    *,
    manifest_game_version: str | None = None,
) -> tuple[BeatSaberInstall, list[BeatSaberInstall]]:
    install, candidates, _reason = resolve_install(game_dir, manifest_game_version=manifest_game_version)
    return install, candidates


def describe_reference_dirs(install: BeatSaberInstall) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for relative in REFERENCE_DIRS:
        directory = install.path / relative
        count = 0
        if directory.is_dir():
            try:
                count = sum(1 for p in directory.glob("*.dll") if p.is_file())
            except OSError:
                count = 0
        result.append({"relative_dir": str(relative), "path": str(directory), "dll_count": count})
    return result


def validate_game_dir(game_dir: str | os.PathLike[str]) -> None:
    if not is_beat_saber_dir(game_dir):
        raise BsmtError(
            f"Not a Beat Saber installation: {game_dir}",
            hint="Expected 'Beat Saber.exe' and 'Beat Saber_Data/Managed'.",
        )
