# ledger-domain-core

Beancount 账本的**核心库**。它不包含界面、不启动服务、不依赖 DSH：UI、Agent 适配器、脚本都调用这一组 Python 函数，或通过同一份 JSON CLI 契约调用。

## 写入目标

- 交易：`trans/collect.bean`
- 对账：`trans/reconciliation.bean`
- `trans/main.bean` 只 include 上述两个文件，不写交易。
- 不再按 `YYYY/MM.bean` 分月写入。
- Core 负责整个账本写入；`init-ledger` 暂保留，MVP-0 从 `ledger-domain-assets` 已有账本开始。

## 三个独立项目

| 项目 | 根目录 | 职责 |
|---|---|---|
| 核心库 | `~/ledger-domain/ledger-domain-core` | 本仓库：别名/周期治理、对账重建、草稿队列、校验后原子写入、查询投影 |
| 账本源 | `~/ledger-domain/ledger-domain-assets` | 只存 `.bean` 和账本资源，不放代码 |
| DSH 插件 | `~/ledger-domain/dsh-ledger-domain` | 只做展示页，通过 CLI 调本库，不含账本规则 |

## 函数接口

```python
from ledger_domain import create_ledger_domain

ledger = create_ledger_domain(root="/home/dhr2333/Documents/账本-mvp")

ledger.setup()                                    # 是否已有 main.bean
ledger.init_ledger(title="我的账本", currency="CNY")  # 生成新账本并 bean-check
ledger.resolve_alias("工行5244")                 # → {"account": "Assets:...", "matched": "alias"}
ledger.list_accounts()
ledger.get_due_reconciliations()
ledger.set_account_metadata(account="Assets:...", alias="工行5244", reconcile="monthly")
ledger.preview_reconciliation({"account": "工行5244", "asOf": "2026-11-30", "actual": "1000.00"})
ledger.commit_reconciliation({...preview fields...})       # create / same-asOf update
ledger.reconcile_history(account="工行5244")
ledger.open_account(account="Assets:Savings:Bank:BOC0814", currencies=["CNY"], alias="中国银行 0814", reconcile="monthly")
ledger.update_draft(draft_id="draft-...", payload={"payee": "星巴克"})
ledger.submit_draft({...})                        # 入队，不写账本
ledger.approve_draft(draft_id="draft-...")        # 校验 → 原子追加
ledger.query_transactions(date_from="2026-10-01", date_to="2026-10-31")
ledger.query_balances(as_of="2026-11-17")
```

### 待办队列：人 vs Agent

人在面板里点的操作（开户、改别名/周期、对账）本身就是人工确认，直接执行。
Agent 发起的写入一律入队等人批准：交易进草稿队列（`submit-draft`），结构变更进
操作队列（`submit-operation`，kind 取 `account-open` / `account-redate` /
`account-metadata` / `balance`）。批准结构变更时 Core 会自动重算待审草稿的账户解析，
让原本卡在 `unresolved_account` 的草稿立刻可批。

`LedgerError` 带 `code`、`message`、`details`；UI/CLI 直接展示 code 对应的本地化文案。

## 新账本模板（legacy，不在 MVP-0）

> MVP-0 直接使用 `ledger-domain-assets`；初始化后续由 DSH 实现。以下 `init_ledger()` 是旧模板，保留但不在 MVP-0 验收。

`init_ledger()` 使用 **Beancount-Trans-Assets 的目录约定和科目命名**，但只带一份干净的起步模板：

- `main.bean`
- `account/{assets,liabilities,expenses,income,equity}.bean`
- 当前年份 `YYYY/00.bean` + 12 个月度文件
- `trans/main.bean`
- `document/`
- `.gitignore`、`README.md`

起步账户包含现金、中国银行、支付宝、余额宝、微信、零钱通、招商银行、餐饮、交通、房租、购物、娱乐、医疗、其他支出、工资、其他收入、期初余额、调整；余额宝/零钱通默认按天对账。生成后先在临时目录运行 `bean-check`，通过后才 rename 到目标路径。

## 数据放在哪

- 别名、对账周期：账本 `open` 指令的 metadata。
- 对账到期日：每次从最近 `balance` 断言和周期重建，不落库。
- 草稿、锁、审计：账本根目录的 `.ledger/`（运行态，已在账本源 `.gitignore`）。
- 正式交易：审核批准后追加到 `trans/collect.bean`；对账追加/替换 `trans/reconciliation.bean`。

## CLI 契约

任何语言都可以通过子进程调用：

```bash
python -m ledger_domain --root /path/to/ledger setup
python -m ledger_domain --root /path/to/ledger init <<'JSON'
{"title": "我的账本", "currency": "CNY"}
JSON
python -m ledger_domain --root /path/to/ledger bootstrap
python -m ledger_domain --root /path/to/ledger balance <<'JSON'
{"account": "工行5244", "amount": "-30.00", "currency": "CNY"}
JSON
```

stdout 固定为：

```json
{"ok": true, "value": {...}}
{"ok": false, "error": {"code": "...", "message": "...", "details": {}}}
```

业务失败也返回退出码 0；用法/内部错误返回 1。命令：`health`、`setup`、`init`、`bootstrap`、`status`、`accounts`、`resolve`、`due`、`metadata`、`account-open`、`account-redate`、`balance`、`reconcile-preview`、`reconcile-commit`、`reconcile-history`、`refresh-drafts`、`approve-all-drafts`、`op-submit`、`op-list`、`op-get`、`op-approve`、`op-approve-all`、`op-reject`、`list-drafts`、`get-draft`、`submit-draft`、`update-draft`、`approve-draft`、`reject-draft`、`quick-draft`、`query-transactions`、`query-balances`、`validate`、`audit-metadata`。

## 运行时

核心库只依赖标准库；校验和查询调用外部程序：

- `bean-check` 来自 `beancount`
- `bean-query` 来自 `beanquery`

查找顺序：显式配置路径 → 账本 `.venv/bin` → **当前解释器同目录** → PATH。最后一项让“托管引擎”里的核心库直接找到同一个 venv 的 Beancount 命令，不需要再配置 `beanCheck`。

DSH 插件可以为普通用户托管这个运行时（私有 venv + 一键安装/修复）；核心库本身不关心安装方式。插件托管安装需要系统 Python 3.11+、网络与核心库源码路径；发行版应把核心库作为依赖一并分发。

## 写入门禁

1. 候选内容写入影子树（未改动文件用 symlink，改动文件写真实副本）。
2. 在影子树运行真实的 `bean-check`。
3. 通过后，在 `.ledger/write.lock` 锁内 `os.replace` 原子替换目标文件。
4. 失败时返回 `bean_check_failed` / `balance_mismatch`，原文件零改动。

## 测试

```bash
cd ledger-domain-core
PYTHONPATH=. python -m unittest discover -s tests -v
```

测试在临时账本副本上覆盖：别名冲突回滚、对账重建、余额不一致零写入、草稿去重/批准/驳回、并发写串行化、月份 include 创建、隐式账户、崩溃恢复、查询投影。

## 对账闭环

- 到期：有 balance 时 `nextDue = add_period(balance.date) - 1 天`；没有 balance 时立即进待办，`unreconciled = true`，UI 显示"未对账"。
- 预览/提交：`reconcile-preview` 返回 expected / difference / allocations / directives / hashes；`reconcile-commit` 在同一写门禁里原子写 `transaction* + pad? + balance`。
- 更新：同一 `asOf` 再次提交走 `mode=update`，替换最近一次 block，旧历史 `superseded`；更早期间不可改。
- 补记：对账后仍允许日期 ≤ asOf 的草稿。有 pad 时由 pad 吸收；没有 pad 时 `approve-draft` 自动往最近一次 block 插入 pad；自动补 pad 失败才返回 `reconcile_update_required`。
- 历史：`.ledger/reconciliations/<id>.json`，记录 expected/actual/allocations/pad/balance/directives/hash/文件行，可回放、可审计。

## 已知限制

- `init_ledger()` 只写入空目录；非空目录返回 `directory_not_empty`，不会覆盖现有文件。
- 导入已有账本（选择目录/校验/接管）还没有做；当前需要把插件 `root` 指向已有账本。
- 交易/对账写入 `trans/collect.bean` / `trans/reconciliation.bean`；create 追加，update 原位替换最近一次 block；文件不存在时报 `ledger_not_initialized`。
- `init_ledger()` 仍是旧的 16 账户/月度模板；MVP-0 用 `ledger-domain-assets`，新账本初始化由 DSH 插件实现（复制模板 → staging `bean-check` → 原子 rename）。
- 草稿只支持 `*` / `!`；每个 posting 必须显式给金额；不支持成本/价格/自动平衡。
- 由 `auto_accounts` 隐式创建、没有 `open` 指令的账户可查询/对账/进草稿，但不能维护别名与周期。
- `bean-check`/`bean-query` 是外部程序；不导入 Beancount Python 包。
- 托管引擎首次安装需要从包索引安装 `beancount` 与 `beanquery`；装好后可离线。
- 对账漂移（补记被 pad 吸收后历史 `expected` 与当前账本不一致）当前不主动告警；`validate` / `reconcile-preview` 不返回 `reconciliation_drift`。

## CI

Jenkins Multibranch Pipeline，配置与本地等价命令见 [docs/CI.md](docs/CI.md)。
