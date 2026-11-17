"""Exact decimal amounts for a ledger that rejects floats.

Every amount entering the domain is a decimal string, is validated here, and is
summed as an integer scaled by 10^6. No float ever reaches a ``.bean`` file.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import fail

SCALE = 6
SCALE_FACTOR = 10**SCALE
AMOUNT_RE = re.compile(r"^-?(?:0|[1-9]\d{0,14})(?:\.\d{1,6})?$")


def assert_amount(value: object, label: str = "amount") -> str:
    """Validate one decimal amount string and return it trimmed."""
    if not isinstance(value, str):
        fail("invalid_amount", f"{label} must be a decimal string, not {type(value).__name__}")
    text = value.strip()
    if not AMOUNT_RE.match(text):
        fail("invalid_amount", f'{label} must be a plain decimal with at most 6 fractional digits, got "{text}"')
    return text


def to_scaled(text: str) -> int:
    """Convert an amount string to an integer scaled by 10^6."""
    value = assert_amount(text)
    negative = value.startswith("-")
    unsigned = value[1:] if negative else value
    whole, _, fraction = unsigned.partition(".")
    scaled = int(whole) * SCALE_FACTOR + int((fraction or "0").ljust(SCALE, "0")[:SCALE] or "0")
    return -scaled if negative else scaled


def from_scaled(scaled: int, min_fraction_digits: int = 2) -> str:
    """Format a scaled integer as a decimal string."""
    minimum = max(0, min(SCALE, min_fraction_digits))
    negative = scaled < 0
    absolute = -scaled if negative else scaled
    whole, fraction = divmod(absolute, SCALE_FACTOR)
    digits = f"{fraction:0{SCALE}d}"
    while len(digits) > minimum and digits.endswith("0"):
        digits = digits[:-1]
    digits = digits.ljust(minimum, "0")
    return f"{'-' if negative else ''}{whole}{'.' + digits if digits else ''}"


def normalize_amount(text: str) -> str:
    """Normalize an amount for stable storage (at least two decimals)."""
    return from_scaled(to_scaled(text), min_fraction_digits=2)


def sum_amounts(amounts: list[str]) -> int:
    """Sum validated amount strings as scaled integers."""
    return sum((to_scaled(amount) for amount in amounts), 0)


def parse_balance_failure(stderr: str) -> dict[str, Any] | None:
    """Read expected/accumulated amounts out of a Beancount balance error."""
    match = re.search(
        r"Balance failed for '([^']+)': expected ([-+]?\d+(?:\.\d+)?) ([A-Z][A-Z0-9'._-]*)"
        r" != accumulated ([-+]?\d+(?:\.\d+)?) ([A-Z][A-Z0-9'._-]*)",
        stderr,
    )
    if match is None:
        return None
    expected, accumulated = match.group(2), match.group(4)
    try:
        difference = from_scaled(to_scaled(accumulated) - to_scaled(expected), min_fraction_digits=2)
    except Exception:
        difference = ""
    return {
        "account": match.group(1),
        "expected": expected,
        "accumulated": accumulated,
        "difference": difference,
        "currency": match.group(3),
    }
