"""The draft queue: structured proposals that wait for a human decision.

A draft is not a ledger write. Submitting one validates its shape, resolves every
posting through the alias index and stores the unresolved remainder as a visible
issue. Only approval renders a transaction, validates the whole ledger in a
shadow tree and atomically appends it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .amounts import assert_amount, from_scaled, normalize_amount, to_scaled
from .batches import BATCH_ID_RE, SOURCE_KINDS, BatchStore, coverage_of, normalize_batch_envelope
from .bean import (
    TAG_RE,
    append_block,
    contains_draft_id,
    is_account_name,
    is_currency,
    quote,
    render_transaction,
)
from .dates import add_days, is_date, iso, today_in_zone
from .errors import LedgerError, fail
from .imports import SourceRegistry
from .ledger import has_account, project_name, resolve_name
from .target import resolve_append_target
from .validation import atomic_write, validate_ledger, write_lock

FLAGS = {"*", "!"}
ROLES = {"category", "funding"}
FALLBACKS = {"expense", "income"}
BLOCKING_ISSUES = {"unresolved_account", "funding_unresolved", "no_real_account", "unbalanced", "before_account_open", "closed_account"}
ACCOUNT_ROOTS = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}
FUNDING_ROOTS = {"Assets", "Liabilities"}
# Money can also enter or leave the ledger through the equity adjustment
# account: reconciliation pads to it, and a person may book an entry against it
# directly. It is an allowlist, not a sixth root — every other Equity account
# stays a category.
FUNDING_EXTRA_ACCOUNTS = {"Equity:Adjustments"}
CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]")
RESERVED_METADATA = {"draft_id"}
MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE = 200
DRAFT_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Wallet accounts can only fund a row whose bill row actually says so. When the
# bill names a bank card and the funding account is a wallet, one of the two is
# wrong — this is exactly how a 43-draft batch ended up 35 rows on the wrong
# account.
WALLET_ACCOUNTS = {
    "Assets:Savings:Web:WechatPay",
    "Assets:Savings:Web:WechatFund",
    "Assets:Savings:Web:AliPay",
    "Assets:Savings:Web:AliFund",
}
BANK_WORDS = (
    "中国银行", "招商银行", "中信银行", "华夏银行", "工商银行", "建设银行",
    "农业银行", "交通银行", "邮政储蓄", "邮储", "民生银行", "浦发银行",
    "兴业银行", "光大银行", "宁波银行", "广发银行", "平安银行",
)
CARD_DIGITS_RE = re.compile(r"\d{3,4}")


def funding_mismatch(hint: object, account: object) -> str | None:
    """Return why a bill's payment hint contradicts the resolved funding account."""
    if not isinstance(hint, str) or not hint.strip():
        return None
    if not isinstance(account, str) or account not in WALLET_ACCOUNTS:
        return None
    text = hint.strip()
    if CARD_DIGITS_RE.search(text) or any(word in text for word in BANK_WORDS):
        return (
            f'账单支付方式是「{text}」，资金账户却是钱包 {account}；'
            "钱包只能用于账单明写余额/零钱/余额宝的行"
        )
    return None


def is_funding_account(account: str) -> bool:
    """True when the account is a money leg: an asset, a liability, or the adjustment account."""
    return account in FUNDING_EXTRA_ACCOUNTS or account.split(":", 1)[0] in FUNDING_ROOTS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def prose(value: object, label: str, *, max_length: int = 200, allow_empty: bool = False) -> str:
    text = "" if value is None else value
    if not isinstance(text, str):
        fail("invalid_draft", f"{label} must be a string")
    text = text.strip()
    if text == "" and not allow_empty:
        fail("invalid_draft", f"{label} must not be empty")
    if CONTROL_RE.search(text):
        fail("invalid_draft", f"{label} must not contain control or bidirectional-override characters")
    if len(text) > max_length:
        fail("invalid_draft", f"{label} must be at most {max_length} characters")
    return text


def normalize_metadata(value: object) -> dict[str, str]:
    """Validate optional transaction metadata carried by a draft.

    Values are strings so the renderer can quote them without type ambiguity;
    insertion order is preserved because it is the order written to the ledger.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        fail("invalid_draft", "metadata must be an object")
    if len(value) > MAX_METADATA_KEYS:
        fail("invalid_draft", f"metadata must have at most {MAX_METADATA_KEYS} entries")
    metadata: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or DRAFT_METADATA_KEY_RE.match(key) is None:
            fail("invalid_draft", f'"{key}" is not a valid metadata key (lowercase letters, digits, _ or -)')
        if key in RESERVED_METADATA:
            fail("invalid_draft", f'"{key}" is reserved by the draft protocol')
        if not isinstance(item, str):
            fail("invalid_draft", f"metadata.{key} must be a string")
        if CONTROL_RE.search(item):
            fail("invalid_draft", f"metadata.{key} must not contain control or bidirectional-override characters")
        text = item.strip()
        if len(text) > MAX_METADATA_VALUE:
            fail("invalid_draft", f"metadata.{key} must be at most {MAX_METADATA_VALUE} characters")
        metadata[key] = text
    return metadata


def infer_posting_role(account: str | None, suggested: str | None, explicit: object = None) -> str:
    """Return ``category`` or ``funding``; the resolved account wins over a hint."""
    declared = ""
    if explicit is not None:
        if not isinstance(explicit, str):
            fail("invalid_draft", "role must be a string")
        declared = explicit.strip()
        if declared != "" and declared not in ROLES:
            fail("invalid_draft", f'"role" must be one of {", ".join(sorted(ROLES))}')
    if account is not None:
        inferred = "funding" if is_funding_account(account) else "category"
        if declared != "" and declared != inferred:
            fail(
                "invalid_draft",
                f'role "{declared}" conflicts with the resolved account {account}',
                {"account": account},
            )
        return inferred
    if isinstance(suggested, str) and is_account_name(suggested):
        return "funding" if is_funding_account(suggested) else "category"
    return declared or "category"


def normalize_fallback(value: object, role: str) -> str | None:
    """Validate the optional fallback hint used for unclassified category postings."""
    if value is None:
        return None
    if not isinstance(value, str):
        fail("invalid_draft", "fallback must be a string")
    text = value.strip()
    if text == "":
        return None
    if text not in FALLBACKS:
        fail("invalid_draft", f'"fallback" must be one of {", ".join(sorted(FALLBACKS))}')
    if role != "category":
        fail("invalid_draft", "fallback is only valid on category postings")
    return text


def normalize_posting(
    posting: object,
    state: dict[str, Any],
    index: int,
    draft: dict[str, str],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(posting, dict):
        fail("invalid_draft", f"postings[{index}] must be an object")
    amount = normalize_amount(assert_amount(posting.get("amount"), f"postings[{index}].amount"))
    currency = posting.get("currency").strip() if isinstance(posting.get("currency"), str) else ""
    if not is_currency(currency):
        fail("invalid_draft", f"postings[{index}].currency is not a Beancount currency")

    raw_account = posting.get("account").strip() if isinstance(posting.get("account"), str) else ""
    raw_suggested = posting.get("suggested").strip() if isinstance(posting.get("suggested"), str) else ""
    if raw_account == "" and raw_suggested == "":
        fail("invalid_draft", f"postings[{index}] needs account or suggested")

    account: str | None = None
    suggested: str | None = None
    if raw_account != "":
        resolution = resolve_name(state, raw_account)
        if resolution is None:
            suggested = raw_account
        else:
            account = resolution["account"]
    else:
        suggested = raw_suggested

    role = infer_posting_role(account, suggested, posting.get("role"))
    fallback = normalize_fallback(posting.get("fallback"), role)
    funding_hint = posting.get("fundingHint")
    if funding_hint is not None and not isinstance(funding_hint, str):
        fail("invalid_draft", f"postings[{index}].fundingHint must be a string")
    funding_hint = funding_hint.strip()[:200] if isinstance(funding_hint, str) and funding_hint.strip() else None
    if role == "funding":
        mismatch = funding_mismatch(funding_hint, account)
        if mismatch is not None:
            issues.append(
                {
                    "code": "funding_mismatch",
                    "message": f"postings[{index}]: {mismatch}",
                    "postingIndex": index,
                    "account": account,
                    "hint": funding_hint,
                }
            )

    if account is None:
        if role == "funding":
            issues.append(
                {
                    "code": "funding_unresolved",
                    "message": f'postings[{index}]: funding account "{suggested}" is not open yet',
                    "postingIndex": index,
                    "suggested": suggested,
                }
            )
        else:
            issues.append(
                {
                    "code": "unresolved_account",
                    "message": f"postings[{index}]: unclassified ({suggested})",
                    "postingIndex": index,
                    "suggested": suggested,
                }
            )

    if account is not None:
        opened = state["opens"].get(account)
        if opened is not None and opened["date"] > draft["date"]:
            issues.append(
                {
                    "code": "before_account_open",
                    "message": f"postings[{index}]: {account} opens on {opened['date']}, after the draft date",
                    "postingIndex": index,
                    "account": account,
                }
            )
        closed = state["closes"].get(account)
        if closed is not None and closed["date"] <= draft["date"]:
            issues.append(
                {
                    "code": "closed_account",
                    "message": f"postings[{index}]: {account} is closed on the draft date",
                    "postingIndex": index,
                    "account": account,
                }
            )
    return {
        "account": account,
        "suggested": suggested,
        "role": role,
        "fallback": fallback,
        "fundingHint": funding_hint,
        "amount": amount,
        "currency": currency,
    }


def check_balanced(postings: list[dict[str, Any]], issues: list[dict[str, Any]]) -> bool:
    totals: dict[str, int] = {}
    for posting in postings:
        totals[posting["currency"]] = totals.get(posting["currency"], 0) + to_scaled(posting["amount"])
    differences = [
        {"currency": currency, "difference": from_scaled(total, min_fraction_digits=2)}
        for currency, total in totals.items()
        if total != 0
    ]
    if differences:
        issues.append(
            {
                "code": "unbalanced",
                "message": "postings do not sum to zero: "
                + ", ".join(f"{entry['difference']} {entry['currency']}" for entry in differences),
                "differences": differences,
            }
        )
        return False
    return True


def check_real_account(postings: list[dict[str, Any]], issues: list[dict[str, Any]]) -> None:
    """A rendered transaction must move at least one real money account."""
    if any(posting.get("account") is None for posting in postings):
        return
    if any(is_funding_account(posting.get("account") or "") for posting in postings):
        return
    issues.append(
        {
            "code": "no_real_account",
            "message": "the transaction has no asset, liability or equity adjustment posting",
        }
    )


def _normalize_tokens(value: object, label: str, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        fail("invalid_draft", f"{label} must be an array")
    if len(value) > maximum:
        fail("invalid_draft", f"{label} must have at most {maximum} entries")
    tokens = []
    for token in value:
        if not isinstance(token, str) or TAG_RE.match(token.strip()) is None:
            fail("invalid_draft", f"{label} contains an invalid Beancount token")
        tokens.append(token.strip())
    return tokens


def normalize_draft_input(
    payload: object,
    state: dict[str, Any],
    *,
    today: str,
    max_evidence_bytes: int = 16_384,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        fail("invalid_draft", "a draft must be a JSON object")
    date = payload.get("date").strip() if isinstance(payload.get("date"), str) else ""
    if not is_date(date):
        fail("invalid_draft", "date must be a real YYYY-MM-DD date")
    flag = payload.get("flag").strip() if isinstance(payload.get("flag"), str) else "*"
    if flag not in FLAGS:
        fail("invalid_draft", "flag must be * or !")

    payee = prose(payload.get("payee"), "payee", max_length=200, allow_empty=True)
    narration = prose(payload.get("narration"), "narration", max_length=500, allow_empty=True)
    if payee == "" and narration == "":
        fail("invalid_draft", "a draft needs a payee or a narration")

    issues: list[dict[str, Any]] = []
    raw_postings = payload.get("postings")
    if not isinstance(raw_postings, list) or not 2 <= len(raw_postings) <= 50:
        fail("invalid_draft", "postings must be an array of 2 to 50 entries")
    postings = [
        normalize_posting(posting, state, index, {"date": date}, issues)
        for index, posting in enumerate(raw_postings)
    ]
    check_balanced(postings, issues)
    check_real_account(postings, issues)

    tags = _normalize_tokens(payload.get("tags"), "tags", 10)
    links = _normalize_tokens(payload.get("links"), "links", 10)
    metadata = normalize_metadata(payload.get("metadata"))

    raw_source = payload.get("source") if payload.get("source") is not None else {}
    if not isinstance(raw_source, dict):
        fail("invalid_draft", "source must be an object")
    kind = raw_source.get("kind") if isinstance(raw_source.get("kind"), str) else "ai"
    if kind not in SOURCE_KINDS:
        fail("invalid_draft", f"source.kind must be one of {', '.join(sorted(SOURCE_KINDS))}")
    ref = (
        prose(raw_source.get("ref"), "source.ref", max_length=200, allow_empty=True)
        if isinstance(raw_source.get("ref"), str)
        else ""
    )
    evidence = raw_source.get("evidence")
    encoded = "" if evidence is None else json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_evidence_bytes:
        fail("invalid_draft", f"source.evidence must be at most {max_evidence_bytes} bytes")
    source = {"kind": kind, "ref": ref, "evidence": evidence}

    confidence = payload.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            fail("invalid_draft", "confidence must be a number between 0 and 1")
        confidence = float(confidence)

    if date > today:
        issues.append({"code": "future_dated", "message": f"draft date {date} is in the future"})

    draft = {
        "date": date,
        "flag": flag,
        "payee": payee,
        "narration": narration,
        "metadata": metadata,
        "tags": tags,
        "links": links,
        "postings": postings,
        "source": source,
        "confidence": confidence,
    }
    return draft, issues


def assess_draft(document: dict[str, Any], state: dict[str, Any], *, today: str) -> tuple[list[dict[str, Any]], bool]:
    issues: list[dict[str, Any]] = []
    for index, posting in enumerate(document.get("postings") or []):
        account = posting.get("account")
        role = infer_posting_role(account, posting.get("suggested"), posting.get("role"))
        if account is None:
            issues.append(
                {
                    "code": "funding_unresolved" if role == "funding" else "unresolved_account",
                    "message": (
                        f'postings[{index}]: funding account "{posting.get("suggested") or ""}" is not open yet'
                        if role == "funding"
                        else f"postings[{index}]: unclassified"
                    ),
                    "postingIndex": index,
                    "suggested": posting.get("suggested") or "",
                }
            )
            continue
        if not has_account(state, account):
            issues.append(
                {
                    "code": "unresolved_account",
                    "message": f"postings[{index}]: account is no longer open in the ledger",
                    "postingIndex": index,
                }
            )
            continue
        if role == "funding":
            mismatch = funding_mismatch(posting.get("fundingHint"), account)
            if mismatch is not None:
                issues.append(
                    {
                        "code": "funding_mismatch",
                        "message": f"postings[{index}]: {mismatch}",
                        "postingIndex": index,
                        "account": account,
                        "hint": posting.get("fundingHint"),
                    }
                )
        opened = state["opens"].get(account)
        if opened is not None and opened["date"] > document["date"]:
            issues.append(
                {"code": "before_account_open", "message": f"postings[{index}] opens after the draft date", "postingIndex": index}
            )
        closed = state["closes"].get(account)
        if closed is not None and closed["date"] <= document["date"]:
            issues.append(
                {"code": "closed_account", "message": f"postings[{index}] is closed on the draft date", "postingIndex": index}
            )
    check_balanced(document.get("postings") or [], issues)
    check_real_account(document.get("postings") or [], issues)
    if document["date"] > today:
        issues.append({"code": "future_dated", "message": f"draft date {document['date']} is in the future"})
    approvable = document.get("status") == "pending" and not any(
        issue["code"] in BLOCKING_ISSUES for issue in issues
    )
    return issues, approvable


def hash_payload(draft: dict[str, Any]) -> str:
    facts = {
        "date": draft["date"],
        "flag": draft["flag"],
        "payee": draft["payee"],
        "narration": draft["narration"],
        "metadata": draft.get("metadata", {}),
        "tags": draft["tags"],
        "links": draft["links"],
        "postings": [
            {
                "account": posting["account"],
                "suggested": posting["suggested"],
                "amount": posting["amount"],
                "currency": posting["currency"],
            }
            for posting in draft["postings"]
        ],
    }
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def is_draft_document(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("id"), str) or re.match(r"^draft-[0-9]{8}-[a-f0-9]{6}$", value["id"]) is None:
        return False
    if value.get("status") not in {"pending", "approved", "rejected"}:
        return False
    if not is_date(value.get("date")):
        return False
    if not isinstance(value.get("payee"), str) or not isinstance(value.get("narration"), str):
        return False
    if not isinstance(value.get("tags"), list) or not isinstance(value.get("links"), list):
        return False
    metadata = value.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or len(metadata) > MAX_METADATA_KEYS:
            return False
        for key, item in metadata.items():
            if not isinstance(key, str) or DRAFT_METADATA_KEY_RE.match(key) is None or key in RESERVED_METADATA:
                return False
            if not isinstance(item, str) or len(item) > MAX_METADATA_VALUE or CONTROL_RE.search(item):
                return False
    batch = value.get("batch")
    if batch is not None and (not isinstance(batch, str) or BATCH_ID_RE.match(batch) is None):
        return False
    postings = value.get("postings")
    if not isinstance(postings, list) or len(postings) < 2:
        return False
    for posting in postings:
        if not isinstance(posting, dict):
            return False
        if posting.get("account") is not None and not isinstance(posting.get("account"), str):
            return False
        if not isinstance(posting.get("amount"), str) or not isinstance(posting.get("currency"), str):
            return False
        role = posting.get("role")
        if role is not None and role not in ROLES:
            return False
        fallback = posting.get("fallback")
        if fallback is not None and fallback not in FALLBACKS:
            return False
    return True


def _find_draft_location(state: dict[str, Any], draft_id: str) -> dict[str, Any] | None:
    for file, content in state["contents"].items():
        if not contains_draft_id(content, draft_id):
            continue
        index = content.find(f'draft_id: "{draft_id}"')
        rel_file = Path(file).resolve().relative_to(Path(state["root"]).resolve()).as_posix()
        return {"relFile": rel_file, "line": content[:index].count("\n") + 1}
    return None


def _normalize_approval_postings(
    value: object, document: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    if value is None:
        return [
            {"account": posting["account"], "amount": normalize_amount(posting["amount"]), "currency": posting["currency"]}
            for posting in document["postings"]
        ]
    if not isinstance(value, list) or not 2 <= len(value) <= 50:
        fail("invalid_draft", "postings must be an array of 2 to 50 entries")
    normalized = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            fail("invalid_draft", f"postings[{index}] must be an object")
        amount = normalize_amount(assert_amount(entry.get("amount"), f"postings[{index}].amount"))
        currency = entry.get("currency").strip() if isinstance(entry.get("currency"), str) else ""
        if not is_currency(currency):
            fail("invalid_currency", f"postings[{index}].currency is not a Beancount currency")
        name = entry.get("account").strip() if isinstance(entry.get("account"), str) else ""
        if name == "":
            fail("invalid_account", f"postings[{index}] needs an account")
        resolution = resolve_name(state, name)
        if resolution is None:
            fail("unresolved_postings", f'postings[{index}]: "{name}" is not a known account or alias', {"postingIndex": index})
        normalized.append({"account": resolution["account"], "amount": amount, "currency": currency})
    return normalized


class DraftStore:
    """One JSON file per draft plus an append-only audit log."""

    def __init__(
        self,
        *,
        root: str | Path,
        reader: Any,
        bean_check: str | Callable[[], str],
        state_dir: str = ".ledger",
        time_zone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
        max_drafts: int = 500,
        max_evidence_bytes: int = 16_384,
        timeout: float = 60.0,
        exclude_entries: list[str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.reader = reader
        self.bean_check = bean_check
        self.time_zone = time_zone
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_drafts = max_drafts
        self.max_evidence_bytes = max_evidence_bytes
        self.timeout = timeout
        self.exclude_entries = exclude_entries or []
        self.state_directory = self.root / state_dir
        self.drafts_directory = self.state_directory / "drafts"
        self.batches = BatchStore(root=self.root, state_dir=state_dir)
        self.imports = SourceRegistry(root=self.root, state_dir=state_dir)
        self.audit_file = self.state_directory / "audit.jsonl"
        self.lock_file = self.state_directory / "write.lock"
        # Optional hooks set by BeancountGuard: a late draft that would break a
        # reconciliation balance can be absorbed by auto-inserting a pad. The
        # hook returns extra candidate files; the finalizer records history.
        self.approval_hook: Callable[..., dict[str, Any] | None] | None = None
        self.approval_finalize: Callable[[dict[str, Any]], None] | None = None

    def today(self) -> str:
        return iso(today_in_zone(self.time_zone, self.now()))

    def ensure_directories(self) -> None:
        self.drafts_directory.mkdir(parents=True, exist_ok=True)

    def audit(self, action: str, payload: dict[str, Any]) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": _now(), "action": action, **payload}, ensure_ascii=False)
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _ledger_uuid_location(self, state: dict[str, Any], uuid: str) -> dict[str, Any] | None:
        """Find one uuid rendered in ledger metadata; the ledger is the source of truth."""
        needle = f"uuid: {quote(uuid)}"
        root = Path(state["root"]).resolve()
        for file, content in state["contents"].items():
            index = content.find(needle)
            if index < 0:
                continue
            try:
                rel_file = Path(file).resolve().relative_to(root).as_posix()
            except ValueError:
                rel_file = str(file)
            return {"file": rel_file, "line": content[:index].count("\n") + 1}
        return None

    def _check_duplicate(
        self,
        state: dict[str, Any],
        draft: dict[str, Any],
        pending: list[dict[str, Any]],
        seen_uuids: set[str],
    ) -> tuple[str, dict[str, Any] | None]:
        uuid = (draft.get("metadata") or {}).get("uuid") or ""
        if uuid:
            if uuid in seen_uuids:
                fail(
                    "duplicate_transaction",
                    f'transaction uuid "{uuid}" appears more than once in this batch',
                    {"uuid": uuid, "scope": "batch"},
                )
            location = self._ledger_uuid_location(state, uuid)
            if location is not None:
                fail(
                    "duplicate_transaction",
                    f'transaction uuid "{uuid}" already exists in the ledger',
                    {"uuid": uuid, **location},
                )
        digest = hash_payload(draft)
        if uuid:
            for candidate in pending:
                if (candidate.get("metadata") or {}).get("uuid") != uuid:
                    continue
                if candidate.get("hash") != digest:
                    fail(
                        "duplicate_transaction",
                        f'a pending draft already uses uuid "{uuid}" with different content',
                        {"uuid": uuid, "scope": "pending", "draftId": candidate.get("id")},
                    )
        previous = next((candidate for candidate in pending if candidate.get("hash") == digest), None)
        return digest, previous

    def _write_draft(
        self,
        draft: dict[str, Any],
        issues: list[dict[str, Any]],
        digest: str,
        today: str,
        batch_id: str | None,
    ) -> dict[str, Any]:
        self.ensure_directories()
        draft_id = self.allocate_id(today)
        timestamp = _now()
        document = {
            "id": draft_id,
            "version": 1,
            "status": "pending",
            "hash": digest,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            **draft,
            "batch": batch_id,
            "issues": issues,
        }
        atomic_write(self.draft_file(draft_id), json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        self.audit("draft_submitted", {"id": draft_id, "hash": digest, "source": draft["source"]["kind"], "batch": batch_id})
        approvable = not any(issue["code"] in BLOCKING_ISSUES for issue in issues)
        return {**document, "approvable": approvable}

    def submit(self, payload: object) -> dict[str, Any]:
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today()
            draft, issues = normalize_draft_input(
                payload, state, today=today, max_evidence_bytes=self.max_evidence_bytes
            )
            pending = self.list(status="pending", limit=self.max_drafts)
            digest, previous = self._check_duplicate(state, draft, pending, set())
            if previous is not None:
                return {"draft": previous, "issues": previous.get("issues", []), "deduplicated": True}
            document = self._write_draft(draft, issues, digest, today, None)
            return {"draft": document, "issues": issues, "deduplicated": False}

    def submit_batch(self, payload: object) -> dict[str, Any]:
        """Submit one import batch; per-draft failures never abort the whole batch."""
        if not isinstance(payload, dict):
            fail("invalid_batch", "a batch must be a JSON object")
        envelope = normalize_batch_envelope(payload)
        raw_drafts = envelope.pop("drafts")
        declared_drafts = len(raw_drafts)
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today()
            batch_id = self.batches.allocate_id(today)
            pending = self.list(status="pending", limit=self.max_drafts)
            seen_uuids: set[str] = set()
            items: list[dict[str, Any]] = []
            draft_ids: list[str] = []
            duplicates = 0
            failed = 0
            for index, raw in enumerate(raw_drafts):
                try:
                    draft, issues = normalize_draft_input(
                        raw, state, today=today, max_evidence_bytes=self.max_evidence_bytes
                    )
                    if not (draft.get("source") or {}).get("ref"):
                        draft["source"] = {
                            "kind": envelope["source"]["kind"],
                            "ref": envelope["source"]["ref"],
                            "evidence": (draft.get("source") or {}).get("evidence"),
                        }
                    row_source = raw.get("source") if isinstance(raw, dict) else None
                    row_index = row_source.get("index") if isinstance(row_source, dict) else None
                    if isinstance(row_index, int) and not isinstance(row_index, bool):
                        draft["source"]["index"] = row_index
                    digest, previous = self._check_duplicate(state, draft, pending, seen_uuids)
                    if previous is not None:
                        duplicates += 1
                        items.append(
                            {
                                "index": index,
                                "ok": False,
                                "code": "duplicate_pending",
                                "message": f'draft {previous["id"]} already has the same content',
                                "draftId": previous["id"],
                            }
                        )
                        continue
                    document = self._write_draft(draft, issues, digest, today, batch_id)
                    uuid = (draft.get("metadata") or {}).get("uuid") or ""
                    if uuid:
                        seen_uuids.add(uuid)
                    pending.append(document)
                    draft_ids.append(document["id"])
                    items.append(
                        {
                            "index": index,
                            "ok": True,
                            "draftId": document["id"],
                            "issues": sorted({issue["code"] for issue in issues}),
                        }
                    )
                except LedgerError as error:
                    if error.code == "duplicate_transaction":
                        duplicates += 1
                    else:
                        failed += 1
                    items.append(
                        {
                            "index": index,
                            "ok": False,
                            "code": error.code,
                            "message": error.message,
                            "details": error.details,
                        }
                    )
            manifest = {
                "id": batch_id,
                "version": 1,
                "createdAt": _now(),
                **envelope,
                "draftIds": draft_ids,
                "counts": {
                    "drafts": len(draft_ids),
                    "skipped": len(envelope["skipped"]),
                    "duplicates": duplicates,
                    "failed": failed,
                },
                # Verified at submit time: every source row is owned by exactly one
                # draft or one skipped entry, so this is a fact, not a guess.
                "coverage": {
                    "rows": envelope["source"]["rows"],
                    "drafts": declared_drafts,
                    "skipped": len(envelope["skipped"]),
                    "duplicates": duplicates,
                    "failed": failed,
                    "covered": envelope["source"]["rows"],
                    "verified": "row-index-set",
                    "ok": True,
                },
                "failures": [item for item in items if not item["ok"]],
            }
            self.batches.save(manifest)
            self.imports.mark_batched(envelope["source"].get("fileHash"), batch_id)
            self.audit(
                "batch_submitted",
                {
                    "id": batch_id,
                    "source": envelope["source"]["ref"],
                    "drafts": len(draft_ids),
                    "skipped": len(envelope["skipped"]),
                    "duplicates": duplicates,
                    "failed": failed,
                },
            )
            return {"batch": manifest, "items": items}

    def update(self, draft_id: str, payload: object, *, actor: str = "user") -> dict[str, Any]:
        """Update a pending draft in place and re-run validation."""
        if not isinstance(payload, dict):
            fail("invalid_draft", "a draft update must be a JSON object")
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            document = self.get(draft_id)
            if document["status"] != "pending":
                fail("draft_not_pending", f"draft {draft_id} is {document['status']}, not pending")
            merged = {
                "date": payload.get("date", document["date"]),
                "flag": payload.get("flag", document["flag"]),
                "payee": payload.get("payee", document["payee"]),
                "narration": payload.get("narration", document["narration"]),
                "metadata": payload.get("metadata", document.get("metadata", {})),
                "tags": payload.get("tags", document["tags"]),
                "links": payload.get("links", document["links"]),
                "postings": payload.get("postings", document["postings"]),
                "source": payload.get("source", document["source"]),
                "confidence": payload.get("confidence", document.get("confidence")),
            }
            draft, issues = normalize_draft_input(
                merged, state, today=self.today(), max_evidence_bytes=self.max_evidence_bytes
            )
            digest = hash_payload(draft)
            updated = {
                **document,
                **draft,
                "hash": digest,
                "updatedAt": _now(),
                "issues": issues,
            }
            atomic_write(self.draft_file(draft_id), json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            self.audit("draft_updated", {"id": draft_id, "hash": digest, "fields": sorted(payload), "actor": actor})
            approvable = not any(issue["code"] in BLOCKING_ISSUES for issue in issues)
            return {"draft": {**updated, "approvable": approvable}, "issues": issues}

    def list(self, *, status: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        state = self.reader.refresh()
        today = self.today()
        try:
            names = sorted(os.listdir(self.drafts_directory))
        except FileNotFoundError:
            return []
        documents = []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                document = json.loads((self.drafts_directory / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not is_draft_document(document):
                continue
            if status is not None and document.get("status") != status:
                continue
            issues, approvable = assess_draft(document, state, today=today)
            documents.append({**document, "issues": issues, "approvable": approvable})
        documents.sort(key=lambda document: document.get("createdAt", ""), reverse=True)
        return documents[: (limit if limit is not None else self.max_drafts)]

    def get(self, draft_id: str) -> dict[str, Any]:
        if not isinstance(draft_id, str) or re.match(r"^draft-[0-9]{8}-[a-f0-9]{6}$", draft_id) is None:
            fail("draft_not_found", f'invalid draft id "{draft_id}"')
        try:
            document = json.loads(self.draft_file(draft_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            fail("draft_not_found", f"draft {draft_id} does not exist")
        except ValueError:
            fail("draft_corrupt", f"draft {draft_id} is not valid JSON")
        if not is_draft_document(document):
            fail("draft_corrupt", f"draft {draft_id} does not match the draft schema")
        state = self.reader.refresh()
        issues, approvable = assess_draft(document, state, today=self.today())
        return {**document, "issues": issues, "approvable": approvable}

    def approve(self, *, draft_id: str, postings: object = None, actor: str = "user") -> dict[str, Any]:
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            document = self.get(draft_id)
            existing = _find_draft_location(state, draft_id)
            if existing is not None:
                if document["status"] == "pending":
                    recovered = {
                        **document,
                        "status": "approved",
                        "updatedAt": _now(),
                        "approved": {
                            "at": _now(),
                            "file": existing["relFile"],
                            "line": existing["line"],
                            "recovered": True,
                            "actor": actor,
                        },
                    }
                    atomic_write(self.draft_file(draft_id), json.dumps(recovered, ensure_ascii=False, indent=2) + "\n")
                    self.audit("draft_recovered", {"id": draft_id, "file": existing["relFile"]})
                    return {"draft": recovered, "committed": False, "recovered": True}
                return {"draft": document, "committed": False, "recovered": False}
            if document["status"] == "approved":
                return {"draft": document, "committed": False, "recovered": False}
            if document["status"] != "pending":
                fail("draft_not_pending", f"draft {draft_id} is {document['status']}, not pending")

            normalized = _normalize_approval_postings(postings, document, state)
            issues: list[dict[str, Any]] = []
            check_balanced(normalized, issues)
            if issues:
                fail("unbalanced", issues[0]["message"], {"differences": issues[0].get("differences", [])})
            for posting in normalized:
                if posting["account"] is None:
                    fail("unresolved_postings", "every posting must name a known account before approval")
                if not has_account(state, posting["account"]):
                    fail("unresolved_postings", f"{posting['account']} is not open in the ledger")
            if not any(is_funding_account(posting["account"]) for posting in normalized):
                fail(
                    "no_real_account",
                    "a transaction needs at least one asset, liability or equity adjustment posting",
                    {"postings": [posting["account"] for posting in normalized]},
                )

            block = render_transaction(
                {
                    "date": document["date"],
                    "flag": document["flag"],
                    "payee": document["payee"],
                    "narration": document["narration"],
                    "metadata": document.get("metadata") or {},
                    "tags": document["tags"],
                    "links": document["links"],
                    "draft_id": draft_id,
                    "postings": normalized,
                }
            )
            target = resolve_append_target(self.root, "entries")
            if contains_draft_id(target["content"], draft_id):
                if target["index_changed"]:
                    atomic_write(target["index_file"], target["index_content"])
                self.reader.invalidate()
                index = target["content"].find(f'draft_id: "{draft_id}"')
                recovered = {
                    **document,
                    "status": "approved",
                    "updatedAt": _now(),
                    "approved": {
                        "at": _now(),
                        "file": target["rel_file"],
                        "line": target["content"][:index].count("\n") + 1,
                        "recovered": True,
                        "actor": actor,
                    },
                }
                atomic_write(self.draft_file(draft_id), json.dumps(recovered, ensure_ascii=False, indent=2) + "\n")
                self.audit("draft_recovered", {"id": draft_id, "file": target["rel_file"], "source": "target-file"})
                return {"draft": recovered, "committed": False, "recovered": True}

            next_content = append_block(target["content"], block)
            line = next_content[: next_content.rfind(block)].count("\n") + 1
            overrides = {target["rel_file"]: next_content}
            if target["index_changed"]:
                overrides[target["index_rel"]] = target["index_content"]
            executable = self.bean_check() if callable(self.bean_check) else self.bean_check
            validation = validate_ledger(
                root=self.root,
                main_file=self.reader.main_file,
                executable=executable,
                overrides=overrides,
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            extra_writes: dict[str, str] = {}
            auto_pad: dict[str, Any] | None = None
            if not validation["ok"] and self.approval_hook is not None:
                hook_result = self.approval_hook(document, normalized, overrides, validation)
                if hook_result and hook_result.get("error"):
                    failure = hook_result["error"]
                    fail(failure.get("code", "bean_check_failed"), failure.get("message", "the transaction was rejected"), failure.get("details") or {})
                if hook_result and hook_result.get("writes"):
                    merged = {**overrides, **hook_result["writes"]}
                    retry = validate_ledger(
                        root=self.root,
                        main_file=self.reader.main_file,
                        executable=executable,
                        overrides=merged,
                        timeout=self.timeout,
                        exclude=self.exclude_entries,
                    )
                    if retry["ok"]:
                        validation = retry
                        overrides = merged
                        extra_writes = dict(hook_result["writes"])
                        auto_pad = hook_result.get("autoPad")
            if not validation["ok"]:
                fail("bean_check_failed", f"the transaction was rejected by bean-check: {validation['diagnostic']}", {"diagnostic": validation["diagnostic"]})

            atomic_write(target["file"], next_content)
            for rel_file, content in extra_writes.items():
                atomic_write(self.root / rel_file, content)
            if target["index_changed"]:
                atomic_write(target["index_file"], target["index_content"])
            approved = {
                **document,
                "postings": normalized,
                "status": "approved",
                "updatedAt": _now(),
                "approved": {"at": _now(), "file": target["rel_file"], "line": line, "actor": actor},
            }
            atomic_write(self.draft_file(draft_id), json.dumps(approved, ensure_ascii=False, indent=2) + "\n")
            self.audit("draft_approved", {"id": draft_id, "file": target["rel_file"], "line": line, "actor": actor})
            if auto_pad is not None and self.approval_finalize is not None:
                self.approval_finalize(auto_pad)
            self.reader.invalidate()
            return {"draft": approved, "committed": True, "recovered": False, "autoPad": auto_pad}

    def reject(self, *, draft_id: str, reason: str = "", actor: str = "user") -> dict[str, Any]:
        with write_lock(self.lock_file):
            document = self.get(draft_id)
            if document["status"] != "pending":
                fail("draft_not_pending", f"draft {draft_id} is {document['status']}, not pending")
            reason_text = reason.strip()[:500] if isinstance(reason, str) else ""
            rejected = {
                **document,
                "status": "rejected",
                "updatedAt": _now(),
                "rejected": {"at": _now(), "reason": reason_text, "actor": actor},
            }
            atomic_write(self.draft_file(draft_id), json.dumps(rejected, ensure_ascii=False, indent=2) + "\n")
            self.audit("draft_rejected", {"id": draft_id, "reason": reason_text, "actor": actor})
            return rejected

    def summary(self) -> dict[str, int]:
        documents = self.list(limit=self.max_drafts)
        summary = {"pending": 0, "approved": 0, "rejected": 0}
        for document in documents:
            if document["status"] in summary:
                summary[document["status"]] += 1
        return summary

    def draft_file(self, draft_id: str) -> Path:
        return self.drafts_directory / f"{draft_id}.json"

    def refresh_resolutions(self) -> dict[str, Any]:
        """Re-resolve pending drafts against the current ledger.

        A draft submitted before its funding account existed stores
        ``account: null`` plus the raw ``suggested`` name. Once a structure
        change is approved, those names may resolve; this rewrites only the
        draft queue (never the ledger) so approval stops being blocked by stale
        lookups.
        """
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today()
            resolved = 0
            touched = 0
            for document in self.list(status="pending", limit=self.max_drafts):
                postings = document.get("postings") or []
                updated_postings = []
                changed = False
                for posting in postings:
                    account = posting.get("account")
                    if account is None:
                        suggestion = posting.get("suggested")
                        resolution = resolve_name(state, suggestion) if isinstance(suggestion, str) and suggestion else None
                        if resolution is not None:
                            updated_postings.append({**posting, "account": resolution["account"], "suggested": None})
                            changed = True
                            resolved += 1
                            continue
                    updated_postings.append(posting)
                if not changed:
                    continue
                candidate = {**document, "postings": updated_postings}
                issues, approvable = assess_draft(candidate, state, today=today)
                rewritten = {
                    **candidate,
                    "hash": hash_payload(candidate),
                    "issues": issues,
                    "approvable": approvable,
                    "updatedAt": _now(),
                }
                atomic_write(self.draft_file(document["id"]), json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n")
                touched += 1
            if touched:
                self.audit("drafts_resolved", {"drafts": touched, "postings": resolved})
            return {"drafts": touched, "postings": resolved}

    def batch_summaries(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List batches with counts, totals and touched funding accounts."""
        manifests = self.batches.list(limit=limit)
        if not manifests:
            return []
        documents = self.list(limit=self.max_drafts)
        by_batch: dict[str, list[dict[str, Any]]] = {}
        for document in documents:
            batch_id = document.get("batch")
            if isinstance(batch_id, str):
                by_batch.setdefault(batch_id, []).append(document)
        state = self.reader.refresh()
        summaries = []
        for manifest in manifests:
            items = by_batch.get(manifest["id"], [])
            counts = {"pending": 0, "approved": 0, "rejected": 0, "voided": 0}
            expense = 0
            credit = 0
            income = 0
            funding = 0
            accounts: dict[str, str] = {}
            for document in items:
                status = document.get("status", "pending")
                if document.get("voided"):
                    counts["voided"] += 1
                elif status in counts:
                    counts[status] += 1
                for posting in document.get("postings") or []:
                    account = posting.get("account")
                    if not isinstance(account, str):
                        continue
                    root = account.split(":", 1)[0]
                    amount = to_scaled(posting.get("amount") or "0")
                    if root == "Expenses":
                        if amount >= 0:
                            expense += amount
                        else:
                            credit += -amount
                    elif root == "Income":
                        income += -amount
                    elif is_funding_account(account):
                        funding += amount
                        accounts[account] = project_name(state, account)
            if manifest.get("voidedAt"):
                status = "voided"
            elif counts["pending"] > 0:
                status = "pending"
            elif counts["approved"] > 0:
                status = "approved"
            elif counts["rejected"] > 0:
                status = "rejected"
            else:
                status = "empty"
            manifest_skipped = manifest.get("skipped") or []
            summaries.append(
                {
                    **manifest,
                    "skipped": manifest_skipped[:200],
                    "skippedCount": len(manifest_skipped),
                    "status": status,
                    "countsByStatus": counts,
                    "totals": {
                        "expense": from_scaled(expense),
                        "credit": from_scaled(credit),
                        "income": from_scaled(income),
                        "funding": from_scaled(funding),
                        "netOutflow": from_scaled(-funding),
                    },
                    "accounts": [
                        {"account": account, "name": name}
                        for account, name in sorted(accounts.items())
                    ],
                    "coverage": coverage_of(manifest),
                }
            )
        return summaries

    def reject_batch(self, batch_id: str, *, reason: str = "", actor: str = "user") -> dict[str, Any]:
        """Reject pending drafts of one batch; approved drafts are untouched."""
        manifest = self.batches.read(batch_id)
        reason_text = reason.strip()[:500] if isinstance(reason, str) else ""
        with write_lock(self.lock_file):
            documents = self.list(limit=self.max_drafts)
            targets = [
                document
                for document in documents
                if document.get("batch") == batch_id and document["status"] == "pending"
            ]
            for document in targets:
                rejected = {
                    **document,
                    "status": "rejected",
                    "updatedAt": _now(),
                    "rejected": {"at": _now(), "reason": reason_text, "actor": actor},
                }
                atomic_write(self.draft_file(document["id"]), json.dumps(rejected, ensure_ascii=False, indent=2) + "\n")
                self.audit("draft_rejected", {"id": document["id"], "reason": reason_text, "actor": actor, "batch": batch_id})
            if targets:
                manifest = {
                    **manifest,
                    "rejectedAt": _now(),
                    "rejectReason": reason_text,
                }
                self.batches.save(manifest)
            self.audit("batch_rejected", {"id": batch_id, "drafts": len(targets), "actor": actor})
            return {"batch": manifest, "rejected": len(targets)}

    def void_batch(self, batch_id: str, *, actor: str = "user") -> dict[str, Any]:
        """Append reversing entries for every approved draft of one batch."""
        manifest = self.batches.read(batch_id)
        if manifest.get("voidedAt"):
            fail("batch_already_voided", f"batch {batch_id} is already voided")
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today()
            documents = self.list(limit=self.max_drafts)
            pending = [
                document
                for document in documents
                if document.get("batch") == batch_id and document["status"] == "pending"
            ]
            if pending:
                fail(
                    "batch_has_pending",
                    f"batch {batch_id} still has {len(pending)} pending drafts; reject them before voiding",
                    {"batch": batch_id, "pending": len(pending)},
                )
            targets = [
                document
                for document in documents
                if document.get("batch") == batch_id and document["status"] == "approved" and not document.get("voided")
            ]
            if not targets:
                fail("batch_not_approved", f"batch {batch_id} has no approved drafts to void")
            blocks: list[tuple[dict[str, Any], str]] = []
            for document in sorted(targets, key=lambda item: item.get("date", "")):
                postings = document.get("postings") or []
                reversed_postings = []
                for posting in postings:
                    account = posting.get("account")
                    if not isinstance(account, str):
                        fail("draft_corrupt", f'draft {document["id"]} still has an unresolved posting')
                    reversed_postings.append(
                        {
                            "account": account,
                            "amount": from_scaled(-to_scaled(posting.get("amount") or "0")),
                            "currency": posting.get("currency") or "CNY",
                        }
                    )
                narration = f"冲销：{document.get('narration') or ''}"[:500]
                block = render_transaction(
                    {
                        "date": today,
                        "flag": "!",
                        "payee": document.get("payee") or "",
                        "narration": narration,
                        "metadata": {"void_of": document["id"], "batch": batch_id},
                        "tags": [],
                        "links": [],
                        "postings": reversed_postings,
                    }
                )
                blocks.append((document, block))
            target = resolve_append_target(self.root, "entries")
            next_content = target["content"]
            for _, block in blocks:
                next_content = append_block(next_content, block)
            lines: dict[str, int] = {}
            cursor = 0
            for document, block in blocks:
                index = next_content.find(block, cursor)
                lines[document["id"]] = next_content[:index].count("\n") + 1
                cursor = index + len(block)
            overrides = {target["rel_file"]: next_content}
            if target["index_changed"]:
                overrides[target["index_rel"]] = target["index_content"]
            executable = self.bean_check() if callable(self.bean_check) else self.bean_check
            validation = validate_ledger(
                root=self.root,
                main_file=self.reader.main_file,
                executable=executable,
                overrides=overrides,
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            if not validation["ok"]:
                fail(
                    "bean_check_failed",
                    f"the void was rejected by bean-check: {validation['diagnostic']}",
                    {"diagnostic": validation["diagnostic"]},
                )
            atomic_write(target["file"], next_content)
            if target["index_changed"]:
                atomic_write(target["index_file"], target["index_content"])
            for document, _ in blocks:
                voided = {
                    **document,
                    "voided": {
                        "at": _now(),
                        "file": target["rel_file"],
                        "line": lines[document["id"]],
                        "actor": actor,
                    },
                }
                atomic_write(self.draft_file(document["id"]), json.dumps(voided, ensure_ascii=False, indent=2) + "\n")
            manifest = {**manifest, "voidedAt": _now(), "voidedBy": actor}
            self.batches.save(manifest)
            self.audit("batch_voided", {"id": batch_id, "entries": len(blocks), "actor": actor})
            self.reader.invalidate()
            return {"batch": manifest, "voided": len(blocks)}

    def allocate_id(self, today: str) -> str:
        self.ensure_directories()
        prefix = f"draft-{today.replace('-', '')}-"
        for _ in range(8):
            candidate = f"{prefix}{secrets.token_hex(3)}"
            if not self.draft_file(candidate).exists():
                return candidate
        fail("draft_id_exhausted", "cannot allocate a unique draft id")
