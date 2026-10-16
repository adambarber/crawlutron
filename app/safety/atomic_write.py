"""Crash-safe file writes: one audited mkstemp + fsync + replace dance.

Every disk write goes through :func:`atomic_write_text` so the mid-write-corrupt-file
crash class is closed in exactly one place.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | Path, data: str, *, encoding: str = "utf-8") -> None:
    """Write ``data`` to ``path`` atomically (temp file + fsync + replace).

    A crash or error mid-write leaves the previous file intact and never
    leaves temp files behind. Parent directories are created as needed.
    """
    target = Path(path)
    parent = target.parent
    if str(parent):
        os.makedirs(parent, exist_ok=True)
    directory = str(parent) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
