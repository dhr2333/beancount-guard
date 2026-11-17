"""Ledger domain core library.

Public API:
    create_ledger_domain(root=...)
    LedgerDomain
    LedgerError

The package has no third-party dependencies. ``bean-check`` and ``bean-query``
are invoked as external programs; Beancount itself is never imported.
"""

from __future__ import annotations

from .core import LedgerDomain, create_ledger_domain
from .errors import LedgerError, fail

__all__ = ["LedgerDomain", "create_ledger_domain", "LedgerError", "fail"]
__version__ = "0.1.0"
