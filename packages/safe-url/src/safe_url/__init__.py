"""SSRF-safe URL admission: NormalizedUrl, HostPolicy, credential-safe redaction."""

from __future__ import annotations

from importlib import import_module

from safe_url.address_class import AddressClass, classify, is_loopback_bind
from safe_url.host_canonical import idna_host, punycode_host
from safe_url.host_policy import (
    HostPolicy,
    allow_private_targets,
    host_egress_reason,
    host_policy_reason,
    reset_private_target_memo,
)
from safe_url.normalized_url import NormalizedUrl, admit_or_none, admit_with_reason, refusal_literals
from safe_url.registrable_domain import is_public_suffix, registrable_domain
from safe_url.url_redaction import is_secret_name, redact_url, redact_urls_in_text, redacted_marker

__all__ = [
    "DEFAULT_MATCH_TIMEOUT_SECONDS",
    "MAX_PATTERN_LENGTH",
    "AddressClass",
    "BoundedPattern",
    "HostPolicy",
    "NormalizedUrl",
    "RegexTimeout",
    "RegexUnavailable",
    "UnsafePattern",
    "admit_or_none",
    "admit_with_reason",
    "allow_private_targets",
    "classify",
    "compile_untrusted",
    "host_egress_reason",
    "host_policy_reason",
    "idna_host",
    "is_loopback_bind",
    "is_public_suffix",
    "is_secret_name",
    "match_bounded",
    "psl_available",
    "punycode_host",
    "redact_url",
    "redact_urls_in_text",
    "redacted_marker",
    "refusal_literals",
    "registrable_domain",
    "reset_private_target_memo",
    "scope_allows",
]

#: Regex-backed names and their owning module, loaded lazily via ``__getattr__``
#: so the base import stays free of the ``regex`` C extension.
_REGEX_BACKED: dict[str, str] = {
    "DEFAULT_MATCH_TIMEOUT_SECONDS": "safe_url.safe_regex",
    "MAX_PATTERN_LENGTH": "safe_url.safe_regex",
    "BoundedPattern": "safe_url.safe_regex",
    "RegexTimeout": "safe_url.safe_regex",
    "UnsafePattern": "safe_url.safe_regex",
    "compile_untrusted": "safe_url.safe_regex",
    "match_bounded": "safe_url.safe_regex",
    "scope_allows": "safe_url.scope_match",
}


class RegexUnavailable(ImportError):
    """A regex-backed name was touched without the ``[regex]`` extra.

    Install hint: ``pip install crawlutron-safe-url[regex]``.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"safe_url.{name} requires the regex extension: install crawlutron-safe-url[regex]")


def __getattr__(name: str):
    """PEP 562 lazy loader for the regex-backed names.

    A name that needs the ``regex`` extension raises :class:`RegexUnavailable`
    -- never a bare ImportError -- when the extra is absent.
    """
    module = _REGEX_BACKED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        mod = import_module(module)
    except ImportError as error:
        raise RegexUnavailable(name) from error
    return getattr(mod, name)


def psl_available() -> bool:
    """Whether the publicsuffixlist dataset loaded this process.

    When False, registrable_domain answers from the documented two-label
    heuristic: site grouping degrades explicitly, never silently.
    """
    from safe_url.registrable_domain import _psl_available

    return _psl_available()
