"""Local read-only MCP server for the ledger skills.

Run as ``python -m ledger_domain.mcp_server --root /path/to/ledger``. The
server exposes the same tool/resource names as the former Backend MCP so the
existing ``ledger-overview`` / ``monthly-review`` / ``insight-review`` skills
keep working unchanged.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .bql import BqlError, run_bql as execute_bql
from .core import create_ledger_domain

MAX_ROWS = 100
MAX_FILE_BYTES = 1_048_576
TAG_RE = re.compile(r"#([A-Za-z0-9][A-Za-z0-9_\-/.]{0,63})")

BQL_REFERENCE = """\
BQL 只读查询（beanquery 子集）
- 账户：account ~ '^Expenses'；日期：year = 2026 AND month = 11 或 date >= 2026-01-01
- 金额：sum(units(position))；支出过滤 number > 100；收入绝对值 number < -100
- 标签：'路径' IN tags；链接：'id' IN links
- 分组：SELECT account, sum(units(position)) ... GROUP BY account ORDER BY sum(units(position)) DESC
- 禁止：写语句、HAVING、tags ~、units(position) 比较、WHERE 聚合
""".strip()


class BqlToolResult(BaseModel):
    bql: str = Field(description="executed BQL")
    result_text: str = Field(description="table-formatted result")
    row_count: int = Field(description="row count before truncation")
    truncated: bool = Field(description="whether rows were truncated")


class LedgerFile(BaseModel):
    path: str = Field(description="path relative to the ledger root")
    size_bytes: int = Field(description="file size")
    content: str = Field(description="file content")


class LocalLedger:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.main = self.root / "main.bean"
        self.domain = create_ledger_domain(root=self.root)

    def relative(self, path: str) -> Path:
        raw = (path or "").strip()
        if not raw:
            raise ValueError("path must not be empty")
        candidate = Path(raw)
        full = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if full != self.root and self.root not in full.parents:
            raise ValueError("path must stay inside the ledger root")
        if full.suffix.lower() != ".bean":
            raise ValueError("only .bean files can be read")
        if not full.is_file():
            raise ValueError(f"file does not exist: {raw}")
        if full.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"file is too large: {raw}")
        return full

    def accounts_text(self) -> str:
        lines = ["账本账户目录（账户路径 → alias）:"]
        for account in self.domain.list_accounts(limit=500):
            alias = account.get("alias")
            lines.append(f"  {account['account']} → {alias}" if alias else f"  {account['account']}")
        return "\n".join(lines)

    def tags_text(self) -> str:
        tags: set[str] = set()
        for path in self.root.rglob("*.bean"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tags.update(TAG_RE.findall(text))
        lines = ["账本标签目录（标签路径）:"]
        lines.extend(f"  {tag}" for tag in sorted(tags))
        if len(lines) == 1:
            lines.append("  （暂无标签）")
        return "\n".join(lines)

    def context(self) -> str:
        status = self.domain.status()
        lines = [
            f"基准日期: {status['today']}",
            f"默认货币: {status.get('operatingCurrency') or 'CNY'}",
            self.accounts_text(),
            self.tags_text(),
            BQL_REFERENCE,
        ]
        return "\n\n".join(lines)


def build_server(ledger: LocalLedger) -> MCPServer:
    server = MCPServer(
        "beancount-local",
        title="Local Beancount ledger",
        instructions=(
            "Read-only access to the local Beancount ledger. All amounts must come from "
            "run_bql; never edit the ledger. Use get_ledger_context before account-specific queries."
        ),
    )

    @server.tool(name="get_ledger_context", description="Current ledger context: date, currency, accounts, tags, BQL rules.")
    def get_ledger_context() -> str:
        return ledger.context()

    @server.tool(name="run_bql", description="Run one read-only BQL SELECT query; max 100 rows.")
    def run_bql_tool(query: str) -> BqlToolResult:
        try:
            result = execute_bql(str(ledger.main), query, max_rows=MAX_ROWS)
        except BqlError as error:
            raise ValueError(f"BQL validation failed: {error}") from error
        except Exception as error:  # noqa: BLE001 - surface the real query error
            raise ValueError(f"BQL execution failed: {error}") from error
        return BqlToolResult(
            bql=result.bql,
            result_text=result.result_text,
            row_count=result.row_count,
            truncated=result.truncated,
        )

    @server.tool(name="read_ledger_file", description="Read one .bean file inside the ledger root.")
    def read_ledger_file(path: str) -> LedgerFile:
        full = ledger.relative(path)
        content = full.read_text(encoding="utf-8", errors="replace")
        return LedgerFile(
            path=str(full.relative_to(ledger.root)),
            size_bytes=full.stat().st_size,
            content=content,
        )

    @server.resource("ledger://accounts", name="accounts", description="Local account catalog", mime_type="text/plain")
    def accounts_resource() -> str:
        return ledger.accounts_text()

    @server.resource("ledger://tags", name="tags", description="Local tag catalog", mime_type="text/plain")
    def tags_resource() -> str:
        return ledger.tags_text()

    @server.resource("ledger://bql-reference", name="bql_reference", description="BQL reference", mime_type="text/plain")
    def bql_reference_resource() -> str:
        return BQL_REFERENCE

    @server.resource("ledger://main.bean", name="main_bean", description="Ledger entry file", mime_type="text/x-beancount")
    def main_bean_resource() -> str:
        return ledger.main.read_text(encoding="utf-8", errors="replace")

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only Beancount MCP server")
    parser.add_argument("--root", default=os.environ.get("LEDGER_ROOT") or os.getcwd())
    args = parser.parse_args(argv)
    ledger = LocalLedger(Path(args.root))
    if not ledger.main.is_file():
        raise SystemExit(f"ledger main.bean not found under {ledger.root}")
    server = build_server(ledger)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
