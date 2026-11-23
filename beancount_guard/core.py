"""The guard's function interface.

UI, agent adapters and scripts call this object, never the ``.bean`` files:
aliases resolve on the way in, names project on the way out, and every ledger
mutation goes through one validated, locked append.

    from beancount_guard import create_beancount_guard
    guard = create_beancount_guard(root="/path/to/ledger")
    guard.get_due_reconciliations()
    guard.submit_balance(account="工行5244", amount="-30.00", currency="CNY")
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .amounts import assert_amount, normalize_amount, parse_balance_failure
from .bean import (
    append_block,
    assert_alias,
    is_account_name,
    is_currency,
    normalize_alias,
    render_balance,
    render_open,
    rewrite_open_date,
    rewrite_open_metadata,
)
from .dates import is_date, is_period, iso, today_in_zone
from .drafts import DraftStore, assess_draft
from .operations import OperationStore
from .errors import LedgerError, fail
from .ledger import (
    LedgerReader,
    audit_state,
    has_account,
    is_closed_on,
    list_account_states,
    project_name,
    project_text,
    read_ledger_text,
    resolve_name,
)
from .query import query_balances as query_balances_impl
from .query import query_transactions as query_transactions_impl
from .quickpath import build_quick_draft, match_merchants
from .reconciliation import Reconciler
from .templates import render_ledger
from .reconcile import count_reconciliations, reconciliation_of
from .reconcile import list_reconciliations as list_reminders
from .target import resolve_append_target
from .validation import atomic_write, resolve_program, validate_ledger, write_lock

UNSET = object()


def create_beancount_guard(**options: Any) -> "BeancountGuard":
    return BeancountGuard(**options)


class BeancountGuard:
    """Client over one ledger root."""

    def __init__(
        self,
        *,
        root: str | Path,
        bean_file: str = "main.bean",
        state_dir: str = ".ledger",
        bean_check: str | None = None,
        bean_query: str | None = None,
        time_zone: str = "Asia/Shanghai",
        grace_days: int = 3,
        now: Callable[[], datetime] | None = None,
        max_drafts: int = 500,
        max_evidence_bytes: int = 16_384,
        timeout: float = 60.0,
        exclude_entries: list[str] | None = None,
    ) -> None:
        if not isinstance(root, (str, Path)) or str(root).strip() == "":
            fail("invalid_ledger_root", "a ledger root is required")
        self.root = Path(root).resolve()
        self.bean_file = bean_file
        self.state_dir = state_dir
        self.bean_check_config = bean_check
        self.bean_query_config = bean_query
        self.time_zone = time_zone
        self.grace_days = grace_days if isinstance(grace_days, int) and grace_days >= 0 else 3
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else 60.0
        self.exclude_entries = exclude_entries or ["beancount-guard", "beancount_guard"]
        self.reader = LedgerReader(self.root, bean_file)
        self.drafts = DraftStore(
            root=self.root,
            reader=self.reader,
            bean_check=self.check_program,
            state_dir=state_dir,
            time_zone=time_zone,
            now=self.now,
            max_drafts=max_drafts,
            max_evidence_bytes=max_evidence_bytes,
            timeout=self.timeout,
            exclude_entries=self.exclude_entries,
        )
        self.operations = OperationStore(
            root=self.root,
            reader=self.reader,
            state_dir=state_dir,
            time_zone=time_zone,
            now=self.now,
        )
        self.reconciler = Reconciler(self)
        self.drafts.approval_hook = self.reconciler.auto_pad_for_draft
        self.drafts.approval_finalize = self.reconciler.finalize_auto_pad
        self._programs: dict[str, str] = {}

    def today(self) -> date:
        return today_in_zone(self.time_zone, self.now())

    def today_text(self) -> str:
        return iso(self.today())

    @property
    def lock_file(self) -> Path:
        return self.root / self.state_dir / "write.lock"

    # -------------------------------------------------------------------- setup

    def setup(self) -> dict[str, Any]:
        """Describe the configured root without requiring it to exist."""
        main = self.root / self.bean_file
        initialized = main.is_file()
        title = self._read_title(main) if initialized else None
        has_entries = False
        if self.root.is_dir():
            try:
                has_entries = next(self.root.iterdir(), None) is not None
            except OSError:
                has_entries = False
        return {
            "initialized": initialized,
            "root": str(self.root),
            "beanFile": self.bean_file,
            "mainBean": str(main),
            "today": self.today_text(),
            "title": title,
            "hasEntries": has_entries,
        }

    def init_ledger(self, *, title: str = "我的账本", currency: str = "CNY", year: int | str | None = None) -> dict[str, Any]:
        """Create a clean Beancount-Trans-Assets-style ledger at ``root``.

        The candidate is written into a sibling staging directory, validated by
        the real ``bean-check``, and renamed into place only after validation.
        """
        title_text = title.strip() if isinstance(title, str) else ""
        if not title_text:
            fail("invalid_title", "账本名称不能为空")
        if len(title_text) > 80 or re.search(r"[\u0000-\u001f\u007f]", title_text):
            fail("invalid_title", "账本名称最多 80 个字符，且不能包含控制字符")
        if not isinstance(currency, str) or not is_currency(currency):
            fail("invalid_currency", f'"{currency}" is not a Beancount currency')
        if year is None:
            year_text = self.today_text()[:4]
        else:
            year_text = str(year)
        if re.fullmatch(r"\d{4}", year_text) is None or not 2000 <= int(year_text) <= 2100:
            fail("invalid_year", "year must be a four-digit year from 2000 to 2100")

        if self.root.exists():
            if not self.root.is_dir():
                fail("invalid_ledger_root", f"{self.root} is not a directory")
            entries = list(self.root.iterdir())
            if any(entry.name == self.bean_file for entry in entries):
                fail("ledger_exists", f"{self.root} already contains {self.bean_file}", {"root": str(self.root)})
            if entries:
                fail("directory_not_empty", f"{self.root} is not empty; choose an empty directory", {"root": str(self.root)})

        self.root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self.root.parent, prefix=f".{self.root.name}.init-"))
        try:
            files = render_ledger(title=title_text, currency=currency, year=year_text, today=self.today_text())
            for relative, content in files.items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            validation = validate_ledger(
                root=staging,
                main_file=staging / self.bean_file,
                executable=self.check_program(),
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            if not validation["ok"]:
                fail(
                    "bean_check_failed",
                    f"the generated ledger was rejected by bean-check: {validation['diagnostic']}",
                    {"diagnostic": validation["diagnostic"]},
                )
            if self.root.exists():
                self.root.rmdir()
            os.replace(staging, self.root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.reader.invalidate()
        return {
            "root": str(self.root),
            "title": title_text,
            "currency": currency,
            "year": year_text,
            "today": self.today_text(),
            "files": sorted(files),
            "validated": True,
        }

    @staticmethod
    def _read_title(main: Path) -> str | None:
        try:
            content = main.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        match = re.search(r'option[ \t]+"title"[ \t]+"((?:[^"\\]|\\.)*)"', content)
        return match.group(1) if match else None

    def check_program(self) -> str:
        if "check" not in self._programs:
            self._programs["check"] = resolve_program(self.root, self.bean_check_config, "bean-check")
        return self._programs["check"]

    def query_program(self) -> str:
        if "query" not in self._programs:
            self._programs["query"] = resolve_program(self.root, self.bean_query_config, "bean-query")
        return self._programs["query"]

    # ---------------------------------------------------------------- read side

    def resolve_alias(self, name: str) -> dict[str, str] | None:
        state = self.reader.refresh()
        return resolve_name(state, name)

    def project_account(self, account: str) -> str:
        state = self.reader.refresh()
        return project_name(state, account)

    def project_text(self, text: str) -> str:
        state = self.reader.refresh()
        return project_text(state, text)

    def list_accounts(
        self,
        *,
        query: str | None = None,
        include_closed: bool = True,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        state = self.reader.refresh()
        today = self.today()
        needle = query.strip().lower() if isinstance(query, str) else ""
        accounts = []
        for account in list_account_states(state):
            if not include_closed and account["closeDate"] is not None and account["closeDate"] <= iso(today):
                continue
            if needle and needle not in f"{account['account']} {account['alias'] or ''}".lower():
                continue
            accounts.append(self._decorate_account(account, today))
            if len(accounts) >= limit:
                break
        return accounts

    def account(self, name: str) -> dict[str, Any] | None:
        state = self.reader.refresh()
        resolution = resolve_name(state, name)
        if resolution is None:
            return None
        for account in list_account_states(state):
            if account["account"] == resolution["account"]:
                return self._decorate_account(account, self.today())
        return None

    def _decorate_account(self, account: dict[str, Any], today: date) -> dict[str, Any]:
        reminder = reconciliation_of(
            account, account.get("lastBalance"), today=today, grace_days=self.grace_days
        )
        return {
            **account,
            "closed": account["closeDate"] is not None and account["closeDate"] <= iso(today),
            "due": reminder,
            "status": reminder["status"] if reminder is not None else None,
            "nextDue": reminder["nextDue"] if reminder is not None else None,
            "coveredThrough": reminder.get("coveredThrough") if reminder is not None else None,
            "unreconciled": reminder.get("unreconciled") if reminder is not None else False,
        }

    def get_due_reconciliations(self, *, include_scheduled: bool = True) -> dict[str, Any]:
        state = self.reader.refresh()
        today = self.today()
        accounts = [
            self._decorate_account(account, today) for account in list_account_states(state)
        ]
        reminders = list_reminders(
            accounts, today=today, grace_days=self.grace_days, include_scheduled=include_scheduled
        )
        decorated = []
        for reminder in reminders:
            decorated.append(
                {
                    **reminder,
                    "lastBalanceDate": reminder["lastBalance"]["date"] if reminder["lastBalance"] else None,
                }
            )
        return {"today": iso(today), "reminders": decorated, "counts": count_reconciliations(reminders)}

    def status(self) -> dict[str, Any]:
        state = self.reader.refresh()
        today = self.today()
        accounts = [self._decorate_account(account, today) for account in list_account_states(state)]
        reminders = list_reminders(accounts, today=today, grace_days=self.grace_days, include_scheduled=True)
        due = [reminder for reminder in reminders if reminder["status"] in {"due", "overdue"}]
        return {
            "today": iso(today),
            "root": str(self.root),
            "beanFile": self.bean_file,
            "operatingCurrency": state["operating_currency"],
            "accountCount": len(accounts),
            "configuredCount": len(reminders),
            "due": due,
            "counts": count_reconciliations(reminders),
            "drafts": self.drafts.summary(),
            "operations": self.operations.summary(),
            "issues": audit_state(state),
        }

    def audit_metadata(self) -> dict[str, Any]:
        state = self.reader.refresh()
        return {"today": self.today_text(), "issues": audit_state(state)}

    # --------------------------------------------------------------- write side

    def set_account_metadata(
        self,
        *,
        account: str,
        alias: str | None | object = UNSET,
        reconcile: str | None | object = UNSET,
    ) -> dict[str, Any]:
        values: dict[str, str | None] = {}
        if alias is not UNSET:
            values["alias"] = alias  # type: ignore[assignment]
            if alias is not None:
                values["alias"] = assert_alias(alias)
        if reconcile is not UNSET:
            values["reconcile"] = reconcile  # type: ignore[assignment]
            if reconcile is not None and not is_period(reconcile):
                fail("invalid_period", f'"{reconcile}" is not a supported reconciliation period', {"supported": ["none", "daily", "weekly", "monthly", "quarterly", "yearly"]})
        if not values:
            fail("invalid_argument", "set alias and/or reconcile")

        with write_lock(self.lock_file):
            state = self.reader.refresh()
            resolution = resolve_name(state, account)
            if resolution is None:
                fail("account_not_found", f'"{account}" is not a known account or alias')
            canonical = resolution["account"]
            opened = state["opens"].get(canonical)
            if opened is None:
                fail(
                    "account_not_opened",
                    f"{canonical} exists only through transactions or balances; add an open directive to govern its metadata",
                    {"account": canonical, "implicit": True},
                )

            if "reconcile" in values and canonical.split(":", 1)[0] not in {"Assets", "Liabilities"}:
                fail(
                    "not_reconcilable",
                    "only asset and liability accounts can be reconciled",
                    {"account": canonical},
                )

            if values.get("alias") is not None:
                normalized = normalize_alias(values["alias"])  # type: ignore[arg-type]
                conflicts = [
                    entry
                    for entry in state["aliases"].get(normalized, [])
                    if entry["account"] != canonical
                ]
                if conflicts:
                    fail(
                        "alias_conflict",
                        f'alias "{values["alias"]}" is already used by '
                        + ", ".join(project_name(state, entry["account"]) for entry in conflicts),
                        {"alias": values["alias"], "accounts": [entry["account"] for entry in conflicts]},
                    )
                canonical_collision = next(
                    (
                        candidate
                        for candidate in [*state["opens"], *state["implicit"]]
                        if normalize_alias(candidate) == normalized
                    ),
                    None,
                )
                if canonical_collision is not None:
                    fail(
                        "alias_conflict",
                        f'alias "{values["alias"]}" collides with the canonical account {canonical_collision}',
                        {"alias": values["alias"], "accounts": [canonical_collision]},
                    )

            content = read_ledger_text(opened["file"])
            rewritten, changed = rewrite_open_metadata(content, canonical, values)
            if not changed:
                return self.account(canonical) or {}
            validation = validate_ledger(
                root=self.root,
                main_file=self.reader.main_file,
                executable=self.check_program(),
                overrides={opened["rel_file"]: rewritten},
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            if not validation["ok"]:
                fail("bean_check_failed", f"the metadata change was rejected by bean-check: {validation['diagnostic']}", {"diagnostic": validation["diagnostic"]})
            atomic_write(opened["file"], rewritten)
            self.drafts.audit("metadata_updated", {"account": canonical, "changed": list(values)})
            self.reader.invalidate()
            updated = self.account(canonical)
            if updated is None:
                fail("account_not_found", f"{canonical} disappeared during the write")
            return updated

    def open_account(
        self,
        *,
        account: str,
        date_text: str | None = None,
        currencies: list[str] | None = None,
        alias: str | None = None,
        reconcile: str | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Append one open directive to account/*.bean."""
        if not isinstance(account, str) or not is_account_name(account.strip()):
            fail("invalid_account", f'"{account}" is not a Beancount account')
        canonical = account.strip()
        root_name = canonical.split(":", 1)[0]
        files = {
            "Assets": "account/assets.bean",
            "Liabilities": "account/liabilities.bean",
            "Expenses": "account/expenses.bean",
            "Income": "account/income.bean",
            "Equity": "account/equity.bean",
        }
        rel_file = files.get(root_name)
        if rel_file is None:
            fail("invalid_account", f"{canonical} must start with Assets, Liabilities, Expenses, Income or Equity")
        open_date = date_text or self.today_text()
        if not is_date(open_date):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        if open_date > self.today_text():
            fail("future_date", "an open directive cannot be dated in the future", {"date": open_date, "today": self.today_text()})

        currency_values = list(currencies) if isinstance(currencies, list) else []
        cleaned_currencies: list[str] = []
        for currency in currency_values:
            if not isinstance(currency, str) or not is_currency(currency.strip()):
                fail("invalid_currency", f'"{currency}" is not a Beancount currency')
            if currency.strip() not in cleaned_currencies:
                cleaned_currencies.append(currency.strip())

        alias_text = assert_alias(alias) if alias is not None else None
        reconcile_text = reconcile
        if reconcile_text is not None:
            if root_name not in {"Assets", "Liabilities"}:
                fail("not_reconcilable", "only asset and liability accounts can be reconciled", {"account": canonical})
            if not is_period(reconcile_text):
                fail("invalid_period", f'"{reconcile_text}" is not a supported reconciliation period', {"supported": ["none", "daily", "weekly", "monthly", "quarterly", "yearly"]})

        path = self.root / rel_file
        if not path.is_file():
            fail("account_file_missing", f"the ledger has no {rel_file}", {"file": rel_file})

        with write_lock(self.lock_file):
            state = self.reader.refresh()
            if canonical in state["opens"] or canonical in state["implicit"]:
                fail("account_exists", f"{canonical} already exists", {"account": canonical})
            if alias_text is not None:
                normalized = normalize_alias(alias_text)
                conflicts = [entry for entry in state["aliases"].get(normalized, []) if entry["account"] != canonical]
                if conflicts:
                    fail(
                        "alias_conflict",
                        f'alias "{alias_text}" is already used by '
                        + ", ".join(project_name(state, entry["account"]) for entry in conflicts),
                        {"alias": alias_text, "accounts": [entry["account"] for entry in conflicts]},
                    )
                collision = next(
                    (
                        candidate
                        for candidate in [*state["opens"], *state["implicit"]]
                        if normalize_alias(candidate) == normalized
                    ),
                    None,
                )
                if collision is not None:
                    fail("alias_conflict", f'alias "{alias_text}" collides with the canonical account {collision}', {"alias": alias_text, "accounts": [collision]})

            content = read_ledger_text(path)
            block = render_open(
                {
                    "date": open_date,
                    "account": canonical,
                    "currencies": cleaned_currencies,
                    "alias": alias_text,
                    "reconcile": reconcile_text,
                }
            )
            next_content = append_block(content, block)
            validation = validate_ledger(
                root=self.root,
                main_file=self.reader.main_file,
                executable=self.check_program(),
                overrides={rel_file: next_content},
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            if not validation["ok"]:
                fail("bean_check_failed", f"the account open was rejected by bean-check: {validation['diagnostic']}", {"diagnostic": validation["diagnostic"]})
            atomic_write(path, next_content)
            self.drafts.audit("account_opened", {"account": canonical, "file": rel_file, "alias": alias_text, "reconcile": reconcile_text, "actor": actor})
            self.reader.invalidate()
            opened = self.account(canonical)
            if opened is None:
                fail("account_not_found", f"{canonical} disappeared during the write")
            return opened

    def redate_accounts(
        self,
        *,
        date_text: str,
        accounts: list[str] | None = None,
        all_accounts: bool = False,
    ) -> dict[str, Any]:
        """Move existing ``open`` directives earlier, e.g. to backfill an import.

        Only moving a date *earlier* is allowed: a later date could orphan
        transactions that are already recorded against the account, and
        ``bean-check`` does not detect that. Multiple files are validated in one
        shadow-tree pass and only then replaced one by one.
        """
        if not is_date(date_text):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        if date_text > self.today_text():
            fail("future_date", "an open directive cannot be dated in the future", {"date": date_text, "today": self.today_text()})
        requested = [item.strip() for item in (accounts or []) if isinstance(item, str) and item.strip()]
        if not all_accounts and not requested:
            fail("invalid_argument", "pass accounts or all: true")

        with write_lock(self.lock_file):
            state = self.reader.refresh()
            if all_accounts:
                targets = sorted(state["opens"])
            else:
                targets = []
                for name in requested:
                    resolution = resolve_name(state, name)
                    if resolution is None:
                        fail("account_not_found", f'"{name}" is not a known account or alias')
                    canonical = resolution["account"]
                    if canonical not in state["opens"]:
                        fail(
                            "account_not_opened",
                            f"{canonical} exists only through transactions or balances; there is no open directive to redate",
                            {"account": canonical, "implicit": True},
                        )
                    if canonical not in targets:
                        targets.append(canonical)

            rewrites: dict[str, str] = {}
            changed: list[dict[str, Any]] = []
            unchanged: list[str] = []
            for canonical in targets:
                opened = state["opens"][canonical]
                current = opened["date"]
                if current <= date_text:
                    if not all_accounts and current < date_text:
                        fail(
                            "date_not_earlier",
                            f"{canonical} is already open on {current}; redate only moves an open date earlier",
                            {"account": canonical, "current": current, "requested": date_text},
                        )
                    unchanged.append(canonical)
                    continue
                rel_file = opened["rel_file"]
                current_content = rewrites.get(rel_file)
                if current_content is None:
                    current_content = read_ledger_text(opened["file"])
                rewritten, _ = rewrite_open_date(current_content, canonical, date_text)
                rewrites[rel_file] = rewritten
                changed.append({"account": canonical, "from": current, "to": date_text, "file": rel_file})

            if not changed:
                return {"date": date_text, "changed": [], "unchanged": unchanged}

            validation = validate_ledger(
                root=self.root,
                main_file=self.reader.main_file,
                executable=self.check_program(),
                overrides=rewrites,
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            if not validation["ok"]:
                fail(
                    "bean_check_failed",
                    f"the redate was rejected by bean-check: {validation['diagnostic']}",
                    {"diagnostic": validation["diagnostic"]},
                )
            for rel_file, content in rewrites.items():
                atomic_write(self.root / rel_file, content)
            self.drafts.audit(
                "accounts_redated",
                {"date": date_text, "accounts": [item["account"] for item in changed]},
            )
            self.reader.invalidate()
            return {"date": date_text, "changed": changed, "unchanged": unchanged}

    def submit_balance(
        self,
        *,
        account: str,
        amount: str,
        currency: str | None = None,
        date_text: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        assert_date = date_text or self.today_text()
        if not is_date(assert_date):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        amount_text = normalize_amount(assert_amount(amount, "amount"))

        with write_lock(self.lock_file):
            state = self.reader.refresh()
            today = self.today_text()
            if assert_date > today:
                fail("future_date", "a balance assertion cannot be dated in the future", {"date": assert_date, "today": today})
            resolution = resolve_name(state, account)
            if resolution is None:
                fail("account_not_found", f'"{account}" is not a known account or alias')
            canonical = resolution["account"]
            opened = state["opens"].get(canonical)
            implicit = state["implicit"].get(canonical)
            first_date = opened["date"] if opened is not None else (implicit["first_date"] if implicit else None)
            if first_date is None:
                fail("account_not_found", f"{canonical} is not known to the ledger")
            if assert_date < first_date:
                fail("before_account_open", f"{canonical} first appears on {first_date}", {"openDate": first_date})
            if is_closed_on(state, canonical, assert_date):
                fail("closed_account", f"{canonical} is closed on {assert_date}")

            currencies = opened["currencies"] if opened is not None else []
            currency_text = (currency or (currencies[0] if currencies else None) or state["operating_currency"] or "").strip()
            if not is_currency(currency_text):
                fail("invalid_currency", f'"{currency_text}" is not a Beancount currency', {"currency": currency_text})
            if currencies and currency_text not in currencies:
                fail("invalid_currency", f"{canonical} is not opened for {currency_text}", {"allowed": currencies, "currency": currency_text})
            duplicate = next(
                (
                    entry
                    for entry in state["balances"]
                    if entry["account"] == canonical and entry["date"] == assert_date and entry["currency"] == currency_text
                ),
                None,
            )
            if duplicate is not None:
                fail("balance_exists", f"{canonical} already has a {currency_text} assertion on {assert_date}", {"file": duplicate["rel_file"], "line": duplicate["line"]})

            target = resolve_append_target(self.root, "reconciliation")
            block = render_balance({"date": assert_date, "account": canonical, "amount": amount_text, "currency": currency_text})
            if block in target["content"]:
                if target["index_changed"]:
                    atomic_write(target["index_file"], target["index_content"])
                self.reader.invalidate()
                line = target["content"][: target["content"].find(block)].count("\n") + 1
                updated = self.account(canonical) or {}
                self.drafts.audit("balance_already_present", {"account": canonical, "date": assert_date, "amount": amount_text, "currency": currency_text, "file": target["rel_file"], "line": line})
                return self._balance_result(canonical, updated, assert_date, amount_text, currency_text, target["rel_file"], line)

            content = append_block(target["content"], block)
            line = content[: content.rfind(block)].count("\n") + 1
            overrides = {target["rel_file"]: content}
            if target["index_changed"]:
                overrides[target["index_rel"]] = target["index_content"]
            validation = validate_ledger(
                root=self.root,
                main_file=self.reader.main_file,
                executable=self.check_program(),
                overrides=overrides,
                timeout=self.timeout,
                exclude=self.exclude_entries,
            )
            if not validation["ok"]:
                mismatch = parse_balance_failure(validation["stderr"])
                if mismatch is not None and mismatch["account"] == canonical:
                    fail(
                        "balance_mismatch",
                        f"balance mismatch for {canonical}: the ledger accumulates {mismatch['accumulated']} {mismatch['currency']}, "
                        f"you declared {mismatch['expected']} {mismatch['currency']}",
                        {**mismatch, "account": canonical, "name": project_name(state, canonical), "date": assert_date, "file": target["rel_file"]},
                    )
                fail("bean_check_failed", f"the balance assertion was rejected by bean-check: {validation['diagnostic']}", {"diagnostic": validation["diagnostic"]})

            atomic_write(target["file"], content)
            if target["index_changed"]:
                atomic_write(target["index_file"], target["index_content"])
            self.drafts.audit(
                "balance_submitted",
                {"account": canonical, "date": assert_date, "amount": amount_text, "currency": currency_text, "file": target["rel_file"], "note": note[:500] if isinstance(note, str) else ""},
            )
            self.reader.invalidate()
            updated = self.account(canonical) or {}
            return self._balance_result(canonical, updated, assert_date, amount_text, currency_text, target["rel_file"], line)

    def _balance_result(
        self,
        canonical: str,
        updated: dict[str, Any],
        date_text: str,
        amount: str,
        currency: str,
        file: str,
        line: int,
    ) -> dict[str, Any]:
        reminder = updated.get("due")
        return {
            "account": canonical,
            "name": updated.get("name", canonical),
            "date": date_text,
            "amount": amount,
            "currency": currency,
            "file": file,
            "line": line,
            "balance": {"date": date_text, "amount": amount, "currency": currency},
            "reconciliation": reminder,
            "accountView": updated,
        }

    # --------------------------------------------------------------- reconcile

    def preview_reconciliation(self, payload: object) -> dict[str, Any]:
        return self.reconciler.preview(payload)

    def commit_reconciliation(self, payload: object) -> dict[str, Any]:
        return self.reconciler.commit(payload)

    def reconcile_history(self, *, account: str | None = None, limit: int = 50) -> dict[str, Any]:
        return self.reconciler.history(account=account, limit=limit)

    # ------------------------------------------------------------------- drafts

    def list_drafts(self, *, status: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return self.drafts.list(status=status, limit=limit)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self.drafts.get(draft_id)

    def submit_draft(self, payload: object) -> dict[str, Any]:
        return self.drafts.submit(payload)

    def refresh_drafts(self) -> dict[str, Any]:
        """Re-resolve pending drafts after the account set changed (queue only)."""
        return self.drafts.refresh_resolutions()

    def update_draft(self, *, draft_id: str, payload: object, actor: str = "user") -> dict[str, Any]:
        return self.drafts.update(draft_id, payload, actor=actor)

    def approve_draft(self, *, draft_id: str, postings: object = None, actor: str = "user") -> dict[str, Any]:
        return self.drafts.approve(draft_id=draft_id, postings=postings, actor=actor)

    def reject_draft(self, *, draft_id: str, reason: str = "", actor: str = "user") -> dict[str, Any]:
        return self.drafts.reject(draft_id=draft_id, reason=reason, actor=actor)

    # ------------------------------------------------------------ operation queue

    def _project_operation(self, document: dict[str, Any]) -> dict[str, Any]:
        return {
            **document,
            "approvable": document.get("status") == "pending" and not document.get("issues"),
        }

    def list_operations(self, *, status: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return [self._project_operation(item) for item in self.operations.list(status=status, limit=limit)]

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._project_operation(self.operations.get(operation_id))

    def submit_operation(self, payload: object) -> dict[str, Any]:
        stored = self.operations.submit(payload)
        return {"operation": self._project_operation(stored["operation"]), "deduplicated": stored["deduplicated"]}

    def approve_operation(self, *, operation_id: str, actor: str = "user") -> dict[str, Any]:
        """Execute a queued structure write through the normal, validated methods."""
        document = self.operations.get(operation_id)
        if document.get("status") != "pending":
            fail("operation_not_pending", f"operation {operation_id} is {document.get('status')}, not pending")
        kind = document["kind"]
        payload = document["payload"]
        if kind == "account-open":
            result: dict[str, Any] = self.open_account(
                account=payload["account"],
                date_text=payload["date"],
                currencies=payload["currencies"],
                alias=payload.get("alias"),
                reconcile=payload.get("reconcile"),
                actor=actor,
            )
        elif kind == "account-redate":
            result = self.redate_accounts(
                date_text=payload["date"],
                accounts=payload.get("accounts") or [],
                all_accounts=payload.get("all") is True,
            )
        elif kind == "account-metadata":
            kwargs: dict[str, Any] = {"account": payload["account"]}
            if "alias" in payload:
                kwargs["alias"] = payload["alias"]
            if "reconcile" in payload:
                kwargs["reconcile"] = payload["reconcile"]
            result = self.set_account_metadata(**kwargs)
        elif kind == "balance":
            result = self.submit_balance(
                account=payload["account"],
                amount=payload["amount"],
                currency=payload.get("currency"),
                date_text=payload.get("date"),
                note=payload.get("note") or None,
            )
        else:  # pragma: no cover - guarded by normalize_operation
            fail("invalid_operation", f"unknown operation kind {kind}")
        marked = self.operations.mark(
            operation_id, "approved", actor=actor, detail={"summary": document.get("summary", "")}
        )
        # 结构落盘后立刻让原本卡住的草稿重新解析，否则批准完开户还要人工逐笔重填。
        if kind in {"account-open", "account-redate"}:
            refreshed = self.drafts.refresh_resolutions()
            if refreshed["drafts"]:
                marked = {**marked, "refreshedDrafts": refreshed}
        return marked

    def approve_all_operations(self, *, actor: str = "user") -> dict[str, Any]:
        """Approve every pending operation, oldest first, one block per click."""
        pending = sorted(
            (item for item in self.operations.list(status="pending")),
            key=lambda item: item.get("createdAt", ""),
        )
        done: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for document in pending:
            try:
                approved = self.approve_operation(operation_id=document["id"], actor=actor)
                done.append({"id": document["id"], "summary": document.get("summary", ""), "operation": approved})
            except LedgerError as error:
                failed.append({"id": document["id"], "summary": document.get("summary", ""), "code": error.code, "message": error.message})
        return {"approved": len(done), "failed": failed, "results": done}

    def reject_operation(self, *, operation_id: str, reason: str = "", actor: str = "user") -> dict[str, Any]:
        return self.operations.mark(operation_id, "rejected", actor=actor, detail={"reason": reason})

    def approve_all_drafts(self, *, actor: str = "user") -> dict[str, Any]:
        """Approve every approvable pending draft, by date, in one pass."""
        self.drafts.refresh_resolutions()
        pending = [self.project_draft(item) for item in self.drafts.list(status="pending", limit=self.drafts.max_drafts)]
        ordered = sorted(pending, key=lambda item: (item.get("date", ""), item.get("createdAt", "")))
        done: list[str] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for draft in ordered:
            if not draft.get("approvable"):
                skipped.append({
                    "id": draft["id"],
                    "payee": draft.get("payee") or draft.get("narration"),
                    "issues": sorted({issue.get("code", "") for issue in draft.get("issues", [])}),
                })
                continue
            try:
                self.approve_draft(draft_id=draft["id"], actor=actor)
                done.append(draft["id"])
            except LedgerError as error:
                failed.append({"id": draft["id"], "code": error.code, "message": error.message})
        return {"approved": len(done), "skipped": skipped, "failed": failed}

    # ------------------------------------------------------------------ proposal

    def match_merchant(self, text: str) -> list[dict[str, Any]]:
        state = self.reader.refresh()
        return match_merchants(state, text)

    def propose_quick_draft(
        self,
        *,
        merchant: str,
        amount: str,
        currency: str,
        source_account: str,
        date_text: str | None = None,
        narration: str | None = None,
    ) -> dict[str, Any]:
        state = self.reader.refresh()
        date_text = date_text or self.today_text()
        if not is_date(date_text):
            fail("invalid_date", "date must be a real YYYY-MM-DD date")
        proposal = build_quick_draft(
            state=state,
            date=date_text,
            merchant=merchant,
            amount=amount,
            currency=currency,
            source_account=source_account,
            narration=narration,
        )
        if not proposal["matched"]:
            return {"matched": False, "candidates": [{"name": item["name"], "alias": item["alias"]} for item in proposal["candidates"]]}
        stored = self.drafts.submit(proposal["draft"])
        return {
            "matched": True,
            "candidate": proposal["candidate"],
            "draft": stored["draft"],
            "issues": stored["issues"],
            "deduplicated": stored["deduplicated"],
        }

    # -------------------------------------------------------------------- query

    def query_transactions(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        account: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        state = self.reader.refresh()
        date_to = date_to or self.today_text()
        if date_from is None:
            if not is_date(date_to):
                fail("invalid_date", "to must be a YYYY-MM-DD date")
            date_from = iso(date.fromisoformat(date_to) - timedelta(days=30))
        return query_transactions_impl(
            state=state,
            root=str(self.root),
            bean_file=self.bean_file,
            executable=self.query_program(),
            date_from=date_from,
            date_to=date_to,
            account=account,
            limit=limit,
            timeout=self.timeout,
        )

    def query_balances(self, *, as_of: str | None = None) -> dict[str, Any]:
        state = self.reader.refresh()
        return query_balances_impl(
            state=state,
            root=str(self.root),
            bean_file=self.bean_file,
            executable=self.query_program(),
            as_of=as_of or self.today_text(),
            timeout=self.timeout,
        )

    def validate(self) -> dict[str, Any]:
        validation = validate_ledger(
            root=self.root,
            main_file=self.reader.main_file,
            executable=self.check_program(),
            timeout=self.timeout,
            exclude=self.exclude_entries,
        )
        return {"ok": validation["ok"], "diagnostic": validation["diagnostic"]}

    # ------------------------------------------------------------- presentation

    def project_draft(self, document: dict[str, Any]) -> dict[str, Any]:
        state = self.reader.refresh()
        # 待审草稿的 issues 按当前账本重算：结构变更（开户/改开立日）批准后，
        # 原本卡住的草稿应当立刻变成可批准，而不是保留落盘时的旧结论。
        live_issues = document.get("issues", [])
        live_approvable = document.get("approvable")
        if document.get("status") == "pending":
            live_issues, live_approvable = assess_draft(document, state, today=self.today_text())
        return {
            "id": document["id"],
            "status": document["status"],
            "date": document["date"],
            "flag": document.get("flag", "*"),
            "payee": document.get("payee", ""),
            "narration": document.get("narration", ""),
            "metadata": document.get("metadata", {}),
            "tags": document.get("tags", []),
            "links": document.get("links", []),
            "confidence": document.get("confidence"),
            "source": {
                "kind": (document.get("source") or {}).get("kind", "unknown"),
                "ref": (document.get("source") or {}).get("ref", ""),
            },
            "postings": [
                {
                    "account": None if posting.get("account") is None else project_name(state, posting["account"]),
                    "suggested": posting.get("suggested"),
                    "amount": posting.get("amount"),
                    "currency": posting.get("currency"),
                }
                for posting in document.get("postings", [])
            ],
            "issues": [
                {**issue, "message": project_text(state, issue.get("message", ""))}
                for issue in live_issues
            ],
            "approvable": live_approvable,
            "createdAt": document.get("createdAt"),
            "approved": document.get("approved"),
            "rejected": document.get("rejected"),
        }

    def bootstrap(self) -> dict[str, Any]:
        status = self.status()
        accounts = self.list_accounts(limit=1000)
        drafts = self.list_drafts(limit=300)
        pending = []
        recent = []
        for document in drafts:
            projected = self.project_draft(document)
            if document["status"] == "pending":
                pending.append(projected)
            elif len(recent) < 12:
                recent.append(projected)
        state = self.reader.refresh()
        issues = [
            {
                "code": issue.get("code", ""),
                "message": project_text(state, issue.get("message", "")),
                "file": issue.get("file"),
            }
            for issue in status["issues"]
        ]
        return {
            "today": status["today"],
            "root": status["root"],
            "beanFile": status["beanFile"],
            "operatingCurrency": status["operatingCurrency"],
            "counts": status["counts"],
            "drafts": status["drafts"],
            "issues": issues,
            "accounts": [
                {
                    "account": account["account"],
                    "name": account["name"],
                    "alias": account["alias"],
                    "period": account["period"],
                    "openDate": account["openDate"],
                    "closeDate": account["closeDate"],
                    "currencies": account["currencies"],
                    "lastBalance": (
                        {
                            "date": account["lastBalance"]["date"],
                            "amount": account["lastBalance"]["amount"],
                            "currency": account["lastBalance"]["currency"],
                        }
                        if account["lastBalance"]
                        else None
                    ),
                    "nextDue": account.get("nextDue"),
                    "status": account.get("status"),
                    "coveredThrough": account.get("coveredThrough"),
                    "unreconciled": account.get("unreconciled", False),
                    "closed": account["closed"],
                    "implicit": account["implicit"],
                }
                for account in accounts
            ],
            "pendingDrafts": pending,
            "pendingOperations": self.list_operations(status="pending", limit=300),
            "operations": self.operations.summary(),
            "recentDrafts": recent,
        }
