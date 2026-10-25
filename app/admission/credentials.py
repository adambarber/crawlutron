"""Origin-scoped credentials, keyed by canonical origin, owning the two decisions
that must never disagree: ``origin_of`` and ``authorization_value``. Cross-host
stripping is the redirect loop's, not this module's.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from urllib.parse import urlsplit

from app.models.credential import Credential
from app.models.normalized_url import NormalizedUrl
from app.safety.header_values import reject_injectable_header_value

__all__ = ["apply_credentials", "authorization_value", "origin_of"]


def origin_of(url: str) -> str:
    """Canonical ``scheme://host[:port]`` for ``url``: lowercases scheme/host, drops
    default ports, rejects non-HTTP schemes and userinfo (never addressed through
    ``user@``). Idempotent.
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


def authorization_value(credential: Credential) -> str:
    """The single wire shape for a validated credential -- a pure projection, since
    usability is decided by ``Credential``'s validator at the read edge (it used
    to raise here, three layers down, on the first request that used a bad one).
    """
    if credential.kind == "bearer":
        return f"Bearer {credential.token}"
    raw = f"{credential.username}:{credential.password}".encode()
    return f"Basic {base64.b64encode(raw).decode()}"


def apply_credentials(
    headers: dict[str, str],
    url: NormalizedUrl,
    credentials: Mapping[str, Credential],
) -> dict[str, str]:
    """Attach the Authorization header iff ``url``'s origin holds one, by exact
    canonical-origin equality: a credential for origin A can never attach to origin B.
    The built value is wire-law-checked too (#441), even past model validation.
    """
    if not credentials:
        return headers
    origin = url.origin
    credential = credentials.get(origin)
    if credential is not None:
        value = authorization_value(credential)
        reject_injectable_header_value("Authorization", value)
        headers["Authorization"] = value
    return headers
