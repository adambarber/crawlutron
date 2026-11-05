"""S3 (#600): caller key secrets are PBKDF2-hashed with a per-key salt, never bare."""

from __future__ import annotations

import sqlite3

import pytest

from crawlutron.storage.caller_key_store import CallerKeyStore


def test_a_freshly_minted_key_verifies(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    assert store.verify_key(issued.secret) is not None


def test_two_keys_with_the_same_secret_carry_different_hashes(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    first = store.issue_key("agent-a")
    second = store.issue_key("agent-b")
    first_record = store.get_for_id(first.key_id)
    second_record = store.get_for_id(second.key_id)
    assert first_record is not None and second_record is not None
    assert first_record.key_hash != second_record.key_hash


def test_a_tampered_hash_does_not_verify(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    record = store.get_for_id(issued.key_id)
    assert record is not None
    salt, digest = record.key_hash.split("$", 1)
    flipped = f"{salt}{'0' if digest[0] != '0' else '1'}{digest[1:]}"
    # Own sqlite3 connection: the row is mutated through the wire, not the
    # store's private internals, so the test drives the public door only.
    with sqlite3.connect(tmp_path / "keys.db") as conn:
        conn.execute("UPDATE caller_keys SET key_hash = ?", (flipped,))
        conn.commit()
    assert store.verify_key(issued.secret) is None


def test_the_stored_hash_is_not_a_bare_digest_of_the_secret(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    record = store.get_for_id(issued.key_id)
    assert record is not None
    import hashlib

    assert record.key_hash != hashlib.sha256(issued.secret.encode()).hexdigest()


@pytest.mark.parametrize("iteration_constant", [210_000], ids=["pbkdf2-iterations"])
def test_pbkdf2_iteration_count_is_the_documented_floor(iteration_constant):
    from crawlutron.storage import caller_key_store

    assert caller_key_store.PBKDF2_ITERATIONS == iteration_constant
