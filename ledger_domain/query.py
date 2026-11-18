"""Read projection: query through Beancount's own query engine, then translate
every canonical account into its display alias before the result leaves the
domain.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from .amounts import from_scaled, normalize_amount, to_scaled
from .dates import is_date
from .errors import fail
from .ledger import project_name, resolve_name
from .validation import DEFAULT_EXCLUDES, mirror_tree, run_process

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


def query_account_balance(
    *,
    root: str,
    bean_file: str,
    executable: str,
    account: str,
    currency: str,
    as_of: str,
    overrides: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Return one account's accumulated position at ``as_of``.

    When ``overrides`` is supplied the query runs against a disposable shadow
    tree, which is how an update preview asks "what would the ledger say if the
    block being replaced were not there?".
    """
    if not is_date(as_of):
        fail("invalid_date", "asOf must be a YYYY-MM-DD date")
    import os
    import shutil
    import tempfile
    from pathlib import Path as _Path

    root_path = _Path(root).resolve()
    query = (
        "SELECT account, currency, sum(position) WHERE "
        f"account = '{account}' AND currency = '{currency}' AND date <= {as_of} "
        "GROUP BY account, currency"
    )
    shadow = None
    if overrides:
        shadow = tempfile.mkdtemp(prefix="ledger-domain-query-")
        try:
            mirror_tree(
                root_path,
                shadow,
                overrides=dict(overrides),
                extra_files={},
                excludes=set(DEFAULT_EXCLUDES),
            )
            rows = _run_query(
                root=shadow,
                bean_file=bean_file,
                executable=executable,
                query=query,
                timeout=timeout,
            )
        finally:
            shutil.rmtree(shadow, ignore_errors=True)
    else:
        rows = _run_query(
            root=str(root_path),
            bean_file=bean_file,
            executable=executable,
            query=query,
            timeout=timeout,
        )
    if not rows:
        return {"amount": "0.00", "currency": currency, "hasCost": False}
    header = [name.strip() for name in rows[0]]
    columns = {name: index for index, name in enumerate(header)}
    amount_total = 0
    has_cost = False
    for row in rows[1:]:
        position = parse_position(row[columns["sum(position)"]] if "sum(position)" in columns and columns["sum(position)"] < len(row) else "")
        if position is None:
            continue
        amount_total += to_scaled(position["amount"])
        if position.get("cost"):
            has_cost = True
    return {
        "amount": from_scaled(amount_total, min_fraction_digits=2),
        "currency": currency,
        "hasCost": has_cost,
    }


def query_account_activity(
    *,
    root: str,
    bean_file: str,
    executable: str,
    account: str,
    currency: str,
    date_from: str,
    date_to: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Count and sum one account's postings in a date window."""
    if not is_date(date_from) or not is_date(date_to):
        fail("invalid_date", "activity bounds must be YYYY-MM-DD dates")
    query = (
        "SELECT date, position WHERE "
        f"account = '{account}' AND currency = '{currency}' "
        f"AND date >= {date_from} AND date <= {date_to}"
    )
    rows = _run_query(
        root=root,
        bean_file=bean_file,
        executable=executable,
        query=query,
        timeout=timeout,
    )
    if not rows:
        return {"count": 0, "inflow": "0.00", "outflow": "0.00"}
    header = [name.strip() for name in rows[0]]
    columns = {name: index for index, name in enumerate(header)}
    count = 0
    inflow = 0
    outflow = 0
    for row in rows[1:]:
        position = parse_position(row[columns["position"]] if "position" in columns and columns["position"] < len(row) else "")
        if position is None:
            continue
        count += 1
        value = to_scaled(position["amount"])
        if value > 0:
            inflow += value
        elif value < 0:
            outflow += value
    return {
        "count": count,
        "inflow": from_scaled(inflow, min_fraction_digits=2),
        "outflow": from_scaled(outflow, min_fraction_digits=2),
    }
