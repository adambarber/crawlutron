"""The PBKDF2 call counter stays exact under concurrent verifications (#675)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from crawlutron.storage import caller_key_store
from crawlutron.storage.caller_key_store import CallerKeyStore


def test_concurrent_verifies_count_every_pbkdf2_call(tmp_path):
    """verify_key runs on the store executor's worker threads (atwin); the
    counter must not lose increments under concurrency."""
    store = CallerKeyStore(tmp_path / "keys.db")
    key = store.issue_key("agent-a")

    def hammer(secret: str) -> None:
        for _ in range(20):
            store.verify_key(secret)

    before = caller_key_store.pbkdf2_calls
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(hammer, [key.secret] * 8))
    counted = caller_key_store.pbkdf2_calls - before
    assert counted == 160, f"lost {160 - counted} increments"
