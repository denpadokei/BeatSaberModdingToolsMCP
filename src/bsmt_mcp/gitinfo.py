"""Git information used for artifact names and CI builds (BSMT GetCommitInfo)."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from .util import run_process


def find_git() -> str | None:
    return shutil.which("git") or shutil.which("git.exe")


def _git(project_dir: Path, *args: str, timeout: float = 20) -> tuple[bool, str]:
    git = find_git()
    if not git:
        return False, "git executable not found"
    result = run_process([git, "-C", str(project_dir), *args], timeout=timeout)
    if result.timed_out:
        return False, "git command timed out"
    if result.returncode != 0:
        return False, result.output.strip()
    return True, result.output.strip()


def parse_github_user(url: str | None) -> str | None:
    """Extract ``owner`` from a GitHub remote URL."""

    if not url:
        return None
    normalized = url.strip()
    patterns = [
        r"(?:https?://|ssh://git@|git@)github\.com[/:]([^/]+)/",
        r"github\.com[/:]([^/]+)/",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def git_info(project_dir: str | os.PathLike[str], *, hash_length: int = 7) -> dict[str, Any]:
    """Return commit/branch/status information without raising on failure."""

    directory = Path(project_dir)
    errors: list[str] = []
    data: dict[str, Any] = {
        "directory": str(directory),
        "available": False,
        "commit_hash": "local",
        "commit_hash_full": None,
        "branch": None,
        "modified": None,
        "origin_url": None,
        "git_user": None,
        "is_pull_request": False,
        "dirty_files": 0,
        "untracked_files": 0,
        "errors": errors,
    }
    if not find_git():
        errors.append("git executable not found")
        return data

    ok, root = _git(directory, "rev-parse", "--show-toplevel")
    if not ok:
        errors.append("not a git repository")
        return data
    data["available"] = True
    data["root"] = root

    ok, full_hash = _git(directory, "rev-parse", "HEAD")
    if ok:
        data["commit_hash_full"] = full_hash
        data["commit_hash"] = full_hash[: max(1, hash_length)]
    else:
        errors.append(f"rev-parse HEAD failed: {full_hash}")

    ok, branch = _git(directory, "rev-parse", "--abbrev-ref", "HEAD")
    if ok:
        data["branch"] = branch
        if branch == "HEAD":
            ok_short, short = _git(directory, "rev-parse", f"--short={max(1, hash_length)}", "HEAD")
            data["branch"] = f"detached@{short}" if ok_short else "detached"
    else:
        errors.append(f"rev-parse --abbrev-ref failed: {branch}")

    ok, status = _git(directory, "status", "--porcelain")
    if ok:
        lines = [line for line in status.splitlines() if line.strip()]
        untracked = [line for line in lines if line.startswith("??")]
        changed = [line for line in lines if not line.startswith("??")]
        data["dirty_files"] = len(changed)
        data["untracked_files"] = len(untracked)
        # Match BSMT: only untracked files still counts as Unmodified.
        data["modified"] = "Modified" if changed else "Unmodified"
    else:
        errors.append(f"git status failed: {status}")

    ok, origin = _git(directory, "config", "--get", "remote.origin.url")
    if ok and origin:
        data["origin_url"] = origin
        data["git_user"] = parse_github_user(origin)

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    data["is_pull_request"] = event == "pull_request" or bool(
        data["branch"] and str(data["branch"]).startswith("pull/")
    )
    return data
