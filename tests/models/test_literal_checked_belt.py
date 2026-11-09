"""The LiteralChecked construction belt: forgetting super() cannot drop the check."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from crawlutron.models.literal_checked import LiteralChecked
from crawlutron.models.taxonomy import Classification
from crawlutron.vocabulary.taxonomy import ResultStatus


@dataclass(frozen=True)
class _ForgetfulResult(LiteralChecked):
    """A subclass whose own __post_init__ does NOT call super().__post_init__()."""

    status: ResultStatus
    classification: Classification

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", self.status)  # subclass work, no super()


def test_a_subclass_that_forgets_super_still_rejects_a_non_member():
    """Forgetting ``super().__post_init__()`` cannot smuggle an out-of-taxonomy
    value: the base wraps the subclass's ``__post_init__`` at class creation,
    so the taxonomy check runs after the subclass's own work either way.
    """
    # The hostile value arrives at runtime through an untyped boundary (a wire
    # body, an env var); reproduce that here with a cast-free untyped Any door.
    hostile: Any = "not-a-status"
    with pytest.raises(ValueError, match="not a member"):
        _ForgetfulResult(status=hostile, classification="rendered")


def test_a_subclass_that_forgets_super_still_rejects_a_disagreeing_pair():
    with pytest.raises(ValueError, match="disagrees"):
        _ForgetfulResult(status="success", classification="network_error")


def test_valid_values_still_construct_through_the_belt():
    record = _ForgetfulResult(status="success", classification="rendered")
    assert record.status == "success"
