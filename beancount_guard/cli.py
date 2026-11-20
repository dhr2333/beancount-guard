"""JSON command-line adapter for beancount-guard.

One process, one command, one JSON envelope on stdout. A frontend bridge treats
this as a transport and does no ledger work itself.

    python -m beancount_guard --root /path/to/ledger bootstrap
    python -m beancount_guard --root /path/to/ledger balance <<'JSON'
    {"account": "工行5244", "amount": "-30.00", "currency": "CNY"}
    JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import metadata
from typing import Any

from . import PROTOCOL, __version__
from .core import BeancountGuard, create_beancount_guard
from .errors import LedgerError, fail


def _read_payload() -> dict[str, Any]:
    text = sys.stdin.read()
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except ValueError as error:
        fail("invalid_argument", f"stdin is not valid JSON: {error}")
    if not isinstance(payload, dict):
        fail("invalid_argument", "stdin payload must be a JSON object")
    return payload


def _core_version() -> str:
    """Report the installed distribution version, falling back to the source literal.

    A frontend compares this against PyPI to offer an upgrade, so it must be the
    version of the files actually imported, not a hand-copied string.
    """
    try:
        return metadata.version("beancount-guard")
    except metadata.PackageNotFoundError:
        return __version__


def _emit(value: Any) -> None:
    json.dump({"ok": True, "value": value}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _emit_error(error: LedgerError) -> None:
    json.dump({"ok": False, "error": error.to_dict()}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _account_summary(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": account.get("account"),
        "name": account.get("name"),
        "alias": account.get("alias"),
        "period": account.get("period"),
        "nextDue": account.get("nextDue"),
        "status": account.get("status"),
        "coveredThrough": account.get("coveredThrough"),
        "unreconciled": account.get("unreconciled", False),
        "implicit": account.get("implicit", False),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beancount-guard", description="Beancount write guard and reconciliation core")
    parser.add_argument("--root", default=os.environ.get("LEDGER_ROOT") or os.getcwd(), help="ledger root containing main.bean")
    parser.add_argument("--bean-file", default="main.bean")
    parser.add_argument("--state-dir", default=".ledger")
    parser.add_argument("--bean-check", default=None)
    parser.add_argument("--bean-query", default=None)
    parser.add_argument("--time-zone", default="Asia/Shanghai")
    parser.add_argument("--grace-days", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    commands.add_parser("setup")
    commands.add_parser("init")
    commands.add_parser("bootstrap")
    commands.add_parser("status")
    commands.add_parser("audit-metadata")
    commands.add_parser("validate")

    accounts = commands.add_parser("accounts")
    accounts.add_argument("--query", default=None)
    accounts.add_argument("--include-closed", action="store_true")
    accounts.add_argument("--limit", type=int, default=500)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("name")

    due = commands.add_parser("due")
    due.add_argument("--include-scheduled", action="store_true")

    metadata = commands.add_parser("metadata")
    metadata.add_argument("--json", action="store_true", help="read the request from stdin (always true; kept for symmetry)")

    commands.add_parser("balance")
    list_drafts = commands.add_parser("list-drafts")
    list_drafts.add_argument("--status", default=None)
    list_drafts.add_argument("--limit", type=int, default=100)
    get_draft = commands.add_parser("get-draft")
    get_draft.add_argument("draft_id")
    commands.add_parser("submit-draft")
    commands.add_parser("update-draft")
    commands.add_parser("account-open")
    commands.add_parser("account-redate")
    commands.add_parser("approve-draft")
    commands.add_parser("reject-draft")
    commands.add_parser("quick-draft")
    commands.add_parser("refresh-drafts")
    commands.add_parser("op-approve-all")
    commands.add_parser("approve-all-drafts")
    commands.add_parser("op-submit")
    op_list = commands.add_parser("op-list")
    op_list.add_argument("--status", default=None)
    op_list.add_argument("--limit", type=int, default=100)
    op_get = commands.add_parser("op-get")
    op_get.add_argument("operation_id")
    commands.add_parser("op-approve")
    commands.add_parser("op-reject")
    commands.add_parser("reconcile-preview")
    commands.add_parser("reconcile-commit")
    reconcile_history = commands.add_parser("reconcile-history")
    reconcile_history.add_argument("--account", default=None)
    reconcile_history.add_argument("--limit", type=int, default=50)
    commands.add_parser("query-transactions")
    query_balances = commands.add_parser("query-balances")
    query_balances.add_argument("--as-of", default=None)
    return parser


def _domain(args: argparse.Namespace) -> BeancountGuard:
    return create_beancount_guard(
        root=args.root,
        bean_file=args.bean_file,
        state_dir=args.state_dir,
        bean_check=args.bean_check,
        bean_query=args.bean_query,
        time_zone=args.time_zone,
        grace_days=args.grace_days,
        timeout=args.timeout,
    )


def _dispatch(args: argparse.Namespace, domain: BeancountGuard) -> Any:
    command = args.command
    if command == "health":
        return {
            "root": str(domain.root),
            "beanFile": domain.bean_file,
            "today": domain.today_text(),
            "version": _core_version(),
            "protocol": PROTOCOL,
        }
    if command == "setup":
        return domain.setup()
    if command == "init":
        payload = _read_payload()
        return domain.init_ledger(
            title=payload.get("title") if isinstance(payload.get("title"), str) else "我的账本",
            currency=payload.get("currency") if isinstance(payload.get("currency"), str) else "CNY",
            year=payload.get("year") if isinstance(payload.get("year"), (int, str)) else None,
        )
    if command == "bootstrap":
        return domain.bootstrap()
    if command == "status":
        return domain.status()
    if command == "audit-metadata":
        return domain.audit_metadata()
    if command == "validate":
        return domain.validate()
    if command == "accounts":
        return domain.list_accounts(query=args.query, include_closed=args.include_closed, limit=args.limit)
    if command == "resolve":
        return domain.resolve_alias(args.name)
    if command == "due":
        return domain.get_due_reconciliations(include_scheduled=args.include_scheduled)
    if command == "metadata":
        payload = _read_payload()
        account = payload.get("account")
        if not isinstance(account, str) or account.strip() == "":
            fail("invalid_argument", "account is required")
        kwargs: dict[str, Any] = {"account": account}
        if payload.get("clear_alias") is True:
            kwargs["alias"] = None
        elif "alias" in payload:
            kwargs["alias"] = payload.get("alias")
        if "reconcile" in payload:
            kwargs["reconcile"] = payload.get("reconcile")
        result = domain.set_account_metadata(**kwargs)
        return {"account": _account_summary(result)}
    if command == "balance":
        payload = _read_payload()
        account = payload.get("account")
        amount = payload.get("amount")
        if not isinstance(account, str) or not isinstance(amount, str):
            fail("invalid_argument", "account and amount are required strings")
        result = domain.submit_balance(
            account=account,
            amount=amount,
            currency=payload.get("currency") if isinstance(payload.get("currency"), str) else None,
            date_text=payload.get("date") if isinstance(payload.get("date"), str) else None,
            note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        )
        return {
            "result": {
                "account": result["name"],
                "date": result["date"],
                "amount": result["amount"],
                "currency": result["currency"],
                "file": result["file"],
                "reconciliation": result["reconciliation"],
            }
        }
    if command == "list-drafts":
        drafts = [domain.project_draft(draft) for draft in domain.list_drafts(status=args.status, limit=args.limit)]
        return {"drafts": drafts}
    if command == "get-draft":
        return {"draft": domain.project_draft(domain.get_draft(args.draft_id))}
    if command == "submit-draft":
        stored = domain.submit_draft(_read_payload())
        return {"draft": domain.project_draft(stored["draft"]), "deduplicated": stored["deduplicated"]}
    if command == "update-draft":
        payload = _read_payload()
        draft_id = payload.get("id")
        if not isinstance(draft_id, str):
            fail("invalid_argument", "id is required")
        fields = {key: value for key, value in payload.items() if key not in {"id", "actor"}}
        updated = domain.update_draft(
            draft_id=draft_id,
            payload=fields,
            actor="panel" if payload.get("actor") == "panel" else "user",
        )
        return {"draft": domain.project_draft(updated["draft"])}
    if command == "account-open":
        payload = _read_payload()
        account = payload.get("account")
        if not isinstance(account, str) or account.strip() == "":
            fail("invalid_argument", "account is required")
        currencies = payload.get("currencies")
        if not isinstance(currencies, list):
            currencies = [payload["currency"]] if isinstance(payload.get("currency"), str) else []
        opened = domain.open_account(
            account=account,
            date_text=payload.get("date") if isinstance(payload.get("date"), str) else None,
            currencies=[item for item in currencies if isinstance(item, str)],
            alias=payload.get("alias") if isinstance(payload.get("alias"), str) else None,
            reconcile=payload.get("reconcile") if isinstance(payload.get("reconcile"), str) else None,
            actor="panel" if payload.get("actor") == "panel" else "user",
        )
        return {"account": _account_summary(opened)}
    if command == "account-redate":
        payload = _read_payload()
        date_text = payload.get("date")
        if not isinstance(date_text, str):
            fail("invalid_argument", "date is required")
        accounts = payload.get("accounts")
        if accounts is not None and not isinstance(accounts, list):
            fail("invalid_argument", "accounts must be an array")
        return domain.redate_accounts(
            date_text=date_text,
            accounts=[item for item in (accounts or []) if isinstance(item, str)],
            all_accounts=payload.get("all") is True,
        )
    if command == "op-approve-all":
        return domain.approve_all_operations(actor="panel")
    if command == "approve-all-drafts":
        return domain.approve_all_drafts(actor="panel")
    if command == "refresh-drafts":
        return domain.refresh_drafts()
    if command == "op-submit":
        stored = domain.submit_operation(_read_payload())
        return {"operation": stored["operation"], "deduplicated": stored["deduplicated"]}
    if command == "op-list":
        return {"operations": domain.list_operations(status=args.status, limit=args.limit)}
    if command == "op-get":
        return {"operation": domain.get_operation(args.operation_id)}
    if command == "op-approve":
        payload = _read_payload()
        operation_id = payload.get("id")
        if not isinstance(operation_id, str):
            fail("invalid_argument", "id is required")
        return {"operation": domain.approve_operation(operation_id=operation_id, actor="panel" if payload.get("actor") == "panel" else "user")}
    if command == "op-reject":
        payload = _read_payload()
        operation_id = payload.get("id")
        if not isinstance(operation_id, str):
            fail("invalid_argument", "id is required")
        return {"operation": domain.reject_operation(operation_id=operation_id, reason=payload.get("reason") if isinstance(payload.get("reason"), str) else "", actor="panel" if payload.get("actor") == "panel" else "user")}
    if command == "approve-draft":
        payload = _read_payload()
        draft_id = payload.get("id")
        if not isinstance(draft_id, str):
            fail("invalid_argument", "id is required")
        result = domain.approve_draft(
            draft_id=draft_id,
            postings=payload.get("postings"),
            actor="panel" if payload.get("actor") == "panel" else "user",
        )
        return {
            "committed": result["committed"],
            "recovered": result["recovered"],
            "autoPad": result.get("autoPad"),
            "draft": domain.project_draft(result["draft"]),
        }
    if command == "reconcile-preview":
        return domain.preview_reconciliation(_read_payload())
    if command == "reconcile-commit":
        return domain.commit_reconciliation(_read_payload())
    if command == "reconcile-history":
        return domain.reconcile_history(account=args.account, limit=args.limit)
    if command == "reject-draft":
        payload = _read_payload()
        draft_id = payload.get("id")
        if not isinstance(draft_id, str):
            fail("invalid_argument", "id is required")
        rejected = domain.reject_draft(
            draft_id=draft_id,
            reason=payload.get("reason") if isinstance(payload.get("reason"), str) else "",
            actor="panel" if payload.get("actor") == "panel" else "user",
        )
        return {"draft": domain.project_draft(rejected)}
    if command == "quick-draft":
        payload = _read_payload()
        for key in ("merchant", "amount", "currency", "source_account"):
            if not isinstance(payload.get(key), str):
                fail("invalid_argument", f"{key} is required")
        proposal = domain.propose_quick_draft(
            merchant=payload["merchant"],
            amount=payload["amount"],
            currency=payload["currency"],
            source_account=payload["source_account"],
            date_text=payload.get("date") if isinstance(payload.get("date"), str) else None,
            narration=payload.get("narration") if isinstance(payload.get("narration"), str) else None,
        )
        if not proposal["matched"]:
            return proposal
        return {
            "matched": True,
            "candidate": proposal["candidate"]["name"],
            "draft": domain.project_draft(proposal["draft"]),
            "deduplicated": proposal["deduplicated"],
        }
    if command == "query-transactions":
        payload = _read_payload()
        return domain.query_transactions(
            date_from=payload.get("from") if isinstance(payload.get("from"), str) else None,
            date_to=payload.get("to") if isinstance(payload.get("to"), str) else None,
            account=payload.get("account") if isinstance(payload.get("account"), str) else None,
            limit=int(payload.get("limit", 100)) if isinstance(payload.get("limit"), (int, float)) else 100,
        )
    if command == "query-balances":
        return domain.query_balances(as_of=args.as_of)
    fail("invalid_argument", f"unknown command {command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        domain = _domain(args)
        _emit(_dispatch(args, domain))
        return 0
    except LedgerError as error:
        _emit_error(error)
        return 0
    except Exception as error:  # noqa: BLE001 - the CLI owns the final error envelope
        _emit_error(LedgerError("internal_error", str(error), {}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
