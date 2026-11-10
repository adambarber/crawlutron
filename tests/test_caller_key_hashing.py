"""S3 (#600): the PBKDF2 floor pin for caller-key hashing."""

from __future__ import annotations

import pytest

from tests.conftest import real_pbkdf2_iterations


def test_pbkdf2_iteration_count_is_the_documented_floor() -> None:
    """The unclamped PBKDF2 work factor is the documented 210k floor:
    production drift to 1_000 reds this pin."""
    assert real_pbkdf2_iterations() == 210_000


def test_pbkdf2_is_clamped_to_the_test_floor() -> None:
    """The autouse clamp fixture holds caller_key_store.PBKDF2_ITERATIONS at
    1_000 in the default battery, so no keyed-key test pays the real 210k
    derivation; the real path is covered by a real_pbkdf2-marked test."""
    from crawlutron.storage import caller_key_store

    assert caller_key_store.PBKDF2_ITERATIONS == 1_000


@pytest.mark.real_pbkdf2
def test_real_pbkdf2_path_verifies_issued_secret(tmp_path):
    """The unclamped 210k work factor still round-trips: issue, then verify."""
    from crawlutron.storage.caller_key_store import CallerKeyStore

    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("real-path")
    assert store.verify_key(issued.secret) is not None
    assert store.verify_key(issued.secret + "x") is None
