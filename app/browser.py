"""Browser rendering strategy.

The :class:`BrowserRenderer` implementation now lives in
:mod:`app.crawl.strategies.browser`. This module re-exports it (and the
classification sets) so ``import app.browser`` keeps working for existing
callers and tests.
"""

from app.crawl.strategies.browser import (
    BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    TRANSIENT_CLASSIFICATIONS,
    BrowserRenderer,
)

__all__ = [
    "BrowserRenderer",
    "BLOCKED_CLASSIFICATIONS",
    "FAILED_CLASSIFICATIONS",
    "TRANSIENT_CLASSIFICATIONS",
]
