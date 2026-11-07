"""Origin-scoped credentials, keyed by canonical origin, owning the two decisions
that must never disagree: ``origin_of`` and ``authorization_value``. Cross-host
stripping is the redirect loop's, not this module's.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping

from crawlutron.models.credential import Credential
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.header_values import reject_injectable_header_value

__all__ = ["apply_credentials", "authorization_value", "origin_of"]


def origin_of(url: str) -> str:
    """Canonical ``scheme://host[:port]`` for ``url``, via admission: one spelling,
    the one NormalizedUrl produces. Raises on non-HTTP schemes, userinfo, and any
    host admission refuses -- so a credential for an origin no crawl can reach is
    loud at config load, never silently dead.
    """
    return NormalizedUrl(url.strip(), policy=HostPolicy.unrestricted()).origin


def authorization_value(credential: Credential) -> str:
    """The single wire shape for a validated credential -- a pure projection, since
    usability is decided by ``Credential``'s validator at the read edge (it used
    to raise here, three layers down, on the first request that used a bad one).
    """
    if credential.kind == "bearer":
        tok = credential.token
        if tok is None:
            raise ValueError("bearer credential must have token")
        secret = tok.get_secret_value() if hasattr(tok, "get_secret_value") else tok
        return f"Bearer {secret}"
    pwd = credential.password
    if pwd is None:
        raise ValueError("basic credential must have password")
    pwd_secret = pwd.get_secret_value() if hasattr(pwd, "get_secret_value") else pwd
    user = credential.username
    if user is None:
        raise ValueError("basic credential must have username")
    username_secret = user.get_secret_value() if hasattr(user, "get_secret_value") else user
    raw = f"{username_secret}:{pwd_secret}".encode()
    return f"Basic {base64.b64encode(raw).decode()}"


def apply_credentials(
    headers: dict[str, str],
    url: NormalizedUrl,
    credentials: Mapping[str, Credential],
) -> dict[str, str]:
    """Attach the Authorization header iff ``url``'s origin holds one, by exact
    canonical-origin equality: a credential for origin A can never attach to origin B.
    The built value is wire-law-checked too, even past model validation.
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
