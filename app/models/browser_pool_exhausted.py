"""No renderer became available within the acquire timeout."""

from __future__ import annotations


class BrowserPoolExhaustedError(Exception):
    """Lives beside the other declarations rather than in ``browser_pool``.

    The shared error classifier in ``app.crawl.result_helpers`` must
    isinstance-check it without importing a strategy module: browser_pool
    imports browser, and browser imports result_helpers, so defining it there
    would create an import cycle. ``browser_pool`` re-exports it for its existing
    importers.
    """
