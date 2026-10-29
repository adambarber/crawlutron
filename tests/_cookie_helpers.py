"""Helpers for tests that exercise the cookie jar's on-disk persistence.

Both consumers write a real file, read it back, and clean up; the copy that
skipped the unlink left a previous run's file for an atomicity test to read.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from crawlutron.transport.cookie_jar import CookieJar


@contextmanager
def cookie_file(jar: CookieJar, name: str) -> Iterator[str]:
    """Export ``jar`` to ``name``; yield the path, removed on exit, and removed
    *first* if a previous run left one behind so an atomicity test never reads a
    file it did not write.
    """
    target = os.path.join(tempfile.gettempdir(), name)
    if os.path.exists(target):
        os.unlink(target)
    try:
        jar.export_to_file(target)
        yield target
    finally:
        if os.path.exists(target):
            os.unlink(target)


def read_cookie_file(path: str) -> dict[str, dict[str, str]]:
    """The JSON a cookie export wrote, as the mapping the jar persists."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
