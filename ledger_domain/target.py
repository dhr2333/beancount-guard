"""Resolve the monthly file a dated ledger write belongs to.

The domain never invents a new ledger layout: it appends to ``YYYY/MM.bean``
when the year directory is managed, and edits ``YYYY/00.bean`` only to add the
month include when it is missing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .bean import split_document
from .errors import fail


def resolve_monthly_target(root: str | Path, date: str) -> dict[str, Any]:
    """Resolve and read the monthly target file for an ISO date."""
    root_path = Path(root)
    year, month = date[:4], date[5:7]
    year_dir = root_path / year
    if not year_dir.is_dir():
        fail("year_not_managed", f"the ledger has no {year}/ directory; create it before writing {date}", {"year": year, "date": date})

    index_rel = f"{year}/00.bean"
    index_file = root_path / index_rel
    try:
        index_content = index_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail("year_not_managed", f"the ledger has no {index_rel}; create it before writing {date}", {"year": year, "date": date})

    rel_file = f"{year}/{month}.bean"
    file = root_path / rel_file
    try:
        content = file.read_text(encoding="utf-8")
        exists = True
    except FileNotFoundError:
        content = ""
        exists = False

    include_pattern = re.compile(rf'^[ \t]*include[ \t]+"{month}\.bean"[ \t]*(?:;.*)?\r?$', re.MULTILINE)
    index_changed = include_pattern.search(index_content) is None
    return {
        "year": year,
        "month": month,
        "rel_file": rel_file,
        "file": str(file),
        "content": content,
        "exists": exists,
        "index_rel": index_rel,
        "index_file": str(index_file),
        "index_content": insert_month_include(index_content, month) if index_changed else index_content,
        "index_changed": index_changed,
    }


def insert_month_include(content: str, month: str) -> str:
    """Insert one include line in month order."""
    if content == "":
        return f'include "{month}.bean"\n'
    document = split_document(content)
    lines = document.lines
    target = int(month)
    if document.final_newline and lines and lines[-1] == "":
        lines.pop()
    insert_at = len(lines)
    last_month = -1
    last_include = -1
    for index, line in enumerate(lines):
        month_match = re.match(r'^[ \t]*include[ \t]+"(\d{2})\.bean"[ \t]*(?:;.*)?$', line)
        if month_match is not None:
            value = int(month_match.group(1))
            last_include = index
            if value < target:
                last_month = index
            if value > target:
                insert_at = index
                break
            continue
        if re.match(r'^[ \t]*include[ \t]+"', line):
            last_include = index
    if insert_at == len(lines):
        if last_month >= 0:
            insert_at = last_month + 1
        elif last_include >= 0:
            insert_at = last_include + 1
    lines.insert(insert_at, f'include "{month}.bean"')
    return document.eol.join(lines) + document.eol


APPEND_TARGETS = {
    "entries": "trans/collect.bean",
    "reconciliation": "trans/reconciliation.bean",
}


def resolve_append_target(root: str | Path, kind: str) -> dict[str, Any]:
    """Resolve the fixed append target introduced by ledger-domain-assets.

    Entries go to ``trans/collect.bean``; reconciliation directives go to
    ``trans/reconciliation.bean``. Both files are part of the include closure
    and must exist before the first write.
    """
    rel_file = APPEND_TARGETS.get(kind)
    if rel_file is None:
        fail("invalid_argument", f"unknown append target {kind!r}")
    path = Path(root) / rel_file
    if not path.is_file():
        fail(
            "ledger_not_initialized",
            f"the ledger has no {rel_file}; initialize it from ledger-domain-assets before writing",
            {"file": rel_file},
        )
    content = path.read_text(encoding="utf-8")
    # Keep the historical target shape used by callers. There is no year index
    # in the fixed-target layout; index_changed is always false.
    return {
        "rel_file": rel_file,
        "file": str(path),
        "content": content,
        "index_changed": False,
        "index_rel": rel_file,
        "index_file": str(path),
        "index_content": content,
    }
