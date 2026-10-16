"""Inbound caller keys: issuance, verification, listing, and revocation."""

from __future__ import annotations

from app.storage.caller_key_store import CallerKeyStore


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


def test_issued_keys_carry_distinct_secrets(tmp_path):
    store = CallerKeyStore(tmp_path / "keys.db")

    first = store.issue_key("agent-a")
    second = store.issue_key("agent-a")

    assert first.key_id != second.key_id
    assert first.secret != second.secret
    assert store.verify_key(first.secret) is not None
    assert store.verify_key(second.secret) is not None
