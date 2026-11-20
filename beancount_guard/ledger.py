"""Cached include-closure scan: accounts, aliases, balances and close dates.

The scan is a cache, never an authority. Every read re-stats the files it read
before and rescans when any mtime or size moved, so an edit in the editor, Fava
or Git is visible on the next call.
"""

from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bean import (
    assert_alias,
    is_account_name,
    is_currency,
    normalize_alias,
    parse_balance_line,
    parse_close_line,
    parse_include_line,
    parse_open_line,
    parse_posting_account,
    parse_quoted_string,
    read_metadata_block,
    split_document,
)
from .dates import is_period, iso
from .errors import fail


def resolve_include(including_file: str, spec: str) -> str:
    """Resolve an include path exactly the way Beancount does."""
    expanded = os.path.join(os.path.expanduser("~"), spec[2:]) if spec.startswith("~/") else spec
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(os.path.dirname(including_file), expanded))


def read_ledger_text(file: str | Path) -> str:
    """Read one ledger file that is about to be rewritten.

    A ledger file saved by a Windows editor in the ANSI code page is not
    UTF-8; the rewrite must stop with a stable code instead of letting
    ``UnicodeDecodeError`` escape as an internal failure.
    """
    path = Path(file)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        fail(
            "ledger_encoding_error",
            f"{path.name} 不是 UTF-8 编码（{error.reason}）；请用 UTF-8 重新保存后再写入",
            {"file": str(path), "encoding": error.encoding, "offset": error.start},
        )


class LedgerReader:
    """Cached include-closure reader."""

    def __init__(self, root: str | Path, bean_file: str = "main.bean") -> None:
        self.root = Path(root).resolve()
        self.bean_file = bean_file
        self._cache: tuple[dict[str, Any], str] | None = None

    @property
    def main_file(self) -> Path:
        return (self.root / self.bean_file).resolve()

    def invalidate(self) -> None:
        self._cache = None

    def rel_file(self, file: str | Path) -> str:
        return Path(file).resolve().relative_to(self.root).as_posix()

    def refresh(self) -> dict[str, Any]:
        if self._cache is not None:
            state, signature = self._cache
            current = self._signature(state["files"])
            if current is not None and current == signature:
                return state
            self._cache = None
        state = self.scan()
        signature = self._signature(state["files"])
        if signature is not None:
            self._cache = (state, signature)
        return state

    @staticmethod
    def _signature(files: list[str]) -> str | None:
        parts = []
        for file in files:
            try:
                info = os.stat(file)
            except OSError:
                return None
            parts.append(f"{file}:{info.st_mtime_ns}:{info.st_size}")
        return "|".join(parts)

    def scan(self) -> dict[str, Any]:
        files: list[str] = []
        contents: dict[str, str] = {}
        includes: dict[str, list[dict[str, Any]]] = {}
        issues: list[dict[str, Any]] = []
        seen: set[str] = set()
        queue: deque[str] = deque([str(self.main_file)])

        while queue:
            file = queue.popleft()
            if file in seen:
                continue
            seen.add(file)
            try:
                content = Path(file).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                issues.append(
                    {
                        "code": "invalid_encoding",
                        "message": f"{self.rel_file(file)} 不是 UTF-8 编码；请用 UTF-8 重新保存",
                        "file": self.rel_file(file),
                    }
                )
                continue
            except OSError as error:
                issues.append(
                    {
                        "code": "missing_file" if isinstance(error, FileNotFoundError) else "unreadable_file",
                        "message": f"cannot read {self.rel_file(file)}: {error}",
                        "file": self.rel_file(file),
                    }
                )
                continue
            if content.startswith("\ufeff"):
                content = content[1:]
            files.append(file)
            contents[file] = content

            edges = []
            for line in split_document(content).lines:
                spec = parse_include_line(line)
                if spec is None:
                    continue
                resolved = resolve_include(file, spec)
                missing = resolved not in seen and not os.path.exists(resolved)
                edges.append({"spec": spec, "file": resolved, "missing": missing})
                if not missing:
                    queue.append(resolved)
            includes[file] = edges

        state: dict[str, Any] = {
            "root": str(self.root),
            "bean_file": self.bean_file,
            "main_file": str(self.main_file),
            "files": files,
            "contents": contents,
            "includes": includes,
            "opens": {},
            "closes": {},
            "balances": [],
            "implicit": {},
            "aliases": {},
            "alias_by_account": {},
            "operating_currency": None,
            "issues": issues,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        used_accounts: dict[str, dict[str, Any]] = {}

        for file in files:
            content = contents[file]
            rel_file = self.rel_file(file)
            lines = split_document(content).lines
            directive_date: str | None = None
            for index, line in enumerate(lines):
                if state["operating_currency"] is None:
                    option = _parse_operating_currency(line)
                    if option is not None:
                        state["operating_currency"] = option

                date_match = line[:10]
                if len(date_match) == 10 and date_match[4:5] == "-" and date_match[7:8] == "-":
                    directive_date = date_match

                opened = parse_open_line(line)
                if opened is not None:
                    end, entries = read_metadata_block(lines, index + 1)
                    metadata = _parse_open_metadata(entries, state["issues"], opened["account"], rel_file)
                    if opened["account"] in state["opens"]:
                        state["issues"].append(
                            {
                                "code": "duplicate_open",
                                "message": f"{opened['account']} is opened more than once",
                                "account": opened["account"],
                                "file": rel_file,
                            }
                        )
                    else:
                        state["opens"][opened["account"]] = {
                            "account": opened["account"],
                            "date": opened["date"],
                            "currencies": opened["currencies"],
                            "file": file,
                            "rel_file": rel_file,
                            "line": index + 1,
                            "metadata": metadata,
                            "raw_entries": [{"key": entry["key"], "raw": entry["raw"]} for entry in entries],
                        }
                    continue

                closed = parse_close_line(line)
                if closed is not None:
                    state["closes"][closed["account"]] = {
                        "account": closed["account"],
                        "date": closed["date"],
                        "file": file,
                        "rel_file": rel_file,
                        "line": index + 1,
                    }
                    continue

                balance = parse_balance_line(line)
                if balance is not None:
                    state["balances"].append({**balance, "file": file, "rel_file": rel_file, "line": index + 1})
                    used_accounts.setdefault(
                        balance["account"],
                        {"account": balance["account"], "first_date": balance["date"], "file": file, "rel_file": rel_file, "line": index + 1},
                    )
                    if balance["date"] < used_accounts[balance["account"]]["first_date"]:
                        used_accounts[balance["account"]]["first_date"] = balance["date"]
                    continue

                posting_account = parse_posting_account(line)
                if posting_account is not None and directive_date is not None:
                    used_accounts.setdefault(
                        posting_account,
                        {"account": posting_account, "first_date": directive_date, "file": file, "rel_file": rel_file, "line": index + 1},
                    )
                    if directive_date < used_accounts[posting_account]["first_date"]:
                        used_accounts[posting_account]["first_date"] = directive_date

        for account, used in used_accounts.items():
            if account not in state["opens"]:
                state["implicit"][account] = {**used, "implicit": True}

        for account, opened in state["opens"].items():
            alias = opened["metadata"]["alias"]
            if alias is None:
                continue
            normalized = normalize_alias(alias)
            state["aliases"].setdefault(normalized, []).append(
                {"account": account, "alias": alias, "normalized": normalized}
            )
            state["alias_by_account"][account] = {"alias": alias, "normalized": normalized}
        for entries in state["aliases"].values():
            if len(entries) > 1:
                state["issues"].append(
                    {
                        "code": "duplicate_alias",
                        "message": f'alias "{entries[0]["alias"]}" is bound to '
                        + ", ".join(entry["account"] for entry in entries),
                        "alias": entries[0]["alias"],
                    }
                )
        for account, opened in state["opens"].items():
            period = opened["metadata"]["reconcile"]
            if period is not None and not is_period(period):
                state["issues"].append(
                    {
                        "code": "invalid_period",
                        "message": f'{account} has unsupported reconcile period "{period}"',
                        "account": account,
                        "period": period,
                        "file": opened["rel_file"],
                    }
                )
        return state


def _parse_operating_currency(line: str) -> str | None:
    import re

    match = re.match(r'^[ \t]*option[ \t]+"operating_currency"[ \t]+"([^"]+)"[ \t]*(?:;.*)?$', line)
    if match is None or not is_currency(match.group(1)):
        return None
    return match.group(1)


def _parse_open_metadata(
    entries: list[dict[str, Any]], issues: list[dict[str, Any]], account: str, rel_file: str
) -> dict[str, str | None]:
    result: dict[str, str | None] = {"alias": None, "reconcile": None}
    alias_entry = next((entry for entry in entries if entry["key"] == "alias"), None)
    if alias_entry is not None:
        raw = parse_quoted_string(alias_entry["raw"])
        if raw is None:
            issues.append(
                {"code": "invalid_metadata", "message": f"{account} has an alias that is not a quoted string", "account": account, "file": rel_file}
            )
        else:
            try:
                result["alias"] = assert_alias(raw)
            except Exception as error:
                issues.append(
                    {"code": "invalid_metadata", "message": f"{account} has an invalid alias: {error}", "account": account, "file": rel_file}
                )
    reconcile_entry = next((entry for entry in entries if entry["key"] == "reconcile"), None)
    if reconcile_entry is not None:
        result["reconcile"] = parse_quoted_string(reconcile_entry["raw"]) or reconcile_entry["raw"].strip()
    return result


def has_account(state: dict[str, Any], account: str) -> bool:
    return account in state["opens"] or account in state["implicit"]


def resolve_name(state: dict[str, Any], name: object) -> dict[str, str] | None:
    text = str(name or "").strip()
    if not text:
        return None
    if has_account(state, text):
        return {"account": text, "matched": "canonical"}
    entries = state["aliases"].get(normalize_alias(text), [])
    if not entries:
        return None
    if len(entries) > 1:
        fail(
            "ambiguous_alias",
            f'alias "{text}" is bound to more than one account',
            {"candidates": [entry["account"] for entry in entries]},
        )
    return {"account": entries[0]["account"], "matched": "alias", "alias": entries[0]["alias"]}


def project_name(state: dict[str, Any], account: str) -> str:
    entry = state["alias_by_account"].get(account)
    return entry["alias"] if entry else account


def project_text(state: dict[str, Any], text: str) -> str:
    output = str(text)
    replacements = sorted(
        ((account, entry["alias"]) for account, entry in state["alias_by_account"].items() if entry["alias"] != account),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for account, alias in replacements:
        if account in output:
            output = output.replace(account, alias)
    return output


def last_balance_of(state: dict[str, Any], account: str) -> dict[str, Any] | None:
    latest = None
    for balance in state["balances"]:
        if balance["account"] != account:
            continue
        if latest is None or balance["date"] > latest["date"] or (
            balance["date"] == latest["date"] and balance["line"] > latest["line"]
        ):
            latest = balance
    return dict(latest) if latest else None


def is_closed_on(state: dict[str, Any], account: str, when: str) -> bool:
    closed = state["closes"].get(account)
    return closed is not None and closed["date"] <= when


def list_account_states(state: dict[str, Any]) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for account, opened in state["opens"].items():
        alias_entry = state["alias_by_account"].get(account)
        closed = state["closes"].get(account)
        accounts.append(
            {
                "account": account,
                "name": alias_entry["alias"] if alias_entry else account,
                "alias": alias_entry["alias"] if alias_entry else None,
                "period": opened["metadata"]["reconcile"],
                "openDate": opened["date"],
                "currencies": opened["currencies"],
                "closeDate": closed["date"] if closed else None,
                "file": opened["rel_file"],
                "line": opened["line"],
                "implicit": False,
                "lastBalance": last_balance_of(state, account),
            }
        )
    for account, implicit in state["implicit"].items():
        accounts.append(
            {
                "account": account,
                "name": account,
                "alias": None,
                "period": None,
                "openDate": implicit["first_date"],
                "currencies": [],
                "closeDate": None,
                "file": None,
                "line": None,
                "implicit": True,
                "lastBalance": last_balance_of(state, account),
            }
        )
    accounts.sort(key=lambda account: account["account"])
    return accounts


def audit_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    findings = list(state["issues"])
    for alias, entries in state["aliases"].items():
        if len(entries) != 1:
            continue
        account = entries[0]["account"]
        canonical = entries[0]["alias"]
        if has_account(state, canonical):
            findings.append(
                {
                    "code": "alias_shadows_account",
                    "message": f'alias "{canonical}" of {account} equals the canonical account {canonical}',
                    "account": account,
                    "alias": canonical,
                }
            )
    for account, opened in state["opens"].items():
        if not is_account_name(account):
            findings.append({"code": "invalid_account", "message": f"invalid account name {account}", "account": account, "file": opened["rel_file"]})
        reconcile = (opened.get("metadata") or {}).get("reconcile")
        if reconcile and account.split(":", 1)[0] not in {"Assets", "Liabilities"}:
            findings.append(
                {
                    "code": "invalid_reconcile_account",
                    "message": f"{account} is not an asset or liability account but has reconcile metadata",
                    "account": account,
                    "file": opened["rel_file"],
                }
            )
        for currency in opened["currencies"]:
            if not is_currency(currency):
                findings.append(
                    {
                        "code": "invalid_currency",
                        "message": f"{account} declares invalid currency {currency}",
                        "account": account,
                        "currency": currency,
                        "file": opened["rel_file"],
                    }
                )
    seen: set[tuple[str, str]] = set()
    result = []
    for finding in findings:
        key = (finding.get("code", ""), finding.get("message", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def parse_date_text(text: str) -> str:
    """Validate and return an ISO date string."""
    from .dates import parse_date

    return iso(parse_date(text))
