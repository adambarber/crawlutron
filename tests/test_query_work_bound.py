"""Query work is bounded at the admission chokepoint: byte and field caps refuse."""

from __future__ import annotations

import time
from urllib.parse import parse_qsl

import pytest

from safe_url.normalized_url import NormalizedUrl, admit_with_reason
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

QUERY_BYTE_CAP = 8192
QUERY_FIELD_CAP = 256


def _admit(query: str) -> NormalizedUrl:
    """Admit one URL whose query is the supplied raw text."""
    return NormalizedUrl(f"http://example.com/p?{query}", policy=UNRESTRICTED_HOST_POLICY)


def _reason(query: str) -> str | None:
    """The wire refusal reason for one raw query, via the public wrapper."""
    admitted, reason = admit_with_reason(f"http://example.com/p?{query}", policy=UNRESTRICTED_HOST_POLICY)
    assert admitted is None
    return reason


@pytest.mark.parametrize(
    ("length", "admitted"),
    [
        (QUERY_BYTE_CAP - 1, True),
        (QUERY_BYTE_CAP, True),
        (QUERY_BYTE_CAP + 1, False),
    ],
    ids=["cap-minus-one", "at-cap", "cap-plus-one"],
)
def test_query_byte_cap_boundary(length: int, admitted: bool) -> None:
    """Kills mutant: no byte cap (the unbounded parse_qsl call at the chokepoint)."""
    query = "a=" + "x" * (length - 2)
    assert len(query) == length
    if admitted:
        assert _admit(query).query == query
    else:
        assert _reason(query) == "invalid_url"


@pytest.mark.parametrize(
    ("count", "admitted"),
    [
        (QUERY_FIELD_CAP, True),
        (QUERY_FIELD_CAP + 1, False),
    ],
    ids=["at-field-cap", "field-cap-plus-one"],
)
def test_query_field_count_boundary(count: int, admitted: bool) -> None:
    """Kills mutant: parse_qsl called without max_num_fields."""
    query = "&".join(f"k{i}=" for i in range(count))
    if admitted:
        assert len(parse_qsl(_admit(query).query, keep_blank_values=True)) == count
    else:
        assert _reason(query) == "invalid_url"


def test_tracking_param_flood_is_refused_before_filtering() -> None:
    """Kills mutant: field cap applied after the tracking-param drop (empty output admits)."""
    query = "&".join(["gclid=abc"] * (QUERY_FIELD_CAP + 1))
    assert _reason(query) == "invalid_url"


def test_raw_byte_cap_precedes_canonicalization() -> None:
    """Kills mutant: byte cap measured on the canonical (empty) query instead of the raw one."""
    query = "&".join(["gclid=" + "z" * 40] * 300)
    assert len(query) > QUERY_BYTE_CAP
    assert _reason(query) == "invalid_url"


def test_encoded_ampersand_is_not_a_field_separator_bypass() -> None:
    """Kills mutant: counting decoded separators treats %26 as a field boundary."""
    query = "%26".join(["a"] * 400)
    assert len(query) < QUERY_BYTE_CAP
    assert len(parse_qsl(_admit(query).query, keep_blank_values=True)) == 1


def test_a_single_oversized_field_value_is_refused() -> None:
    """Kills mutant: a byte cap applied per field instead of to the whole query."""
    assert _reason("a=" + "x" * QUERY_BYTE_CAP) == "invalid_url"


def test_blank_fields_count_toward_the_field_cap() -> None:
    """Kills mutant: blank values filtered before the field count (keep_blank_values)."""
    assert _reason("&".join(["a="] * (QUERY_FIELD_CAP + 1))) == "invalid_url"


def test_hostile_query_work_is_bounded() -> None:
    """Kills mutant: the unbounded 400 KB / 100k-field parse on the event loop."""
    query = "&".join(["a=1"] * 100_000)
    assert len(query) > QUERY_BYTE_CAP
    started = time.monotonic()
    assert _reason(query) == "invalid_url"
    assert time.monotonic() - started < 1.0
