"""Bounded-execution regex for attacker-influenced patterns and inputs.

N-1 root cause: static shape-rejection of "pathological" regexes is
bypassable by construction — the depth-3 nested-quantifier bypass
``(((a|aa)))*`` defeated every guard heuristics could enumerate (measured:
4.5s per match at 36 chars, doubling per character). Static rejection is
effectively undecidable, so the durable defense is to BOUND EXECUTION:
the third-party ``regex`` engine implements ``timeout=`` inside its C
matching loop, which is interruptible even under the fast_sleeps test
harness (unlike stdlib re, whose C loop holds the GIL — the same wedge
class as curl_cffi under fast_sleeps, #1746).
"""

from __future__ import annotations

import re

import regex as _engine

DEFAULT_MATCH_TIMEOUT_SECONDS = 0.5


class RegexTimeout(TimeoutError):
    """A bounded match exceeded its wall-clock budget (probable ReDoS)."""


class UnsafePattern(ValueError):
    """A pattern was rejected structurally (empty, over-long, unparseable)."""


MAX_PATTERN_LENGTH = 256


def compile_untrusted(pattern: str | None, *, max_len: int = MAX_PATTERN_LENGTH):
    """Compile ``pattern`` with the bounded ``regex`` engine.

    Rejects only structurally unusable input — empty, over-long, or
    unparseable. Deliberately does NOT try to guess which shapes are
    "pathological": safety comes from bounded matching, not bounded
    compilation. Returns None for None input; raises UnsafePattern for
    rejected input.
    """
    if pattern is None:
        return None
    if not pattern or len(pattern) > max_len:
        raise UnsafePattern(f"pattern rejected: length {len(pattern)} > {max_len}")
    try:
        return _engine.compile(pattern)
    except _engine.error as error:
        raise UnsafePattern(f"pattern rejected: {error}") from error


def match_bounded(
    compiled,
    text: str,
    *,
    op: str = "search",
    timeout: float = DEFAULT_MATCH_TIMEOUT_SECONDS,
):
    """Bounded match; raises RegexTimeout instead of hanging.

    Fail-closed direction (G8): crawl scope treats ``RegexTimeout`` as
    ``no-match`` (fail-closed exclusion); robots matcher treats it as
    disallowed; document extractor returns empty. No unbounded hang escapes.
    """
    if compiled is None:
        return None
    fn = getattr(compiled, op)
    try:
        return fn(text, timeout=timeout)
    except TimeoutError as error:
        raise RegexTimeout(str(error)) from error


def search_bounded(compiled, text: str, *, timeout: float = DEFAULT_MATCH_TIMEOUT_SECONDS):
    return match_bounded(compiled, text, op="search", timeout=timeout)


# Re-exported so callers can type-annotate without importing regex directly.
Pattern = _engine.Pattern
RegexError = re.error
