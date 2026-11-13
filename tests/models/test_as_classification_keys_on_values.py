"""``as_classification`` keys on member VALUES, not names.

A distinct-value enum (a real type, in the test) proves a valid value is not lost
to the default; the module's ``Classification`` symbol is bound, never mutated.
"""

from enum import StrEnum

from crawlutron.models import taxonomy as taxonomy_mod
from crawlutron.models.taxonomy import Classification, as_classification


class _DistinctValueClassification(StrEnum):
    """A classification-like enum whose values differ from their names."""

    success_in_name = "the_real_success_value"
    failed_in_name = "the_real_failed_value"


def test_a_valid_distinct_value_routes_to_its_member(monkeypatch):
    """The value (not the name) of a member must resolve to that member."""
    monkeypatch.setattr(taxonomy_mod, "Classification", _DistinctValueClassification)
    result = as_classification("the_real_success_value")
    assert result is _DistinctValueClassification.success_in_name


def test_an_unrecognized_string_routes_to_the_default(monkeypatch):
    """A string that is neither a member name nor a value still degrades to default."""
    monkeypatch.setattr(taxonomy_mod, "Classification", _DistinctValueClassification)
    assert as_classification("no_such_classification").value == "unknown_error"


def test_todays_name_equals_value_members_are_unchanged():
    """Every real member still resolves by value -- the no-regression surface."""
    for _member in Classification:
        assert as_classification(_member.value) is _member
