from app.validation import HostPolicy, get_env_int, registrable_domain
from app.validation import normalize_public_http_url as _normalize_public_http_url
from app.validation import public_url_or_none as _public_url_or_none

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()


def normalize_public_http_url(url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _normalize_public_http_url(url, policy=policy)


def public_url_or_none(raw, *, resolve_against=None, policy=UNRESTRICTED_HOST_POLICY):
    return _public_url_or_none(raw, resolve_against=resolve_against, policy=policy)


__all__ = [
    "UNRESTRICTED_HOST_POLICY",
    "get_env_int",
    "normalize_public_http_url",
    "public_url_or_none",
    "registrable_domain",
]
