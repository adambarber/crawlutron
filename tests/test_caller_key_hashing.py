"""S3 (#600): the PBKDF2 floor pin for caller-key hashing."""

from __future__ import annotations

import pytest

from crawlutron.storage import caller_key_store


@pytest.mark.parametrize("iteration_constant", [210_000], ids=["pbkdf2-iterations"])
def test_pbkdf2_iteration_count_is_the_documented_floor(iteration_constant):
    assert caller_key_store.PBKDF2_ITERATIONS == iteration_constant
