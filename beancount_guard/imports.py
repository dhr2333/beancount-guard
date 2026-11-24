"""Declared import sources: the inventory a session promises to account for.

Batch coverage answers "did every row of *this file* get an owner". It cannot
answer "did every file I was given get a batch" — a session that silently drops
one attachment still looks green, because nothing knows the attachment existed.

This registry is that missing layer. The agent declares every source up front
(file name, hash, row count), each batch claims one source by hash, and
:meth:`SourceRegistry.status` reports the ones still unclaimed. Nothing here is
ledger truth: it is a checklist that survives context loss.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import fail
from .validation import atomic_write

MAX_SOURCES = 200
MAX_ROWS = 20000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object, label: str, *, maximum: int = 200, allow_empty: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        fail("invalid_source", f"{label} must be a string")
    text = value.strip()
    if text == "" and not allow_empty:
        fail("invalid_source", f"{label} must not be empty")
    if len(text) > maximum:
        fail("invalid_source", f"{label} must be at most {maximum} characters")
    return text


def _key(entry: dict[str, Any]) -> str:
    """A source is identified by its hash, falling back to its file name."""
    return entry.get("fileHash") or f"name:{entry.get('fileName')}"


def normalize_source(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        fail("invalid_source", "a source must be a JSON object")
    file_name = _text(payload.get("fileName"), "source.fileName", maximum=200)
    file_hash = _text(payload.get("fileHash"), "source.fileHash", maximum=120)
    if not file_name and not file_hash:
        fail("invalid_source", "a source needs a fileName or a fileHash")
    rows = payload.get("rows")
    if rows is not None:
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0 or rows > MAX_ROWS:
            fail("invalid_source", f"source.rows must be an integer between 0 and {MAX_ROWS}")
    return {
        "fileName": file_name or None,
        "fileHash": file_hash or None,
        "rows": rows,
        "ref": _text(payload.get("ref"), "source.ref", maximum=200) or None,
        "channel": _text(payload.get("channel"), "source.channel", maximum=40) or None,
    }


class SourceRegistry:
    """One JSON checklist under ``.ledger/imports``."""

    def __init__(self, *, root: str | Path, state_dir: str = ".ledger") -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / state_dir / "imports"
        self.path = self.directory / "sources.json"

    def _read(self) -> list[dict[str, Any]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        sources = document.get("sources") if isinstance(document, dict) else None
        return [item for item in sources if isinstance(item, dict)] if isinstance(sources, list) else []

    def _write(self, sources: list[dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            json.dumps({"version": 1, "updatedAt": _now(), "sources": sources}, ensure_ascii=False, indent=2) + "\n",
        )

    def declare(self, payload: object) -> dict[str, Any]:
        """Upsert the sources of one import session and report what is missing.

        Re-declaring the same file is idempotent (hash or name wins); a source
        that already has a batch keeps its link.
        """
        raw = payload.get("sources") if isinstance(payload, dict) else payload
        if not isinstance(raw, list) or not raw:
            fail("invalid_source", "sources must be a non-empty array")
        if len(raw) > MAX_SOURCES:
            fail("invalid_source", f"sources must have at most {MAX_SOURCES} entries")
        sources = self._read()
        index = {_key(entry): entry for entry in sources}
        declared: list[str] = []
        for item in raw:
            entry = normalize_source(item)
            key = _key(entry)
            existing = index.get(key)
            if existing is None:
                entry.update({"declared": True, "declaredAt": _now(), "batchId": None})
                sources.append(entry)
                index[key] = entry
            else:
                for field, value in entry.items():
                    if value is not None:
                        existing[field] = value
                existing.setdefault("declared", True)
                existing.setdefault("batchId", None)
            declared.append(key)
        self._write(sources)
        return {**self.status(), "declared": declared}

    def mark_batched(self, file_hash: object, batch_id: str) -> None:
        """Link a submitted batch to the source it consumed (by hash, else skip)."""
        if not isinstance(file_hash, str) or not file_hash.strip():
            return
        sources = self._read()
        key = file_hash.strip()
        for entry in sources:
            if entry.get("fileHash") == key:
                entry["batchId"] = batch_id
                entry["batchedAt"] = _now()
                self._write(sources)
                return
        # A batch that arrived before its declaration still shows up in the list,
        # flagged as undeclared, so the inventory stays truthful.
        sources.append(
            {
                "fileName": None,
                "fileHash": key,
                "rows": None,
                "ref": None,
                "channel": None,
                "declared": False,
                "declaredAt": None,
                "batchId": batch_id,
                "batchedAt": _now(),
            }
        )
        self._write(sources)

    def status(self) -> dict[str, Any]:
        sources = self._read()
        uncovered = [entry for entry in sources if not entry.get("batchId")]
        undeclared = [entry for entry in sources if entry.get("declared") is False]
        return {
            "sources": [
                {
                    "fileName": entry.get("fileName"),
                    "fileHash": entry.get("fileHash"),
                    "rows": entry.get("rows"),
                    "ref": entry.get("ref"),
                    "channel": entry.get("channel"),
                    "declared": entry.get("declared", True),
                    "batchId": entry.get("batchId"),
                }
                for entry in sources
            ],
            "declaredCount": sum(1 for entry in sources if entry.get("declared") is not False),
            "uncovered": [
                {"fileName": entry.get("fileName"), "fileHash": entry.get("fileHash"), "rows": entry.get("rows")}
                for entry in uncovered
            ],
            "uncoveredCount": len(uncovered),
            "undeclared": [
                {"fileName": entry.get("fileName"), "fileHash": entry.get("fileHash"), "batchId": entry.get("batchId")}
                for entry in undeclared
            ],
        }
