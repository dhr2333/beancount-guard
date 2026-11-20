# Positioning: the write boundary for agent-written Beancount

This document is the reference for how `beancount-guard` presents itself: in the README, in release notes, in issue replies, and in conversations with frontend maintainers. It is not a feature roadmap.

## Positioning statement

For people and teams who keep plain-text Beancount books and want AI agents to help write them, `beancount-guard` is a self-hosted write kernel that turns every agent write into a reviewed proposal and commits only after a real `bean-check` — unlike query-only MCP servers that cannot write, direct-write tools that trust the agent, or hosted services that take custody of the ledger.

One sentence:

> The write boundary between AI agents and your plain-text Beancount ledger.

Tagline:

> Agents propose. You approve. `bean-check` decides.

## Category and naming

Use:

- **Beancount write kernel**
- **Ledger write guard**
- **Write boundary** (in narrative copy)

Avoid:

- **AI accounting software** — puts the project in a product category it does not occupy.
- **Bookkeeping app** — implies a UI and a workflow the project deliberately lacks.
- **MCP server** — describes one adapter, not the project; it also invites comparison with read-only query servers.
- **Autonomous bookkeeping** — contradicts the human-approval model.

The category determines the comparison set. If the project is called an "AI accounting tool", it competes with hosted products and agent CLIs. If it is called a "write kernel", it complements them: they generate and display, it guards the commit.

## Message hierarchy

### One sentence

`beancount-guard` is the write boundary between AI agents and your plain-text Beancount ledger.

### Three bullets

1. **Every write is a proposal.** Transactions enter a draft queue; structure changes enter an operation queue. Nothing reaches the ledger at proposal time.
2. **Every commit is validated for real.** The candidate is checked by the actual `bean-check` in a disposable shadow tree, then replaced atomically under a lock. A failed check leaves the original untouched.
3. **Your ledger stays plain text, local, and yours.** No account, no hosted custody, no second database. The guard is a library, a JSON CLI, and a deliberately read-only MCP surface.

### 30-second pitch

Agents are good at drafting entries and bad at guaranteeing atomicity, `include` consistency, and an audit trail. `beancount-guard` sits between the agent and the `.bean` file: it accepts structured proposals, validates the whole ledger with the real `bean-check` in a shadow tree, waits for a human approval, and then commits atomically under a lock. It also understands ledger semantics — aliases, reconciliation periods, due dates, and late entries — so the review is about the bookkeeping decision, not about repairing corrupted text.

### What the project does not claim

It does not make the agent smarter, and it does not judge whether the category chosen for a transaction is correct. It guarantees structure, atomicity, and auditability. This honesty is part of the position: the guard is the part you can verify.

## Audience and use cases

Primary audiences:

1. **Self-hosting Beancount users** who already keep books in plain text and want an agent to draft entries without giving it unrestricted file access.
2. **Agent and tool builders** who need a ledger write layer they can call from any framework instead of reimplementing validation, locking, and review.
3. **Frontend maintainers** (Fava extensions, Obsidian plugins, custom panels) who want a shared write boundary instead of embedding ledger semantics in the UI.

Representative use cases:

- A bank-statement import produces draft transactions; a human reviews and approves them.
- An agent proposes opening a new account, changing an alias or reconciliation period, or submitting a balance assertion; the structure change waits in the operation queue.
- A month-end reconciliation is previewed, committed, and later updated for the same `asOf`; late drafts are absorbed with an auto-inserted pad.
- Several agents or frontends share one ledger without each inventing its own write path.

## Competitive landscape

The following table compares public positioning, not implementation quality. Sources were checked on **2026-11-20**; no independent benchmark was run. Details may change after that date.

| Project / form | Primary job | Write path | Human review | Ledger semantics | Deployment |
|---|---|---|---|---|---|
| **beancount-guard** | Write boundary for plain-text Beancount | Structured drafts and operations, then shadow validation and atomic commit | Explicit approval queue | Aliases, reconciliation periods, due dates, same-`asOf` updates, auto-pad | Self-hosted library / CLI |
| `beanquery-mcp` | Query Beancount with BQL over MCP | None | n/a | Query only | Local MCP |
| `beanie-mcp` | Query Beancount v3 ledgers with BQL over MCP | None | n/a | Query only | Local MCP |
| `StdioA/beancount-mcp` | Query and submit transactions over MCP | Direct transaction submit | Not described in the public README | Minimal | Local MCP |
| `bea` (beancount.io CLI) | Agent-oriented local ledger CLI | Preview then apply | Preview-first review | Import, check, report | Local CLI, optional hosted |
| beancount.io hosted MCP | Hosted ledger access | Read/write/admin scopes over OAuth | Account and scope dependent | Hosted platform | Cloud |
| ERPClaw | AI-native open-source accounting | Agent posts to a database | Product workflow | Database accounting | Self-hosted or cloud |
| Fava | Beancount web UI | None | n/a | Presentation | Local or self-hosted |

Sources:

- `beanquery-mcp`: <https://github.com/vanto/beanquery-mcp>
- `beanie-mcp`: <https://github.com/klinikal/beanie-mcp>
- `StdioA/beancount-mcp`: <https://github.com/stdioa/beancount-mcp>
- `bea` CLI: <https://beancount.io/blog/2026/09/10/bea-accounting-cli-for-ai-agents>
- beancount.io hosted MCP: <https://mcp.tc/i/beancount-io>
- ERPClaw: <https://www.erpclaw.ai/ai-accounting/>
- Write-safety framing for accounting MCP tools: <https://www.layer3labs.io/comparisons/ai-bookkeeping-tools-claude-chatgpt>

### The gap the project occupies

The ecosystem already has places to **read** a Beancount ledger and products that **keep** one. It also has tools that **write** one. What it lacks is a shared, auditable **write boundary** that any reader, UI, or agent can sit behind.

That is the project's slot:

- Read-only query MCPs answer questions. They need a write path; this project is that path.
- Direct-write MCPs and agent CLIs own the write. This project turns the write into a proposal with a review gate.
- Hosted services provide convenience but take custody. This project keeps the ledger local and plain text.
- Full accounting systems replace the ledger. This project refuses to replace it.

### Two axes

|  | Reusable kernel | Product |
|---|---|---|
| **Read-only** | Query MCPs | Fava, hosted dashboards |
| **Write-capable** | **beancount-guard** | Agent CLIs, hosted accounting |

The empty-looking cell is deliberate: a write-capable product already exists in several forms; a write-capable **kernel** that other tools compose is the less crowded position, and it is the one this project already implements.

## Proof points mapped to code

| Claim | Where it is true |
|---|---|
| Writes are proposals, not direct edits | `beancount_guard/drafts.py`, `beancount_guard/operations.py` |
| Validation is a real `bean-check`, not a model promise | `beancount_guard/validation.py` (`validate_ledger`, shadow tree) |
| Commits are atomic and serialized | `beancount_guard/validation.py` (`atomic_write`, `write_lock`) |
| Crash recovery is possible | `drafts.py` `_find_draft_location`, target-file `draft_id` recovery |
| The guard understands ledger semantics | `core.py` (aliases, periods), `reconciliation.py` (preview/commit/update/history), `quickpath.py` (alias fast path) |
| Frontends share one contract | `cli.py` (JSON envelope), `mcp_server.py` (read-only MCP), `PROTOCOL` |
| Failure does not damage the ledger | `tests/test_domain.py` cases for balance mismatch, invalid metadata, concurrency, crash recovery |
| The local ledger stays plain text | Fixed targets `trans/collect.bean` and `trans/reconciliation.bean`; runtime state only in `.ledger/` |

Test count: 51 test methods (47 domain, 4 CLI contract). Update this number when tests change.

## Deliberate non-goals

These are positioning decisions, not temporary gaps:

- **No MCP write tools in the current stage.** The MCP surface is read-only by design. Write proposals go through the JSON CLI or Python API so the model never receives direct ledger write access.
- **No hosted service.** The project does not store, sync, or take custody of a ledger.
- **No ledger UI.** Fava, Obsidian plugins, and custom panels remain the views.
- **No transaction classifier or model.** Categorization is the agent's or the human's job; the guard checks structure and semantics.
- **No bank connection or importer.** Importers can call the proposal API; the guard does not own the source integration.
- **No replacement for Beancount or Fava.** The project is the layer in front of them.

If any of these changes, update this document first, then `FROZEN.md`, then the `PROTOCOL` if the public contract changes.

## Ecosystem placement

### GitHub repository metadata

Suggested About text:

> The write boundary between AI agents and plain-text Beancount ledgers. Reviewed proposals, real `bean-check` validation, atomic commits.

Suggested topics:

`beancount`, `plain-text-accounting`, `plaintext-accounting`, `ledger`, `accounting`, `ai-agents`, `human-in-the-loop`, `mcp`, `reconciliation`, `write-guard`

### PyPI metadata

Keywords should mirror the topics. The package description should lead with the category, not a feature list:

> Agent-era write guard for Beancount ledgers: reviewed drafts, real bean-check validation, atomic writes, reconciliation.

### Pairing stories

- **Query MCP + write guard.** Use `beanquery-mcp` or `beanie-mcp` to read and answer; use `beancount-guard` to propose and commit. The two do not compete.
- **Fava / Obsidian + write guard.** The view stays a view. The guard is the boundary behind it.
- **Custom agent + write guard.** The agent calls the Python API or JSON CLI; the same guard works across frameworks and languages.

### Content angles for later (not part of this round)

These are candidate angles only; no external post is part of this round:

- "Why an agent needs a write boundary, not a write tool."
- "The missing layer in the Beancount MCP stack."
- "Query with one MCP, write through a guard."
- "What `bean-check` can and cannot protect you from."

External placement candidates (verify current submission rules before acting): plain-text accounting awesome lists, Beancount community lists, MCP server registries, and package directories.

## Messaging do / don't

Do:

- boundary, proposal, review, atomic, audit, semantic guard
- "plain text stays yours"
- "real `bean-check`", "shadow tree", "write lock"
- "the guard does not judge the category"

Don't:

- only, best, first, revolutionary
- "prevents wrong categorization"
- "replaces Fava" or "replaces Beancount"
- "fully autonomous bookkeeping"
- competitor comparisons without a source and a date

## Review checklist for future copy

Before publishing a README change, release note, or comparison:

1. Does the first sentence name the category (write boundary / write kernel)?
2. Does it answer "why not just let the agent edit the file?"
3. Is every capability claim backed by a file or a test?
4. Is every competitor claim sourced and dated?
5. Does it avoid claiming judgment or model quality?
6. Does it keep the local-first, plain-text, no-custody promise?
