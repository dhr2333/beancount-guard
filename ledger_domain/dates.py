"""Calendar-date arithmetic for reconciliation periods.

Dates are date-only values. All arithmetic runs on plain ``datetime.date`` so a
daylight-saving rule can never shift a due date by one day, and "today" is
computed in the configured IANA zone.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import fail

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: Supported reconciliation periods. ``none`` keeps an account out of reminders.
PERIODS = ("none", "daily", "weekly", "monthly", "quarterly", "yearly")


def parse_date(text: object) -> date:
    """Parse and validate one ``YYYY-MM-DD`` date."""
    match = DATE_RE.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        fail("invalid_date", f'"{text}" is not a YYYY-MM-DD date')
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        fail("invalid_date", f'"{text}" is not a real calendar date')


def is_date(text: object) -> bool:
    """Whether a value is a real date-only string."""
    try:
        parse_date(text)
        return True
    except Exception:
        return False


def today_in_zone(time_zone: str, now: datetime | None = None) -> date:
    """Today's date in one IANA zone, falling back to UTC for an invalid zone."""
    current = now or datetime.now(timezone.utc)
    try:
        return current.astimezone(ZoneInfo(time_zone)).date()
    except Exception:
        return current.astimezone(timezone.utc).date()


def iso(value: date) -> str:
    """Format a date as ``YYYY-MM-DD``."""
    return value.isoformat()


def add_days(value: date, days: int) -> date:
    """Add whole days."""
    return value + timedelta(days=days)


def diff_days(later: date, earlier: date) -> int:
    """Whole days from ``earlier`` to ``later``."""
    return (later - earlier).days


def compare_date(left: date, right: date) -> int:
    """Three-way date comparison."""
    return (left > right) - (left < right)


def add_months(value: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month's end."""
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(value.day, last_day))


def add_period(anchor: date, period: str) -> date | None:
    """Add one reconciliation period; ``none`` returns None."""
    if period == "daily":
        return add_days(anchor, 1)
    if period == "weekly":
        return add_days(anchor, 7)
    if period == "monthly":
        return add_months(anchor, 1)
    if period == "quarterly":
        return add_months(anchor, 3)
    if period == "yearly":
        return add_months(anchor, 12)
    if period == "none":
        return None
    fail("invalid_period", f'"{period}" is not a supported reconciliation period', {"supported": list(PERIODS)})


def is_period(value: object) -> bool:
    """Whether a value is a supported period."""
    return isinstance(value, str) and value in PERIODS
