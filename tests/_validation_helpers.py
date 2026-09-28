from tests._policy_factories import UNRESTRICTED_HOST_POLICY

from app.validation import get_env_int, registrable_domain
from app.validation import normalize_public_http_url as _normalize_public_http_url
from app.validation import public_url_or_none as _public_url_or_none


def normalize_public_http_url(url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _normalize_public_http_url(url, policy=policy)


def public_url_or_none(raw, *, resolve_against=None, policy=UNRESTRICTED_HOST_POLICY):
    return _public_url_or_none(raw, resolve_against=resolve_against, policy=policy)


def assert_url_refused(url: str, policy, reason: str) -> None:
    """``normalize_public_http_url`` must reject `url` for exactly `reason`.

    The rejection and the reason are one assertion. Stated as a bare
    ``pytest.raises(ValueError)``, the reason is the part that goes missing: a URL
    refused as ``private_network_target`` when the test meant ``host_denied``
    passes, and which of those two reasons wins is the entire subject of the
    host-policy tests.
    """
    import pytest

    from app.validation import normalize_public_http_url as normalize

    with pytest.raises(ValueError, match=reason):
        normalize(url, policy=policy)


__all__ = [
    "UNRESTRICTED_HOST_POLICY",
    "assert_url_refused",
    "get_env_int",
    "normalize_public_http_url",
    "public_url_or_none",
    "registrable_domain",
]
