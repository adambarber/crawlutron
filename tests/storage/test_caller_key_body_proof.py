"""Body-proof gating for caller keys (#1124): a live key id proves only when the
presented body matches the cached verification proof, never on presence alone.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.storage.caller_key_store import CallerKeyStore


async def _averify(store: CallerKeyStore, secret: str) -> bool:
    return await store.akey_body_proven(secret)


def _same_id_wrong_body(secret: str) -> str:
    return secret.split(".", 1)[0] + ".tampered-body"


def _assert_proven(store: CallerKeyStore, secret: str) -> None:
    """Verify ``secret`` and assert its proof is cached (it proves)."""
    store.verify_key(secret)
    assert store.key_body_proven(secret) is True


def test_proof_follows_body_not_id(tmp_path):
    """Right body proves True; the same live id with a wrong body proves False
    (kills the presence-only mutant)."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    store.verify_key(issued.secret)
    wrong = _same_id_wrong_body(issued.secret)
    assert store.key_body_proven(issued.secret) is True
    assert store.key_body_proven(wrong) is False
    assert asyncio.run(_averify(store, issued.secret)) is True
    assert asyncio.run(_averify(store, wrong)) is False


def test_rotation_invalidates_old_body_proof(tmp_path):
    """Rotating a key clears its cached proof: the old body no longer proves,
    and the rotated body must be re-verified before it proves."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    _assert_proven(store, issued.secret)
    new_secret = "ck_" + issued.key_id[len("key_") :] + ".rotated-body-xyz"
    assert store.rotate_key(issued.key_id, new_secret) is True
    assert store.key_body_proven(issued.secret) is False
    assert store.key_body_proven(new_secret) is False
    assert store.verify_key(new_secret) is not None
    assert store.key_body_proven(new_secret) is True


def test_revocation_refuses_late_proof(tmp_path):
    """Revoking a key makes even a cached proof refuse immediately; rotate and
    revoke both discard it, so the belt-and-suspenders pair leaves the other."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    _assert_proven(store, issued.secret)
    assert store.revoke_key(issued.key_id) is True
    assert store.key_body_proven(issued.secret) is False


def test_bounded_cache_evicts_oldest_proof(tmp_path):
    """The proof cache is bounded: verifying cap+1 distinct keys evicts the
    oldest proof, so the oldest no longer proves while the newest still does."""
    store = CallerKeyStore(tmp_path / "keys.db")
    cap = 256  # _PROVEN_BODY_CACHE_MAX (private; not importable from tests)
    issued = [store.issue_key(f"agent-{i}") for i in range(cap + 1)]
    for row in issued:
        store.verify_key(row.secret)
        assert store.key_body_proven(row.secret) is True
    assert store.key_body_proven(issued[0].secret) is False
    assert store.key_body_proven(issued[cap].secret) is True


@pytest.mark.parametrize(
    "secret",
    [
        "ckx_0000000000000000000000000000000000000000000000000000000000000000.anybody",
        "ck_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff.anybody",
    ],
)
def test_hostile_ids_never_prove(tmp_path, secret):
    """A ``ckx_`` prefix or an unknown id never proves."""
    store = CallerKeyStore(tmp_path / "keys.db")
    assert store.key_body_proven(secret) is False


def test_non_ascii_and_oversized_bodies_never_prove(tmp_path):
    """Non-ASCII and 100 KiB bodies never prove (and never raise)."""
    store = CallerKeyStore(tmp_path / "keys.db")
    issued = store.issue_key("agent-a")
    prefix = issued.secret.split(".", 1)[0]
    assert store.key_body_proven(prefix + ".caf\xff") is False
    assert store.key_body_proven(prefix + "." + "x" * (100 * 1024)) is False
