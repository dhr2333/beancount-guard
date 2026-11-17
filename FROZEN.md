# ACTIVE

Status: **active** since 2026-11-17 for the MVP-0 vertical slice.

The design baseline is v0.9. This repository now implements:

- fixed append targets `trans/collect.bean` / `trans/reconciliation.bean`;
- draft `update-draft`;
- `account-open`;
- the local read-only MCP server (`ledger_domain.mcp_server`) with BQL;
- 23 core tests.

Still out of MVP-0: initialization (DSH later), `account-close`, full `pad`
reconciliation (MVP-1), reconcile revoke (explicitly not done).

Design source: local Obsidian note `本地账本系统设计基线` (v0.9).
