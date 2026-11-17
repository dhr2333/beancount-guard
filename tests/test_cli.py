"""CLI contract tests for the DSH bridge.

The panel does not import the package; it executes ``python -m ledger_domain``.
These tests pin that JSON-in/JSON-out surface.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path("/home/daihaorui/桌面/GitHub/Assets")
BEAN_CHECK = os.environ.get("BEAN_CHECK") or (
    str(ASSETS / ".venv/bin/bean-check") if (ASSETS / ".venv/bin/bean-check").exists() else "bean-check"
)
BEAN_QUERY = os.environ.get("BEAN_QUERY") or (
    str(ASSETS / ".venv/bin/bean-query") if (ASSETS / ".venv/bin/bean-query").exists() else "bean-query"
)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ledger-domain-cli-"))
        (self.root / "account").mkdir()
        (self.root / "2026").mkdir()
        (self.root / "main.bean").write_text(
            'option "operating_currency" "CNY"\n\ninclude "account/accounts.bean"\ninclude "2026/00.bean"\n',
            encoding="utf-8",
        )
        (self.root / "account/accounts.bean").write_text(
            "2026-01-01 open Assets:Bank:Test CNY\n2026-01-01 open Expenses:Food CNY\n",
            encoding="utf-8",
        )
        (self.root / "2026/00.bean").write_text('include "11.bean"\n', encoding="utf-8")
        (self.root / "2026/11.bean").write_text("; November\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, command: str, payload: dict | None = None, *extra: str) -> dict:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledger_domain",
                "--root",
                str(self.root),
                "--bean-check",
                BEAN_CHECK,
                "--bean-query",
                BEAN_QUERY,
                command,
                *extra,
            ],
            cwd=ROOT,
            input=json.dumps(payload, ensure_ascii=False) if payload is not None else "",
            text=True,
            capture_output=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_health_and_bootstrap(self) -> None:
        health = self.run_cli("health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["value"]["today"], "2026-11-17")
        bootstrap = self.run_cli("bootstrap")
        self.assertTrue(bootstrap["ok"])
        self.assertEqual(len(bootstrap["value"]["accounts"]), 2)
        self.assertEqual(bootstrap["value"]["pendingDrafts"], [])

    def test_metadata_balance_and_resolve(self) -> None:
        metadata = self.run_cli(
            "metadata",
            {"account": "Assets:Bank:Test", "alias": "测试卡", "reconcile": "monthly"},
        )
        self.assertTrue(metadata["ok"])
        self.assertEqual(metadata["value"]["account"]["name"], "测试卡")
        self.assertIn('alias: "测试卡"', (self.root / "account/accounts.bean").read_text(encoding="utf-8"))

        resolved = self.run_cli("resolve", None, "测试卡")
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["value"]["account"], "Assets:Bank:Test")

        balance = self.run_cli("balance", {"account": "测试卡", "amount": "0.00", "currency": "CNY", "date": "2026-11-17"})
        self.assertTrue(balance["ok"])
        self.assertEqual(balance["value"]["result"]["account"], "测试卡")
        self.assertEqual(balance["value"]["result"]["reconciliation"]["nextDue"], "2026-12-17")
        self.assertIn("balance Assets:Bank:Test 0.00 CNY", (self.root / "2026/11.bean").read_text(encoding="utf-8"))

    def test_future_balance_is_a_business_error(self) -> None:
        result = self.run_cli("balance", {"account": "Assets:Bank:Test", "amount": "0.00", "date": "2099-01-01"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "future_date")


if __name__ == "__main__":
    unittest.main()
