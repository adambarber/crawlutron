"""The fetch strategies, by name and fallback order.

A strategy says HOW bytes are retrieved (parsing them is ``extraction``, orthogonal);
the implementations and the names they are selected by live in ``app.transport``.
"""

from __future__ import annotations

from typing import Final, Literal

# Fetch strategies (how bytes are retrieved). Orthogonal to extraction.
# ---------------------------------------------------------------------------
HTTP_DECLARED_CRAWLER: Final = "HTTP_DECLARED_CRAWLER"
HTTP_CFFI_IMPERSONATE: Final = "HTTP_CFFI_IMPERSONATE"
BROWSER_HEADLESS: Final[Literal["BROWSER_HEADLESS"]] = "BROWSER_HEADLESS"

# TLS-fingerprint target for the HTTP_CFFI_IMPERSONATE strategy: the
# fetcher and the agent-hints copy both reference this, so they cannot drift.
CFFI_IMPERSONATE_TARGET: Final[str] = "firefox135"
BROWSER_HEADED_WARMED: Final[Literal["BROWSER_HEADED_WARMED"]] = "BROWSER_HEADED_WARMED"

# Strategy ordering / registries.
HTTP_STRATEGIES: set[str] = {HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE}
BROWSER_STRATEGIES: set[str] = {BROWSER_HEADLESS, BROWSER_HEADED_WARMED}
DEFAULT_STRATEGIES: list[str] = [
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
]
