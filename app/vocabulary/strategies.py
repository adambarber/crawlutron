"""The fetch strategies, by name.

Which strategies exist, and the order they are tried in. A strategy says HOW
bytes are retrieved; how those bytes become text is ``app.vocabulary.extraction``
and is orthogonal to this list. The strategy implementations live in
``app.transport`` after #149's slice 2; the names they are selected by live here.
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
CFFI_IMPERSONATE_TARGET: str = "firefox135"
BROWSER_HEADED_WARMED: Final[Literal["BROWSER_HEADED_WARMED"]] = "BROWSER_HEADED_WARMED"

# Strategy ordering / registries (imported by service.py)
HTTP_STRATEGIES: set[str] = {HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE}
BROWSER_STRATEGIES: set[str] = {BROWSER_HEADLESS, BROWSER_HEADED_WARMED}
SUPPORTED_STRATEGIES: set[str] = HTTP_STRATEGIES | BROWSER_STRATEGIES
DEFAULT_STRATEGIES: list[str] = [
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
]
