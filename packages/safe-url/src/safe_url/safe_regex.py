"""Bounded-execution regex for attacker-influenced patterns and inputs.

The implementation lives once, here, in safe_url; core imports it from
:mod:`safe_url.safe_regex` (core → safe-url).
"""

from __future__ import annotations

import regex

type BoundedPattern = regex.Pattern[str]
DEFAULT_MATCH_TIMEOUT_SECONDS = 0.5


class RegexTimeout(TimeoutError):
    """A bounded match exceeded its wall-clock budget (probable ReDoS)."""


class UnsafePattern(ValueError):
    """A pattern was rejected structurally (empty, over-long, unparseable)."""


MAX_PATTERN_LENGTH = 256


def compile_untrusted(
    pattern: str | None,
    *,
    max_len: int = MAX_PATTERN_LENGTH,
) -> BoundedPattern | None:
    """Compile ``pattern`` with the bounded ``regex`` engine, rejecting only
    structurally unusable input -- it deliberately does not guess which shapes are
    "pathological." Returns None for None input; raises ``UnsafePattern`` otherwise.
    """
    if pattern is None:
        return None
    if not pattern or len(pattern) > max_len:
        raise UnsafePattern(f"pattern rejected: length {len(pattern)} > {max_len}")
    try:
        return regex.compile(pattern)
    except regex.error as error:
        raise UnsafePattern(f"pattern rejected: {error}") from error


def match_bounded(
    compiled,
    text: str,
    *,
    op: str = "search",
    timeout: float = DEFAULT_MATCH_TIMEOUT_SECONDS,
):
    """Bounded match or raise ``RegexTimeout`` instead of hanging. Every consumer
    treats the timeout as a fail-closed no-match (exclusion / disallowed / empty),
    so no unbounded hang escapes.
    """
    if compiled is None:
        return None
    fn = getattr(compiled, op)
    try:
        return fn(text, timeout=timeout)
    except TimeoutError as error:
        raise RegexTimeout(str(error)) from error


__all__ = [
    "DEFAULT_MATCH_TIMEOUT_SECONDS",
    "MAX_PATTERN_LENGTH",
    "BoundedPattern",
    "RegexTimeout",
    "UnsafePattern",
    "compile_untrusted",
    "match_bounded",
]
