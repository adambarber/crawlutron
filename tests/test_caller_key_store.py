"""Inbound caller keys: issuance, verification, listing, and revocation."""

from __future__ import annotations

import sqlite3

from crawlutron.storage.caller_key_store import CallerKeyStore


def test_issued_key_verifies_to_its_record(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")

    issued = store.issue_key("agent-a")

    record = store.verify_key(issued.secret)
    assert record is not None
    assert record.key_id == issued.key_id
    assert record.label == "agent-a"
    assert record.key_hash != issued.secret


def test_unknown_secret_does_not_verify(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    store.issue_key("agent-a")

    assert store.verify_key("ck_no_such_key") is None


def test_revoked_key_does_not_verify(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")

    assert store.revoke_key(issued.key_id) is True

    assert store.verify_key(issued.secret) is None


def test_revoking_an_unknown_key_reports_false(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")

    assert store.revoke_key("key_no_such_key") is False


def test_list_keys_reports_every_live_key_without_secrets(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    first = store.issue_key("agent-a")
    second = store.issue_key("agent-b", max_pages=10)
    store.revoke_key(first.key_id)

    listed = store.list_keys()

    assert [record.key_id for record in listed] == [second.key_id]
    assert listed[0].max_pages == 10


def test_blank_label_and_negative_quota_are_refused(tmp_path):
    import pytest

    store = CallerKeyStore(tmp_path / "keys.db")

    with pytest.raises(ValueError):
        store.issue_key("   ")
    with pytest.raises(ValueError):
        store.issue_key("agent-a", max_pages=-1)
    with pytest.raises(ValueError):
        store.issue_key("agent-a", max_concurrency=-2)


async def test_async_twins_round_trip_off_the_loop(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")

    issued = await store.aissue_key("agent-a")

    assert await store.averify_key(issued.secret) is not None
    assert [record.key_id for record in await store.alist_keys()] == [issued.key_id]
    assert await store.arevoke_key(issued.key_id) is True
    assert await store.averify_key(issued.secret) is None


def test_two_mints_carry_different_hashes(tmp_path):
    """Per-key salt: two issued keys never share a stored hash."""
    store = CallerKeyStore(tmp_path / "keys.db")
    first = store.issue_key("agent-a")
    second = store.issue_key("agent-b")
    first_record = store.get_for_id(first.key_id)
    second_record = store.get_for_id(second.key_id)
    assert first_record is not None and second_record is not None
    assert first_record.key_hash != second_record.key_hash


def test_a_tampered_hash_does_not_verify(tmp_path):
    """A row hash mutated through the wire turns verification to None."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    record = store.get_for_id(issued.key_id)
    assert record is not None
    salt, digest = record.key_hash.split("$", 1)
    flipped = f"{salt}{'0' if digest[0] != '0' else '1'}{digest[1:]}"
    with sqlite3.connect(tmp_path / "keys.db") as conn:
        conn.execute("UPDATE caller_keys SET key_hash = ?", (flipped,))
        conn.commit()
    assert store.verify_key(issued.secret) is None


def test_issued_keys_carry_distinct_secrets(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")

    first = store.issue_key("agent-a")
    second = store.issue_key("agent-a")

    assert first.key_id != second.key_id
    assert first.secret != second.secret
    assert store.verify_key(first.secret) is not None
    assert store.verify_key(second.secret) is not None


def test_verification_embeds_the_key_id_in_the_secret(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")

    issued = store.issue_key("agent-a")

    assert issued.secret.startswith("ck_")
    assert issued.key_id[len("key_") :] in issued.secret


def test_verification_cost_is_constant_in_key_count(tmp_path):
    from crawlutron.storage import caller_key_store

    store = CallerKeyStore(tmp_path / "keys.db")
    for index in range(5):
        store.issue_key(f"agent-{index}")

    before = caller_key_store.pbkdf2_calls
    store.verify_key("ck_0000000000000000.not-a-real-secret")

    assert caller_key_store.pbkdf2_calls - before == 1


def test_unknown_key_id_miss_costs_one_pbkdf2(tmp_path):
    from crawlutron.storage import caller_key_store

    store = CallerKeyStore(tmp_path / "keys.db")
    store.issue_key("agent-a")

    before = caller_key_store.pbkdf2_calls
    assert store.verify_key("ck_deadbeefdeadbeef.absent-secret") is None

    assert caller_key_store.pbkdf2_calls - before == 1


def test_a_secret_without_an_embedded_key_id_is_refused(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    store.issue_key("agent-a")

    assert store.verify_key("ck_" + "a" * 43) is None


def test_a_wrong_secret_for_a_real_key_id_verifies_to_none(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")

    assert store.verify_key(f"ck_{issued.key_id[4:]}.wrong-body") is None


def test_the_secret_format_has_one_parser(tmp_path):
    """#666: the ck_<id>.<body> format has ONE parser; verify_key's body
    extraction and the id extraction cannot drift into two spellings.

    Red note: the deleted second spelling (verify_key's raw split) was found
    behaviorally EQUIVALENT on the live tree -- the id parser refuses any
    secret whose body carries a dot, so both spellings always agreed. The
    defect was drift exposure, not live divergence; the mutation proof is a
    wrong-body-extraction mutant in _parse_secret, which reds the round-trip
    clause below (only one body spelling can satisfy it).
    """
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")

    # The parser and the store agree on every well-formed secret: the round
    # trip through issue -> verify lands on the live row.
    assert store.verify_key(issued.secret) is not None

    # A two-dot secret (a dot inside the body) is refused whole -- not split
    # at the first dot with the remainder verified as a body.
    assert store.verify_key(f"{issued.secret}.extra") is None
