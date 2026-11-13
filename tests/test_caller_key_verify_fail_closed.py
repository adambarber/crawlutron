"""verify_key must refuse a credential whose stored salt$hash it cannot parse."""

import secrets
import sqlite3
from contextlib import closing

from crawlutron.storage.caller_key_store import CallerKeyStore


def test_a_non_hex_salt_in_the_stored_hash_fails_closed(tmp_path):
    """A corrupted salt half cannot be derived: verify returns None, not ValueError."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-alpha")
    record = store.get_for_id(issued.key_id)
    assert record is not None
    digest = record.key_hash.partition("$")[2]
    with closing(sqlite3.connect(tmp_path / "keys.db")) as conn:
        conn.execute(
            "UPDATE caller_keys SET key_hash = ? WHERE key_id = ?",
            (f"zz-top${digest}", issued.key_id),
        )
        conn.commit()
    assert store.verify_key(issued.secret) is None


def test_a_non_hex_digest_in_the_stored_hash_fails_closed(tmp_path):
    """The digest half is only compared: bad hex there is a plain miss, never a raise."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-beta")
    record = store.get_for_id(issued.key_id)
    assert record is not None
    salt = record.key_hash.partition("$")[0]
    raw = sqlite3.connect(tmp_path / "keys.db")
    try:
        raw.execute("UPDATE caller_keys SET key_hash = ? WHERE key_id = ?", (f"{salt}$nope", issued.key_id))
        raw.commit()
    finally:
        raw.close()
    assert store.verify_key(issued.secret) is None


def test_a_stored_hash_without_a_separator_fails_closed(tmp_path):
    """No ``$`` means no digest half to compare against: fail closed."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-gamma")
    record = store.get_for_id(issued.key_id)
    assert record is not None
    bare = record.key_hash.partition("$")[0]
    conn = sqlite3.connect(tmp_path / "keys.db")
    try:
        conn.execute("UPDATE caller_keys SET key_hash = ? WHERE key_id = ?", (bare, issued.key_id))
        conn.commit()
    finally:
        conn.close()
    assert store.verify_key(issued.secret) is None


def test_an_empty_stored_hash_fails_closed(tmp_path):
    """A blank column has no salt to derive with: the door stays shut."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-delta")
    with closing(sqlite3.connect(tmp_path / "keys.db")) as conn:
        conn.execute("UPDATE caller_keys SET key_hash = '' WHERE key_id = ?", (issued.key_id,))
        conn.commit()
    assert store.verify_key(issued.secret) is None


def test_an_odd_length_salt_hex_fails_closed(tmp_path):
    """``abc`` is an odd number of hex digits: fromhex rejects it, verify refuses."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-epsilon")
    with closing(sqlite3.connect(tmp_path / "keys.db")) as conn:
        row = conn.execute("SELECT key_hash FROM caller_keys WHERE key_id = ?", (issued.key_id,)).fetchone()
        assert row is not None
        digest = row[0].partition("$")[2]
        conn.execute("UPDATE caller_keys SET key_hash = ? WHERE key_id = ?", (f"abc${digest}", issued.key_id))
        conn.commit()
    assert store.verify_key(issued.secret) is None


def test_a_well_formed_row_still_verifies(tmp_path):
    """No regression: a clean salt$hash column round-trips through verify_key."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-zeta")
    record = store.verify_key(issued.secret)
    assert record is not None
    assert record.key_id == issued.key_id


def test_a_corrupted_previous_hash_fails_closed_in_the_grace_window(tmp_path):
    """Rotation parked the old hash in previous_hash; corrupting it refuses the old secret."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-eta")
    new_secret = f"ck_{issued.key_id[4:]}.{secrets.token_urlsafe(32)}"
    assert store.rotate_key(issued.key_id, new_secret) is True
    conn = sqlite3.connect(tmp_path / "keys.db")
    try:
        conn.execute("UPDATE caller_keys SET previous_hash = ? WHERE key_id = ?", ("garbage", issued.key_id))
        conn.commit()
    finally:
        conn.close()
    assert store.verify_key(issued.secret) is None
    assert store.verify_key(new_secret) is not None


def test_a_corrupted_row_still_burns_exactly_one_derivation(tmp_path):
    """Timing stays locked: a malformed stored hash costs one PBKDF2, like any miss."""
    from crawlutron.storage import caller_key_store

    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-theta")
    with closing(sqlite3.connect(tmp_path / "keys.db")) as conn:
        conn.execute("UPDATE caller_keys SET key_hash = ? WHERE key_id = ?", ("zzz-not-hex$", issued.key_id))
        conn.commit()
    before = caller_key_store.pbkdf2_calls
    assert store.verify_key(issued.secret) is None
    assert caller_key_store.pbkdf2_calls - before == 1
