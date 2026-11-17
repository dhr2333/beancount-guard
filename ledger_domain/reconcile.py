"""Reconciliation periods and status, rebuilt from the ledger on every call.

No stored "due" flag exists: the anchor is the latest balance assertion (or the
open date), the period comes from open-directive metadata, and the status
follows from today. A restart cannot lose reconciliation state.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .dates import add_period, compare_date, diff_days, iso, is_period, parse_date

STATUS_RANK = {"overdue": 0, "due": 1, "scheduled": 2}


def reconciliation_of(
    account: dict[str, Any],
    latest: dict[str, Any] | None,
    *,
    today: date,
    grace_days: int,
) -> dict[str, Any] | None:
    period = account.get("period")
    if period in (None, "none") or not is_period(period):
        return None
    close_date = account.get("closeDate")
    if close_date is not None and close_date <= iso(today):
        return None

    balance = latest if latest is not None and latest["date"] <= iso(today) else None
    anchor = balance["date"] if balance is not None else account["openDate"]
    next_due = add_period(parse_date(anchor), period)
    if next_due is None:
        return None
    to_due = diff_days(next_due, today)
    if to_due > 0:
        status = "scheduled"
    elif to_due >= -grace_days:
        status = "due"
    else:
        status = "overdue"
    last_balance = None
    if balance is not None:
        last_balance = {
            "date": balance["date"],
            "amount": balance["amount"],
            "currency": balance["currency"],
            "relFile": balance.get("rel_file") or balance.get("relFile"),
            "line": balance.get("line"),
        }
    return {
        "account": account["account"],
        "name": account["name"],
        "period": period,
        "anchorDate": anchor,
        "anchorKind": "balance" if balance is not None else "open",
        "lastBalance": last_balance,
        "nextDue": iso(next_due),
        "status": status,
        "daysUntilDue": to_due,
        "daysOverdue": -to_due if to_due < 0 else 0,
    }


def list_reconciliations(
    accounts: list[dict[str, Any]],
    *,
    today: date,
    grace_days: int,
    include_scheduled: bool = True,
) -> list[dict[str, Any]]:
    reminders = []
    for account in accounts:
        reminder = reconciliation_of(account, account.get("lastBalance"), today=today, grace_days=grace_days)
        if reminder is None:
            continue
        if not include_scheduled and reminder["status"] == "scheduled":
            continue
        reminders.append(reminder)
    reminders.sort(
        key=lambda reminder: (
            STATUS_RANK[reminder["status"]],
            reminder["nextDue"],
            reminder["name"],
        )
    )
    return reminders


def count_reconciliations(reminders: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"due": 0, "overdue": 0, "scheduled": 0, "total": len(reminders)}
    for reminder in reminders:
        counts[reminder["status"]] += 1
    return counts
