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
from .bean import (
    TAG_RE,
    append_block,
    contains_draft_id,
    is_currency,
    render_transaction,
)
from .dates import add_days, is_date, iso, today_in_zone
from .errors import fail
from .ledger import has_account, resolve_name
from .target import resolve_append_target
from .validation import atomic_write, validate_ledger, write_lock

FLAGS = {"*", "!"}
SOURCE_KINDS = {"ai", "quickpath", "manual", "import", "external"}
BLOCKING_ISSUES = {"unresolved_account", "unbalanced", "before_account_open", "closed_account"}
CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]")


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
            issues.append(
                {
                    "code": "unresolved_account",
                    "message": f'postings[{index}]: "{raw_account}" is not a known account or alias',
                    "postingIndex": index,
                    "suggested": raw_account,
                }
            )
        else:
            account = resolution["account"]
    else:
        suggested = raw_suggested
        issues.append(
            {
                "code": "unresolved_account",
                "message": f"postings[{index}]: unclassified ({raw_suggested})",
                "postingIndex": index,
                "suggested": raw_suggested,
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
                }
            )
        closed = state["closes"].get(account)
        if closed is not None and closed["date"] <= draft["date"]:
            issues.append(
                {
                    "code": "closed_account",
                    "message": f"postings[{index}]: {account} is closed on the draft date",
                    "postingIndex": index,
                }
            )
    return {"account": account, "suggested": suggested, "amount": amount, "currency": currency}


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

    tags = _normalize_tokens(payload.get("tags"), "tags", 10)
    links = _normalize_tokens(payload.get("links"), "links", 10)

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
        if account is None:
            issues.append(
                {
                    "code": "unresolved_account",
                    "message": f"postings[{index}]: unclassified",
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
        self.audit_file = self.state_directory / "audit.jsonl"
        self.lock_file = self.state_directory / "write.lock"

    def today(self) -> str:
        return iso(today_in_zone(self.time_zone, self.now()))

    def ensure_directories(self) -> None:
        self.drafts_directory.mkdir(parents=True, exist_ok=True)

    def audit(self, action: str, payload: dict[str, Any]) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": _now(), "action": action, **payload}, ensure_ascii=False)
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def submit(self, payload: object) -> dict[str, Any]:
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today()
            draft, issues = normalize_draft_input(
                payload, state, today=today, max_evidence_bytes=self.max_evidence_bytes
            )
            digest = hash_payload(draft)
            pending = self.list(status="pending", limit=self.max_drafts)
            previous = next((candidate for candidate in pending if candidate.get("hash") == digest), None)
            if previous is not None:
                return {"draft": previous, "issues": previous.get("issues", []), "deduplicated": True}
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
                "issues": issues,
            }
            atomic_write(self.draft_file(draft_id), json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            self.audit("draft_submitted", {"id": draft_id, "hash": digest, "source": draft["source"]["kind"]})
            approvable = not any(issue["code"] in BLOCKING_ISSUES for issue in issues)
            return {"draft": {**document, "approvable": approvable}, "issues": issues, "deduplicated": False}

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

            block = render_transaction(
                {
                    "date": document["date"],
                    "flag": document["flag"],
                    "payee": document["payee"],
                    "narration": document["narration"],
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
            if not validation["ok"]:
                fail("bean_check_failed", f"the transaction was rejected by bean-check: {validation['diagnostic']}", {"diagnostic": validation["diagnostic"]})

            atomic_write(target["file"], next_content)
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
            self.reader.invalidate()
            return {"draft": approved, "committed": True, "recovered": False}

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

    def allocate_id(self, today: str) -> str:
        self.ensure_directories()
        prefix = f"draft-{today.replace('-', '')}-"
        for _ in range(8):
            candidate = f"{prefix}{secrets.token_hex(3)}"
            if not self.draft_file(candidate).exists():
                return candidate
        fail("draft_id_exhausted", "cannot allocate a unique draft id")
