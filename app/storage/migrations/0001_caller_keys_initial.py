"""Inbound caller keys: issuance, verification, listing, and revocation."""

from app.storage.migrations.ddl import table_steps
from app.storage.migrations.manifest import CALLER_KEYS_SCHEMA

__depends__: set[str] = set()

steps = list(table_steps(CALLER_KEYS_SCHEMA))
