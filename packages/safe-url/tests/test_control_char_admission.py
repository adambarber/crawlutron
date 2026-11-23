"""One compiled-regex prefilter replaces the per-char walk in _reject_control_chars:
identical verdicts over the fuzz corpus, same refusals kept."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl, _reject_control_chars

UNRESTRICTED = HostPolicy.unrestricted()


#: Reference form of the OLD per-char predicate being replaced.
def _old_predicate(text: str) -> None:
    """Per-char walk the prefilter replaces, kept only as the reference verdict."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("invalid_url")


def _verdict(func: Callable[[str], None], text: str) -> tuple[bool, str | None]:
    """Run a predicate; return (accepted, refusal-reason-or-None)."""
    try:
        func(text)
    except ValueError as error:
        return False, str(error)
    return True, None


def _fuzz_corpus() -> list[str]:
    """Every byte 0x00-0xFF as a single char, plus multi-char mixes and real URLs."""
    samples = [chr(code) for code in range(0x100)]
    samples.extend(
        [
            "\x00\x01\x7f",
            "a\x1fb",
            "http://example.com/" + chr(0x1F),
            "http://exa" + chr(0x7F) + "mple.com/",
            "http://example.com/a%00b",
            "http://example.com/a%7Fb",
            "http://bücher.example/",
            "http://[::1]/path?q=1",
        ]
    )
    return samples


def test_prefilter_is_a_compiled_regex():
    """Admission scans with one compiled regex (one C pass), not per-char Python."""
    from safe_url.normalized_url import _CONTROL_CHAR_RE

    assert isinstance(_CONTROL_CHAR_RE, re.Pattern)
    assert _CONTROL_CHAR_RE.pattern == r"[\x00-\x1f\x7f]"


def test_prefilter_verdicts_match_old_predicate_over_fuzz_corpus():
    """New prefilter gives identical accept/reject verdicts to the old walk for
    every corpus sample, and every refusal keeps the ``invalid_url`` reason."""
    for sample in _fuzz_corpus():
        accepted_old, reason_old = _verdict(_old_predicate, sample)
        accepted_new, reason_new = _verdict(_reject_control_chars, sample)
        assert accepted_old == accepted_new, f"verdict differs for {sample!r}"
        if not accepted_new:
            assert reason_new == "invalid_url", f"wrong reason for {sample!r}"


@pytest.mark.parametrize("char", ["\x00", "\x01", "\x1f", "\x7f"])
def test_raw_control_in_authority_is_refused(char: str) -> None:
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(f"http://exa{char}mple.com/", policy=UNRESTRICTED)


@pytest.mark.parametrize("char", ["\x00", "\x01", "\x1f", "\x7f"])
def test_raw_control_in_tail_is_refused(char: str) -> None:
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(f"http://example.com/a{char}b", policy=UNRESTRICTED)


def test_percent_escaped_control_stays_admitted() -> None:
    """A %-escaped control (%00 / %7F) is literal text and passes admission."""
    url = NormalizedUrl("http://example.com/a%00b%7F", policy=UNRESTRICTED)
    assert url.raw == "http://example.com/a%00b%7F"
