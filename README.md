# beancount-guard

**The write boundary between AI agents and your plain-text Beancount ledger.**

Agents propose. You approve. `bean-check` decides.

[![PyPI](https://img.shields.io/pypi/v/beancount-guard.svg)](https://pypi.org/project/beancount-guard/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://pypi.org/project/beancount-guard/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/dhr2333/beancount-guard/blob/main/LICENSE)
[![Protocol](https://img.shields.io/badge/protocol-1-blueviolet.svg)](https://github.com/dhr2333/beancount-guard/blob/main/docs/POSITIONING.md)

[中文说明](https://github.com/dhr2333/beancount-guard/blob/main/README.zh-CN.md) · [Positioning](https://github.com/dhr2333/beancount-guard/blob/main/docs/POSITIONING.md)

## The problem

An agent can draft a transaction in milliseconds. A plain-text ledger has no transaction boundary: a partial write, a broken `include`, or an unvalidated bulk import can leave the only source of truth in a state no one can trust.

The scarce resource is not generation. It is a write you can trust.

`beancount-guard` is the layer that makes agent writes trustworthy. It is not another ledger app. It is a small, local, UI-agnostic kernel that any frontend, script, or agent can call.

## What this is / is not

| It is | It is not |
|---|---|
| A write kernel for Beancount ledgers | A ledger app or web UI |
| A review queue for agent proposals | A hosted service or SaaS |
| A real `bean-check` gate plus atomic commit | A model or a transaction classifier |
| A Python library, JSON CLI, and read-only MCP | An MCP write server |
| Local-first, plain text stays yours | A replacement for Beancount or Fava |

## How a write happens

1. **Propose.** An agent submits a structured transaction draft, or a structure operation such as account open, redate, metadata, or balance. Nothing is written to the ledger.
2. **Validate.** The candidate content is mirrored into a disposable shadow tree and checked by the real `bean-check`.
3. **Approve.** A human approves the proposal. The click is the confirmation.
4. **Commit.** The target file is replaced atomically under a write lock, and the decision is appended to the audit log.

If validation fails, the original ledger is unchanged. If the process crashes mid-write, the next approval can recover from the `draft_id` already present in the ledger.

## Why not just let an agent edit the file?

- Query-only MCP servers can read a ledger but cannot write it.
- Direct-write tools trust the model with live balances.
- Hosted services take custody of the ledger.
- `beancount-guard` turns every write into a reviewed proposal, validates it with the same `bean-check` you already trust, and commits it atomically.

See [docs/POSITIONING.md](https://github.com/dhr2333/beancount-guard/blob/main/docs/POSITIONING.md) for the full comparison and the project's place in the ecosystem.

## What's inside

- **Reviewed transaction drafts.** Submitting a draft never touches the ledger. Approval renders the transaction and appends it.
- **Entry metadata.** A draft may carry an optional string `metadata` map (for example `time`, `uuid`, `status`); approval renders it into the transaction so imported bills keep their provenance. See [Draft metadata](#draft-metadata).
- **Structure operation queue.** `account-open`, `account-redate`, `account-metadata`, and `balance` wait for approval when an agent proposes them.
- **One write gate.** Shadow-tree validation, a process-level lock, and atomic replace for every ledger mutation.
- **Ledger semantics, not just syntax.** Alias resolution, reconciliation periods, due/overdue rebuilds, reconciliation preview/commit, same-`asOf` updates, history, and auto-pad for late drafts.
- **Frontend-neutral contracts.** A stable JSON CLI envelope, a Python API, and a read-only MCP surface with a protocol version.
- **Local-first.** Standard library only. The ledger remains plain text in a directory you own.

## Install

Published on PyPI. No frontend is required:

```bash
pip install beancount-guard
python -m beancount_guard --root /path/to/ledger status     # JSON CLI
python -m beancount_guard --root /path/to/ledger health     # version + protocol
python -m beancount_guard.mcp_server --root /path/to/ledger # read-only MCP
```

For the MCP server, install the optional extra:

```bash
pip install "beancount-guard[mcp]"
```

A new ledger can be created from the built-in template:

```bash
python -m beancount_guard --root /path/to/new-ledger init
python -m beancount_guard --root /path/to/new-ledger bootstrap
```

## Python API

```python
from beancount_guard import create_beancount_guard

ledger = create_beancount_guard(root="/path/to/ledger")

ledger.setup()                                    # whether main.bean already exists
ledger.init_ledger(title="My ledger", currency="CNY")  # create and bean-check a new ledger
ledger.resolve_alias("bank-5244")                 # -> {"account": "Assets:...", "matched": "alias"}
ledger.list_accounts()
ledger.get_due_reconciliations()
ledger.set_account_metadata(account="Assets:...", alias="bank-5244", reconcile="monthly")
ledger.preview_reconciliation({"account": "bank-5244", "asOf": "2026-11-30", "actual": "1000.00"})
ledger.commit_reconciliation({...preview fields...})       # create / same-asOf update
ledger.reconcile_history(account="bank-5244")
ledger.open_account(account="Assets:Savings:Bank:BOC0814", currencies=["CNY"], alias="BOC 0814", reconcile="monthly")
ledger.update_draft(draft_id="draft-...", payload={"payee": "Starbucks"})
ledger.submit_draft({...})                        # enqueue, do not write
ledger.approve_draft(draft_id="draft-...")        # validate -> atomic append

# Draft metadata is optional and string-only; it becomes transaction metadata.
ledger.submit_draft({
    "date": "2026-10-30",
    "payee": "月梅早餐",
    "narration": "早餐",
    "metadata": {"time": "07:23:07", "uuid": "53110002338023202610300670672032", "status": "WeChat - 已转账"},
    "postings": [
        {"account": "Expenses:Food:Breakfast", "amount": "3.50", "currency": "CNY"},
        {"account": "Assets:Savings:Web:WechatFund", "amount": "-3.50", "currency": "CNY"},
    ],
})
ledger.query_transactions(date_from="2026-10-01", date_to="2026-10-31")
ledger.query_balances(as_of="2026-11-17")
```

`LedgerError` carries a stable `code`, a `message`, and `details`. Adapters display localized text for the code; they never parse human text to recover meaning.

## Draft metadata

Draft `metadata` is an optional object of string keys to string values:

- keys match `^[a-z][a-z0-9_-]*$`; `draft_id` is reserved;
- at most 20 keys, each value at most 200 characters;
- approval renders the entries in insertion order, before `draft_id` and postings;
- `update-draft` keeps existing metadata unless `metadata` is supplied;
- metadata participates in the dedupe hash, so the same transaction with a different `uuid` is not collapsed;
- it only affects newly approved entries; existing ledger files are never rewritten.

## Import batches

A batch is the agent's import envelope around the draft queue. It is bookkeeping
for review and audit, never a ledger write by itself.

```bash
python -m beancount_guard --root /path/to/ledger submit-batch <<'JSON'
{
  "source": {"kind": "import", "ref": "Alipay 2026-10", "rows": 90},
  "claimed": {"net": "9084.32", "currency": "CNY"},
  "skipped": [{"index": 7, "reason": "closed order", "amount": "18.00"}],
  "drafts": [ ... standard draft payloads ... ]
}
JSON
```

- `source.rows` is the claimed source row count; `skipped` lists rows deliberately
  not drafted. The panel checks `drafts + skipped + duplicates + failed == rows`.
- `metadata.uuid` is an idempotency key: a uuid already rendered in
  `trans/*.bean`, or used by another pending draft with different content, is
  rejected with `duplicate_transaction`.
- A posting can add `role` (`category` / `funding`) and `fallback`
  (`expense` / `income`). An unresolved `funding` posting is `funding_unresolved`
  and blocks approval; it is never rewritten to a fallback account.
- Approval requires at least one `Assets:` / `Liabilities:` posting
  (`no_real_account` otherwise).
- `approve-batch`, `reject-batch` and `void-batch` operate on one batch. Void
  appends reversing entries dated today, validates the whole shadow tree once,
  and records `void_of` / `batch` metadata; it never deletes or rewrites history.

## Pending queues: human vs agent

Operations a human clicks in a UI (opening an account, changing an alias or period, reconciling) are already confirmed, so they execute directly. Writes initiated by an agent always enter a queue and wait for human approval:

- Transactions enter the draft queue (`submit-draft` or `submit-batch`).
- Structure changes enter the operation queue (`submit-operation`, with kind `account-open` / `account-redate` / `account-metadata` / `balance`).

Approving a structure operation automatically re-resolves pending drafts against the updated account set, so drafts previously blocked on `unresolved_account` can become approvable immediately.

## Data layout

- Transactions: `trans/collect.bean`
- Reconciliation: `trans/reconciliation.bean`
- `trans/main.bean` only includes those two files and never holds transactions.
- Aliases and reconciliation periods: metadata on the ledger's `open` directives.
- Reconciliation due dates: rebuilt from the most recent `balance` assertion and the period; never stored as a second ledger.
- Drafts, locks, and audit: `.ledger/` under the ledger root (runtime state, already ignored by the ledger's `.gitignore`).
- Approved transactions: appended to `trans/collect.bean`; reconciliation directives are appended to or replace the latest block in `trans/reconciliation.bean`.

## CLI contract

Any language can call the CLI as a subprocess:

```bash
python -m beancount_guard --root /path/to/ledger setup
python -m beancount_guard --root /path/to/ledger bootstrap
python -m beancount_guard --root /path/to/ledger balance <<'JSON'
{"account": "bank-5244", "amount": "-30.00", "currency": "CNY"}
JSON
```

stdout is always one of:

```json
{"ok": true, "value": {...}}
{"ok": false, "error": {"code": "...", "message": "...", "details": {}}}
```

Business failures still exit `0`; usage or internal errors exit `1`. Commands: `health`, `setup`, `init`, `bootstrap`, `status`, `accounts`, `resolve`, `due`, `metadata`, `account-open`, `account-redate`, `balance`, `reconcile-preview`, `reconcile-commit`, `reconcile-history`, `refresh-drafts`, `approve-all-drafts`, `op-submit`, `op-list`, `op-get`, `op-approve`, `op-approve-all`, `op-reject`, `list-drafts`, `get-draft`, `submit-draft`, `submit-batch`, `list-batches`, `approve-batch`, `reject-batch`, `void-batch`, `update-draft`, `approve-draft`, `reject-draft`, `quick-draft`, `query-transactions`, `query-balances`, `validate`, `audit-metadata`.

## Runtime

The package depends only on the standard library. Validation and querying call external programs:

- `bean-check` from Beancount
- `bean-query` from beanquery

Lookup order: an explicit configured path, the ledger's `.venv/bin`, the directory of the running interpreter, then `PATH`. The interpreter-sibling fallback lets a managed virtualenv find its own Beancount commands without extra configuration.

A frontend may manage this runtime for end users (a private venv plus install/repair). The core does not create a venv or care how it was installed.

## Write gate

1. Candidate content is written into a shadow tree (unchanged files are symlinked; changed files are real copies; Windows without symlink permission falls back to content-only copies).
2. The real `bean-check` runs against the shadow tree.
3. On success, the target file is replaced atomically with `os.replace` inside `.ledger/write.lock`. A read-only or occupied target returns `ledger_not_writable`; file attributes are never silently changed.
4. On failure, the call returns `bean_check_failed` or `balance_mismatch`, and the original file is untouched.
5. The shadow directory is deleted after use; leftovers older than one day are swept before the next validation.

## Reconciliation loop

- Due: with a balance, `nextDue = add_period(balance.date) - 1 day`; without one, the account is immediately due and `unreconciled = true`.
- Preview/commit: `reconcile-preview` returns expected / difference / allocations / directives / hashes; `reconcile-commit` writes `transaction* + pad? + balance` atomically through the same write gate.
- Update: committing again for the same `asOf` uses `mode=update`, replaces the latest block, and marks the old history `superseded`; earlier periods cannot be changed.
- Late entries: drafts dated `<= asOf` are still allowed after reconciliation. A `pad` absorbs them when present; otherwise `approve-draft` inserts a pad into the latest block. If that fails, the call returns `reconcile_update_required`.
- History: `.ledger/reconciliations/<id>.json` records expected/actual/allocations/pad/balance/directives/hash/file line, so a reconciliation can be replayed and audited.

## Contract and versioning

- `beancount_guard.PROTOCOL` is the CLI/MCP contract version. It increments only on a breaking change; frontends use it to decide compatibility.
- `health` returns `{"version": "<installed version>", "protocol": 1, ...}`; a frontend can compare the version with the latest on PyPI and offer an upgrade.
- Frontends must not depend on private implementation details: only the CLI JSON envelope, the MCP tool set, and these two fields.

## Ecosystem fit

`beancount-guard` is designed to sit *between* tools, not to replace them:

- **With query MCP servers** (`beanquery-mcp`, `beanie-mcp`): they read and answer questions; this project guards the writes.
- **With Fava or an Obsidian plugin**: they remain the view; this project is the write boundary behind them.
- **With a custom agent or script**: call the Python API or the JSON CLI; the same guard works regardless of the agent framework.

The MCP surface is deliberately read-only. Write proposals use the stable JSON CLI or Python API, so a model is never handed direct write access to the ledger. See [docs/POSITIONING.md](https://github.com/dhr2333/beancount-guard/blob/main/docs/POSITIONING.md) for the reasoning and the competitive landscape.

## Status and limits

- `init_ledger()` generates a new ledger from the built-in template. The external `ledger-domain-assets` repository is optional and not required by this library.
- Importing an existing ledger (directory picker, validation, takeover) is not implemented; point `root` at an existing ledger instead.
- Transactions and reconciliation write `trans/collect.bean` and `trans/reconciliation.bean`; create appends, update replaces the latest block in place. A missing file returns `ledger_not_initialized`.
- Drafts support only `*` / `!`. Every posting must carry an explicit amount. Costs, prices, and automatic balancing are not supported.
- Batches are runtime state under `.ledger/batches/`; deleting `.ledger/` loses batch history but never the ledger. A batch cannot be partially voided.
- Accounts created implicitly by `auto_accounts`, with no `open` directive, can be queried, reconciled, and used in drafts, but cannot carry aliases or periods.
- `bean-check` / `bean-query` are external programs; the Beancount Python package is never imported.
- Ledger files must be UTF-8. The read side reports `invalid_encoding`; writes return `ledger_encoding_error`. GBK/ANSI files must be saved as UTF-8 first.
- On Windows, an overly long absolute path (260-character limit) makes the shadow copy fail with `shadow_copy_failed` and the offending path; move the ledger to a shorter path.
- Initial installation of a managed venv needs package-index access for `beancount` and `beanquery`; afterwards it can work offline.
- Reconciliation drift is not proactively alerted; `validate` / `reconcile-preview` do not return `reconciliation_drift`.
- This library guarantees structure, atomicity, and auditability. It does not judge whether an agent picked the right category for a transaction.

## Contributing

See [CONTRIBUTING.md](https://github.com/dhr2333/beancount-guard/blob/main/CONTRIBUTING.md). The short version: keep the core standard-library only, add a test for every behavior change, and do not add an MCP write surface without updating `FROZEN.md` and `PROTOCOL`.

## Development

```bash
cd beancount-guard
PYTHONPATH=. python -m unittest discover -s tests -v
```

Tests run on temporary ledger copies and cover alias conflicts, reconciliation rebuilds, zero writes on balance mismatch, draft transitions, concurrent writes, crash recovery, and query projection.

## CI

Jenkins Multibranch Pipeline. The configuration and local equivalent commands are in [docs/CI.md](https://github.com/dhr2333/beancount-guard/blob/main/docs/CI.md).

## License

MIT. See [LICENSE](https://github.com/dhr2333/beancount-guard/blob/main/LICENSE).

## 中文摘要

`beancount-guard` 是 Agent 与纯文本 Beancount 账本之间的写入边界。Agent 只能提交交易草稿或结构操作，人工批准后才落账；每次写入先在影子树由真实 `bean-check` 校验，再在写锁内原子替换，失败时原文件零改动。它不提供账本 UI、不做托管，也不把直接写权限交给模型。完整中文说明见 [README.zh-CN.md](https://github.com/dhr2333/beancount-guard/blob/main/README.zh-CN.md)。
