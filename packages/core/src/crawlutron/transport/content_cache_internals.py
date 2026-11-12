"""Low-level pieces of the disk content cache: envelope math and file work.

``ContentCache`` composes these; each helper owns one part of the disk
contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from crawlutron.models.cache_envelope import CacheEnvelope
from crawlutron.models.json_value import JsonValue
from crawlutron.safety.atomic_write import atomic_write_text
from crawlutron.transport.freshness import is_expired

#: An unparsable stored expiry resolves to before the clock, so every
#: freshness comparison reads it as expired rather than raising.
UNPARSEABLE_EXPIRY = float("-inf")

#: A ``.tmp`` orphan older than this is swept: atomic_write's crash window
#: (mkstemp -> os.replace) leaves it behind, invisible to glob and budget.
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


def decode_envelope(raw: bytes) -> CacheEnvelope | None:
    """One on-disk envelope, checked against the record the writer declared.

    Not an envelope (or not JSON) reads as a miss, never a crash out of
    ``resolve_expiry``, which used to receive ``json.loads``'s ``Any`` raw.
    """
    try:
        parsed: JsonValue = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("cached_at"), int | float):
        return None
    return cast(CacheEnvelope, parsed)


def atomic_write(path: Path, data: str) -> None:
    """Persist a pre-serialized envelope atomically (the default disk writer)."""
    # A cache entry is reproducible from the origin, so it writes best-effort.
    atomic_write_text(path, data, durable=False)


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
        envelope = decode_envelope(raw)
        if envelope is None:
            # Corrupt or not an envelope but present: keep the file (the read
            # path drops it on the next access) and repair its size below.
            surviving[path] = len(raw)
            continue
        expires = expiry_of(envelope)
        if is_expired(now, expires):
            expired.append((path, len(raw)))
        else:
            surviving[path] = len(raw)
    return expired, surviving
