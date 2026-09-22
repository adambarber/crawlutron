"""Origin-scoped credentials (Tier 2b chokepoint).

One module owns the two decisions that must never disagree:

- ``origin_of``: the canonical origin form. Credential KEYS are validated
  against it (``origin_of(key) == key``), job-row policies are normalized
  through it, and every request looks up with it -- one rule, one site.
- ``authorization_value``: the only place a credential becomes an
  ``Authorization`` header value.

Cross-host containment is NOT re-implemented here: the produced value lands
in the normal header set, and the redirect loop's Fix-3 scrub
(``_CREDENTIAL_HEADERS`` strips ``authorization`` across hosts) enforces
"credentials never cross host boundaries" at the hop boundary, exactly as
it does for cookies.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

__all__ = ["apply_credentials", "authorization_value", "origin_of"]


def origin_of(url: str) -> str:
    """Canonical ``scheme://host[:port]`` for ``url``.

    Lowercases scheme/host, drops default ports, rejects non-HTTP schemes
    and userinfo (a credential must never be addressed through ``user@``).
    Idempotent: ``origin_of(origin_of(u)) == origin_of(u)``.
    """
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("credential origin requires http/https")
    host = parsed.hostname
    if not host:
        raise ValueError("credential origin requires a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credential origin may not carry userinfo")
    port = parsed.port  # raises ValueError for a malformed port
    if port is not None and not ((scheme == "http" and port != 80) or (scheme == "https" and port != 443)):
        port = None
    return f"{scheme}://{host}" if port is None else f"{scheme}://{host}:{port}"


def authorization_value(credential: Mapping[str, Any]) -> str:
    """The single wire shape for a validated credential mapping."""
    kind = credential.get("kind")
    if kind == "bearer":
        token = credential.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("bearer credential requires a token")
        return f"Bearer {token}"
    if kind == "basic":
        username = credential.get("username")
        password = credential.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("basic credential requires username and password")
        raw = f"{username}:{password}".encode()
        return f"Basic {base64.b64encode(raw).decode()}"
    raise ValueError("credential kind must be bearer or basic")


def apply_credentials(
    headers: dict[str, str],
    url: str,
    credentials: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """Attach the Authorization header iff ``url``'s origin holds one.

    Lookup is exact canonical-origin equality: a credential written for
    origin A can never attach to a request for origin B (same-origin
    redirects keep it; cross-host redirects strip it via Fix 3).
    """
    if not credentials:
        return headers
    try:
        origin = origin_of(url)
    except ValueError:
        return headers
    credential = credentials.get(origin)
    if credential is not None:
        headers["Authorization"] = authorization_value(credential)
    return headers
