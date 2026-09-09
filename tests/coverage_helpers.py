"""Shared imports and fixtures used by the split test_coverage_gaps files.

This module is imported (via ``from coverage_helpers import *``) by each
per-module coverage test file so the original monolith can be split without
duplicating import boilerplate or helper fakes.
"""

"""Targeted coverage tests for branches missed by the behavioral suites.

These exercise pure functions and orchestration with real inputs only (no
unittest.mock). Browser-dependent paths use real Camoufox or a real in-process
HTTP server.
"""
import os

# allow loopback crawl targets in this process too
os.environ.setdefault("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")

from helpers.server import spawn_server

from app.models import CrawlResult


def _fake_browser(html=None, text="doc", metadata=None):
    def worker(job):
        return CrawlResult(
            url=job.url, status="success", classification="rendered",
            final_url=job.url, html=html, text=text, attempts=1,
            elapsed_ms=1, domain="x", metadata=metadata or {},
        )
    return worker


def _worker(status="success", classification="rendered"):
    def worker(job):
        return CrawlResult(
            url=job.url, status=status, classification=classification,
            final_url=job.url, html="<html></html>", text="t", attempts=1,
            elapsed_ms=1, domain="x", metadata={},
        )
    return worker


def _policy(extra=None):
    policy = {"request_profile": {}, "headers": {}, "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50}}
    if extra:
        policy.update(extra)
    return policy


def _temp_cache(ttl=1, clock_start=1000.0):
    import tempfile

    from app.crawl.content_cache import ContentCache
    clock_vals = [clock_start]
    return ContentCache(tempfile.mkdtemp(), default_ttl=ttl, clock=lambda: clock_vals[0]), clock_vals


def _spawn_fetch(fn):
    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


# Export every imported name (including underscore-prefixed internals that the
# split coverage tests exercise) so ``from coverage_helpers import *`` works.
__all__ = [name for name in globals() if not name.startswith("__")]
