"""Compatibility shim: core's public regex names re-bound from
:mod:`safe_url.safe_regex`, which owns the single implementation."""

from __future__ import annotations

import safe_url.safe_regex as _safe_regex

DEFAULT_MATCH_TIMEOUT_SECONDS = _safe_regex.DEFAULT_MATCH_TIMEOUT_SECONDS
MAX_PATTERN_LENGTH = _safe_regex.MAX_PATTERN_LENGTH
BoundedPattern = _safe_regex.BoundedPattern
RegexTimeout = _safe_regex.RegexTimeout
UnsafePattern = _safe_regex.UnsafePattern
compile_untrusted = _safe_regex.compile_untrusted
match_bounded = _safe_regex.match_bounded
