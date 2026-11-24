# ACTIVE

Status: **active** since 2026-11-17 for the MVP-0 vertical slice; reconciliation closed loop since 2026-11-18; positioning baseline since 2026-11-20.

The design baseline is v0.26. This repository now implements:

- fixed append targets `trans/collect.bean` / `trans/reconciliation.bean`;
- draft `update-draft`;
- optional draft `metadata` (0.1.2), rendered into the approved transaction before `draft_id` and postings; this is an explicit user-requested exception to the feature freeze;
- import batches (unreleased): `submit-batch`, `list-batches`, `approve-batch`, `reject-batch`, `void-batch`; a batch envelope stores the claimed row count, skipped rows and draft ids under `.ledger/batches/`;
- posting `role` (`category` / `funding`) and `fallback` (`expense` / `income`), with `funding_unresolved` blocking approval and a `no_real_account` invariant at approval;
- `metadata.uuid` idempotency checked against rendered ledger metadata, not only the pending queue; same-batch and pending-uuid conflicts are rejected;
- batch void appends reversing entries in one shadow-validated write; it never deletes or rewrites history;
- `account-open`, `account-redate`, `account-metadata` and `balance` operation queues;
- `init_ledger` from the built-in template;
- the local read-only MCP server (`beancount_guard.mcp_server`) with BQL;
- the MVP-1 reconciliation closed loop: due/unreconciled, preview/commit, same-`asOf` update, history, auto-pad on late drafts;
- 61 test methods (57 domain, 4 CLI contract).

Still out: `account-close`, reconcile revoke (explicitly not done), month-end batch (explicitly not done), multi-currency (not planned).

## 2026-11-23 import batch exception

The human owner explicitly requested the import-batch slice after a real 90-row
Alipay import. This lifts the feature freeze for that slice only; no version is
bumped here. The MCP surface stays read-only: batch commands are JSON CLI / Python
API proposals, and the plugin's `ledger_*` Agent tools only enqueue proposals.
`PROTOCOL` stays 1 because every addition is optional and additive.

## Positioning (2026-11-20)

- This repository is a **write kernel**, not a ledger app and not an MCP write server.
- Category: **Beancount write kernel** / **ledger write guard**.
- Tagline: **Agents propose. You approve. `bean-check` decides.**
- The MCP surface is deliberately read-only. Write proposals go through the JSON CLI or Python API, so no model receives direct ledger write access.
- Adding MCP write tools is a deliberate non-goal at this stage. Changing that decision requires updating this file first, then `docs/POSITIONING.md`, then `PROTOCOL` if the public contract changes.
- Positioning work introduces no runtime behavior changes; the feature freeze remains in effect.
- Entry metadata (0.1.2) is the only approved runtime exception: agents can attach `time` / `uuid` / `status` (or any valid string metadata) to a draft so the approved transaction keeps the bill's provenance.

Design source: local Obsidian note `本地账本系统设计基线` (v0.26).
