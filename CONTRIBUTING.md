# Contributing to beancount-guard

Thanks for helping. This project is a **write kernel** for plain-text Beancount ledgers, not a ledger app. Read [docs/POSITIONING.md](docs/POSITIONING.md) before proposing a feature: it explains the category, the deliberate non-goals, and the public contract.

## What belongs here

In scope:

- the write gate: proposals, shadow-tree `bean-check` validation, atomic commit, locking, audit;
- ledger semantics: aliases, reconciliation periods, due dates, reconciliation history;
- frontend-neutral contracts: Python API, JSON CLI, read-only MCP;
- tests and documentation that make those guarantees verifiable.

Out of scope:

- a ledger UI, a hosted service, or a second database;
- a transaction classifier or a model;
- MCP write tools (the MCP surface is deliberately read-only);
- bank connections or importers.

If you want to change an out-of-scope decision, open an issue first. It requires updating `FROZEN.md`, then `docs/POSITIONING.md`, then `PROTOCOL` if the public contract changes.

## Development setup

Requirements: Python 3.11+ and the external Beancount tools.

```bash
git clone https://github.com/dhr2333/beancount-guard.git
cd beancount-guard
python -m venv .venv
. .venv/bin/activate
python -m pip install "beancount==3.2.3" "beanquery==0.2.0"
python -m pip install -e .
```

The core itself has no third-party dependencies. `bean-check` and `bean-query` are invoked as external programs; Beancount is never imported by the package.

The test suite uses `BEAN_CHECK` / `BEAN_QUERY` when set, then falls back to a managed `Assets/.venv` on the maintainer machine, then to `PATH`. In CI both are installed into the same Python environment.

## Running the checks

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
python -m compileall -q beancount_guard
PYTHONPATH=. python -m beancount_guard --root . health
```

For a packaging check:

```bash
python -m pip install build twine
python -m build
python -m twine check dist/*
```

All tests run on temporary ledger copies. Never point a test at a real ledger, and never commit a real ledger file or personal financial data.

## Code rules

- **Standard library only in the core.** Do not add runtime dependencies to `beancount_guard/`.
- **All writes go through the gate.** Use `validate_ledger`, `atomic_write`, and `write_lock` from `validation.py`. Do not write a `.bean` file directly.
- **Fail with a stable code.** Raise `LedgerError` through `fail(code, message, details)`; do not raise bare exceptions for business failures.
- **Keep the CLI and MCP surfaces explicit.** A new CLI command must be wired in `_build_parser` and `_dispatch` in `cli.py` and documented in the README. Do not add an MCP write tool.
- **Respect the protocol.** `PROTOCOL` increments only on a breaking change to the CLI/MCP contract. Additive changes do not bump it.
- **Keep ledger semantics in one place.** Alias resolution, periods, and reconciliation rules belong in the existing modules; do not duplicate them in adapters.
- **Add a test for every behavior change.** Cover failure paths as well as success: a failed validation must leave the original file untouched.
- **Match the existing style.** No formatter is enforced; keep the surrounding style, comments in Chinese or English as the file already uses, and avoid unrelated refactors.

## Pull requests

1. Open an issue for anything larger than a small fix, so the design can be agreed first.
2. Keep one topic per pull request.
3. Include tests and update the README or `docs/POSITIONING.md` when behavior or positioning changes.
4. Use a short, imperative commit message with a conventional prefix: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`.
5. Do not bump the version or publish a release in a feature pull request; that is a maintainer action.

## Security and privacy

Do not open a public issue containing a working exploit, credentials, or real ledger data. Report the problem privately through the contact information on the maintainer's GitHub profile, with a minimal reproduction using synthetic data.

## License

By contributing, you agree that your contribution is licensed under the MIT License, the same license as this repository.
