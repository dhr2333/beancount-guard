"""The pending queue for ledger *structure* writes.

Transactions already travel through the draft queue. Accounts, metadata and
reconciliation are different: when the human clicks them in the panel the click
itself is the confirmation, so they take effect directly. When an Agent proposes
them there is no human in the loop yet, so they have to park here and wait.

This module only owns the queue and its state. The actual write is still done by
the matching ``BeancountGuard`` method at approval time, which is what keeps the
shadow-tree validation and the write lock in one place.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .amounts import assert_amount, normalize_amount
from .bean import is_account_name, is_currency, normalize_alias, assert_alias
from .dates import is_date, is_period, iso, today_in_zone
from .errors import fail
from .validation import atomic_write, write_lock

KINDS = {"account-open", "account-redate", "account-metadata", "balance"}
MAX_OPERATIONS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object, label: str, *, maximum: int = 200, allow_empty: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        fail("invalid_operation", f"{label} must be a string")
    text = value.strip()
    if not allow_empty and text == "":
        fail("invalid_operation", f"{label} must not be empty")
    if len(text) > maximum:
        fail("invalid_operation", f"{label} must be at most {maximum} characters")
    return text


def normalize_operation(payload: object, state: dict[str, Any], *, today: str) -> dict[str, Any]:
    """Shape-check one operation and return ``(operation, issues)``."""
    if not isinstance(payload, dict):
        fail("invalid_operation", "an operation must be a JSON object")
    kind = payload.get("kind")
    if kind not in KINDS:
        fail("invalid_operation", f"kind must be one of {', '.join(sorted(KINDS))}")
    body = payload.get("payload")
    if not isinstance(body, dict):
        fail("invalid_operation", "payload must be a JSON object")
    issues: list[dict[str, Any]] = []
    normalized: dict[str, Any] = {}

    if kind == "account-open":
        account = _text(body.get("account"), "account", allow_empty=False)
        if not is_account_name(account):
            fail("invalid_account", f'"{account}" is not a Beancount account')
        currencies = body.get("currencies")
        if currencies is None and isinstance(body.get("currency"), str):
            currencies = [body["currency"]]
        if not isinstance(currencies, list) or not currencies:
            fail("invalid_operation", "currencies must be a non-empty array")
        cleaned: list[str] = []
        for currency in currencies:
            if not isinstance(currency, str) or not is_currency(currency.strip()):
                fail("invalid_currency", f'"{currency}" is not a Beancount currency')
            if currency.strip() not in cleaned:
                cleaned.append(currency.strip())
        open_date = _text(body.get("date") or today, "date", allow_empty=False)
        if not is_date(open_date):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        if open_date > today:
            fail("future_date", "an open directive cannot be dated in the future", {"date": open_date, "today": today})
        alias = None
        if body.get("alias") is not None:
            alias = assert_alias(_text(body.get("alias"), "alias", allow_empty=False))
        reconcile = None
        if body.get("reconcile") is not None:
            reconcile = _text(body.get("reconcile"), "reconcile", allow_empty=False)
            if not is_period(reconcile):
                fail("invalid_period", f'"{reconcile}" is not a supported reconciliation period')
            if account.split(":", 1)[0] not in {"Assets", "Liabilities"}:
                fail("not_reconcilable", "only asset and liability accounts can be reconciled", {"account": account})
        if account in state["opens"] or account in state["implicit"]:
            issues.append({"code": "account_exists", "message": f"{account} already exists", "account": account})
        if alias is not None:
            normalized_alias = normalize_alias(alias)
            clashes = [entry for entry in state["aliases"].get(normalized_alias, []) if entry["account"] != account]
            if clashes:
                issues.append({"code": "alias_conflict", "message": f'alias "{alias}" is already in use', "account": account, "alias": alias})
        normalized = {
            "account": account,
            "date": open_date,
            "currencies": cleaned,
            "alias": alias,
            "reconcile": reconcile,
        }
        summary = f"开户 {account}"
        if alias:
            summary += f"（{alias}）"

    elif kind == "account-redate":
        target = _text(body.get("date"), "date", allow_empty=False)
        if not is_date(target):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        if target > today:
            fail("future_date", "an open directive cannot be dated in the future", {"date": target, "today": today})
        all_accounts = body.get("all") is True
        raw_accounts = body.get("accounts") or []
        if not isinstance(raw_accounts, list):
            fail("invalid_operation", "accounts must be an array")
        accounts = [_text(item, "accounts[]", allow_empty=False) for item in raw_accounts]
        if not all_accounts and not accounts:
            fail("invalid_operation", "pass accounts or all: true")
        if all_accounts:
            normalized = {"date": target, "all": True, "accounts": []}
            summary = f"全部账户开立日前移到 {target}"
        else:
            normalized = {"date": target, "all": False, "accounts": accounts}
            summary = f"{len(accounts)} 个账户开立日前移到 {target}"
        for name in accounts:
            if name not in state["opens"] and name not in state["implicit"]:
                issues.append({"code": "account_not_found", "message": f"{name} is not a known account", "account": name})

    elif kind == "account-metadata":
        account = _text(body.get("account"), "account", allow_empty=False)
        values: dict[str, Any] = {}
        if "alias" in body:
            values["alias"] = None if body["alias"] is None else assert_alias(_text(body["alias"], "alias", allow_empty=False))
        if "reconcile" in body:
            reconcile = body["reconcile"]
            if reconcile is not None:
                reconcile = _text(reconcile, "reconcile", allow_empty=False)
                if not is_period(reconcile):
                    fail("invalid_period", f'"{reconcile}" is not a supported reconciliation period')
            values["reconcile"] = reconcile
        if not values:
            fail("invalid_argument", "set alias and/or reconcile")
        if account not in state["opens"]:
            issues.append({"code": "account_not_opened", "message": f"{account} has no open directive", "account": account})
        normalized = {"account": account, **values}
        summary = f"设置 {account} 的" + "/".join(sorted(values))

    else:  # balance
        account = _text(body.get("account"), "account", allow_empty=False)
        amount = normalize_amount(assert_amount(body.get("amount"), "amount"))
        currency = _text(body.get("currency") or state.get("operating_currency") or "CNY", "currency", allow_empty=False)
        if not is_currency(currency):
            fail("invalid_currency", f'"{currency}" is not a Beancount currency')
        when = _text(body.get("date") or today, "date", allow_empty=False)
        if not is_date(when):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        if account not in state["opens"] and account not in state["implicit"]:
            issues.append({"code": "account_not_found", "message": f"{account} is not a known account", "account": account})
        root_name = account.split(":", 1)[0] if ":" in account else account
        if root_name not in {"Assets", "Liabilities"}:
            issues.append({"code": "not_reconcilable", "message": "only asset and liability accounts can be reconciled", "account": account})
        normalized = {
            "account": account,
            "amount": amount,
            "currency": currency,
            "date": when,
            "note": _text(body.get("note"), "note"),
        }
        summary = f"对账 {account} = {amount} {currency}"

    operation = {
        "kind": kind,
        "payload": normalized,
        "summary": summary,
        "reason": _text(payload.get("reason"), "reason", maximum=500),
        "proposedBy": _text(payload.get("proposedBy") or "agent", "proposedBy", maximum=64) or "agent",
    }
    digest = hashlib.sha256(
        json.dumps({"kind": kind, "payload": normalized}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    operation["hash"] = digest
    return {"operation": operation, "issues": issues}


class OperationStore:
    """One JSON file per pending operation, alongside the draft queue."""

    def __init__(
        self,
        *,
        root: str | Path,
        reader: Any,
        state_dir: str = ".ledger",
        time_zone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.reader = reader
        self.time_zone = time_zone
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state_directory = self.root / state_dir
        self.operations_directory = self.state_directory / "operations"
        self.audit_file = self.state_directory / "audit.jsonl"
        self.lock_file = self.state_directory / "write.lock"

    def today(self) -> str:
        return iso(today_in_zone(self.time_zone, self.now()))

    def ensure_directories(self) -> None:
        self.operations_directory.mkdir(parents=True, exist_ok=True)

    def audit(self, action: str, payload: dict[str, Any]) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": _now(), "action": action, **payload}, ensure_ascii=False)
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def operation_file(self, operation_id: str) -> Path:
        return self.operations_directory / f"{operation_id}.json"

    def allocate_id(self, today: str) -> str:
        self.ensure_directories()
        prefix = f"op-{today.replace('-', '')}-"
        for _ in range(8):
            candidate = f"{prefix}{secrets.token_hex(3)}"
            if not self.operation_file(candidate).exists():
                return candidate
        fail("operation_id_exhausted", "cannot allocate a unique operation id")

    def get(self, operation_id: str) -> dict[str, Any]:
        if not isinstance(operation_id, str) or not operation_id.startswith("op-") or "/" in operation_id:
            fail("invalid_operation", "a valid operation id is required")
        path = self.operation_file(operation_id)
        if not path.is_file():
            fail("operation_not_found", f"no operation {operation_id}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            fail("corrupt_operation", f"operation {operation_id} is not valid JSON: {error}")
        return document

    def list(self, *, status: str | None = None, limit: int = MAX_OPERATIONS) -> list[dict[str, Any]]:
        if not self.operations_directory.is_dir():
            return []
        documents = []
        for path in sorted(self.operations_directory.glob("op-*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if status is not None and document.get("status") != status:
                continue
            documents.append(document)
        documents.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
        return documents[: max(1, min(int(limit or MAX_OPERATIONS), MAX_OPERATIONS))]

    def summary(self) -> dict[str, int]:
        summary = {"pending": 0, "approved": 0, "rejected": 0}
        for document in self.list(limit=MAX_OPERATIONS):
            if document.get("status") in summary:
                summary[document["status"]] += 1
        return summary

    def submit(self, payload: object) -> dict[str, Any]:
        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today()
            prepared = normalize_operation(payload, state, today=today)
            operation = prepared["operation"]
            previous = next(
                (
                    candidate
                    for candidate in self.list(status="pending")
                    if candidate.get("hash") == operation["hash"]
                ),
                None,
            )
            if previous is not None:
                return {"operation": previous, "deduplicated": True}
            self.ensure_directories()
            operation_id = self.allocate_id(today)
            timestamp = _now()
            document = {
                "id": operation_id,
                "version": 1,
                "status": "pending",
                "createdAt": timestamp,
                "updatedAt": timestamp,
                **operation,
                "issues": prepared["issues"],
            }
            atomic_write(self.operation_file(operation_id), json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            self.audit("operation_submitted", {"id": operation_id, "kind": operation["kind"], "hash": operation["hash"]})
            return {"operation": document, "deduplicated": False}

    def mark(self, operation_id: str, status: str, *, actor: str = "user", detail: dict[str, Any] | None = None) -> dict[str, Any]:
        if status not in {"approved", "rejected"}:
            fail("invalid_argument", "status must be approved or rejected")
        with write_lock(self.lock_file):
            document = self.get(operation_id)
            if document.get("status") != "pending":
                fail("operation_not_pending", f"operation {operation_id} is {document.get('status')}, not pending")
            timestamp = _now()
            updated = {
                **document,
                "status": status,
                "updatedAt": timestamp,
                status: {"at": timestamp, "actor": actor, **(detail or {})},
            }
            atomic_write(self.operation_file(operation_id), json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            self.audit(f"operation_{status}", {"id": operation_id, "kind": document.get("kind"), "actor": actor})
            return updated
