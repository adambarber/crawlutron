"""One compiled-regex prefilter replaces the per-char walk in _reject_control_chars:
identical verdicts over the fuzz corpus, same refusals kept."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
import pytest

from safe_url.host_canonical import idna_host
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import _CONTROL_CHAR_RE, NormalizedUrl, _reject_control_chars

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


@pytest.mark.parametrize("separator", ["\t", "\n", "\r"])
def test_raw_control_in_scheme_separator_is_refused(separator: str) -> None:
    """A control in the scheme separator hides the raw authority from the prefilter."""
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(f"http{separator}://example.com/", policy=UNRESTRICTED)


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x7f"])
def test_leading_control_is_refused(control: str) -> None:
    """Raw controls are refused on the whole URL, before a scheme is located."""
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(f"{control}http://example.com/", policy=UNRESTRICTED)


def test_tab_scheme_with_space_authority_is_refused() -> None:
    """The verdict host must never differ from the transport host."""
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl("http\t://exam ple.com/", policy=UNRESTRICTED)


def test_space_in_authority_is_refused() -> None:
    """The authority-whitespace rule survives the whole-URL prefilter."""
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl("http://exam ple.com/", policy=UNRESTRICTED)


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com/a",
        "http://example.com:8080/a?b=c",
        "http://bücher.example/",
        "http://[2606:4700::1111]/",
    ],
)
def test_admitted_host_matches_transport_host(raw: str) -> None:
    """Where the transport host is stable, it names exactly the admitted host."""
    url = NormalizedUrl(raw, policy=UNRESTRICTED)
    assert idna_host(httpx.URL(url.raw).host) == url.host


#: Legitimate identities and the hostile shapes that broke them in the past.
_CANDIDATES = [
    "http://example.com/",
    "https://example.com/a/b?x=1",
    "http://example.com:8080/x",
    "http://bücher.example/",
    "http://[2606:4700::1111]/",
    "http://EXAMPLE.com./",
    "http://example.com..",
    "http://example.com/a%00b%7F",
    "http://example.com/?q=a%20b",
    "http\t://exam\x00ple.com/",
    "http\n://exam\x00ple.com/",
    "http\r://example.com/",
    "\x00http://example.com/",
    "http\t://exam ple.com/",
    "http://exam ple.com/",
    "http://./",
    "http://../",
    "http://.../",
    "http://.example.com",
    "http://127.0.0.1/",
    "not a url",
]


def _assert_admission_contract(url: NormalizedUrl) -> None:
    """An admitted answer is transport-constructible, re-admittable, and clean."""
    assert idna_host(httpx.URL(url.raw).host) == url.host
    again = NormalizedUrl(url.raw, policy=url.policy)
    assert again.raw == url.raw
    assert again.host == url.host
    authority = urlparse(url.raw).netloc
    assert _CONTROL_CHAR_RE.search(authority) is None
    assert not any(ch.isspace() for ch in authority)


def test_every_admitted_answer_satisfies_the_identity_contract() -> None:
    """The oracle for the whole class: no admitted answer may drift from the wire."""
    for candidate in _CANDIDATES:
        try:
            url = NormalizedUrl(candidate, policy=UNRESTRICTED)
        except ValueError:
            continue
        _assert_admission_contract(url)
