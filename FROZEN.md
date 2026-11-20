# ACTIVE

Status: **active** since 2026-11-17 for the MVP-0 vertical slice; reconciliation closed loop since 2026-11-18.

The design baseline is v0.18. This repository now implements:

- fixed append targets `trans/collect.bean` / `trans/reconciliation.bean`;
- draft `update-draft`;
- `account-open`;
- the local read-only MCP server (`beancount_guard.mcp_server`) with BQL;
- the MVP-1 reconciliation closed loop: due/unreconciled, preview/commit, same-as_of update, history, auto-pad on late drafts, 41 core tests.

Still out: initialization (DSH later), `account-close`, reconcile revoke (explicitly not done), month-end batch (explicitly not done), multi-currency (not planned).

Design source: local Obsidian note `本地账本系统设计基线` (v0.25).
