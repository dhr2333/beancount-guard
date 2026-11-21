# beancount-guard

**Agent 与纯文本账本之间的写入边界。**

Agent 提议，人批准，`bean-check` 裁决。

[English](README.md) | 中文

## 它解决什么问题

Agent 可以在毫秒内起草一笔交易。但纯文本账本没有事务边界：一次半写入、一次 `include` 被破坏、一次未经校验的批量导入，都可能让唯一真相源进入没人敢信的状态。

稀缺的不是生成能力，而是可信任的写入。

`beancount-guard` 就是让 Agent 写入变得可信的那一层。它不是又一个账本应用，而是一个本地、轻量、与 UI 无关的内核，任何前端、脚本或 Agent 都可以调用。

## 是什么 / 不是什么

| 是 | 不是 |
|---|---|
| Beancount 账本的写入内核 | 账本应用或 Web UI |
| Agent 提案的审核队列 | 托管服务或 SaaS |
| 真实 `bean-check` 门禁 + 原子提交 | 模型或交易分类器 |
| Python 库、JSON CLI、只读 MCP | MCP 写入服务器 |
| 本地优先，纯文本始终归你 | Beancount / Fava 的替代品 |

## 一次写入如何发生

1. **提议。** Agent 提交结构化交易草稿，或账户开户、改开立日、改元数据、余额断言等结构操作。账本文件不动。
2. **校验。** 候选内容写入临时影子树，由真实 `bean-check` 检查。
3. **批准。** 人工批准提案；点击本身就是确认。
4. **提交。** 在写锁内原子替换目标文件，并把决定追加到审计日志。

校验失败时原文件零改动；进程在写入中途崩溃时，下一次批准可以根据账本里已有的 `draft_id` 恢复。

## 为什么不直接让 Agent 改 `.bean`

- 只读 MCP 能读账本，但不能写。
- 直写工具把实时余额直接交给模型。
- 托管服务拿走了账本的保管权。
- `beancount-guard` 把每次写入变成可审提案，用你本来就信任的 `bean-check` 校验，再原子提交。

完整对比与生态位置见 [docs/POSITIONING.md](docs/POSITIONING.md)。

## 包含什么

- **交易草稿队列。** 提交草稿不碰账本；批准时才渲染交易并追加。
- **结构操作队列。** Agent 发起的 `account-open`、`account-redate`、`account-metadata`、`balance` 都先入队等批准。
- **统一写入门禁。** 每次账本变更都经过影子树校验、进程级写锁和原子替换。
- **账本语义，而不只是语法。** 别名解析、对账周期、到期/逾期重建、对账 preview/commit、同 `asOf` 更新、历史记录、晚到草稿自动补 pad。
- **前端无关契约。** 稳定的 JSON CLI 信封、Python API、带协议版本的只读 MCP。
- **本地优先。** 只依赖标准库，账本始终是你目录里的纯文本。

## 安装

已发布到 PyPI，不依赖任何前端：

```bash
pip install beancount-guard
python -m beancount_guard --root /path/to/ledger status     # JSON CLI
python -m beancount_guard --root /path/to/ledger health     # version + protocol
python -m beancount_guard.mcp_server --root /path/to/ledger # 只读 MCP
```

使用 MCP 服务器需要可选依赖：

```bash
pip install "beancount-guard[mcp]"
```

可以用内置模板创建新账本：

```bash
python -m beancount_guard --root /path/to/new-ledger init
python -m beancount_guard --root /path/to/new-ledger bootstrap
```

## 函数接口

```python
from beancount_guard import create_beancount_guard

ledger = create_beancount_guard(root="/path/to/ledger")

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

`LedgerError` 带稳定的 `code`、`message`、`details`；适配层按 code 展示本地化文案，不解析人类文本。

## 待办队列：人 vs Agent

人在界面上点的操作（开户、改别名/周期、对账）本身就是人工确认，直接执行。Agent 发起的写入一律入队等批准：

- 交易进草稿队列（`submit-draft`）。
- 结构变更进操作队列（`submit-operation`，kind 取 `account-open` / `account-redate` / `account-metadata` / `balance`）。

批准结构变更时，本库会自动重算待审草稿的账户解析，让原本卡在 `unresolved_account` 的草稿立刻可批。

## 数据放在哪

- 交易：`trans/collect.bean`
- 对账：`trans/reconciliation.bean`
- `trans/main.bean` 只 include 上述两个文件，不写交易。
- 别名、对账周期：账本 `open` 指令的 metadata。
- 对账到期日：每次从最近 `balance` 断言和周期重建，不落库。
- 草稿、锁、审计：账本根目录的 `.ledger/`（运行态，已在账本源 `.gitignore`）。
- 正式交易：审核批准后追加到 `trans/collect.bean`；对账追加或替换 `trans/reconciliation.bean` 中最近一次 block。

## CLI 契约

任何语言都可以通过子进程调用：

```bash
python -m beancount_guard --root /path/to/ledger setup
python -m beancount_guard --root /path/to/ledger bootstrap
python -m beancount_guard --root /path/to/ledger balance <<'JSON'
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

本库只依赖标准库；校验和查询调用外部程序：

- `bean-check` 来自 `beancount`
- `bean-query` 来自 `beanquery`

查找顺序：显式配置路径 → 账本 `.venv/bin` → 当前解释器同目录 → `PATH`。最后一项让托管 venv 里的本库直接找到同一个 venv 的 Beancount 命令，不需要再配置 `beanCheck`。

前端可以为普通用户托管这个运行时（私有 venv + 一键安装/修复）；本库本身不关心安装方式，也不负责创建 venv。

## 写入门禁

1. 候选内容写入影子树（未改动文件用 symlink，改动文件写真实副本；Windows 无 symlink 权限时退化为只复制内容）。
2. 在影子树运行真实的 `bean-check`。
3. 通过后，在 `.ledger/write.lock` 锁内 `os.replace` 原子替换目标文件；目标只读或被占用时返回 `ledger_not_writable`，不擅自改文件属性。
4. 失败时返回 `bean_check_failed` / `balance_mismatch`，原文件零改动。
5. 影子目录用完即删；上次残留（超过一天）会在下一次校验前自动清掉。

## 对账闭环

- 到期：有 balance 时 `nextDue = add_period(balance.date) - 1 天`；没有 balance 时立即进待办，`unreconciled = true`。
- 预览/提交：`reconcile-preview` 返回 expected / difference / allocations / directives / hashes；`reconcile-commit` 在同一写门禁里原子写 `transaction* + pad? + balance`。
- 更新：同一 `asOf` 再次提交走 `mode=update`，替换最近一次 block，旧历史 `superseded`；更早期间不可改。
- 补记：对账后仍允许日期 ≤ asOf 的草稿。有 pad 时被吸收；没有 pad 时 `approve-draft` 自动往最近一次 block 插入 pad；自动补 pad 失败才返回 `reconcile_update_required`。
- 历史：`.ledger/reconciliations/<id>.json`，记录 expected/actual/allocations/pad/balance/directives/hash/文件行，可回放、可审计。

## 契约与版本

- `beancount_guard.PROTOCOL` 是 CLI/MCP 契约版本：只有破坏性变更才 +1，前端据此判断兼容性。
- `health` 返回 `{"version": "<安装版本>", "protocol": 1, ...}`；前端用它和 PyPI 上的最新版本比较，决定是否提示升级。
- 前端不得依赖私有实现：只用 CLI JSON 信封、MCP 工具集和这两个字段。

## 生态位置

`beancount-guard` 设计成工具之间的那一层，而不是替代它们：

- **与查询型 MCP**（`beanquery-mcp`、`beanie-mcp`）：它们负责读和回答，本项目负责守写入。
- **与 Fava 或 Obsidian 插件**：它们仍是视图，本项目是它们背后的写入边界。
- **与自建 Agent 或脚本**：调用 Python API 或 JSON CLI，同一个守卫不绑定 Agent 框架。

MCP 面刻意保持只读：写入提案走稳定的 JSON CLI 或 Python API，不把账本的直接写权限交给模型。原因与竞争格局见 [docs/POSITIONING.md](docs/POSITIONING.md)。

## 已知限制

- `init_ledger()` 使用内置模板生成新账本；外部 `ledger-domain-assets` 仓库是可选项，本库不依赖它。
- 导入已有账本（选择目录/校验/接管）还没有做；当前需要把 `root` 指向已有账本。
- 交易/对账写 `trans/collect.bean` / `trans/reconciliation.bean`；create 追加，update 原位替换最近一次 block；文件不存在时报 `ledger_not_initialized`。
- 草稿只支持 `*` / `!`；每个 posting 必须显式给金额；不支持成本/价格/自动平衡。
- 由 `auto_accounts` 隐式创建、没有 `open` 指令的账户可查询/对账/进草稿，但不能维护别名与周期。
- `bean-check` / `bean-query` 是外部程序；不导入 Beancount Python 包。
- 账本文件只接受 UTF-8：读侧记为 `invalid_encoding` 问题，写入返回 `ledger_encoding_error`；GBK/ANSI 保存的文件需先另存为 UTF-8。
- 账本绝对路径过长（Windows 260 字符上限）时，影子副本复制返回 `shadow_copy_failed` 并带出错路径；需要把账本移到更短的路径。
- 托管 venv 首次安装需要从包索引安装 `beancount` 与 `beanquery`；装好后可离线。
- 对账漂移当前不主动告警；`validate` / `reconcile-preview` 不返回 `reconciliation_drift`。
- 本库保证结构、原子性与可审计性；不判断 Agent 选的科目是否正确。

## 测试

```bash
cd beancount-guard
PYTHONPATH=. python -m unittest discover -s tests -v
```

测试在临时账本副本上覆盖：别名冲突、对账重建、余额不一致零写入、草稿流转、并发写、崩溃恢复、查询投影。

## CI

Jenkins Multibranch Pipeline，配置与本地等价命令见 [docs/CI.md](docs/CI.md)。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。简短版：核心保持只用标准库；行为变更要补测试；新增 MCP 写入面必须先更新 `FROZEN.md` 和 `PROTOCOL`。

## 许可证

MIT，见 [LICENSE](LICENSE)。
