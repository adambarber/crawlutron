"""Test-only host assertion: exact canonical-host membership, never substring.

CodeQL flags in/startswith on URL text; these are behavioral assertions, not
gates, but they are imprecise. Route every URL assertion through this helper.
"""

from __future__ import annotations

from urllib.parse import urlparse


def host_of(value) -> str:
    """Canonical host of ``value``: a ``NormalizedUrl``, an ``httpx.URL``, or a
    URL/host string. Plain host strings pass through unchanged."""
    host = getattr(value, "host", None)
    if host is not None:
        return str(host)
    if isinstance(value, str):
        parsed = urlparse(value if "://" in value else f"//{value}")
        return parsed.hostname or value.split("/", 1)[0].split(":", 1)[0]
    return str(value)


def host_in(container, host: str) -> bool:
    """True when some item in ``container`` has canonical host ``host``.

    Exact host equality -- ``example.com`` never matches ``notexample.com``."""
    return any(host_of(item) == host for item in container)
