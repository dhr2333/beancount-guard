"""New-ledger template.

The directory layout and account taxonomy follow Beancount-Trans-Assets (the
canonical template the platform clones for a new user), reduced to a small,
usable starter chart. A new ledger must open cleanly in Fava and be writable by
the core without any further configuration.
"""

from __future__ import annotations

from typing import Any

from .bean import quote


def render_ledger(*, title: str, currency: str, year: str, today: str) -> dict[str, str]:
    """Render every file of a new ledger, keyed by ledger-relative path."""
    files: dict[str, str] = {
        "main.bean": _main(title, currency, year, today),
        "account/assets.bean": _assets(today),
        "account/liabilities.bean": _liabilities(today),
        "account/expenses.bean": _expenses(today),
        "account/income.bean": _income(today),
        "account/equity.bean": _equity(today),
        "trans/main.bean": "; Beancount-Trans 写入的交易。main.bean 已 include 本文件。\n",
        ".gitignore": _gitignore(),
        "README.md": _readme(title),
        f"{year}/00.bean": _year_index(),
    }
    for month in range(1, 13):
        files[f"{year}/{month:02d}.bean"] = ""
    return files


def _main(title: str, currency: str, year: str, today: str) -> str:
    return f"""; 1. 账本信息（结构遵循 Beancount-Trans-Assets）
option "title" {quote(title)}
option "operating_currency" {quote(currency)}

{today} custom "fava-option" "language" "zh_CN"
{today} custom "fava-option" "auto-reload" "true"
{today} custom "fava-option" "default-page" "balance_sheet/"
{today} custom "fava-option" "indent" "2"
{today} custom "fava-option" "sidebar-show-queries" "7"

; 2. 账户
include "account/assets.bean"
include "account/liabilities.bean"
include "account/expenses.bean"
include "account/income.bean"
include "account/equity.bean"

; 3. 插件
; 未显式 open 的账户会被自动创建；建议先在 account/ 里补 open 再维护别名。
plugin "beancount.plugins.auto_accounts"

; 4. 交易
include "trans/main.bean"
include "{year}/00.bean"
"""


def _assets(today: str) -> str:
    return f"""; 资产。canonical 名称沿用 Beancount-Trans-Assets 的科目树。
{today} open Assets:Savings:Cash CNY
  alias: "现金"

{today} open Assets:Savings:Bank:BOC CNY
  alias: "中国银行"

{today} open Assets:Savings:Web:AliPay CNY
  alias: "支付宝"

{today} open Assets:Savings:Web:AliFund CNY
  alias: "余额宝"
  reconcile: "daily"

{today} open Assets:Savings:Web:WechatPay CNY
  alias: "微信"

{today} open Assets:Savings:Web:WechatFund CNY
  alias: "零钱通"
  reconcile: "daily"
"""


def _liabilities(today: str) -> str:
    return f"""; 负债。
{today} open Liabilities:CreditCard:Bank:CMB CNY
  alias: "招商银行"
"""


def _expenses(today: str) -> str:
    return f"""; 支出。先给最常用的几类，后续在账户页补别名或新增科目。
{today} open Expenses:Food CNY
  alias: "餐饮"

{today} open Expenses:TransPort:Public CNY
  alias: "交通"

{today} open Expenses:Home:Rent CNY
  alias: "房租"

{today} open Expenses:Shopping CNY
  alias: "购物"

{today} open Expenses:Culture:Entertainment CNY
  alias: "娱乐"

{today} open Expenses:Health:Medical CNY
  alias: "医疗"

{today} open Expenses:Other CNY
  alias: "其他支出"
"""


def _income(today: str) -> str:
    return f"""; 收入。
{today} open Income:Active:Salary CNY
  alias: "工资"

{today} open Income:Other CNY
  alias: "其他收入"
"""


def _equity(today: str) -> str:
    return f"""; 权益。
{today} open Equity:Opening-Balances CNY
  alias: "期初余额"

{today} open Equity:Adjustments CNY
  alias: "调整"
"""


def _year_index() -> str:
    lines = [f'include "{month:02d}.bean"' for month in range(1, 13)]
    return "\n".join(lines) + "\n"


def _gitignore() -> str:
    return """trans/*
!trans/main.bean
.venv/
.ledger/
__pycache__/
*.py[cod]
.DS_Store
"""


def _readme(title: str) -> str:
    return f"""# {title}

这是你的账本，全部是纯文本，归你所有。

- 备份：复制整个文件夹即可；也可以用 Git 或网盘同步。
- 查看：`fava main.bean`，或在 Beancount-Trans 中打开。
- 目录：`account/` 账户定义，`YYYY/MM.bean` 交易与对账，`trans/` 平台写入（已 gitignore），`.ledger/` 运行态（草稿/锁/审计，已 gitignore）。

不要把 `.bean` 文件交给某个云端服务独占；换工具时随时可以带走。
"""


def template_files() -> dict[str, Any]:
    """Expose the template shape for documentation/tests."""
    return {"layout": ["main.bean", "account/*.bean", "YYYY/00.bean", "trans/main.bean"]}
