"""Shared pytest fixtures and path setup for the browser-crawler test suite.

No per-file ``sys.path`` hacks: this root conftest adds the crawler package
and its shared logging helper to ``sys.path`` once, so every test file can
import :mod:`app` and :mod:`helpers` directly.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # docker/browser-crawler
SHARED = ROOT.parent / "shared" / "python"

for candidate in (str(ROOT), str(SHARED), str(HERE)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = os.environ.get(
    "BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1"
)
os.environ["BROWSER_CRAWLER_LOG_FILE"] = os.environ.get(
    "BROWSER_CRAWLER_LOG_FILE", "/tmp/browser_crawler_test.log"
)
