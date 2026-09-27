"""Helpers for tests that exercise the cookie jar's on-disk persistence.

Both consumers -- the atomic-write test and the namespace round-trip test --
write to a real file, read it back as JSON, and clean up after themselves. That
is four lines each, and the copy that skipped the unlink first left a previous
run's file in place for a test about atomicity to read.
"""

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def cookie_file(jar: Any, name: str) -> Iterator[str]:
    """Export `jar` to a fresh file named `name`; yield the path.

    The file is removed on exit whether or not the body asserted, and it is
    removed *first* if a previous run left one behind -- otherwise a test about
    atomicity is reading a file it did not write.
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
