"""Inbound caller keys: the persisted secrets callers crawl as.

The secret is shown once at issuance and never stored; verification hashes what
the caller presents and compares digests only, one primary-key lookup."""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from pathlib import Path

from crawlutron.models.caller_key import CallerKey
from crawlutron.models.issued_key import IssuedKey
from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations.manifest import StoreSchema

#: The audit write is best-effort: a failing trail must not take the key door
#: down or orphan a fresh secret (fail-open, per the auth posture).
_logger = logging.getLogger(__name__)

_KEY_ID_BYTES = 8
_SECRET_BODY_BYTES = 32
_SALT_BYTES = 16

# PBKDF2-SHA256 at 210k: the documented accelerated-hardware floor keeps lookup
# sub-20ms at the request door; measure before raising.
PBKDF2_ITERATIONS = 210_000

#: Grace window in seconds: a rotated-away key still verifies against the
#: previous slot while now - previous_rotated_at <= GRACE_WINDOW_SECONDS.
GRACE_WINDOW_SECONDS = 300.0

#: PBKDF2 derivations per verify_key call, hit or miss: one for current, plus
#: one more for previous inside the grace window (at most two total, locked).
pbkdf2_calls = 0
_pbkdf2_calls_lock = threading.Lock()


def _hash_secret(secret: str, salt: bytes) -> str:
    global pbkdf2_calls
    with _pbkdf2_calls_lock:
        pbkdf2_calls += 1
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def _store_hash(secret: str) -> str:
    """``salt$hash`` for a fresh key: the salt rides the column so verification
    needs no schema change, and the UNIQUE index keeps holding one row per key."""
    salt = secrets.token_bytes(_SALT_BYTES)
    return f"{salt.hex()}${_hash_secret(secret, salt)}"


def _record_from_row(row: tuple) -> CallerKey:
    """One key record from its selected columns, in select order."""
    (
        key_id,
        label,
        key_hash,
        max_pages,
        max_concurrency,
        created_at,
        previous_hash,
        previous_rotated_at,
    ) = row
    return CallerKey(
        key_id=key_id,
        label=label,
        key_hash=key_hash,
        max_pages=max_pages,
        max_concurrency=max_concurrency,
        created_at=created_at,
        previous_hash=previous_hash or "",
        previous_rotated_at=previous_rotated_at or 0.0,
    )


_DUMMY_SALT = b"crawlutron-dummy-salt"
_DUMMY_VERIFIER = f"{_DUMMY_SALT.hex()}${_hash_secret('', _DUMMY_SALT)}"


def _secret_id_and_body(secret: str) -> tuple[str, str] | None:
    """Split a ``ck_<id-hex>.<body>`` secret into (key_id, body), or None."""
    dot = secret.find(".")
    if dot < 0 or not secret.startswith("ck_") or "." in secret[dot + 1 :]:
        return None
    id_hex = secret[3:dot]
    if len(id_hex) != 2 * _KEY_ID_BYTES:
        return None
    try:
        bytes.fromhex(id_hex)
    except ValueError:
        return None
    body = secret[dot + 1 :]
    # #927: an empty body (``ck_<id>.``) would set a hash-of-empty credential;
    # the secret must carry a non-empty body to be an adversarial choice.
    if not body:
        return None
    return f"key_{id_hex}", body


class CallerKeyStore(CoordinationStore):
    """SQLite-backed store of inbound caller key records keyed by key id.

    With an audit sink attached, every mint, list, and revoke appends one
    structured row -- the single write site per admin action (#697).
    """

    schema = StoreSchema.CALLER_KEYS

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(db_path)
        self._audit = None

    def attach_audit(self, audit) -> None:
        """Wire the audit sink; a store without one stays unrecorded (tests).
        Production wiring always attaches, so an omission is visible at review.
        """
        self._audit = audit

    def _commit_and_audit(self, action: str, key_id: str) -> None:
        """Commit the in-flight write and record one admin audit row."""
        self._conn.commit()
        if self._audit is not None:
            try:
                self._audit.record_event(
                    actor="admin", action=action, target_key_id=key_id, outcome="granted"
                )
            except Exception:
                _logger.warning("admin_audit_write_failed", extra={"action": action, "key_id": key_id})

    def issue_key(self, label: str, *, max_pages: int = 0, max_concurrency: int = 0) -> IssuedKey:
        """Mint a key for ``label``; the secret is returned once, never stored."""
        name = label.strip()
        if not name or len(name) > 128:
            raise ValueError("key label must be 1..128 characters")
        if max_pages < 0 or max_concurrency < 0:
            raise ValueError("key quotas must be zero (unbounded) or positive")
        key_id = f"key_{secrets.token_hex(_KEY_ID_BYTES)}"
        body = secrets.token_urlsafe(_SECRET_BODY_BYTES)
        secret = f"ck_{key_id[len('key_') :]}.{body}"
        # wall: persisted row stamps anchor to wall so they survive restarts.
        created_at = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO caller_keys (key_id, label, key_hash, max_pages, max_concurrency,"
                " created_at, previous_hash, previous_rotated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key_id, name, _store_hash(body), max_pages, max_concurrency, created_at, None, None),
            )
            self._commit_and_audit("key_mint", key_id)
        return IssuedKey(
            key_id=key_id, label=name, secret=secret, max_pages=max_pages, max_concurrency=max_concurrency
        )

    def _reread_row(self, key_id: str | None) -> tuple | None:
        """The current row for ``key_id`` under the lock, for post-derive re-checks."""
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT key_id, label, key_hash, max_pages, max_concurrency,"
                    " created_at, previous_hash, previous_rotated_at"
                    " FROM caller_keys WHERE key_id = ?",
                    (key_id,),
                ).fetchone()
                if key_id is not None
                else None
            )

    def verify_key(self, secret: str) -> CallerKey | None:
        """The record for ``secret``, or None when it names no live key.

        Current first, then ``previous`` inside the grace window: one PBKDF2
        for current, at most one more for previous; a miss burns a fixed dummy.
        """
        parsed = _secret_id_and_body(secret)
        key_id = parsed[0] if parsed is not None else None
        body = parsed[1] if parsed is not None else ""
        row = self._reread_row(key_id)
        # Current first: one PBKDF2; on a miss, ``previous`` inside the grace
        # window gets one more -- at most two derivations, body-independent timing.
        stored = row[2] if row is not None else _DUMMY_VERIFIER
        salt_hex, _, digest_hex = stored.partition("$")
        computed = _hash_secret(body, bytes.fromhex(salt_hex))
        if row is not None:
            # Re-read under the lock: the row may have been revoked or rotated
            # while the derivation ran; the post-derive row decides.
            row = self._reread_row(key_id)
        if row is not None and secrets.compare_digest(computed, digest_hex):
            return _record_from_row(row)
        if row is not None and row[6] and row[7] is not None and time.time() - row[7] <= GRACE_WINDOW_SECONDS:
            salt_hex, _, digest_hex = row[6].partition("$")
            computed = _hash_secret(body, bytes.fromhex(salt_hex))
            if secrets.compare_digest(computed, digest_hex):
                return _record_from_row(row)
        return None

    def list_keys(self) -> list[CallerKey]:
        """Every live key record, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key_id, label, key_hash, max_pages, max_concurrency,"
                " created_at, previous_hash, previous_rotated_at"
                " FROM caller_keys ORDER BY created_at ASC"
            ).fetchall()
        if self._audit is not None:
            self._audit.record_event(actor="admin", action="key_list", target_key_id=None, outcome="granted")
        return [_record_from_row(row) for row in rows]

    def revoke_key(self, key_id: str) -> bool:
        """Drop ``key_id``; True iff a live key went away."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM caller_keys WHERE key_id = ?", (key_id,))
            self._conn.commit()
        if self._audit is not None:
            self._audit.record_event(
                actor="admin",
                action="key_revoke",
                target_key_id=key_id,
                outcome="granted" if cur.rowcount > 0 else "refused",
            )
        return cur.rowcount > 0

    def rotate_key(self, key_id: str, new_secret: str) -> bool:
        """Rotate ``key_id`` to ``new_secret``: current becomes ``previous``
        (stamped with ``previous_rotated_at``) and ``new_secret`` becomes
        current. True iff a live key existed; unknown ids write nothing.
        """
        parsed = _secret_id_and_body(new_secret)
        if parsed is None or parsed[0] != key_id:
            return False
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT key_hash FROM caller_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE caller_keys SET key_hash = ?, previous_hash = ?,"
                " previous_rotated_at = ? WHERE key_id = ?",
                (_store_hash(parsed[1]), row[0], now, key_id),
            )
            self._commit_and_audit("key_rotate", key_id)
        return True

    def get_for_id(self, key_id: str) -> CallerKey | None:
        """The record for ``key_id``, or None when no live key has that id; the
        watch sweep uses it to meter an owner's check against their concurrency.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT key_id, label, key_hash, max_pages, max_concurrency,"
                " created_at, previous_hash, previous_rotated_at"
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
    arotate_key = atwin(rotate_key)
