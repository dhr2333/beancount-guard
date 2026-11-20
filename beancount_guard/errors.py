"""Stable domain failure with a machine-readable code.

Adapters (the DSH panel bridge, a future MCP server, scripts) translate the
code, message and details; they never parse human text to recover meaning.
"""

from __future__ import annotations

from typing import Any, NoReturn


class LedgerError(Exception):
    """One domain failure with a stable code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def fail(code: str, message: str, details: dict[str, Any] | None = None) -> NoReturn:
    """Raise one domain failure."""
    raise LedgerError(code, message, details)
