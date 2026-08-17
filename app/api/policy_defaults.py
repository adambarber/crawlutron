"""Shared default policy configurations.

Centralizes the default strategy chains and limit configurations
used by both agent_hints.py and main.py documentation/examples.
"""

from app.constants import (
    HTTP_DECLARED_CRAWLER,
    HTTP_CFFI_IMPERSONATE,
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
)


BROWSER_FALLBACK_POLICY = {
    "strategies": [HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE, BROWSER_HEADLESS],
    "limits": {
        "timeout_seconds": 30,
        "max_retries": 2,
        "max_discovered_links": 250,
    },
}

DOCUMENT_EXTRACTION_POLICY = {
    "strategies": [HTTP_DECLARED_CRAWLER],
    "extraction": EXTRACTION_DOCUMENT,
    "limits": {
        "timeout_seconds": 60,
        "max_retries": 1,
        "max_discovered_links": 0,
    },
}
