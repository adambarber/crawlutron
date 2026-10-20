"""Low-level pieces of the disk content cache: envelope math and file work.

``ContentCache`` composes these; each helper owns one part of the disk
contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from app.models.cache_envelope import CacheEnvelope
from app.safety.atomic_write import atomic_write_text
from app.transport.freshness import is_expired

#: An unparsable stored expiry resolves to before the clock, so every
#: freshness comparison reads it as expired rather than raising.
UNPARSEABLE_EXPIRY = float("-inf")

#: A ``.tmp`` orphan older than this is swept: atomic_write's crash window
#: (mkstemp -> os.replace) leaves it behind, invisible to glob and budget (#241).
TMP_ORPHAN_MAX_AGE_SECONDS = 3600


def resolve_expiry(envelope: CacheEnvelope) -> float | None:
    """The row's expiry as a comparable number, or ``None`` when not one.

    Wrong-typed JSON ``expires`` would raise ``TypeError`` through every
    freshness read; corruption here is a miss."""
    raw = envelope.get("expires")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return UNPARSEABLE_EXPIRY
    return float(raw)


def atomic_write(path: Path, data: str) -> None:
    """Persist a pre-serialized envelope atomically (the default disk writer)."""
    atomic_write_text(path, data)


def scan_cache_dir(
    cache_dir: Path,
    namespace: str,
    expiry_of: Callable[[CacheEnvelope], float | None],
    now: float,
) -> tuple[list[tuple[Path, int]], dict[Path, int]]:
    """Verdicts over every namespace file: ``(expired, surviving)``.

    Runs outside the cache lock: the multi-MB read work."""
    expired: list[tuple[Path, int]] = []
    surviving: dict[Path, int] = {}
    for path in cache_dir.glob(f"{namespace}.*.json"):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            envelope = json.loads(raw)
        except ValueError:
            # Corrupt but present: keep the file (the read path drops it on
            # the next access) and repair its recorded size below.
            surviving[path] = len(raw)
            continue
        expires = expiry_of(envelope)
        if is_expired(now, expires):
            expired.append((path, len(raw)))
        else:
            surviving[path] = len(raw)
    return expired, surviving
