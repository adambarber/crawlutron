"""Inbound caller keys: the persisted secrets callers crawl as.

The secret is shown once at issuance and never stored; verification hashes
what the caller presents (PBKDF2-SHA256, per-key salt) and compares digests
only. Legacy rows written by the old bare-SHA-256 writer no longer match and
are re-mintable by the operator via /v1/keys -- the documented one-way reset.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from crawlutron.models.caller_key import CallerKey
from crawlutron.models.issued_key import IssuedKey
from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations.manifest import StoreSchema

_KEY_ID_BYTES = 8
_SECRET_BYTES = 32

# PBKDF2-SHA256 at 210k: the OWASP 600k floor is the comfortable choice, but keys
# verify at the request door, so the documented 210k step ( OWASP's accelerated
# hardware floor ) keeps lookup sub-20ms on the reference machine. Measure before raising.
PBKDF2_ITERATIONS = 210_000
_SALT_BYTES = 16


def _hash_secret(secret: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def _store_hash(secret: str) -> str:
    """``salt$hash`` for a fresh key: the salt rides the column so verification
    needs no schema change, and the UNIQUE index keeps holding one row per key."""
    salt = secrets.token_bytes(_SALT_BYTES)
    return f"{salt.hex()}${_hash_secret(secret, salt)}"


def _stored_hash_matches(secret: str, stored: str) -> bool:
    salt_hex, _, digest_hex = stored.partition("$")
    if not salt_hex or not digest_hex:
        return False
    return secrets.compare_digest(_hash_secret(secret, bytes.fromhex(salt_hex)), digest_hex)


def _record_from_row(row: tuple[str, str, str, int, int, float]) -> CallerKey:
    """One key record from its selected columns, in select order."""
    key_id, label, key_hash, max_pages, max_concurrency, created_at = row
    return CallerKey(
        key_id=key_id,
        label=label,
        key_hash=key_hash,
        max_pages=max_pages,
        max_concurrency=max_concurrency,
        created_at=created_at,
    )


class CallerKeyStore(CoordinationStore):
    """SQLite-backed store of inbound caller key records keyed by key id."""

    schema = StoreSchema.CALLER_KEYS

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(db_path)

    def issue_key(self, label: str, *, max_pages: int = 0, max_concurrency: int = 0) -> IssuedKey:
        """Mint a key for ``label``; the secret is returned once, never stored."""
        name = label.strip()
        if not name or len(name) > 128:
            raise ValueError("key label must be 1..128 characters")
        if max_pages < 0 or max_concurrency < 0:
            raise ValueError("key quotas must be zero (unbounded) or positive")
        key_id = f"key_{secrets.token_hex(_KEY_ID_BYTES)}"
        secret = f"ck_{secrets.token_urlsafe(_SECRET_BYTES)}"
        # wall: persisted row stamps anchor to wall so they survive restarts.
        created_at = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO caller_keys (key_id, label, key_hash, max_pages, max_concurrency,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, name, _store_hash(secret), max_pages, max_concurrency, created_at),
            )
            self._conn.commit()
        return IssuedKey(
            key_id=key_id, label=name, secret=secret, max_pages=max_pages, max_concurrency=max_concurrency
        )

    def verify_key(self, secret: str) -> CallerKey | None:
        """The record for ``secret``, or None when it names no live key.

        PBKDF2's per-key salt breaks hash-equality lookup, so the door scans the
        (small) live key set and compares each salted digest in constant time.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT key_id, label, key_hash, max_pages, max_concurrency, created_at FROM caller_keys"
            ).fetchall()
        for row in rows:
            if _stored_hash_matches(secret, row[2]):
                return _record_from_row(row)
        return None

    def list_keys(self) -> list[CallerKey]:
        """Every live key record, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key_id, label, key_hash, max_pages, max_concurrency, created_at"
                " FROM caller_keys ORDER BY created_at ASC"
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def revoke_key(self, key_id: str) -> bool:
        """Drop ``key_id``; True iff a live key went away."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM caller_keys WHERE key_id = ?", (key_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def get_for_id(self, key_id: str) -> CallerKey | None:
        """The record for ``key_id``, or None when no live key has that id; the
        watch sweep uses it to meter an owner's check against their concurrency.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT key_id, label, key_hash, max_pages, max_concurrency, created_at"
                " FROM caller_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        return None if row is None else _record_from_row(row)

    def exists_key(self, key_id: str) -> bool:
        """Whether a live key row for ``key_id`` exists, without materializing it.

        A PK lookup: the sweeper uses it to deactivate a watch whose owner key was
        revoked instead of letting a dead owner issue checks forever.
        """
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM caller_keys WHERE key_id = ?", (key_id,)).fetchone()
        return row is not None

    aissue_key = atwin(issue_key)
    averify_key = atwin(verify_key)
    alist_keys = atwin(list_keys)
    arevoke_key = atwin(revoke_key)
    aexists_key = atwin(exists_key)
    aget_for_id = atwin(get_for_id)
