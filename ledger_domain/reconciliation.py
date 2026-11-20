"""Reconciliation preview / commit / update / history.

The reconciliation form is the only thing that writes ``pad`` and ``balance``
directives. This module owns that write protocol: a pure preview, a locked
commit, the "same as_of update" path, and the auto-pad bridge used when a late
draft would otherwise break an existing balance assertion.

The ledger itself remains the only source of truth. History under
``.ledger/reconciliations/`` is a replayable audit cache, not a second ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .amounts import from_scaled, normalize_amount, parse_balance_failure, to_scaled
from .bean import append_block, is_account_name, is_currency, render_transaction
from .dates import add_days, add_period, diff_days, is_date, is_period, iso, parse_date, today_in_zone
from .errors import LedgerError, fail
from .ledger import is_closed_on, project_name, read_ledger_text, resolve_name
from .query import query_account_activity, query_account_balance
from .target import resolve_append_target
from .validation import atomic_write, validate_ledger, write_lock

RECONCILABLE_ROOTS = {"Assets", "Liabilities"}
CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]")
MAX_ALLOCATIONS = 50
MAX_TAGS = 10
MAX_NOTE = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _amount_total(amounts: list[str]) -> int:
    return sum((to_scaled(item) for item in amounts), 0)


def _root(account: str) -> str:
    return account.split(":", 1)[0]


def ledger_revision(state: dict[str, Any]) -> str:
    """SHA-256 over the include closure: relative path + bytes, sorted."""
    root = Path(state["root"]).resolve()
    digest = hashlib.sha256()
    for file in sorted(state["files"]):
        path = Path(file).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        try:
            content = Path(file).read_bytes()
        except OSError:
            content = b""
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _json_hash(value: object) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _prose(value: object, label: str, *, maximum: int, allow_empty: bool = False) -> str:
    text = "" if value is None else value
    if not isinstance(text, str):
        fail("invalid_allocation", f"{label} must be a string")
    text = text.strip()
    if text == "" and not allow_empty:
        fail("invalid_allocation", f"{label} must not be empty")
    if CONTROL_RE.search(text):
        fail("invalid_allocation", f"{label} must not contain control or bidirectional-override characters")
    if len(text) > maximum:
        fail("invalid_allocation", f"{label} must be at most {maximum} characters")
    return text


def _tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        fail("invalid_allocation", f"tags must be an array of at most {MAX_TAGS} entries")
    cleaned = []
    for tag in value:
        if not isinstance(tag, str) or re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", tag.strip()) is None:
            fail("invalid_allocation", "tags contain an invalid Beancount tag")
        cleaned.append(tag.strip())
    return cleaned


class ReconciliationStore:
    """One JSON file per reconciliation under ``.ledger/reconciliations``."""

    def __init__(self, root: str | Path, state_dir: str = ".ledger") -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / state_dir / "reconciliations"

    def file(self, record_id: str) -> Path:
        return self.directory / f"{record_id}.json"

    def list(self, *, account: str | None = None, currency: str | None = None) -> list[dict[str, Any]]:
        try:
            names = sorted(self.directory.glob("*.json"))
        except OSError:
            return []
        records = []
        for path in names:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            if account is not None and record.get("account") != account:
                continue
            if currency is not None and record.get("currency") != currency:
                continue
            records.append(record)
        records.sort(key=lambda record: (record.get("asOf", ""), record.get("committedAt", ""), record.get("id", "")), reverse=True)
        return records

    def get(self, record_id: str) -> dict[str, Any] | None:
        if not isinstance(record_id, str) or re.match(r"^rec-[A-Za-z0-9-]+$", record_id) is None:
            return None
        try:
            record = json.loads(self.file(record_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) and record.get("id") == record_id else None

    def latest_active(self, account: str, currency: str) -> dict[str, Any] | None:
        for record in self.list(account=account, currency=currency):
            if record.get("status", "active") == "active":
                return record
        return None

    def write(self, record: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(self.file(record["id"]), json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def supersede(self, record_id: str, new_id: str) -> None:
        record = self.get(record_id)
        if record is None:
            return
        updated = {**record, "status": "superseded", "supersededBy": new_id, "supersededAt": _now()}
        self.write(updated)


class Reconciler:
    """Domain-facing reconciliation service."""

    def __init__(self, domain: Any) -> None:
        self.domain = domain
        self.store = ReconciliationStore(domain.root, domain.state_dir)

    # ----------------------------------------------------------------- helpers

    def _today(self) -> date:
        return self.domain.today()

    def _request(self, payload: object, *, for_commit: bool = False) -> dict[str, Any]:
        if not isinstance(payload, dict):
            fail("invalid_argument", "a reconciliation request must be a JSON object")
        account = payload.get("account")
        if not isinstance(account, str) or account.strip() == "":
            fail("invalid_argument", "account is required")
        mode = payload.get("mode") or ("create" if for_commit else "auto")
        if mode not in {"auto", "create", "update"}:
            fail("invalid_argument", "mode must be auto, create or update")
        if for_commit and mode == "auto":
            fail("invalid_argument", "commit mode must be create or update")
        as_of_text = payload.get("asOf") or payload.get("as_of") or self.domain.today_text()
        if not is_date(as_of_text):
            fail("invalid_date", "asOf must be a real YYYY-MM-DD date")
        actual = payload.get("actual")
        actual_text = None
        if actual is not None:
            actual_text = normalize_amount(actual if isinstance(actual, str) else fail("invalid_amount", "actual must be a decimal string"))
        currency = payload.get("currency")
        if currency is not None and not isinstance(currency, str):
            fail("invalid_currency", "currency must be a string")
        allocations = payload.get("allocations")
        if allocations is None:
            allocations = []
        if not isinstance(allocations, list):
            fail("invalid_allocation", "allocations must be an array")
        if len(allocations) > MAX_ALLOCATIONS:
            fail("invalid_allocation", f"allocations must have at most {MAX_ALLOCATIONS} entries")
        pad_account = payload.get("padAccount") or payload.get("pad_account")
        if pad_account is not None and not isinstance(pad_account, str):
            fail("invalid_allocation", "padAccount must be a string")
        replaces = payload.get("replaces")
        if replaces is not None and not isinstance(replaces, dict):
            fail("invalid_argument", "replaces must be an object")
        return {
            "account": account.strip(),
            "mode": mode,
            "asOf": as_of_text,
            "actual": actual_text,
            "currency": currency.strip() if isinstance(currency, str) else None,
            "allocations": allocations,
            "padAccount": pad_account.strip() if isinstance(pad_account, str) and pad_account.strip() else None,
            "replaces": replaces,
            "previewId": payload.get("previewId"),
            "ledgerRevision": payload.get("ledgerRevision"),
            "inputsHash": payload.get("inputsHash"),
            "directivesHash": payload.get("directivesHash"),
            "idempotencyKey": payload.get("idempotencyKey"),
        }

    def _resolve(self, state: dict[str, Any], request: dict[str, Any], *, today: date) -> dict[str, Any]:
        resolution = resolve_name(state, request["account"])
        if resolution is None:
            fail("account_not_found", f'"{request["account"]}" is not a known account or alias')
        account = resolution["account"]
        if _root(account) not in RECONCILABLE_ROOTS:
            fail("not_reconcilable", "only asset and liability accounts can be reconciled", {"account": account})
        opened = state["opens"].get(account)
        if opened is None:
            fail("account_not_opened", f"{account} has no open directive; implicit accounts cannot be reconciled", {"account": account, "implicit": True})
        descendants = [name for name in [*state["opens"], *state["implicit"]] if name.startswith(account + ":")]
        if descendants:
            fail("account_has_children", f"{account} has child accounts; MVP-1 reconciles leaf accounts only", {"account": account, "children": sorted(descendants)[:20]})
        as_of_text = request["asOf"]
        as_of = parse_date(as_of_text)
        if as_of > today:
            fail("future_date", "asOf cannot be in the future", {"date": as_of_text, "today": iso(today)})
        if as_of < parse_date(opened["date"]):
            fail("before_account_open", f"{account} opens on {opened['date']}", {"openDate": opened["date"]})
        if is_closed_on(state, account, as_of_text):
            fail("closed_account", f"{account} is closed on {as_of_text}", {"account": account})
        currencies = opened.get("currencies") or []
        if len(currencies) > 1:
            fail("multiple_currencies", f"{account} has multiple opened currencies; MVP-1 supports one currency per account", {"account": account, "currencies": currencies})
        currency = request["currency"] or (currencies[0] if currencies else (state.get("operating_currency") or "CNY"))
        if not is_currency(currency):
            fail("invalid_currency", f'"{currency}" is not a Beancount currency')
        if currencies and currency not in currencies:
            fail("invalid_currency", f"{account} is not opened for {currency}", {"allowed": currencies, "currency": currency})
        return {
            "account": account,
            "name": project_name(state, account),
            "opened": opened,
            "asOf": as_of_text,
            "asOfDate": as_of,
            "currency": currency,
        }

    def _balances(self, state: dict[str, Any], account: str, currency: str) -> list[dict[str, Any]]:
        return [
            entry
            for entry in state["balances"]
            if entry["account"] == account and entry["currency"] == currency
        ]

    def _latest_balance(self, state: dict[str, Any], account: str, currency: str) -> dict[str, Any] | None:
        balances = self._balances(state, account, currency)
        return max(balances, key=lambda entry: entry["date"]) if balances else None

    def _closed_through(self, state: dict[str, Any], account: str, currency: str, *, exclude_date: str | None = None) -> str | None:
        balances = [
            entry
            for entry in self._balances(state, account, currency)
            if exclude_date is None or entry["date"] != exclude_date
        ]
        if not balances:
            return None
        latest = max(entry["date"] for entry in balances)
        return iso(add_days(parse_date(latest), -1))

    def _mode(self, state: dict[str, Any], resolved: dict[str, Any], request: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        latest_record = self.store.latest_active(resolved["account"], resolved["currency"])
        requested = request["mode"]
        if latest_record is None:
            if requested == "update":
                fail("not_latest", "there is no active reconciliation to update", {"account": resolved["account"], "currency": resolved["currency"]})
            return "create", None
        if request["asOf"] == latest_record.get("asOf"):
            if requested == "create":
                fail("update_required", "the same asOf already has a reconciliation; commit with mode=update", {"reconciliation": latest_record["id"], "asOf": request["asOf"]})
            return "update", latest_record
        if request["asOf"] < latest_record.get("asOf", ""):
            fail("out_of_order", "a later reconciliation already exists; only the latest can be updated", {"latest": latest_record["id"], "latestAsOf": latest_record.get("asOf")})
        if requested == "update":
            fail("not_latest", "only the latest reconciliation can be updated", {"latest": latest_record["id"], "latestAsOf": latest_record.get("asOf")})
        return "create", latest_record

    def _base_overrides(self, state: dict[str, Any], record: dict[str, Any] | None) -> dict[str, str]:
        if record is None:
            return {}
        target_file = self.domain.root / "trans" / "reconciliation.bean"
        try:
            content = read_ledger_text(target_file)
        except OSError:
            fail("ledger_not_initialized", "the ledger has no trans/reconciliation.bean")
        block = record.get("directives")
        if not isinstance(block, str) or block == "" or block not in content:
            fail("update_conflict", "the reconciliation block is missing or was edited outside the core", {"reconciliation": record.get("id")})
        return {"trans/reconciliation.bean": content.replace(block, "", 1)}

    def _expected(self, state: dict[str, Any], resolved: dict[str, Any], record: dict[str, Any] | None) -> str:
        overrides = self._base_overrides(state, record)
        result = query_account_balance(
            root=str(self.domain.root),
            bean_file=self.domain.bean_file,
            executable=self.domain.query_program(),
            account=resolved["account"],
            currency=resolved["currency"],
            as_of=resolved["asOf"],
            overrides=overrides,
            timeout=self.domain.timeout,
        )
        if result.get("hasCost"):
            fail("unsupported_position", "accounts with cost/price positions are not supported in MVP-1", {"account": resolved["account"]})
        return result["amount"]

    def _previous_covered(self, state: dict[str, Any], resolved: dict[str, Any], record: dict[str, Any] | None) -> str | None:
        if record is not None:
            return self._closed_through(state, resolved["account"], resolved["currency"], exclude_date=record.get("balanceDate"))
        latest = self._latest_balance(state, resolved["account"], resolved["currency"])
        if latest is None:
            return None
        return iso(add_days(parse_date(latest["date"]), -1))

    def _default_pad_account(self, state: dict[str, Any], resolved: dict[str, Any], record: dict[str, Any] | None) -> str:
        if record is not None:
            first = bool(record.get("isFirstReconciliation"))
        else:
            first = not self._balances(state, resolved["account"], resolved["currency"])
        return "Equity:Opening-Balances" if first else "Equity:Adjustments"

    def _resolve_pad_account(self, state: dict[str, Any], requested: str, currency: str) -> str:
        resolution = resolve_name(state, requested)
        if resolution is None:
            fail("pad_account_missing", f'"{requested}" is not a known pad account')
        account = resolution["account"]
        if _root(account) != "Equity":
            fail("pad_account_not_equity", "the pad account must be an Equity account", {"account": account})
        opened = state["opens"].get(account)
        if opened is None:
            fail("pad_account_missing", f"{account} has no open directive", {"account": account})
        if opened.get("currencies") and currency not in opened["currencies"]:
            fail("pad_account_missing", f"{account} is not opened for {currency}", {"account": account, "currency": currency})
        return account

    def _normalize_allocations(
        self,
        state: dict[str, Any],
        resolved: dict[str, Any],
        rows: list[Any],
        *,
        previous_covered: str | None,
        pad_account_requested: str | None,
        difference: int | None,
        first: bool,
        record: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
        """Return normalized fixed/remainder rows, plus optional auto remainder."""
        normalized: list[dict[str, Any]] = []
        remainder_index = None
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                fail("invalid_allocation", f"allocations[{index}] must be an object")
            account_text = raw.get("account")
            if not isinstance(account_text, str) or account_text.strip() == "":
                fail("allocation_account_missing", f"allocations[{index}] needs an account")
            resolution = resolve_name(state, account_text.strip())
            if resolution is None:
                fail("allocation_account_missing", f'allocations[{index}]: "{account_text}" is not a known account', {"index": index})
            account = resolution["account"]
            if account == resolved["account"]:
                fail("allocation_account_same", "an allocation cannot target the reconciled account", {"index": index})
            if account not in state["opens"]:
                fail("allocation_account_missing", f'allocations[{index}]: {account} has no open directive', {"index": index})
            mode = raw.get("mode") or "fixed"
            if mode not in {"fixed", "remainder", "auto"}:
                fail("invalid_allocation", f"allocations[{index}].mode must be fixed or remainder")
            if mode in {"remainder", "auto"}:
                if remainder_index is not None:
                    fail("multiple_remainders", "at most one remainder allocation is allowed")
                remainder_index = len(normalized)
                normalized.append({"account": account, "mode": "remainder"})
                continue
            amount_text = raw.get("amount")
            if not isinstance(amount_text, str):
                fail("invalid_amount", f"allocations[{index}].amount must be a decimal string")
            amount = normalize_amount(amount_text)
            if to_scaled(amount) == 0:
                fail("invalid_amount", f"allocations[{index}].amount must not be zero")
            date_text = raw.get("date") or resolved["asOf"]
            if not is_date(date_text):
                fail("invalid_date", f"allocations[{index}].date must be a YYYY-MM-DD date")
            if date_text > resolved["asOf"]:
                fail("allocation_after_as_of", f"allocations[{index}].date is after asOf", {"index": index, "date": date_text})
            opened = state["opens"].get(account)
            if opened is not None and date_text < opened["date"]:
                fail("allocation_before_open", f"allocations[{index}]: {account} opens on {opened['date']}", {"index": index})
            if is_closed_on(state, account, date_text):
                fail("allocation_account_closed", f"allocations[{index}]: {account} is closed on {date_text}", {"index": index})
            if previous_covered is not None and date_text <= previous_covered:
                fail("allocation_in_closed_period", f"allocations[{index}].date is inside a previous reconciliation period", {"index": index, "date": date_text, "coveredThrough": previous_covered})
            if state["opens"].get(account, {}).get("currencies") and resolved["currency"] not in state["opens"][account]["currencies"]:
                fail("allocation_currency_mismatch", f"allocations[{index}]: {account} is not opened for {resolved['currency']}", {"index": index})
            target_closed = self._closed_through(state, account, resolved["currency"])
            if target_closed is not None and date_text <= target_closed:
                fail("allocation_in_closed_period", f"allocations[{index}].date is inside a closed period for {account}", {"index": index, "date": date_text, "coveredThrough": target_closed})
            normalized.append(
                {
                    "account": account,
                    "mode": "fixed",
                    "amount": amount,
                    "date": date_text,
                    "note": _prose(raw.get("note"), f"allocations[{index}].note", maximum=MAX_NOTE, allow_empty=True),
                    "tags": _tags(raw.get("tags")),
                }
            )

        requested_pad = pad_account_requested
        if remainder_index is not None:
            remainder_account = normalized[remainder_index]["account"]
            if requested_pad is not None:
                resolved_pad = resolve_name(state, requested_pad)
                if resolved_pad is None or resolved_pad["account"] != remainder_account:
                    fail("invalid_allocation", "padAccount conflicts with the remainder allocation account")
            if _root(remainder_account) == "Equity":
                normalized[remainder_index]["mode"] = "remainder"
            else:
                normalized[remainder_index]["mode"] = "transaction"
        elif difference is not None and difference != 0 and (requested_pad is not None or not normalized):
            if requested_pad is not None:
                remainder_account = self._resolve_pad_account(state, requested_pad, resolved["currency"])
            else:
                remainder_account = self._default_pad_account(state, resolved, record)
                if _root(remainder_account) != "Equity":
                    fail("pad_account_missing", f"{remainder_account} is not an Equity account")
            normalized.append({"account": remainder_account, "mode": "remainder"})
            remainder_index = len(normalized) - 1

        pad = None
        if remainder_index is not None:
            remainder = normalized[remainder_index]
            remainder_account = remainder["account"]
            if _root(remainder_account) == "Equity":
                pad = {"account": remainder_account}
            else:
                remainder["mode"] = "transaction"
        return normalized, pad, None

    # ------------------------------------------------------------- rendering

    def _render_block(
        self,
        *,
        record_id: str,
        account: str,
        currency: str,
        as_of: str,
        balance_date: str,
        balance_amount: str,
        transactions: list[dict[str, Any]],
        pad: dict[str, Any] | None,
    ) -> str:
        parts = [f"; reconcile {record_id} as_of={as_of} account={account} currency={currency}"]
        for item in transactions:
            transaction = render_transaction(
                {
                    "date": item["date"],
                    "flag": "*",
                    "payee": "对账调整",
                    "narration": item.get("note") or "差额分配",
                    "tags": item.get("tags") or [],
                    "metadata": {"reconcile_id": record_id},
                    "postings": [
                        {
                            "account": item["account"],
                            "amount": normalize_amount(item["amount"]),
                            "currency": currency,
                        },
                        {
                            "account": account,
                            "amount": normalize_amount(from_scaled(-to_scaled(item["amount"]), min_fraction_digits=2)),
                            "currency": currency,
                        },
                    ],
                }
            )
            parts.append(transaction)
            parts.append("")
        if pad is not None:
            parts.append(f"{as_of} pad {account} {pad['account']}")
            parts.append("")
        parts.append(f"{balance_date} balance {account} {normalize_amount(balance_amount)} {currency}")
        return "\n".join(parts) + "\n"

    def _pending_drafts(self, state: dict[str, Any], account: str, as_of: str) -> list[dict[str, Any]]:
        blocking = []
        for document in self.domain.drafts.list(status="pending", limit=500):
            if document.get("date", "") > as_of:
                continue
            if any(posting.get("account") == account for posting in document.get("postings", [])):
                blocking.append(
                    {
                        "id": document["id"],
                        "date": document.get("date"),
                        "payee": document.get("payee", ""),
                        "narration": document.get("narration", ""),
                    }
                )
        return blocking

    def _context(
        self,
        state: dict[str, Any],
        resolved: dict[str, Any],
        *,
        previous_covered: str | None,
        today: date,
    ) -> dict[str, Any]:
        account = resolved["account"]
        currency = resolved["currency"]
        as_of = resolved["asOf"]
        balances = self._balances(state, account, currency)
        is_first = not balances
        if is_first:
            date_from = resolved["opened"]["date"]
        elif previous_covered is not None:
            date_from = previous_covered
        else:
            date_from = resolved["opened"]["date"]
        activity = {"count": 0, "inflow": "0.00", "outflow": "0.00"}
        try:
            activity = query_account_activity(
                root=str(self.domain.root),
                bean_file=self.domain.bean_file,
                executable=self.domain.query_program(),
                account=account,
                currency=currency,
                date_from=date_from,
                date_to=as_of,
                timeout=self.domain.timeout,
            )
        except LedgerError:
            pass
        pending = self._pending_drafts(state, account, as_of)
        return {
            "transactionCount": activity.get("count", 0),
            "inflow": activity.get("inflow", "0.00"),
            "outflow": activity.get("outflow", "0.00"),
            "tags": [],
            "blockingDrafts": pending,
            "otherPendingDrafts": 0,
            "coveredFrom": date_from,
        }

    def _record_summary(self, record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {
            "id": record.get("id"),
            "asOf": record.get("asOf"),
            "balanceDate": record.get("balanceDate"),
            "actual": record.get("actual"),
            "coveredThrough": iso(add_days(parse_date(record["balanceDate"]), -1)) if record.get("balanceDate") else None,
            "directivesHash": record.get("directivesHash"),
            "file": record.get("file"),
            "line": record.get("line"),
        }

    # ----------------------------------------------------------------- preview

    def preview(self, payload: object) -> dict[str, Any]:
        state = self.domain.reader.refresh()
        request = self._request(payload)
        return self._preview(state, request, for_commit=False)

    def _preview(self, state: dict[str, Any], request: dict[str, Any], *, for_commit: bool) -> dict[str, Any]:
        today = self._today()
        resolved = self._resolve(state, request, today=today)
        mode, record = self._mode(state, resolved, request)
        balance_date = iso(add_days(resolved["asOfDate"], 1))
        balances = self._balances(state, resolved["account"], resolved["currency"])
        is_first = bool(record.get("isFirstReconciliation")) if record is not None else not balances
        latest_balance = self._latest_balance(state, resolved["account"], resolved["currency"])
        if mode == "create":
            if latest_balance is not None and latest_balance["date"] == balance_date:
                fail("balance_exists", f"{resolved['account']} already has a {resolved['currency']} balance on {balance_date}", {"file": latest_balance.get("rel_file"), "line": latest_balance.get("line")})
            if latest_balance is not None and latest_balance["date"] > balance_date:
                fail("out_of_order", f"{resolved['account']} already has a later balance", {"latest": latest_balance["date"]})
        else:
            assert record is not None
            if latest_balance is None or latest_balance["date"] != record.get("balanceDate"):
                fail("not_latest", "the latest balance is not owned by the latest reconciliation", {"reconciliation": record["id"]})
            if request.get("replaces"):
                expected = request["replaces"]
                if not isinstance(expected, dict) or expected.get("id") != record["id"] or expected.get("directivesHash") != record.get("directivesHash"):
                    fail("update_conflict", "the reconciliation to replace does not match the latest active record")
        base_record = record if mode == "update" else None
        resolved_replaces = (
            {"id": record["id"], "directivesHash": record.get("directivesHash")}
            if mode == "update" and record is not None
            else None
        )
        if mode == "update" and request.get("replaces"):
            expected_replaces = request["replaces"]
            if not isinstance(expected_replaces, dict) or expected_replaces.get("id") != resolved_replaces["id"] or expected_replaces.get("directivesHash") != resolved_replaces["directivesHash"]:
                fail("update_conflict", "the reconciliation to replace does not match the latest active record")
        previous_covered = self._previous_covered(state, resolved, base_record)
        expected = self._expected(state, resolved, base_record)
        revision = ledger_revision(state)
        context = self._context(state, resolved, previous_covered=previous_covered, today=today)
        default_pad = self._default_pad_account(state, resolved, record)
        base = {
            "mode": mode,
            "account": resolved["account"],
            "name": resolved["name"],
            "currency": resolved["currency"],
            "asOf": resolved["asOf"],
            "balanceDate": balance_date,
            "period": resolved["opened"].get("metadata", {}).get("reconcile"),
            "isFirstReconciliation": is_first,
            "lastReconciliation": self._record_summary(record),
            "replaces": (
                {
                    **self._record_summary(record),
                    "actual": record.get("actual"),
                    "allocations": record.get("allocations", []),
                    "directives": record.get("directives"),
                }
                if resolved_replaces is not None
                else None
            ),
            "expected": expected,
            "actual": request["actual"],
            "difference": None,
            "remaining": None,
            "allocations": [],
            "defaultPadAccount": default_pad,
            "previousCoveredThrough": previous_covered,
            "context": context,
            "warnings": ([{"code": "future_balance", "message": f"余额断言日期 {balance_date} 在未来；提交后该日期之前的补记会被 pad 吸收或提示更新"}] if balance_date > iso(today) else []),
            "ledgerRevision": revision,
            "inputsHash": None,
            "previewId": None,
            "directives": "",
            "directivesHash": None,
        }
        if request["actual"] is None:
            return base
        actual_scaled = to_scaled(request["actual"])
        expected_scaled = to_scaled(expected)
        difference = actual_scaled - expected_scaled
        if difference == 0:
            # A remainder row carries no amount; when the difference is zero it
            # is just an empty default from the form. Only a real fixed
            # allocation is contradictory here.
            fixed_rows = [
                row for row in request["allocations"]
                if isinstance(row, dict) and (row.get("mode") or "fixed") == "fixed"
            ]
            if fixed_rows:
                fail("allocation_not_needed", "actual equals expected; allocations are not needed")
            transactions = []
            pad = None
            remaining = 0
            normalized = []
        else:
            normalized, pad, _ = self._normalize_allocations(
                state,
                resolved,
                request["allocations"],
                previous_covered=previous_covered,
                pad_account_requested=request["padAccount"],
                difference=difference,
                first=is_first,
                record=base_record,
            )
            fixed_total = sum(
                (to_scaled(item["amount"]) for item in normalized if item["mode"] == "fixed"),
                0,
            )
            remaining = difference + fixed_total
            remainder = next((item for item in normalized if item["mode"] in {"remainder", "transaction"}), None)
            if remainder is None:
                if remaining != 0:
                    fail(
                        "unallocated_difference",
                        "allocations do not close the difference and no remainder row was provided",
                        {"difference": from_scaled(difference, 2), "remaining": from_scaled(remaining, 2)},
                    )
                pad = None
                transactions = []
            elif remaining == 0:
                normalized = [item for item in normalized if item is not remainder]
                pad = None
                transactions = []
            elif remainder["mode"] == "remainder":
                pad = {"account": remainder["account"], "amount": from_scaled(remaining, 2)}
                transactions = []
            else:
                pad = None
                transactions = []
                normalized = [item for item in normalized if item is not remainder]
            transactions.extend(
                {
                    "account": item["account"],
                    "amount": item["amount"],
                    "date": item["date"],
                    "note": item.get("note") or "差额分配",
                    "tags": item.get("tags") or [],
                }
                for item in normalized
                if item["mode"] == "fixed"
            )
            if remainder is not None and remainder["mode"] == "transaction" and remaining != 0:
                transactions.append(
                    {
                        "account": remainder["account"],
                        "amount": from_scaled(-remaining, 2),
                        "date": resolved["asOf"],
                        "note": "自动补差",
                        "tags": [],
                    }
                )
        inputs = {
            "mode": mode,
            "account": resolved["account"],
            "currency": resolved["currency"],
            "asOf": resolved["asOf"],
            "actual": request["actual"],
            "allocations": normalized,
            "padAccount": pad.get("account") if pad else None,
            "replaces": resolved_replaces,
        }
        inputs_hash = _json_hash(inputs)
        preview_id = f"rec-{resolved['asOf'].replace('-', '')}-{_sha256(inputs_hash + '|' + revision)[:16]}"
        block = self._render_block(
            record_id=preview_id,
            account=resolved["account"],
            currency=resolved["currency"],
            as_of=resolved["asOf"],
            balance_date=balance_date,
            balance_amount=request["actual"],
            transactions=transactions,
            pad=pad,
        )
        base.update(
            {
                "difference": from_scaled(difference, 2),
                "remaining": from_scaled(remaining, 2),
                "allocations": normalized,
                "pad": pad,
                "transactions": transactions,
                "balance": {"date": balance_date, "amount": request["actual"], "currency": resolved["currency"]},
                "inputsHash": inputs_hash,
                "previewId": preview_id,
                "directives": block,
                "directivesHash": _sha256(block),
            }
        )
        return base

    # ------------------------------------------------------------------ commit

    def commit(self, payload: object) -> dict[str, Any]:
        request = self._request(payload, for_commit=True)
        if not isinstance(request.get("previewId"), str) or not request["previewId"]:
            fail("preview_required", "a previewId is required; call reconcile-preview first")
        if not isinstance(request.get("ledgerRevision"), str):
            fail("preview_required", "a ledgerRevision is required; call reconcile-preview first")
        with write_lock(self.domain.lock_file):
            state = self.domain.reader.refresh()
            existing = self.store.get(request["previewId"])
            if existing is not None:
                if request.get("inputsHash") and existing.get("inputsHash") != request["inputsHash"]:
                    fail("idempotency_conflict", "the same previewId was used with different inputs")
                return {"committed": False, "idempotent": True, "unchanged": False, "reconciliation": existing}
            preview = self._preview(state, request, for_commit=True)
            if preview["mode"] != request["mode"]:
                fail("mode_mismatch", f"resolved mode {preview['mode']} does not match request mode {request['mode']}")
            if preview["ledgerRevision"] != request["ledgerRevision"]:
                fail("stale_preview", "the ledger changed after the preview", {"current": preview["ledgerRevision"]})
            if preview.get("inputsHash") != request.get("inputsHash"):
                fail("preview_mismatch", "inputsHash does not match the recomputed preview", {"field": "inputsHash"})
            if preview.get("directivesHash") != request.get("directivesHash"):
                fail("preview_mismatch", "directivesHash does not match the recomputed preview", {"field": "directivesHash"})
            if preview.get("previewId") != request["previewId"]:
                fail("preview_mismatch", "previewId does not match the recomputed preview", {"field": "previewId"})
            blocking = preview["context"].get("blockingDrafts") or []
            if blocking:
                fail("pending_drafts", "pending drafts affect this account before the reconciliation date", {"drafts": blocking})
            target = resolve_append_target(self.domain.root, "reconciliation")
            content = target["content"]
            record = self.store.latest_active(preview["account"], preview["currency"])
            if request["mode"] == "create":
                candidate = append_block(content, preview["directives"])
            else:
                if record is None:
                    fail("not_latest", "there is no active reconciliation to update")
                if request.get("replaces") and request["replaces"].get("id") != record["id"]:
                    fail("update_conflict", "the reconciliation to replace is no longer the latest")
                old_block = record.get("directives")
                if not isinstance(old_block, str) or old_block == "" or old_block not in content:
                    fail("update_conflict", "the reconciliation block is missing or was edited outside the core")
                candidate = content.replace(old_block, preview["directives"], 1)
            line = candidate[: candidate.rfind(preview["directives"])].count("\n") + 1
            overrides = {target["rel_file"]: candidate}
            validation = validate_ledger(
                root=self.domain.root,
                main_file=self.domain.reader.main_file,
                executable=self.domain.check_program(),
                overrides=overrides,
                timeout=self.domain.timeout,
                exclude=self.domain.exclude_entries,
            )
            if not validation["ok"]:
                mismatch = parse_balance_failure(validation["stderr"])
                if mismatch is not None and mismatch["account"] == preview["account"]:
                    fail(
                        "balance_mismatch",
                        f"balance mismatch for {preview['account']}: ledger accumulates {mismatch['accumulated']} {mismatch['currency']}, declared {mismatch['expected']} {mismatch['currency']}",
                        mismatch,
                    )
                fail("bean_check_failed", "the reconciliation was rejected by bean-check", {"diagnostic": validation["diagnostic"]})
            committed_at = _now()
            new_record = {
                "id": preview["previewId"],
                "account": preview["account"],
                "currency": preview["currency"],
                "asOf": preview["asOf"],
                "balanceDate": preview["balanceDate"],
                "isFirstReconciliation": preview["isFirstReconciliation"],
                "expected": preview["expected"],
                "actual": preview["actual"],
                "difference": preview["difference"],
                "allocations": preview["allocations"],
                "transactions": preview["transactions"],
                "pad": preview.get("pad"),
                "padAmount": preview.get("pad", {}).get("amount") if preview.get("pad") else None,
                "balance": preview["balance"],
                "directives": preview["directives"],
                "directivesHash": preview["directivesHash"],
                "inputsHash": preview["inputsHash"],
                "ledgerRevision": preview["ledgerRevision"],
                "file": target["rel_file"],
                "line": line,
                "actor": "user",
                "committedAt": committed_at,
                "status": "active",
                "mode": preview["mode"],
                "replaces": record.get("id") if request["mode"] == "update" and record else None,
            }
            atomic_write(target["file"], candidate)
            if target["index_changed"]:
                atomic_write(target["index_file"], target["index_content"])
            if record is not None and request["mode"] == "update":
                self.store.supersede(record["id"], new_record["id"])
            self.store.write(new_record)
            self.domain.drafts.audit(
                "reconcile_committed" if request["mode"] == "create" else "reconcile_updated",
                {
                    "id": new_record["id"],
                    "account": new_record["account"],
                    "currency": new_record["currency"],
                    "asOf": new_record["asOf"],
                    "actual": new_record["actual"],
                    "expected": new_record["expected"],
                    "file": new_record["file"],
                    "line": line,
                    "replaces": new_record.get("replaces"),
                },
            )
            self.domain.reader.invalidate()
            return {"committed": True, "idempotent": False, "unchanged": False, "reconciliation": new_record}

    # ----------------------------------------------------------------- history

    def history(self, *, account: str | None = None, limit: int = 50) -> dict[str, Any]:
        state = self.domain.reader.refresh()
        canonical = None
        if account:
            resolution = resolve_name(state, account)
            if resolution is None:
                fail("account_not_found", f'"{account}" is not a known account or alias')
            canonical = resolution["account"]
        records = self.store.list(account=canonical)
        limit = max(1, min(200, int(limit)))
        return {"records": records[:limit]}

    # --------------------------------------------------------------- auto pad

    def auto_pad_for_draft(
        self,
        document: dict[str, Any],
        normalized: list[dict[str, Any]],
        overrides: dict[str, str],
        validation: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Absorb a late draft by adding a pad to the latest reconciliation.

        This is the "no pad" path: instead of rejecting the draft with a manual
        update requirement, the core inserts a pad into the latest block, then
        retries the same shadow validation with both files changed.
        """
        mismatch = parse_balance_failure(validation.get("stderr", ""))
        if mismatch is None:
            return None
        account = mismatch.get("account")
        currency = mismatch.get("currency")
        if not isinstance(account, str) or not isinstance(currency, str):
            return None
        state = self.domain.reader.refresh()
        record = self.store.latest_active(account, currency)
        if record is None:
            return None
        if record.get("pad"):
            return None
        document_date = document.get("date", "")
        if not isinstance(document_date, str) or document_date > record.get("asOf", ""):
            return {
                "error": {
                    "code": "reconcile_update_required",
                    "message": "the late entry is outside the latest reconciliation period",
                    "details": {"account": account, "asOf": record.get("asOf"), "date": document_date, "reconciliation": record.get("id")},
                }
            }
        previous = self._closed_through(state, account, currency, exclude_date=record.get("balanceDate"))
        if previous is not None and document_date <= previous:
            return {
                "error": {
                    "code": "reconcile_update_required",
                    "message": "the late entry belongs to an earlier reconciliation period",
                    "details": {"account": account, "asOf": record.get("asOf"), "date": document_date, "coveredThrough": previous},
                }
            }
        latest_balance = self._latest_balance(state, account, currency)
        if latest_balance is None or latest_balance.get("date") != record.get("balanceDate"):
            return {
                "error": {
                    "code": "reconcile_update_required",
                    "message": "the latest balance is not owned by the latest reconciliation",
                    "details": {"account": account, "reconciliation": record.get("id")},
                }
            }
        target_file = self.domain.root / "trans" / "reconciliation.bean"
        try:
            content = read_ledger_text(target_file)
        except OSError:
            return None
        old_block = record.get("directives")
        if not isinstance(old_block, str) or old_block == "" or old_block not in content:
            return {
                "error": {
                    "code": "reconcile_update_required",
                    "message": "the reconciliation block is missing or was edited outside the core",
                    "details": {"account": account, "reconciliation": record.get("id")},
                }
            }
        pad_account = "Equity:Opening-Balances" if record.get("isFirstReconciliation") else "Equity:Adjustments"
        try:
            pad_account = self._resolve_pad_account(state, pad_account, currency)
        except LedgerError as error:
            return {"error": {"code": "reconcile_update_required", "message": error.message, "details": error.details}}
        transactions = record.get("transactions") or []
        balance = record.get("balance") or {}
        if not transactions and not balance:
            return None
        new_id = f"rec-{record['asOf'].replace('-', '')}-auto{secrets.token_hex(6)}"
        block = self._render_block(
            record_id=new_id,
            account=account,
            currency=currency,
            as_of=record["asOf"],
            balance_date=record["balanceDate"],
            balance_amount=balance.get("amount", record.get("actual")),
            transactions=transactions,
            pad={"account": pad_account},
        )
        new_content = content.replace(old_block, block, 1)
        line = new_content[: new_content.rfind(block)].count("\n") + 1
        new_record = {
            **record,
            "id": new_id,
            "directives": block,
            "directivesHash": _sha256(block),
            "status": "active",
            "autoPad": True,
            "reason": "late_draft",
            "draftId": document.get("id"),
            "replaces": record.get("id"),
            "pad": {"account": pad_account},
            "padAmount": None,
            "line": line,
            "committedAt": _now(),
        }
        return {
            "writes": {"trans/reconciliation.bean": new_content},
            "autoPad": {"old": record, "new": new_record},
        }

    def finalize_auto_pad(self, payload: dict[str, Any]) -> None:
        old = payload.get("old") or {}
        new = payload.get("new") or {}
        if old.get("id") and new.get("id"):
            self.store.supersede(old["id"], new["id"])
        if new.get("id"):
            self.store.write(new)
            self.domain.drafts.audit(
                "reconcile_auto_padded",
                {
                    "id": new["id"],
                    "replaces": new.get("replaces"),
                    "account": new.get("account"),
                    "asOf": new.get("asOf"),
                    "draftId": new.get("draftId"),
                    "pad": new.get("pad"),
                },
            )
