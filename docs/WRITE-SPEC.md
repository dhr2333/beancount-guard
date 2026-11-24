# Beancount Write Spec v0

**Status:** draft, 2026-11-24. Extracted from a working reference implementation (`beancount-guard`, `PROTOCOL` 1). Not yet a community standard.

**Normative terms:** **MUST**, **MUST NOT**, **MAY**.

This spec defines write *semantics*. It does not define an API surface, a transport, or a UI.

## 1. Scope

| In scope | Out of scope |
| --- | --- |
| Proposal, approval, commit, void | Categorization correctness |
| Validation, atomicity, locking | Importers and bank connections |
| Idempotency and audit trail | Query and reporting |
| Authorization units and auto-approval | Presentation, layout, filters |
| Error classification | Hosting, sync, custody |

An implementation MAY expose CLI, HTTP, MCP, or a library. Only the semantics below are shared.

## 2. Roles and truth

- **Truth** is the `.bean` include graph rooted at `main.bean`. There is no second database.
- **Proposer** creates proposals: an agent, a script, or a UI.
- **Approver** authorizes a commit: a human, or a versioned policy (§6).
- **Validator** is the real Beancount parser over the whole ledger, not a model judgment.
- **Writer** is the single process holding the write lock.

Reference profile: transactions append to `trans/collect.bean`; `balance` / `pad` append to `trans/reconciliation.bean`. Other layouts MAY be used if they preserve I6.

## 3. Invariants

Every committed write MUST satisfy all of these.

| ID | Invariant |
| --- | --- |
| I1 | **Single writer.** Exactly one process commits at a time, under a lock. |
| I2 | **Explicit accounts.** A posting targets a declared account; no implicit accounts. |
| I3 | **Whole-ledger validation.** The candidate state passes the real validator before commit. |
| I4 | **Idempotency.** A proposal's idempotency key is checked against committed entries, not only the pending queue. |
| I5 | **Atomicity.** A failed commit leaves every ledger file byte-identical to its pre-state. |
| I6 | **Append-only history.** Committed entries are never edited or deleted; corrections are new entries. Voiding appends reversing entries. |

## 4. Lifecycle

```text
proposed --approve--> committed --void--> committed + reversal
    |
    +--reject--> rejected
```

- A proposal MUST NOT mutate the ledger.
- Structure changes (open account, change open date, metadata, balance) follow the same lifecycle as transactions.
- `rejected` and `voided` records MUST remain readable.

## 5. Authorization unit

The **authorization unit** is the set of proposals bound to one approval decision and committed atomically.

- An implementation MUST declare its unit: per-item or per-batch.
- An approval MUST bind: approver identity, unit id, every member proposal id, validator version, timestamp.
- A unit MUST be all-or-nothing. Partial commits are forbidden.

What one signature covers is a property of the audit trail, not of the screen. How the unit is rendered is out of scope.

## 6. Auto-approval

A unit MAY be committed without a human only under an explicit, versioned policy. The policy MUST be machine-checkable predicates over proposed fields and validator output.

These MUST block auto-approval regardless of policy:

- any validator failure;
- idempotency conflict;
- funding account unresolved (absent, or several candidates);
- `before_account_open`, `closed_account`, `no_real_account`;
- coverage mismatch in a batch (`drafts + skipped + duplicates + failed != rows`).

An implementation that auto-approves anything MUST record the policy name and version in the audit record. An auto-approved unit without a named policy is non-conformant.

## 7. Error classification

Errors MUST be machine-classifiable. Names below are the recommended spellings; the obligation column is normative.

| Class | Meaning | Caller obligation |
| --- | --- | --- |
| `invalid_draft` / `invalid_batch` / `invalid_argument` | Malformed proposal | Fix and resubmit |
| `bean_check_failed` | Candidate ledger fails the validator | Do not retry blindly; report |
| `duplicate_transaction` | Idempotency key already committed | Treat as success; do not retry with a new key |
| `funding_unresolved` | Funding account absent or ambiguous | Ask the human; never guess, never open an account to remove the ambiguity |
| `no_real_account` | Posting targets a non-leaf or non-asset/liability account | Ask the human |
| `before_account_open` / `closed_account` | Entry date falls outside the account's life | Propose an open-date change; do not fall back to another account |
| `ledger_not_writable` / `ledger_encoding_error` / `lock_timeout` | Environment or concurrency state | Report to the operator; do not patch the ledger |

## 8. Audit record

Each committed unit MUST have a durable record containing:

- unit id and member proposal ids;
- approver: a human identity, or `policy:<name>@<version>`;
- input digest or evidence reference;
- validator name and version;
- result, timestamp, and content hashes before and after.

If it is not in the record, it did not happen.

## 9. Conformance

An implementation is conformant when all of these hold:

1. **Crash safety.** Kill the writer between staging and replace: the ledger is either fully committed or byte-identical to its pre-state.
2. **Replay.** Submitting the same proposal twice commits once; the second returns `duplicate_transaction` and changes nothing.
3. **Atomic unit.** Inject a failure on member *n* of a batch: no member is committed.
4. **Audit attribution.** Every committed unit names an approver — human identity or a versioned policy.

## 10. Open questions for v0

- Is the two-file append layout normative, or only a reference profile? Currently: profile.
- Do amount and counterparty thresholds belong in this spec, or in a policy layer above it?
- Multi-currency and cost basis are out of scope for v0.
