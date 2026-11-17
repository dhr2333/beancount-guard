"""The deterministic fast path.

When a merchant string contains an alias the reviewer already bound,
categorization needs no model call: the alias index is the local mapping library.
The result still travels through the draft queue and human approval.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .amounts import normalize_amount, to_scaled
from .bean import is_currency
from .errors import fail
from .ledger import project_name, resolve_name


def normalize_merchant_text(text: str) -> str:
    """NFKC, lower-case text without separators, for containment matching."""
    return re.sub(r"[\s\-_·.,，。:：;；()（）\[\]【】'\"“”‘’/\\]+", "", unicodedata.normalize("NFKC", str(text)).lower())


def match_merchants(state: dict[str, Any], text: str, limit: int = 3) -> list[dict[str, Any]]:
    haystack = normalize_merchant_text(text)
    if not haystack:
        return []
    best: dict[str, dict[str, Any]] = {}
    for entries in state["aliases"].values():
        for entry in entries:
            needle = normalize_merchant_text(entry["alias"])
            if len(needle) < 2 or needle not in haystack:
                continue
            score = 10_000 + len(needle) if haystack == needle else len(needle) * 10
            previous = best.get(entry["account"])
            if previous is None or score > previous["score"]:
                best[entry["account"]] = {
                    "account": entry["account"],
                    "name": project_name(state, entry["account"]),
                    "alias": entry["alias"],
                    "score": score,
                }
    return sorted(best.values(), key=lambda item: (-item["score"], -len(item["alias"])))[:limit]


def build_quick_draft(
    *,
    state: dict[str, Any],
    date: str,
    merchant: str,
    amount: str,
    currency: str,
    source_account: str,
    narration: str | None = None,
) -> dict[str, Any]:
    source = resolve_name(state, source_account)
    if source is None:
        fail("account_not_found", f'"{source_account}" is not a known account or alias')
    if not is_currency(currency):
        fail("invalid_currency", f'"{currency}" is not a Beancount currency')
    normalized_amount = normalize_amount(amount)
    if to_scaled(normalized_amount) <= 0:
        fail("invalid_amount", "the fast path expects a positive spend amount")

    candidates = match_merchants(state, merchant)
    if not candidates:
        return {"matched": False, "candidates": []}
    candidate = candidates[0]
    if candidate["account"] == source["account"]:
        fail("invalid_quick_draft", f"the matched account {candidate['name']} is also the source account")

    draft = {
        "date": date,
        "flag": "*",
        "payee": merchant,
        "narration": narration or merchant,
        "tags": [],
        "links": [],
        "postings": [
            {"account": candidate["account"], "suggested": None, "amount": normalized_amount, "currency": currency},
            {"account": source["account"], "suggested": None, "amount": normalize_amount(f"-{normalized_amount}"), "currency": currency},
        ],
        "source": {"kind": "quickpath", "ref": merchant, "evidence": {"merchant": merchant, "alias": candidate["alias"]}},
        "confidence": 0.99,
    }
    return {
        "matched": True,
        "draft": draft,
        "candidate": {"account": candidate["account"], "name": candidate["name"], "alias": candidate["alias"]},
    }
