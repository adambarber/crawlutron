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

from _testkit import (
    make_content_cache,
    make_content_cache_with_entry,
    make_job_store,
    run_job_control_test,
)
from helpers.server import spawn_server

from app.api.render import render_request
from app.classify import classify_page_with_evidence
from app.constants import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)
from app.crawl.cache import HttpCache
from app.crawl.decompression import decompress_body
from app.crawl.document_text import (
    _collect_document_item_lines,
    _document_item_range,
    _format_extracted_document_text,
    _is_document_item_marker,
    _is_document_non_item_section,
    _join_document_paragraph_lines,
    _looks_like_document_heading,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
)
from app.crawl.engine import (
    _execute_crawl_chain,
    _payload_policy,
    crawl_request,
)
from app.crawl.extraction import (
    _extract_discovered_links,
    _html_diagnostics,
    extract_js_endpoints,
)
from app.crawl.llm_format import _extract_llm_metadata
from app.crawl.strategies.document import (
    _default_document_text_extractor,
    _format_table,
)
from app.crawl.strategies.http import (
    _cookie_header_for_url,
    _decode_body,
    _default_http_fetcher,
    _headers_for_strategy,
    _quality_score,
)
from app.models import CrawlJob, CrawlResult, RenderOptions, to_dict
from app.validation import get_env_int, normalize_public_http_url, public_url_or_none, registrable_domain


def _fake_browser(html=None, text="doc", metadata=None, status="success", classification="rendered"):
    def worker(job):
        return CrawlResult(
            url=job.url,
            status=status,
            classification=classification,
            final_url=job.url,
            html=html,
            text=text,
            attempts=1,
            elapsed_ms=1,
            domain="x",
            metadata=metadata or {},
        )

    return worker


def _policy(extra=None):
    policy = {
        "request_profile": {},
        "headers": {},
        "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    }
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
