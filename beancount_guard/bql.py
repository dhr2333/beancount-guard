"""Read-only BQL validation and execution for the local MCP bridge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import beanquery

_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|DETACH|PRAGMA)\b", re.IGNORECASE)
_UNITS_COMPARE = re.compile(r"units\s*\(\s*position\s*\)\s*[<>]", re.IGNORECASE)
_POSITION_COMPARE = re.compile(r"\bposition\s*[<>]", re.IGNORECASE)
_WHERE_AGGREGATE = re.compile(r"\b(sum|count|avg|min|max)\s*\(", re.IGNORECASE)
_CLAUSE_END = re.compile(r"\b(GROUP BY|ORDER BY|LIMIT)\b", re.IGNORECASE)
_HAVING = re.compile(r"\bHAVING\b", re.IGNORECASE)
_TAGS_REGEX = re.compile(r"\btags\s*~", re.IGNORECASE)


class BqlError(ValueError):
    """Invalid or unsafe BQL."""


def validate_bql(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise BqlError("query must not be empty")
    normalized = query.strip().rstrip(";").strip()
    if ";" in normalized:
        raise BqlError("only one statement is allowed")
    if not re.match(r"^SELECT\b", normalized, re.IGNORECASE):
        raise BqlError("only SELECT queries are allowed")
    if _FORBIDDEN.search(normalized):
        raise BqlError("query contains a forbidden keyword")
    if _HAVING.search(normalized):
        raise BqlError("HAVING is not supported; use WHERE + GROUP BY")
    if _TAGS_REGEX.search(normalized):
        raise BqlError("tags ~ is not supported; use 'path' IN tags")
    if _UNITS_COMPARE.search(normalized) or _POSITION_COMPARE.search(normalized):
        raise BqlError("compare number, not position; use number > N or ORDER BY units(position)")
    where = re.search(r"\bWHERE\b", normalized, re.IGNORECASE)
    if where:
        rest = normalized[where.end():]
        end = _CLAUSE_END.search(rest)
        clause = rest[: end.start()] if end else rest
        if _WHERE_AGGREGATE.search(clause):
            raise BqlError("aggregate functions are not allowed in WHERE; use GROUP BY")
    return normalized


@dataclass
class BqlResult:
    bql: str
    result_text: str
    row_count: int
    truncated: bool


def run_bql(main_file: str, query: str, *, max_rows: int = 100) -> BqlResult:
    bql = validate_bql(query)
    connection = beanquery.connect(None)
    connection.attach(f"beancount:{main_file}")
    cursor = connection.execute(bql)
    rows = cursor.fetchall()
    row_count = len(rows)
    truncated = row_count > max_rows
    if truncated:
        rows = rows[:max_rows]
    columns = [str(column.name) for column in cursor.description]
    lines = [" | ".join(columns), " | ".join("---" for _ in columns)]
    for row in rows:
        lines.append(" | ".join(str(value) for value in row))
    text = "\n".join(lines) if rows else "\n".join(lines) + "\n(no rows)"
    if truncated:
        text += f"\n... truncated: showing {max_rows} of {row_count} rows"
    return BqlResult(bql=bql, result_text=text, row_count=row_count, truncated=truncated)
