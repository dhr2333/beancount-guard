"""Import batches: the proposal envelope around a group of draft writes.

A batch is bookkeeping for the draft queue and the panel, never ledger truth.
It records what the agent claims the source contained (rows, skipped lines) and
which drafts were created from it, so a human can judge completeness once
instead of reading every draft.

The module owns no ledger rules and takes no lock: :class:`DraftStore` writes a
batch while it already holds the queue lock.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from .amounts import assert_amount, normalize_amount
from .bean import is_currency
from .dates import is_date
from .errors import fail
from .validation import atomic_write

SOURCE_KINDS = {"ai", "quickpath", "manual", "import", "external"}
BATCH_ID_RE = re.compile(r"^batch-[0-9]{8}-[a-f0-9]{6}$")
MAX_BATCHES = 100
MAX_BATCH_DRAFTS = 200
MAX_SKIPPED = 5000
MAX_ROWS = 20000

# Every skipped row must say *why* in a closed vocabulary. Free text is kept as a
# human note, but it never decides anything: a reason nobody can enumerate is a
# reason nobody can check.
REASON_CODES = {
    "closed": "订单关闭或未支付，没有资金变动",
    "exact_refund": "等额原路退回；必须给出 refundOf 或 refundOfUuid",
    "zero_amount": "金额为 0，没有实际支出",
    "internal_transfer": "本人资金账户之间划转",
    "interest": "利息或收益，随余额对账处理",
    "duplicate_source": "与其他批次同一笔；必须给出 duplicateOf",
}
REASONS_NEEDING_REFUND = {"exact_refund"}
REASONS_NEEDING_AMOUNT_ZERO = {"zero_amount"}


def _text(value: object, label: str, *, maximum: int = 200, allow_empty: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        fail("invalid_batch", f"{label} must be a string")
    text = value.strip()
    if text == "" and not allow_empty:
        fail("invalid_batch", f"{label} must not be empty")
    if len(text) > maximum:
        fail("invalid_batch", f"{label} must be at most {maximum} characters")
    return text


def _row_index(value: object, label: str, rows: int) -> int:
    """Validate one 0-based source row index against the declared row count."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        fail("invalid_batch", f"{label} must be a non-negative integer")
    if value >= rows:
        fail("row_index_out_of_range", f"{label} is {value} but the source has {rows} rows", {"rows": rows, "index": value})
    return value


def normalize_batch_envelope(payload: object) -> dict[str, Any]:
    """Validate the agent-supplied envelope (everything except the drafts).

    Coverage is a bijection, not a sum: every source row must be claimed by
    exactly one draft or one skipped entry. Counting alone is what let a
    duplicated ``skipped.index`` hide a missing row, so duplicates, out-of-range
    indices and unexplained rows are rejected before anything is written.
    """
    if not isinstance(payload, dict):
        fail("invalid_batch", "a batch must be a JSON object")
    source = payload.get("source")
    if not isinstance(source, dict):
        fail("invalid_batch", "source must be an object")
    kind = _text(source.get("kind") or "import", "source.kind", maximum=40, allow_empty=False)
    if kind not in SOURCE_KINDS:
        fail("invalid_batch", f'source.kind must be one of {", ".join(sorted(SOURCE_KINDS))}')
    ref = _text(source.get("ref"), "source.ref", maximum=200, allow_empty=False)
    rows = source.get("rows")
    if rows is None:
        fail(
            "missing_source_rows",
            "source.rows is required: coverage cannot be verified without the number of source rows",
        )
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0 or rows > MAX_ROWS:
        fail("invalid_batch", f"source.rows must be an integer between 0 and {MAX_ROWS}")
    skipped = payload.get("skipped")
    if skipped is None:
        skipped = []
    if not isinstance(skipped, list) or len(skipped) > MAX_SKIPPED:
        fail("invalid_batch", f"skipped must be an array of at most {MAX_SKIPPED} entries")
    cleaned_skipped: list[dict[str, Any]] = []
    claimed_rows: dict[int, str] = {}
    for index, item in enumerate(skipped):
        if not isinstance(item, dict):
            fail("invalid_batch", f"skipped[{index}] must be an object")
        position = _row_index(item.get("index"), f"skipped[{index}].index", rows)
        if position in claimed_rows:
            fail(
                "row_index_duplicate",
                f'row {position} is declared twice in skipped (also by skipped[{claimed_rows[position]}])',
                {"index": position, "first": claimed_rows[position], "second": index},
            )
        code = _text(item.get("reasonCode"), f"skipped[{index}].reasonCode", maximum=40)
        if code and code not in REASON_CODES:
            fail(
                "invalid_reason_code",
                f'skipped[{index}].reasonCode must be one of {", ".join(sorted(REASON_CODES))}',
                {"code": code, "supported": sorted(REASON_CODES)},
            )
        reason = _text(item.get("reason"), f"skipped[{index}].reason", maximum=100, allow_empty=bool(code))
        entry: dict[str, Any] = {"index": position, "reason": reason}
        if code:
            entry["reasonCode"] = code
        when = _text(item.get("date"), f"skipped[{index}].date", maximum=10)
        if when:
            if not is_date(when):
                fail("invalid_batch", f"skipped[{index}].date must be a real YYYY-MM-DD date")
            entry["date"] = when
        payee = _text(item.get("payee"), f"skipped[{index}].payee", maximum=200)
        if payee:
            entry["payee"] = payee
        amount = _text(item.get("amount"), f"skipped[{index}].amount", maximum=40)
        if amount:
            entry["amount"] = normalize_amount(assert_amount(amount, f"skipped[{index}].amount"))
        if code in REASONS_NEEDING_AMOUNT_ZERO and amount and entry["amount"] != "0.00":
            fail(
                "zero_amount_mismatch",
                f"skipped[{index}] claims zero_amount but carries {entry['amount']}",
                {"index": position, "amount": entry["amount"]},
            )
        refund_of = item.get("refundOf")
        if refund_of is not None:
            entry["refundOf"] = _row_index(refund_of, f"skipped[{index}].refundOf", rows)
        refund_uuid = _text(item.get("refundOfUuid"), f"skipped[{index}].refundOfUuid", maximum=120)
        if refund_uuid:
            entry["refundOfUuid"] = refund_uuid
        if code in REASONS_NEEDING_REFUND and "refundOf" not in entry and "refundOfUuid" not in entry:
            fail(
                "refund_pair_missing",
                f"skipped[{index}] is exact_refund but names neither refundOf nor refundOfUuid",
                {"index": position},
            )
        duplicate_of = _text(item.get("duplicateOf"), f"skipped[{index}].duplicateOf", maximum=200)
        if duplicate_of:
            entry["duplicateOf"] = duplicate_of
        elif code == "duplicate_source":
            fail("invalid_batch", f"skipped[{index}] is duplicate_source but names no duplicateOf", {"index": position})
        claimed_rows[position] = index
        cleaned_skipped.append(entry)

    claimed = payload.get("claimed")
    normalized_claimed: dict[str, str] | None = None
    if claimed is not None:
        if not isinstance(claimed, dict):
            fail("invalid_batch", "claimed must be an object")
        net = _text(claimed.get("net"), "claimed.net", maximum=40)
        if net:
            currency = _text(claimed.get("currency") or "CNY", "claimed.currency", maximum=8, allow_empty=False)
            if not is_currency(currency):
                fail("invalid_currency", f'"{currency}" is not a Beancount currency')
            normalized_claimed = {"net": normalize_amount(assert_amount(net, "claimed.net")), "currency": currency}

    drafts = payload.get("drafts")
    if drafts is None:
        drafts = []
    if not isinstance(drafts, list) or len(drafts) > MAX_BATCH_DRAFTS:
        fail("invalid_batch", f"drafts must be an array of at most {MAX_BATCH_DRAFTS} entries")
    if not drafts and not cleaned_skipped:
        fail("empty_batch", "a batch needs at least one draft or one skipped row")

    draft_rows: dict[int, int] = {}
    for index, draft in enumerate(drafts):
        if not isinstance(draft, dict):
            fail("invalid_batch", f"drafts[{index}] must be an object")
        raw_source = draft.get("source")
        position = raw_source.get("index") if isinstance(raw_source, dict) else None
        if position is None:
            fail(
                "missing_source_index",
                f"drafts[{index}].source.index is required: every source row needs one owner "
                f"(a draft or a skipped entry) for coverage to be verifiable",
                {"draft": index, "rows": rows, "taken": sorted(claimed_rows)},
            )
        position = _row_index(position, f"drafts[{index}].source.index", rows)
        if position in claimed_rows:
            fail(
                "row_index_conflict",
                f'row {position} is both drafts[{index}] and skipped[{claimed_rows[position]}]',
                {"index": position, "skipped": claimed_rows[position], "draft": index},
            )
        if position in draft_rows:
            fail(
                "row_index_duplicate",
                f"row {position} is claimed by both drafts[{draft_rows[position]}] and drafts[{index}]",
                {"index": position, "first": draft_rows[position], "second": index},
            )
        draft_rows[position] = index

    missing = sorted(set(range(rows)) - set(claimed_rows) - set(draft_rows))
    if missing:
        fail(
            "rows_uncovered",
            f"{len(missing)} of {rows} source rows have no draft and no skipped entry",
            {
                "rows": rows,
                "missing": missing[:100],
                "missingCount": len(missing),
                "drafts": len(drafts),
                "skipped": len(cleaned_skipped),
            },
        )

    return {
        "source": {
            "kind": kind,
            "ref": ref,
            "channel": _text(source.get("channel"), "source.channel", maximum=40) or None,
            "rows": rows,
            "fileName": _text(source.get("fileName"), "source.fileName", maximum=200) or None,
            "fileHash": _text(source.get("fileHash"), "source.fileHash", maximum=120) or None,
        },
        "claimed": normalized_claimed,
        "skipped": cleaned_skipped,
        "drafts": drafts,
    }


def coverage_of(manifest: dict[str, Any]) -> dict[str, Any]:
    """Derive the coverage block shown in the panel.

    New manifests are verified at submit time, so ``ok`` is simply true. Legacy
    manifests (no ``rows``, or rows declared without per-row indices) fall back to
    the historical counting rule, which stays ``None`` when rows is unknown.
    """
    source_rows = (manifest.get("source") or {}).get("rows")
    counts = manifest.get("counts") or {}
    stored = manifest.get("coverage")
    if isinstance(stored, dict) and "ok" in stored:
        return {"covered": (manifest.get("source") or {}).get("rows"), **stored}
    covered = (
        int(counts.get("drafts") or 0)
        + int(counts.get("skipped") or 0)
        + int(counts.get("duplicates") or 0)
        + int(counts.get("failed") or 0)
    )
    return {
        "rows": source_rows,
        "drafts": int(counts.get("drafts") or 0),
        "skipped": int(counts.get("skipped") or 0),
        "duplicates": int(counts.get("duplicates") or 0),
        "failed": int(counts.get("failed") or 0),
        "covered": covered,
        "ok": None if source_rows is None else covered == source_rows,
    }


class BatchStore:
    """One JSON file per batch under ``.ledger/batches``."""

    def __init__(self, *, root: str | Path, state_dir: str = ".ledger") -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / state_dir / "batches"

    def ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, batch_id: str) -> Path:
        return self.directory / f"{batch_id}.json"

    def allocate_id(self, today: str) -> str:
        self.ensure_directory()
        prefix = f"batch-{today.replace('-', '')}-"
        for _ in range(8):
            candidate = f"{prefix}{secrets.token_hex(3)}"
            if not self.path(candidate).exists():
                return candidate
        fail("batch_id_exhausted", "cannot allocate a unique batch id")

    def save(self, manifest: dict[str, Any]) -> None:
        self.ensure_directory()
        atomic_write(self.path(manifest["id"]), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    def read(self, batch_id: str) -> dict[str, Any]:
        if not isinstance(batch_id, str) or BATCH_ID_RE.match(batch_id) is None:
            fail("batch_not_found", f'invalid batch id "{batch_id}"')
        try:
            document = json.loads(self.path(batch_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            fail("batch_not_found", f"batch {batch_id} does not exist")
        except ValueError:
            fail("batch_corrupt", f"batch {batch_id} is not valid JSON")
        if not isinstance(document, dict) or document.get("id") != batch_id:
            fail("batch_corrupt", f"batch {batch_id} does not match the batch schema")
        return document

    def list(self, *, limit: int = MAX_BATCHES) -> list[dict[str, Any]]:
        try:
            names = sorted(os.listdir(self.directory), reverse=True)
        except FileNotFoundError:
            return []
        documents = []
        for name in names:
            if not name.endswith(".json") or len(documents) >= limit:
                continue
            try:
                document = json.loads((self.directory / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and isinstance(document.get("id"), str):
                documents.append(document)
        documents.sort(key=lambda document: document.get("createdAt", ""), reverse=True)
        return documents[:limit]

