"""#313: the read edge's max_pages default is invariant over the whole input.

Absent/exploded/corrupt values resolve to DEFAULT_MAX_PAGES; explicit ``0`` is
the only path to the model's unbounded ``0``.
"""

from __future__ import annotations

import pytest

from crawlutron.admission.policy import DEFAULT_MAX_PAGES, read_policy

#: Every way ``max_pages`` can fail to be a usable explicit value. Each must
#: resolve to the documented default -- never to the model's silent ``0``.
_ABSENT_OR_INVALID = [
    (None, "nothing supplied"),
    ({}, "empty policy"),
    ({"limits": {}}, "absent inside limits"),
    ({"limits": {"max_pages": 99999}}, "over the model ceiling (exploded)"),
    ({"limits": {"max_pages": -1}}, "negative"),
    ({"limits": 5}, "limits not a mapping"),
    ({"limits": {"max_pages": "many"}}, "max_pages not an int"),
    ({"limits": {"max_pages": None}}, "max_pages null"),
]


@pytest.mark.parametrize(("raw", "_label"), _ABSENT_OR_INVALID)
def test_absent_or_invalid_max_pages_resolves_to_the_default(raw, _label):
    resolved = read_policy(raw)

    assert resolved.limits.max_pages == DEFAULT_MAX_PAGES


def test_read_policy_none_and_empty_policy_agree():
    """``read_policy(None)`` is the same 'nothing supplied' as ``read_policy({})``."""
    assert read_policy(None) == read_policy({})
    assert read_policy(None).limits.max_pages == DEFAULT_MAX_PAGES


def test_an_explicit_zero_stays_unbounded_on_the_read_edge():
    """The one path to ``0`` is an explicitly-supplied, model-legal zero."""
    assert read_policy({"limits": {"max_pages": 0}}).limits.max_pages == 0


def test_a_valid_explicit_max_pages_is_preserved():
    assert read_policy({"limits": {"max_pages": 500}}).limits.max_pages == 500
