"""End-to-end tests for the ledger domain core on disposable ledger copies."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ledger_domain import LedgerError, create_ledger_domain
from ledger_domain.bean import render_transaction

NOW = datetime(2026, 11, 17, 4, 0, tzinfo=timezone.utc)  # 12:00 Asia/Shanghai
TODAY = "2026-11-17"
ASSETS = Path("/home/daihaorui/桌面/GitHub/Assets")
BEAN_CHECK = os.environ.get("BEAN_CHECK") or (
    str(ASSETS / ".venv/bin/bean-check") if (ASSETS / ".venv/bin/bean-check").exists() else "bean-check"
)
BEAN_QUERY = os.environ.get("BEAN_QUERY") or (
    str(ASSETS / ".venv/bin/bean-query") if (ASSETS / ".venv/bin/bean-query").exists() else "bean-query"
)

MAIN = """option "operating_currency" "CNY"

include "account/assets.bean"
include "account/liabilities.bean"
include "account/expenses.bean"
include "account/income.bean"
include "account/equity.bean"
include "trans/main.bean"
"""

ACCOUNTS = """; 账户定义

2026-01-01 open Assets:Bank:Test CNY  ; 测试卡
  alias: "测试卡"
  reconcile: "monthly"

2026-01-01 open Liabilities:CreditCard:Test CNY
  alias: "信用卡"

2026-01-01 open Expenses:Food CNY
  alias: "餐饮"

2026-01-01 open Expenses:Food:Coffee CNY
  alias: "星巴克"
  reconcile: "none"

2026-01-01 open Assets:Bank:Unmapped CNY  ; 未映射卡

2026-01-01 open Assets:Bank:Closed CNY
2026-02-01 close Assets:Bank:Closed

2026-01-01 open Equity:Opening-Balances CNY
2026-01-01 open Equity:Adjustments CNY
"""

YEAR_INDEX = 'include "collect.bean"\ninclude "reconciliation.bean"\n'

JANUARY = """2026-01-05 * "Cafe" "coffee"
  Expenses:Food    30.00 CNY
  Assets:Bank:Test
"""


class Fixture:
    """One isolated ledger copy plus a domain client."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ledger-domain-test-"))
        (self.root / "account").mkdir()
        (self.root / "trans").mkdir()
        (self.root / "main.bean").write_text(MAIN, encoding="utf-8")
        (self.root / "account/assets.bean").write_text(ACCOUNTS, encoding="utf-8")
        for name in ("liabilities", "expenses", "income", "equity"):
            (self.root / f"account/{name}.bean").write_text("; empty\n", encoding="utf-8")
        (self.root / "trans/main.bean").write_text(YEAR_INDEX, encoding="utf-8")
        (self.root / "trans/collect.bean").write_text(JANUARY, encoding="utf-8")
        (self.root / "trans/reconciliation.bean").write_text("; reconciliation\n", encoding="utf-8")

    def domain(self):
        return create_ledger_domain(
            root=self.root,
            bean_check=BEAN_CHECK,
            bean_query=BEAN_QUERY,
            now=lambda: NOW,
            time_zone="Asia/Shanghai",
            grace_days=3,
        )

    def dispose(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class DomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.ledger = self.fixture.domain()

    def tearDown(self) -> None:
        self.fixture.dispose()

    def assert_code(self, code: str, call) -> LedgerError:
        with self.assertRaises(LedgerError) as caught:
            call()
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception

    def read(self, relative: str) -> str:
        return (self.fixture.root / relative).read_text(encoding="utf-8")

    def test_init_creates_a_valid_ledger(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="ledger-domain-init-"))
        self.addCleanup(shutil.rmtree, parent, True)
        root = parent / "我的账本"
        ledger = create_ledger_domain(root=root, now=lambda: NOW)
        self.assertFalse(ledger.setup()["initialized"])
        result = ledger.init_ledger(title="测试账本", currency="CNY")
        self.assertTrue(result["validated"])
        self.assertTrue((root / "main.bean").is_file())
        self.assertTrue((root / "account/assets.bean").is_file())
        self.assertTrue((root / "2026/11.bean").is_file())
        self.assertTrue((root / "trans/main.bean").is_file())
        self.assertEqual(ledger.setup()["title"], "测试账本")
        self.assertEqual(ledger.validate(), {"ok": True, "diagnostic": ""})
        names = [account["name"] for account in ledger.list_accounts()]
        self.assertIn("中国银行", names)
        self.assertIn("招商银行", names)
        self.assertEqual(ledger.resolve_alias("中国银行")["account"], "Assets:Savings:Bank:BOC")
        self.assertEqual(ledger.resolve_alias("招商银行")["account"], "Liabilities:CreditCard:Bank:CMB")
        self.assert_code("ledger_exists", lambda: ledger.init_ledger(title="再一次"))

    def test_init_refuses_a_non_empty_directory(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="ledger-domain-init-"))
        self.addCleanup(shutil.rmtree, parent, True)
        parent.joinpath("keep.txt").write_text("x", encoding="utf-8")
        ledger = create_ledger_domain(root=parent, now=lambda: NOW)
        self.assert_code("directory_not_empty", lambda: ledger.init_ledger(title="测试"))

    # ---------------------------------------------------------------- read side

    def test_metadata_parse_and_alias_resolution(self) -> None:
        accounts = self.ledger.list_accounts()
        account = next(item for item in accounts if item["account"] == "Assets:Bank:Test")
        self.assertEqual(account["alias"], "测试卡")
        self.assertEqual(account["period"], "monthly")
        self.assertEqual(account["name"], "测试卡")
        self.assertEqual(
            self.ledger.resolve_alias("测试卡"),
            {"account": "Assets:Bank:Test", "matched": "alias", "alias": "测试卡"},
        )
        self.assertEqual(
            self.ledger.resolve_alias("Assets:Bank:Test"),
            {"account": "Assets:Bank:Test", "matched": "canonical"},
        )
        self.assertIsNone(self.ledger.resolve_alias("不存在"))
        self.assertEqual(self.ledger.project_account("Assets:Bank:Test"), "测试卡")
        self.assertEqual(self.ledger.project_account("Assets:Bank:Unmapped"), "Assets:Bank:Unmapped")

    def test_rebuild_due_from_opens_and_balances(self) -> None:
        before = self.ledger.get_due_reconciliations()
        self.assertEqual(before["today"], TODAY)
        reminder = next(item for item in before["reminders"] if item["account"] == "Assets:Bank:Test")
        self.assertEqual(reminder["status"], "due")
        self.assertEqual(reminder["unreconciled"], True)
        self.assertEqual(reminder["anchorKind"], "open")
        self.assertEqual(reminder["nextDue"], TODAY)
        self.assertFalse(any(item["account"] == "Expenses:Food:Coffee" for item in before["reminders"]))

        result = self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text=TODAY)
        self.assertEqual(result["name"], "测试卡")
        self.assertEqual(result["reconciliation"]["status"], "scheduled")
        self.assertEqual(result["reconciliation"]["nextDue"], "2026-12-16")
        self.assertEqual(result["reconciliation"]["anchorKind"], "balance")
        self.assertIn("balance Assets:Bank:Test -30.00 CNY", self.read("trans/reconciliation.bean"))
        bootstrap = self.ledger.bootstrap()
        view = next(item for item in bootstrap["accounts"] if item["account"] == "Assets:Bank:Test")
        self.assertEqual(view["status"], "scheduled")
        self.assertEqual(view["nextDue"], "2026-12-16")

    # --------------------------------------------------------------- write gate

    def test_wrong_balance_writes_nothing(self) -> None:
        before = self.read("trans/reconciliation.bean")
        error = self.assert_code(
            "balance_mismatch",
            lambda: self.ledger.submit_balance(account="测试卡", amount="0.00", currency="CNY", date_text=TODAY),
        )
        self.assertEqual(error.details["expected"], "0.00")
        self.assertEqual(error.details["accumulated"], "-30.00")
        self.assertEqual(self.read("trans/reconciliation.bean"), before)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_future_and_duplicate_balance(self) -> None:
        self.assert_code(
            "future_date",
            lambda: self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text="2026-11-18"),
        )
        self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text=TODAY)
        self.assert_code(
            "balance_exists",
            lambda: self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text=TODAY),
        )

    def test_alias_governance_conflict_clear_and_comment(self) -> None:
        file = self.fixture.root / "account/assets.bean"
        before = file.read_text(encoding="utf-8")
        self.assert_code(
            "alias_conflict",
            lambda: self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", alias="测试卡"),
        )
        self.assertEqual(file.read_text(encoding="utf-8"), before)
        self.assert_code(
            "invalid_period",
            lambda: self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", reconcile="hourly"),
        )
        self.assertEqual(file.read_text(encoding="utf-8"), before)

        updated = self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", alias="工行卡", reconcile="quarterly")
        self.assertEqual(updated["alias"], "工行卡")
        self.assertEqual(updated["period"], "quarterly")
        self.assertIn("未映射卡", file.read_text(encoding="utf-8"))
        self.assertEqual(self.ledger.resolve_alias("工行卡")["account"], "Assets:Bank:Unmapped")

        fresh = create_ledger_domain(
            root=self.fixture.root, bean_check=BEAN_CHECK, bean_query=BEAN_QUERY, now=lambda: NOW
        )
        self.assertEqual(fresh.resolve_alias("工行卡")["account"], "Assets:Bank:Unmapped")

        self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", alias=None)
        self.assertIsNone(self.ledger.resolve_alias("工行卡"))
        self.assertIn("未映射卡", file.read_text(encoding="utf-8"))
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_external_metadata_edit_is_observed(self) -> None:
        file = self.fixture.root / "account/assets.bean"
        content = file.read_text(encoding="utf-8")
        file.write_text(
            content.replace(
                "2026-01-01 open Assets:Bank:Unmapped CNY  ; 未映射卡",
                '2026-01-01 open Assets:Bank:Unmapped CNY  ; 未映射卡\n  alias: "外部绑定"',
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.ledger.resolve_alias("外部绑定")["account"], "Assets:Bank:Unmapped")

    # ------------------------------------------------------------------- drafts

    def test_draft_submit_dedupe_approve_reject(self) -> None:
        payload = {
            "date": TODAY,
            "payee": "餐厅",
            "narration": "午饭",
            "postings": [
                {"account": "餐饮", "amount": "38.00", "currency": "CNY"},
                {"account": "测试卡", "amount": "-38.00", "currency": "CNY"},
            ],
            "source": {"kind": "ai", "ref": "receipt-1", "evidence": {"file": "receipt.jpg"}},
            "confidence": 0.9,
        }
        first = self.ledger.submit_draft(payload)
        self.assertFalse(first["deduplicated"])
        self.assertEqual(first["draft"]["status"], "pending")
        self.assertEqual(first["issues"], [])
        self.assertEqual(first["draft"]["postings"][0]["account"], "Expenses:Food")
        self.assertEqual(first["draft"]["postings"][1]["account"], "Assets:Bank:Test")

        second = self.ledger.submit_draft(payload)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["draft"]["id"], first["draft"]["id"])

        approved = self.ledger.approve_draft(draft_id=first["draft"]["id"], actor="test")
        self.assertTrue(approved["committed"])
        self.assertEqual(approved["draft"]["status"], "approved")
        november = self.read("trans/collect.bean")
        self.assertIn('draft_id: "draft-', november)
        self.assertIn("Expenses:Food  38.00 CNY", november)
        self.assertIn("Assets:Bank:Test  -38.00 CNY", november)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

        again = self.ledger.approve_draft(draft_id=first["draft"]["id"], actor="test")
        self.assertFalse(again["committed"])

        rejected_payload = {
            **payload,
            "payee": "另一家",
            "narration": "晚饭",
            "postings": [
                {"account": "餐饮", "amount": "20.00", "currency": "CNY"},
                {"account": "测试卡", "amount": "-20.00", "currency": "CNY"},
            ],
        }
        third = self.ledger.submit_draft(rejected_payload)
        rejected = self.ledger.reject_draft(draft_id=third["draft"]["id"], reason="not mine")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.ledger.list_drafts(status="pending"), [])

    def test_unresolved_and_unbalanced_stay_out(self) -> None:
        before = self.read("trans/collect.bean")
        unresolved = self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "陌生商户",
                "narration": "无法分类",
                "postings": [
                    {"account": "待分类", "amount": "12.00", "currency": "CNY"},
                    {"account": "测试卡", "amount": "-12.00", "currency": "CNY"},
                ],
            }
        )
        self.assertFalse(unresolved["draft"]["approvable"])
        self.assertEqual(unresolved["issues"][0]["code"], "unresolved_account")
        self.assert_code(
            "unresolved_postings",
            lambda: self.ledger.approve_draft(draft_id=unresolved["draft"]["id"]),
        )
        self.assertEqual(self.read("trans/collect.bean"), before)

        unbalanced = self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "餐厅",
                "narration": "少记了",
                "postings": [
                    {"account": "餐饮", "amount": "38.00", "currency": "CNY"},
                    {"account": "测试卡", "amount": "-30.00", "currency": "CNY"},
                ],
            }
        )
        self.assertFalse(unbalanced["draft"]["approvable"])
        self.assert_code("unbalanced", lambda: self.ledger.approve_draft(draft_id=unbalanced["draft"]["id"]))
        self.assertEqual(self.read("trans/collect.bean"), before)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_quick_path(self) -> None:
        proposal = self.ledger.propose_quick_draft(
            merchant="星巴克咖啡（国贸店）",
            amount="38.00",
            currency="CNY",
            source_account="测试卡",
            date_text=TODAY,
        )
        self.assertTrue(proposal["matched"])
        self.assertEqual(proposal["candidate"]["name"], "星巴克")
        self.assertEqual(proposal["draft"]["postings"][0]["account"], "Expenses:Food:Coffee")
        self.assertEqual(proposal["draft"]["postings"][1]["account"], "Assets:Bank:Test")
        approved = self.ledger.approve_draft(draft_id=proposal["draft"]["id"])
        self.assertTrue(approved["committed"])
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})
        self.assertIn("Expenses:Food:Coffee  38.00 CNY", self.read("trans/collect.bean"))

    # -------------------------------------------------------------------- query

    def test_query_projection(self) -> None:
        transactions = self.ledger.query_transactions(date_from="2026-01-01", date_to="2026-11-30")
        self.assertEqual(len(transactions["transactions"]), 1)
        posting_names = sorted(posting["account"] for posting in transactions["transactions"][0]["postings"])
        self.assertEqual(posting_names, ["测试卡", "餐饮"])
        encoded = json.dumps(transactions, ensure_ascii=False)
        self.assertNotIn("Expenses:Food", encoded)
        self.assertNotIn("Assets:Bank:Test", encoded)

        balances = self.ledger.query_balances(as_of="2026-11-30")
        names = sorted(entry["account"] for entry in balances["balances"])
        self.assertEqual(names, ["测试卡", "餐饮"])
        self.assertNotIn("Assets:Bank", json.dumps(balances, ensure_ascii=False))

    # ------------------------------------------------------------ implicit + recovery

    def test_implicit_accounts_are_read_only_for_metadata(self) -> None:
        main = self.fixture.root / "main.bean"
        main.write_text(
            main.read_text(encoding="utf-8") + '\nplugin "beancount.plugins.auto_accounts"\n',
            encoding="utf-8",
        )
        january = self.fixture.root / "trans/collect.bean"
        january.write_text(
            january.read_text(encoding="utf-8")
            + '\n2026-01-07 * "Implicit" "auto account"\n  Expenses:Food  5.00 CNY\n  Assets:Bank:Implicit\n',
            encoding="utf-8",
        )
        ledger = self.fixture.domain()
        account = ledger.account("Assets:Bank:Implicit")
        self.assertTrue(account["implicit"])
        self.assertEqual(account["openDate"], "2026-01-07")
        self.assertEqual(ledger.resolve_alias("Assets:Bank:Implicit")["account"], "Assets:Bank:Implicit")
        result = ledger.submit_balance(account="Assets:Bank:Implicit", amount="-5.00", currency="CNY", date_text=TODAY)
        self.assertEqual(result["file"], "trans/reconciliation.bean")
        self.assertEqual(ledger.validate(), {"ok": True, "diagnostic": ""})
        self.assert_code(
            "account_not_opened",
            lambda: ledger.set_account_metadata(account="Assets:Bank:Implicit", alias="隐式卡"),
        )

    def test_existing_balance_is_refused(self) -> None:
        target = self.fixture.root / "trans/reconciliation.bean"
        target.write_text("; reconciliation\n\n2026-10-15 balance Assets:Bank:Test -30.00 CNY\n", encoding="utf-8")
        self.assert_code(
            "balance_exists",
            lambda: self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text="2026-10-15"),
        )
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_draft_target_file_recovery(self) -> None:
        submitted = self.ledger.submit_draft(
            {
                "date": "2026-12-05",
                "payee": "餐厅",
                "narration": "晚饭",
                "postings": [
                    {"account": "餐饮", "amount": "20.00", "currency": "CNY"},
                    {"account": "测试卡", "amount": "-20.00", "currency": "CNY"},
                ],
            }
        )
        block = render_transaction(
            {
                "date": "2026-12-05",
                "payee": "餐厅",
                "narration": "晚饭",
                "draft_id": submitted["draft"]["id"],
                "postings": [
                    {"account": "Expenses:Food", "amount": "20.00", "currency": "CNY"},
                    {"account": "Assets:Bank:Test", "amount": "-20.00", "currency": "CNY"},
                ],
            }
        )
        (self.fixture.root / "trans/collect.bean").write_text(f"; collect\n\n{block}\n", encoding="utf-8")
        approved = self.ledger.approve_draft(draft_id=submitted["draft"]["id"])
        self.assertFalse(approved["committed"])
        self.assertTrue(approved["recovered"])
        text = self.read("trans/collect.bean")
        self.assertEqual(text.count(f'draft_id: "{submitted["draft"]["id"]}"'), 1)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_open_account_writes_assets_file(self) -> None:
        opened = self.ledger.open_account(
            account="Assets:Bank:Ningbo",
            currencies=["CNY"],
            alias="宁波银行",
            reconcile="monthly",
        )
        self.assertEqual(opened["account"], "Assets:Bank:Ningbo")
        self.assertEqual(opened["alias"], "宁波银行")
        self.assertEqual(opened["period"], "monthly")
        text = self.read("account/assets.bean")
        self.assertIn("open Assets:Bank:Ningbo CNY", text)
        self.assertIn('alias: "宁波银行"', text)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

        self.assert_code(
            "account_exists",
            lambda: self.ledger.open_account(account="Assets:Bank:Ningbo", currencies=["CNY"]),
        )
        self.assert_code(
            "not_reconcilable",
            lambda: self.ledger.open_account(account="Expenses:Food:Snack", currencies=["CNY"], reconcile="monthly"),
        )

    def test_redate_moves_open_dates_earlier_only(self) -> None:
        result = self.ledger.redate_accounts(date_text="2025-12-01", all_accounts=True)
        changed = {item["account"]: item for item in result["changed"]}
        self.assertEqual(changed["Assets:Bank:Test"]["from"], "2026-01-01")
        self.assertEqual(changed["Assets:Bank:Test"]["to"], "2025-12-01")
        text = self.read("account/assets.bean")
        self.assertIn("2025-12-01 open Assets:Bank:Test CNY", text)
        self.assertNotIn("2026-01-01 open Assets:Bank:Test CNY", text)
        # 注释与 metadata 原样保留
        self.assertIn('alias: "测试卡"', text)
        self.assertIn('reconcile: "monthly"', text)
        self.assertIn("; 测试卡", text)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

        # 已早于目标日的账户不动
        again = self.ledger.redate_accounts(date_text="2025-12-01", all_accounts=True)
        self.assertEqual(again["changed"], [])

        # 只能前移：显式改晚会被拒绝
        self.assert_code(
            "date_not_earlier",
            lambda: self.ledger.redate_accounts(date_text="2026-06-01", accounts=["Assets:Bank:Test"]),
        )
        self.assert_code(
            "future_date",
            lambda: self.ledger.redate_accounts(date_text="2099-01-01", accounts=["Assets:Bank:Test"]),
        )
        self.assert_code("account_not_found", lambda: self.ledger.redate_accounts(date_text="2025-12-01", accounts=["不存在"]))

    def test_redate_resolves_alias(self) -> None:
        self.ledger.redate_accounts(date_text="2025-12-01", accounts=["测试卡"])
        self.assertIn("2025-12-01 open Assets:Bank:Test CNY", self.read("account/assets.bean"))
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_operation_waits_for_approval(self) -> None:
        before = self.read("account/assets.bean")
        submitted = self.ledger.submit_operation(
            {
                "kind": "account-redate",
                "payload": {"date": "2025-12-01", "all": True},
                "reason": "导入历史账单",
                "proposedBy": "agent",
            }
        )
        operation = submitted["operation"]
        self.assertEqual(operation["status"], "pending")
        self.assertTrue(operation["approvable"])
        # 入队不等于落盘
        self.assertEqual(self.read("account/assets.bean"), before)
        self.assertEqual(self.ledger.account("Assets:Bank:Test")["openDate"], "2026-01-01")

        approved = self.ledger.approve_operation(operation_id=operation["id"])
        self.assertEqual(approved["status"], "approved")
        self.assertIn("2025-12-01 open Assets:Bank:Test CNY", self.read("account/assets.bean"))
        self.assertEqual(self.ledger.operations.summary(), {"pending": 0, "approved": 1, "rejected": 0})
        # 重复提交同内容应去重
        again = self.ledger.submit_operation(
            {"kind": "account-redate", "payload": {"date": "2025-12-01", "all": True}}
        )
        self.assertFalse(again["deduplicated"])

    def test_operation_reject_leaves_ledger_untouched(self) -> None:
        before = self.read("account/assets.bean")
        submitted = self.ledger.submit_operation(
            {"kind": "account-open", "payload": {"account": "Assets:Bank:New", "currencies": ["CNY"]}}
        )
        rejected = self.ledger.reject_operation(operation_id=submitted["operation"]["id"], reason="不需要")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNone(self.ledger.account("Assets:Bank:New"))
        self.assertEqual(self.read("account/assets.bean"), before)
        self.assert_code("operation_not_pending", lambda: self.ledger.approve_operation(operation_id=submitted["operation"]["id"]))

    def test_operation_rejects_bad_shape(self) -> None:
        self.assert_code(
            "invalid_operation",
            lambda: self.ledger.submit_operation({"kind": "delete-ledger", "payload": {}}),
        )
        duplicate = self.ledger.submit_operation(
            {"kind": "account-open", "payload": {"account": "Assets:Bank:Test", "currencies": ["CNY"]}}
        )
        self.assertEqual([issue["code"] for issue in duplicate["operation"]["issues"]], ["account_exists"])

    def test_approving_structure_resolves_waiting_drafts(self) -> None:
        draft = self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "新卡消费",
                "postings": [
                    {"account": "Expenses:Food", "amount": "12.00", "currency": "CNY"},
                    {"account": "Assets:Bank:Later", "amount": "-12.00", "currency": "CNY"},
                ],
            }
        )["draft"]
        self.assertFalse(draft["approvable"])
        self.assertEqual([i["code"] for i in draft["issues"]], ["unresolved_account"])

        operation = self.ledger.submit_operation(
            {
                "kind": "account-open",
                "payload": {"account": "Assets:Bank:Later", "date": "2026-01-01", "currencies": ["CNY"]},
            }
        )["operation"]
        approved = self.ledger.approve_operation(operation_id=operation["id"])
        self.assertEqual(approved.get("refreshedDrafts", {}).get("drafts"), 1)

        refreshed = self.ledger.get_draft(draft["id"])
        self.assertTrue(refreshed["approvable"])
        self.assertEqual(refreshed["issues"], [])
        self.ledger.approve_draft(draft_id=draft["id"])
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_approve_all_drafts_skips_blocked(self) -> None:
        for index in range(3):
            self.ledger.submit_draft(
                {
                    "date": TODAY,
                    "payee": f"批量{index}",
                    "postings": [
                        {"account": "Expenses:Food", "amount": "1.00", "currency": "CNY"},
                        {"account": "Assets:Bank:Test", "amount": "-1.00", "currency": "CNY"},
                    ],
                }
            )
        # 一张卡不存在的草稿，应当被跳过而不是报错中断
        self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "卡住的一笔",
                "postings": [
                    {"account": "Expenses:Food", "amount": "2.00", "currency": "CNY"},
                    {"account": "Assets:Bank:Missing", "amount": "-2.00", "currency": "CNY"},
                ],
            }
        )
        result = self.ledger.approve_all_drafts()
        self.assertEqual(result["approved"], 3)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.ledger.drafts.summary(), {"pending": 1, "approved": 3, "rejected": 0})
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_approve_all_operations_continues_past_failure(self) -> None:
        self.ledger.submit_operation(
            {"kind": "account-open", "payload": {"account": "Assets:Bank:Batch", "currencies": ["CNY"]}}
        )
        # 余额与账本不符，批准时应失败并留在待办
        self.ledger.submit_operation(
            {"kind": "balance", "payload": {"account": "Assets:Bank:Batch", "amount": "999.00", "currency": "CNY"}}
        )
        result = self.ledger.approve_all_operations()
        self.assertEqual(result["approved"], 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["code"], "balance_mismatch")
        self.assertEqual(self.ledger.operations.summary(), {"pending": 1, "approved": 1, "rejected": 0})
        self.assertIsNotNone(self.ledger.account("Assets:Bank:Batch"))

    def test_update_draft_persists_fields(self) -> None:
        submitted = self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "旧商户",
                "narration": "旧备注",
                "tags": ["Old"],
                "postings": [
                    {"account": "餐饮", "amount": "20.00", "currency": "CNY"},
                    {"account": "测试卡", "amount": "-20.00", "currency": "CNY"},
                ],
            }
        )
        draft_id = submitted["draft"]["id"]
        updated = self.ledger.update_draft(
            draft_id=draft_id,
            payload={"payee": "新商户", "narration": "新备注", "tags": ["New"]},
        )
        self.assertEqual(updated["draft"]["payee"], "新商户")
        self.assertEqual(updated["draft"]["narration"], "新备注")
        self.assertEqual(updated["draft"]["tags"], ["New"])
        self.assertIn('"payee": "新商户"', self.read(f".ledger/drafts/{draft_id}.json"))
        approved = self.ledger.approve_draft(draft_id=draft_id)
        self.assertTrue(approved["committed"])
        collect = self.read("trans/collect.bean")
        self.assertIn("新商户", collect)
        self.assertIn("新备注", collect)
        self.assert_code(
            "draft_not_pending",
            lambda: self.ledger.update_draft(draft_id=draft_id, payload={"payee": "again"}),
        )

    # ------------------------------------------------------------ concurrency / month

    def test_concurrent_writes_serialize(self) -> None:
        def first():
            return self.fixture.domain().submit_balance(
                account="测试卡", amount="-30.00", currency="CNY", date_text="2026-11-16"
            )

        def second():
            return self.fixture.domain().submit_balance(
                account="Assets:Bank:Unmapped", amount="0.00", currency="CNY", date_text="2026-11-16"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda fn: fn(), [first, second]))
        self.assertEqual(len(results), 2)
        reconciliation = self.read("trans/reconciliation.bean")
        self.assertIn("balance Assets:Bank:Test -30.00 CNY", reconciliation)
        self.assertIn("balance Assets:Bank:Unmapped 0.00 CNY", reconciliation)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_missing_reconciliation_target_is_refused(self) -> None:
        (self.fixture.root / "trans/reconciliation.bean").unlink()
        self.assert_code(
            "ledger_not_initialized",
            lambda: self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text=TODAY),
        )


class ReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.ledger = self.fixture.domain()

    def tearDown(self) -> None:
        self.fixture.dispose()

    def preview_commit(self, **overrides):
        payload = {
            "account": "测试卡",
            "asOf": TODAY,
            "actual": "-30.00",
            "allocations": [],
            "padAccount": None,
        }
        payload.update(overrides)
        preview = self.ledger.preview_reconciliation(payload)
        request = {
            **payload,
            "mode": preview["mode"],
            "previewId": preview["previewId"],
            "ledgerRevision": preview["ledgerRevision"],
            "inputsHash": preview["inputsHash"],
            "directivesHash": preview["directivesHash"],
            "replaces": preview.get("replaces"),
        }
        return preview, self.ledger.commit_reconciliation(request)

    def test_first_due_is_unreconciled_and_balance_anchor(self) -> None:
        reminder = next(
            item for item in self.ledger.get_due_reconciliations()["reminders"] if item["account"] == "Assets:Bank:Test"
        )
        self.assertEqual(reminder["status"], "due")
        self.assertTrue(reminder["unreconciled"])
        self.assertEqual(reminder["nextDue"], TODAY)
        self.assertIsNone(reminder["coveredThrough"])

        preview, result = self.preview_commit()
        self.assertEqual(preview["expected"], "-30.00")
        self.assertEqual(preview["balanceDate"], "2026-11-18")
        self.assertIn("2026-11-18 balance Assets:Bank:Test -30.00 CNY", self.fixture.root.joinpath("trans/reconciliation.bean").read_text())
        self.assertTrue(result["committed"])

        account = self.ledger.account("测试卡")
        self.assertEqual(account["status"], "scheduled")
        self.assertEqual(account["nextDue"], "2026-12-17")
        self.assertEqual(account["coveredThrough"], TODAY)
        self.assertFalse(account["unreconciled"])
        self.assertTrue(self.ledger.validate()["ok"])

    def test_preview_commit_with_allocation_and_pad(self) -> None:
        preview = self.ledger.preview_reconciliation(
            {
                "account": "测试卡",
                "asOf": TODAY,
                "actual": "-40.00",
                "allocations": [
                    {"account": "Expenses:Food", "amount": "20.00", "date": "2026-11-10", "note": "补记", "tags": ["Discretionary"]},
                    {"account": "Equity:Adjustments", "mode": "remainder"},
                ],
            }
        )
        self.assertEqual(preview["expected"], "-30.00")
        self.assertEqual(preview["difference"], "-10.00")
        self.assertEqual(preview["remaining"], "10.00")
        self.assertIn("pad Assets:Bank:Test Equity:Adjustments", preview["directives"])
        self.assertIn("2026-11-18 balance Assets:Bank:Test -40.00 CNY", preview["directives"])
        request = {
            "account": "测试卡",
            "asOf": TODAY,
            "actual": "-40.00",
            "allocations": [
                {"account": "Expenses:Food", "amount": "20.00", "date": "2026-11-10", "note": "补记", "tags": ["Discretionary"]},
                {"account": "Equity:Adjustments", "mode": "remainder"},
            ],
            "mode": preview["mode"],
            "previewId": preview["previewId"],
            "ledgerRevision": preview["ledgerRevision"],
            "inputsHash": preview["inputsHash"],
            "directivesHash": preview["directivesHash"],
            "replaces": preview.get("replaces"),
        }
        result = self.ledger.commit_reconciliation(request)
        self.assertTrue(result["committed"])
        history = self.ledger.reconcile_history(account="测试卡")["records"]
        self.assertEqual(history[0]["status"], "active")
        self.assertTrue(self.ledger.validate()["ok"])

    def test_update_same_asof_replaces_block(self) -> None:
        self.preview_commit()
        preview, result = self.preview_commit(actual="-50.00")
        self.assertEqual(preview["mode"], "update")
        self.assertEqual(preview["expected"], "-30.00")
        self.assertTrue(result["committed"])
        content = self.fixture.root.joinpath("trans/reconciliation.bean").read_text()
        self.assertEqual(content.count("2026-11-18 balance Assets:Bank:Test"), 1)
        self.assertIn("2026-11-18 balance Assets:Bank:Test -50.00 CNY", content)
        history = self.ledger.reconcile_history(account="测试卡")["records"]
        active = [item for item in history if item["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertTrue(any(item["status"] == "superseded" for item in history))
        self.assertTrue(self.ledger.validate()["ok"])

    def test_late_draft_auto_pads_without_a_pad(self) -> None:
        self.preview_commit()
        draft = self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "晚餐",
                "narration": "补记",
                "postings": [
                    {"account": "Expenses:Food", "amount": "20.00", "currency": "CNY"},
                    {"account": "Assets:Bank:Test", "amount": "-20.00", "currency": "CNY"},
                ],
                "source": {"kind": "manual"},
            }
        )
        approved = self.ledger.approve_draft(draft_id=draft["draft"]["id"])
        self.assertTrue(approved["committed"])
        self.assertIsNotNone(approved.get("autoPad"))
        content = self.fixture.root.joinpath("trans/reconciliation.bean").read_text()
        self.assertIn("pad Assets:Bank:Test Equity:Opening-Balances", content)
        self.assertIn("2026-11-18 balance Assets:Bank:Test -30.00 CNY", content)
        history = self.ledger.reconcile_history(account="测试卡")["records"]
        self.assertTrue(any(item.get("autoPad") for item in history))
        self.assertTrue(self.ledger.validate()["ok"])

    def test_expense_cannot_get_a_reconcile_period(self) -> None:
        from ledger_domain import LedgerError

        with self.assertRaises(LedgerError) as caught:
            self.ledger.set_account_metadata(account="Expenses:Food", reconcile="monthly")
        self.assertEqual(caught.exception.code, "not_reconcilable")

    def test_stale_preview_is_refused(self) -> None:
        preview = self.ledger.preview_reconciliation({"account": "Assets:Bank:Test", "asOf": TODAY, "actual": "-30.00"})
        self.ledger.set_account_metadata(account="测试卡", alias="测试卡2")
        request = {
            "account": "Assets:Bank:Test",
            "asOf": TODAY,
            "actual": "-30.00",
            "mode": preview["mode"],
            "previewId": preview["previewId"],
            "ledgerRevision": preview["ledgerRevision"],
            "inputsHash": preview["inputsHash"],
            "directivesHash": preview["directivesHash"],
            "replaces": preview.get("replaces"),
        }
        with self.assertRaises(LedgerError) as caught:
            self.ledger.commit_reconciliation(request)
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertNotIn("balance Assets:Bank:Test", self.fixture.root.joinpath("trans/reconciliation.bean").read_text())

    def test_pending_draft_blocks_commit(self) -> None:
        preview = self.ledger.preview_reconciliation({"account": "测试卡", "asOf": TODAY, "actual": "-30.00"})
        self.ledger.submit_draft(
            {
                "date": TODAY,
                "payee": "晚餐",
                "narration": "补记",
                "postings": [
                    {"account": "Expenses:Food", "amount": "20.00", "currency": "CNY"},
                    {"account": "Assets:Bank:Test", "amount": "-20.00", "currency": "CNY"},
                ],
                "source": {"kind": "manual"},
            }
        )
        request = {
            "account": "测试卡",
            "asOf": TODAY,
            "actual": "-30.00",
            "mode": preview["mode"],
            "previewId": preview["previewId"],
            "ledgerRevision": preview["ledgerRevision"],
            "inputsHash": preview["inputsHash"],
            "directivesHash": preview["directivesHash"],
            "replaces": preview.get("replaces"),
        }
        with self.assertRaises(LedgerError) as caught:
            self.ledger.commit_reconciliation(request)
        self.assertEqual(caught.exception.code, "pending_drafts")

    def test_unallocated_difference_is_refused(self) -> None:
        with self.assertRaises(LedgerError) as caught:
            self.ledger.preview_reconciliation(
                {
                    "account": "测试卡",
                    "asOf": TODAY,
                    "actual": "-40.00",
                    "allocations": [{"account": "Expenses:Food", "amount": "5.00", "date": "2026-11-10"}],
                }
            )
        self.assertEqual(caught.exception.code, "unallocated_difference")

    def test_zero_first_reconciliation_ignores_empty_remainder(self) -> None:
        preview = self.ledger.preview_reconciliation(
            {
                "account": "Assets:Bank:Unmapped",
                "asOf": TODAY,
                "actual": "0.00",
                "allocations": [{"account": "Equity:Opening-Balances", "mode": "remainder"}],
            }
        )
        self.assertEqual(preview["difference"], "0.00")
        self.assertEqual(preview["allocations"], [])
        self.assertIsNone(preview["pad"])
        self.assertIn("balance Assets:Bank:Unmapped 0.00 CNY", preview["directives"])
        request = {
            "account": "Assets:Bank:Unmapped",
            "asOf": TODAY,
            "actual": "0.00",
            "allocations": [{"account": "Equity:Opening-Balances", "mode": "remainder"}],
            "mode": preview["mode"],
            "previewId": preview["previewId"],
            "ledgerRevision": preview["ledgerRevision"],
            "inputsHash": preview["inputsHash"],
            "directivesHash": preview["directivesHash"],
            "replaces": preview.get("replaces"),
        }
        result = self.ledger.commit_reconciliation(request)
        self.assertTrue(result["committed"])
        content = self.fixture.root.joinpath("trans/reconciliation.bean").read_text()
        self.assertIn("balance Assets:Bank:Unmapped 0.00 CNY", content)
        self.assertNotIn("pad Assets:Bank:Unmapped", content)
        self.assertTrue(self.ledger.validate()["ok"])

    def test_daily_period_is_supported_and_due_immediately(self) -> None:
        updated = self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", reconcile="daily")
        self.assertEqual(updated["period"], "daily")
        reminder = next(
            item for item in self.ledger.get_due_reconciliations()["reminders"] if item["account"] == "Assets:Bank:Unmapped"
        )
        self.assertEqual(reminder["status"], "due")
        self.assertTrue(reminder["unreconciled"])
        self.assertEqual(reminder["nextDue"], TODAY)

        preview = self.ledger.preview_reconciliation({"account": "Assets:Bank:Unmapped", "asOf": TODAY, "actual": "0.00"})
        request = {
            "account": "Assets:Bank:Unmapped",
            "asOf": TODAY,
            "actual": "0.00",
            "mode": preview["mode"],
            "previewId": preview["previewId"],
            "ledgerRevision": preview["ledgerRevision"],
            "inputsHash": preview["inputsHash"],
            "directivesHash": preview["directivesHash"],
            "replaces": preview.get("replaces"),
        }
        result = self.ledger.commit_reconciliation(request)
        self.assertTrue(result["committed"])
        account = self.ledger.account("Assets:Bank:Unmapped")
        self.assertEqual(account["status"], "scheduled")
        self.assertEqual(account["nextDue"], "2026-11-18")
        self.assertEqual(account["coveredThrough"], TODAY)
        self.assertTrue(self.ledger.validate()["ok"])


if __name__ == "__main__":
    unittest.main()
