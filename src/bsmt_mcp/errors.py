"""Exception types shared by the CLI and the MCP server."""

from __future__ import annotations

from typing import Any


class BsmtError(Exception):
    """An anticipated failure with an actionable hint.

    The MCP server turns this into a ``ToolError`` so the calling model sees the
    message instead of a generic crash, and the CLI prints it without a traceback.
    """

    def __init__(self, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\nHint: {self.hint}"
        return self.message


class GameNotFoundError(BsmtError):
    """Raised when no usable Beat Saber installation can be resolved."""


class ProjectError(BsmtError):
    """Raised when a project file is missing or cannot be parsed."""


class ManifestError(BsmtError):
    """Raised when a BSIPA manifest is invalid or cannot be written."""


class BuildError(BsmtError):
    """Raised when MSBuild cannot be located or a build fails."""


class DeployError(BsmtError):
    """Raised when copying a build into a game installation fails."""
