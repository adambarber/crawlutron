"""The one IDNA host grammar: every host that crosses this codebase passes it.

Admission refuses hosts it cannot encode; matching sites catch and passthrough.
"""

from __future__ import annotations

__all__ = ["idna_host"]


def idna_host(host: str) -> str:
    """Lower, strip the trailing FQDN dot, punycode, lower. Raises ``ValueError``
    when the host cannot be encoded -- the caller owns its posture (refuse at
    admission, passthrough for policy matching).
    """
    value = host.strip().lower().rstrip(".")
    try:
        return value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ValueError("invalid_idn_host") from error
