"""beancount-guard — the write guard and reconciliation core for Beancount ledgers.

The package owns ledger semantics only: alias/period governance, reconciliation
rebuild, a reviewed draft queue, validated atomic writes and read projections.
It starts no service and depends on no UI; frontends call it through these
functions or through the JSON CLI contract.

Public API:
    create_beancount_guard(root=...)
    BeancountGuard
    LedgerError
    PROTOCOL

The package has no third-party dependencies. ``bean-check`` and ``bean-query``
are invoked as external programs; Beancount itself is never imported.
"""

from __future__ import annotations

from .core import BeancountGuard, create_beancount_guard
from .errors import LedgerError, fail

__all__ = ["BeancountGuard", "create_beancount_guard", "LedgerError", "fail", "PROTOCOL"]
__version__ = "0.1.2"
# The CLI/MCP contract version; bump it only when a frontend must change.
PROTOCOL = 1
