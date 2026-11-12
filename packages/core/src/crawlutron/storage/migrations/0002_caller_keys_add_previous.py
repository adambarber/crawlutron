"""Previous-key slot on caller_keys: previous_hash plus previous_rotated_at.

Verification stays one primary-key lookup; revocation clears both with one DELETE.

"""

from crawlutron.storage.migrations.ddl import add_column_step
from crawlutron.storage.migrations.manifest import CALLER_KEYS_SCHEMA

__depends__: set[str] = set()

steps = [
    *add_column_step(CALLER_KEYS_SCHEMA.name, "previous_hash TEXT"),
    *add_column_step(CALLER_KEYS_SCHEMA.name, "previous_rotated_at REAL"),
]
