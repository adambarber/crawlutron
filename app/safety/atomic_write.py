"""Crash-safe file writes: one audited mkstemp + fsync + replace dance.

Every disk write goes through :func:`atomic_write_text` so the mid-write-corrupt-file
crash class is closed in exactly one place.
"""

from __future__ import annotations

import fcntl
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
        # Hold an advisory lock on the temp file so a concurrent sweep can tell
        # a live write from a SIGKILL orphan (age alone is not ownership, #241).
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
        _fsync_parent(parent)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _fsync_parent(parent: Path) -> None:
    """Fsync the parent directory so a rename survives power loss.
    The replace alone is not durable: the rename lands in the directory's
    journal, so the directory itself must be fsynced to flush it (#241)."""
    dir_fd = os.open(str(parent or "."), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
