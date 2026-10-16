"""No renderer became available within the acquire timeout."""

from __future__ import annotations


class BrowserPoolExhaustedError(Exception):
    """Defined beside the other declarations, not in ``browser_pool``, to avoid a cycle.

    The shared classifier must isinstance-check it without importing a strategy module
    (browser_pool -> browser -> classification); ``browser_pool`` re-exports the name.
    """
