"""Raw C0 controls and DEL are refused in every wire segment of an admitted URL."""

import pytest
from safe_url.normalized_url import NormalizedUrl

from tests._policy_factories import UNRESTRICTED_HOST_POLICY

# The wire-framing class: every C0 control and DEL, spread across boundary ends.
_CONTROL = "\x00\x01\x08\x0a\x0e\x1f\x7f"


def _raw_with(segment: str, char: str) -> str:
    """A URL carrying ``char`` in the named wire segment."""
    if segment == "path":
        return f"http://example.com/a{char}b"
    if segment == "query":
        return f"http://example.com/p?x={char}y"
    if segment == "params":
        return f"http://example.com/p;{char}y?x=1"
    raise AssertionError(f"unknown segment {segment!r}")


@pytest.mark.parametrize("segment", ["path", "query", "params"])
@pytest.mark.parametrize("char", list(_CONTROL))
def test_a_raw_control_char_in_segment_is_refused(segment: str, char: str) -> None:
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(_raw_with(segment, char), policy=UNRESTRICTED_HOST_POLICY)


@pytest.mark.parametrize("char", list(_CONTROL))
def test_a_raw_control_char_in_the_authority_is_refused(char: str) -> None:
    with pytest.raises(ValueError, match="invalid_url"):
        NormalizedUrl(f"http://exa{char}mple.com/", policy=UNRESTRICTED_HOST_POLICY)


def test_a_percent_encoded_control_stays_admitted_in_path() -> None:
    url = NormalizedUrl("http://example.com/a%0Ab", policy=UNRESTRICTED_HOST_POLICY)
    assert url.raw == "http://example.com/a%0Ab"


def test_a_percent_encoded_control_stays_admitted_in_query() -> None:
    url = NormalizedUrl("http://example.com/p?x=%0A", policy=UNRESTRICTED_HOST_POLICY)
    assert url.raw == "http://example.com/p?x=%0A"
