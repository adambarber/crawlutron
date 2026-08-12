"""Shared pytest fixtures and path setup for the browser-crawler test suite.

Centralizes the ``sys.path`` configuration previously duplicated at the top of
every test module so tests can simply ``import app`` and ``import shared``.
"""

from pathlib import Path

import sys

CRAWLER_APP = Path(__file__).resolve().parents[1]
SHARED_PYTHON = CRAWLER_APP.parent / "shared" / "python"

for _p in (str(SHARED_PYTHON), str(CRAWLER_APP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
