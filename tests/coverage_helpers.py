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
import asyncio
import gzip
import os
from typing import Any
import pytest

# allow loopback crawl targets in this process too
os.environ.setdefault("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")

from app.classify import classify_page_with_evidence
from app.crawl.cache import HttpCache
from app.crawl.decompression import decompress_body
from app.crawl.engine import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_TRAFILATURA,
    _execute_crawl_chain,
    _payload_policy,
    crawl_request,
)
from app.crawl.extraction import (
    _extract_discovered_links,
    _html_diagnostics,
    extract_js_endpoints,
)
from app.crawl.firecrawl_format import _extract_firecrawl_metadata
from app.crawl.strategies.document import (
    _default_document_text_extractor,
    _format_table,
)
from app.crawl.strategies.http import (
    _cookie_header_for_url,
    _default_http_fetcher,
    _headers_for_strategy,
    _quality_score,
    _status_for_classification,
    _decode_body,
)
from app.crawl.document_text import (
    _format_extracted_document_text,
    _looks_like_document_heading,
    _collect_document_item_lines,
    _document_item_range,
    _is_document_item_marker,
    _is_document_non_item_section,
    _join_document_paragraph_lines,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
)
from app.constants import HTTP_CFFI_IMPERSONATE, HTTP_DECLARED_CRAWLER
from app.models import CrawlJob, CrawlResult, RenderOptions, to_dict
from app.service import render_request
from app.validation import normalize_public_http_url, registrable_domain
from helpers import (
    make_async_client,
    run_async_test,
    make_content_cache,
    make_content_cache_with_entry,
    make_job_store,
    run_job_control_test,
)
from helpers.server import spawn_server

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
    from app.crawl.content_cache import ContentCache
    import tempfile
    clock_vals = [clock_start]
    return ContentCache(tempfile.mkdtemp(), default_ttl=ttl, clock=lambda: clock_vals[0]), clock_vals


def _spawn_fetch(fn):
    from helpers.server import spawn_server
    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


# Export every imported name (including underscore-prefixed internals that the
# split coverage tests exercise) so ``from coverage_helpers import *`` works.
__all__ = [name for name in globals() if not name.startswith("__")]
