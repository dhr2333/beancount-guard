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

include "account/accounts.bean"
include "2026/00.bean"
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
"""

YEAR_INDEX = 'include "01.bean"\ninclude "11.bean"\n'

JANUARY = """2026-01-05 * "Cafe" "coffee"
  Expenses:Food    30.00 CNY
  Assets:Bank:Test
"""


class Fixture:
    """One isolated ledger copy plus a domain client."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ledger-domain-test-"))
        (self.root / "account").mkdir()
        (self.root / "2026").mkdir()
        (self.root / "main.bean").write_text(MAIN, encoding="utf-8")
        (self.root / "account/accounts.bean").write_text(ACCOUNTS, encoding="utf-8")
        (self.root / "2026/00.bean").write_text(YEAR_INDEX, encoding="utf-8")
        (self.root / "2026/01.bean").write_text(JANUARY, encoding="utf-8")
        (self.root / "2026/11.bean").write_text("; November\n", encoding="utf-8")

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
        self.assertIn("工资卡", names)
        self.assertIn("信用卡", names)
        self.assertEqual(ledger.resolve_alias("工资卡")["account"], "Assets:Savings:Bank:Main")
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
        self.assertEqual(reminder["status"], "overdue")
        self.assertEqual(reminder["anchorKind"], "open")
        self.assertEqual(reminder["nextDue"], "2026-02-01")
        self.assertFalse(any(item["account"] == "Expenses:Food:Coffee" for item in before["reminders"]))

        result = self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text=TODAY)
        self.assertEqual(result["name"], "测试卡")
        self.assertEqual(result["reconciliation"]["status"], "scheduled")
        self.assertEqual(result["reconciliation"]["nextDue"], "2026-12-17")
        self.assertEqual(result["reconciliation"]["anchorKind"], "balance")
        self.assertIn("balance Assets:Bank:Test -30.00 CNY", self.read("2026/11.bean"))
        bootstrap = self.ledger.bootstrap()
        view = next(item for item in bootstrap["accounts"] if item["account"] == "Assets:Bank:Test")
        self.assertEqual(view["status"], "scheduled")
        self.assertEqual(view["nextDue"], "2026-12-17")

    # --------------------------------------------------------------- write gate

    def test_wrong_balance_writes_nothing(self) -> None:
        before = self.read("2026/11.bean")
        error = self.assert_code(
            "balance_mismatch",
            lambda: self.ledger.submit_balance(account="测试卡", amount="0.00", currency="CNY", date_text=TODAY),
        )
        self.assertEqual(error.details["expected"], "0.00")
        self.assertEqual(error.details["accumulated"], "-30.00")
        self.assertEqual(self.read("2026/11.bean"), before)
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
        file = self.fixture.root / "account/accounts.bean"
        before = file.read_text(encoding="utf-8")
        self.assert_code(
            "alias_conflict",
            lambda: self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", alias="测试卡"),
        )
        self.assertEqual(file.read_text(encoding="utf-8"), before)
        self.assert_code(
            "invalid_period",
            lambda: self.ledger.set_account_metadata(account="Assets:Bank:Unmapped", reconcile="daily"),
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
        file = self.fixture.root / "account/accounts.bean"
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
        november = self.read("2026/11.bean")
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
        before = self.read("2026/11.bean")
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
        self.assertEqual(self.read("2026/11.bean"), before)

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
        self.assertEqual(self.read("2026/11.bean"), before)
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
        self.assertIn("Expenses:Food:Coffee  38.00 CNY", self.read("2026/11.bean"))

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
        january = self.fixture.root / "2026/01.bean"
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
        self.assertEqual(result["file"], "2026/11.bean")
        self.assertEqual(ledger.validate(), {"ok": True, "diagnostic": ""})
        self.assert_code(
            "account_not_opened",
            lambda: ledger.set_account_metadata(account="Assets:Bank:Implicit", alias="隐式卡"),
        )

    def test_balance_include_recovery(self) -> None:
        october = self.fixture.root / "2026/10.bean"
        october.write_text("; October\n\n2026-10-15 balance Assets:Bank:Test -30.00 CNY\n", encoding="utf-8")
        result = self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text="2026-10-15")
        self.assertEqual(result["file"], "2026/10.bean")
        self.assertEqual(october.read_text(encoding="utf-8").count("balance Assets:Bank:Test"), 1)
        self.assertIn('include "10.bean"', self.read("2026/00.bean"))
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
        (self.fixture.root / "2026/12.bean").write_text(f"; December\n\n{block}\n", encoding="utf-8")
        approved = self.ledger.approve_draft(draft_id=submitted["draft"]["id"])
        self.assertFalse(approved["committed"])
        self.assertTrue(approved["recovered"])
        text = self.read("2026/12.bean")
        self.assertEqual(text.count(f'draft_id: "{submitted["draft"]["id"]}"'), 1)
        self.assertIn('include "12.bean"', self.read("2026/00.bean"))
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

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
        november = self.read("2026/11.bean")
        self.assertIn("balance Assets:Bank:Test -30.00 CNY", november)
        self.assertIn("balance Assets:Bank:Unmapped 0.00 CNY", november)
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})

    def test_missing_month_file_include_order(self) -> None:
        (self.fixture.root / "2026/11.bean").unlink()
        (self.fixture.root / "2026/12.bean").write_text("; December\n", encoding="utf-8")
        (self.fixture.root / "2026/00.bean").write_text(
            'include "01.bean"\ninclude "12.bean"\n', encoding="utf-8"
        )
        result = self.ledger.submit_balance(account="测试卡", amount="-30.00", currency="CNY", date_text=TODAY)
        self.assertEqual(result["file"], "2026/11.bean")
        self.assertIn(
            'include "01.bean"\ninclude "11.bean"\ninclude "12.bean"',
            self.read("2026/00.bean"),
        )
        self.assertEqual(self.ledger.validate(), {"ok": True, "diagnostic": ""})


if __name__ == "__main__":
    unittest.main()
