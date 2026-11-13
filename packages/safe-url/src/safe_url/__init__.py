"""SSRF-safe URL admission: NormalizedUrl, HostPolicy, credential-safe redaction."""

from __future__ import annotations

from safe_url.address_class import AddressClass, classify, is_loopback_bind
from safe_url.host_canonical import idna_host, punycode_host
from safe_url.host_policy import (
    HostPolicy,
    allow_private_targets,
    host_egress_reason,
    host_policy_reason,
    reset_private_target_memo,
)
from safe_url.mapped_value_errors import (
    clear_mapped_value_errors,
    mapped_value_errors,
    record_mapped_value_error,
)
from safe_url.normalized_url import NormalizedUrl, admit_or_none, admit_with_reason, refusal_literals
from safe_url.registrable_domain import is_public_suffix, registrable_domain
from safe_url.safe_regex import (
    DEFAULT_MATCH_TIMEOUT_SECONDS,
    MAX_PATTERN_LENGTH,
    BoundedPattern,
    RegexTimeout,
    UnsafePattern,
    compile_untrusted,
    match_bounded,
)
from safe_url.scope_match import scope_allows
from safe_url.url_redaction import is_secret_name, redact_url, redact_urls_in_text, redacted_marker

__all__ = [
    "DEFAULT_MATCH_TIMEOUT_SECONDS",
    "MAX_PATTERN_LENGTH",
    "AddressClass",
    "BoundedPattern",
    "HostPolicy",
    "NormalizedUrl",
    "RegexTimeout",
    "UnsafePattern",
    "admit_or_none",
    "admit_with_reason",
    "allow_private_targets",
    "classify",
    "clear_mapped_value_errors",
    "compile_untrusted",
    "host_egress_reason",
    "host_policy_reason",
    "idna_host",
    "is_loopback_bind",
    "is_public_suffix",
    "is_secret_name",
    "mapped_value_errors",
    "match_bounded",
    "punycode_host",
    "record_mapped_value_error",
    "redact_url",
    "redact_urls_in_text",
    "redacted_marker",
    "refusal_literals",
    "registrable_domain",
    "reset_private_target_memo",
    "scope_allows",
]
