"""Read projection: query through Beancount's own query engine, then translate
every canonical account into its display alias before the result leaves the
domain.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from .dates import is_date
from .errors import fail
from .ledger import project_name, resolve_name
from .validation import run_process

POSITION_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s+([A-Z][A-Z0-9'._-]*)(.*)$")


def parse_position(text: str) -> dict[str, str] | None:
    match = POSITION_RE.match(str(text))
    if match is None:
        return None
    rest = match.group(3).strip()
    position = {"amount": match.group(1), "currency": match.group(2)}
    if rest:
        position["cost"] = rest
    return position


def parse_csv(text: str) -> list[list[str]]:
    rows = list(csv.reader(io.StringIO(text)))
    return [row for row in rows if row]


def _run_query(
    *,
    root: str,
    bean_file: str,
    executable: str,
    query: str,
    timeout: float,
) -> list[list[str]]:
    outcome = run_process(executable, [bean_file, query, "--format=csv"], root, timeout=timeout)
    if outcome["spawn_error"] is not None:
        fail(
            "bean_query_unavailable",
            f"cannot run {executable}; install Beancount or set beanQuery",
            {"executable": executable},
        )
    if outcome["timed_out"]:
        fail("query_timeout", "the ledger query timed out")
    if outcome["code"] != 0:
        detail = "\n".join(outcome["stderr"].strip().splitlines()[:8]) or "bean-query failed"
        fail("query_failed", detail)
    return parse_csv(outcome["stdout"])


def query_transactions(
    *,
    state: dict[str, Any],
    root: str,
    bean_file: str,
    executable: str,
    date_from: str,
    date_to: str,
    account: str | None = None,
    limit: int = 100,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not is_date(date_from) or not is_date(date_to):
        fail("invalid_date", "from and to must be YYYY-MM-DD dates")
    if date_from > date_to:
        fail("invalid_date", "from must not be later than to")
    limit = max(1, min(500, limit))

    canonical_account = None
    if isinstance(account, str) and account.strip():
        resolution = resolve_name(state, account)
        if resolution is None:
            fail("account_not_found", f'"{account}" is not a known account or alias')
        canonical_account = resolution["account"]

    where = [f"date >= {date_from}", f"date <= {date_to}"]
    if canonical_account is not None:
        where.append(f"account = '{canonical_account}'")
    rows = _run_query(
        root=root,
        bean_file=bean_file,
        executable=executable,
        query=(
            "SELECT id, date, payee, narration, account, position WHERE "
            + " AND ".join(where)
            + f" ORDER BY date DESC, id LIMIT {min(5000, limit * 8)}"
        ),
        timeout=timeout,
    )
    if not rows:
        return {"from": date_from, "to": date_to, "transactions": [], "truncated": False}
    header = [name.strip() for name in rows[0]]
    columns = {name: index for index, name in enumerate(header)}
    required = ("id", "date", "account", "position")
    if any(name not in columns for name in required):
        fail("query_failed", "bean-query returned an unexpected column set")

    transactions: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        transaction_id = row[columns["id"]] if columns["id"] < len(row) else ""
        if transaction_id == "":
            continue
        position = parse_position(row[columns["position"]] if columns["position"] < len(row) else "")
        if position is None:
            continue
        transaction = transactions.setdefault(
            transaction_id,
            {
                "id": transaction_id,
                "date": row[columns["date"]] if columns["date"] < len(row) else "",
                "payee": row[columns["payee"]] if "payee" in columns and columns["payee"] < len(row) else "",
                "narration": row[columns["narration"]] if "narration" in columns and columns["narration"] < len(row) else "",
                "postings": [],
            },
        )
        transaction["postings"].append(
            {"account": project_name(state, row[columns["account"]]), **position}
        )
    ordered = list(transactions.values())
    return {
        "from": date_from,
        "to": date_to,
        "transactions": ordered[:limit],
        "truncated": len(ordered) > limit,
    }


def query_balances(
    *,
    state: dict[str, Any],
    root: str,
    bean_file: str,
    executable: str,
    as_of: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not is_date(as_of):
        fail("invalid_date", "asOf must be a YYYY-MM-DD date")
    rows = _run_query(
        root=root,
        bean_file=bean_file,
        executable=executable,
        query=f"SELECT account, sum(position) WHERE date <= {as_of} GROUP BY account ORDER BY account",
        timeout=timeout,
    )
    if not rows:
        return {"asOf": as_of, "balances": []}
    header = [name.strip() for name in rows[0]]
    columns = {name: index for index, name in enumerate(header)}
    if "account" not in columns or "sum(position)" not in columns:
        fail("query_failed", "bean-query returned an unexpected column set")
    balances = []
    for row in rows[1:]:
        position = parse_position(row[columns["sum(position)"]] if columns["sum(position)"] < len(row) else "")
        if position is None:
            continue
        balances.append({"account": project_name(state, row[columns["account"]]), **position})
    return {"asOf": as_of, "balances": balances}
