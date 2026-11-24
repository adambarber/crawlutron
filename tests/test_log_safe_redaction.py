"""#1414: the redaction chokepoint yields log-safe, linear-time text.

The shared door protects every sink: a user-controlled value cannot forge a
log line, and hostile oversized input cannot drive regex backtracking.
"""

from __future__ import annotations

import logging
import time

import pytest

from safe_url.url_redaction import redact_urls_in_text
from tests._assertions import one_search_result


async def test_search_log_query_cannot_forge_a_log_entry(
    async_client, override_settings, fake_provider_http, caplog
):
    """A newline in the caller query must not split the completion log entry."""
    one_search_result(override_settings, fake_provider_http)
    query = "https://example.com/x?token=s3cret\nFORGED\r\nhttps://example.com/y?token=leak"
    with caplog.at_level(logging.INFO, logger="crawlutron.search.endpoint"):
        resp = await async_client.post("/v1/search", json={"query": query})
    assert resp.status_code == 200
    rec = next(r for r in caplog.records if r.getMessage() == "search_request_completed")
    assert "\n" not in rec.query and "\r" not in rec.query
    assert "\\n" in rec.query and "\\r" in rec.query
    assert "s3cret" not in rec.query
    assert "leak" not in rec.query


def test_redaction_chokepoint_escapes_every_control_character():
    """C0 controls and DEL become visible escapes, never raw log bytes."""
    safe = redact_urls_in_text("a\nb\rc\td\x00e\x1ff\x7fg")
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in safe)
    assert "\\n" in safe and "\\r" in safe and "\\t" in safe
    assert "\\x00" in safe and "\\x1f" in safe and "\\x7f" in safe
    assert "g" in safe


@pytest.mark.wall_assertion
def test_secret_param_scan_is_linear_on_oversized_input():
    """A run of marker characters has no '=' terminator; scanning must not backtrack."""
    payload = "?" * 40_000
    started = time.perf_counter()
    out = redact_urls_in_text(payload)
    elapsed = time.perf_counter() - started
    assert out == payload
    assert elapsed < 2.0, f"secret-param scan backtracked for {elapsed:.2f}s"


@pytest.mark.wall_assertion
def test_url_span_scan_is_linear_on_oversized_input():
    """A run of scheme characters has no '://' terminator; scanning must not backtrack."""
    payload = "a" * 100_000
    started = time.perf_counter()
    out = redact_urls_in_text(payload)
    elapsed = time.perf_counter() - started
    assert out == payload
    assert elapsed < 2.0, f"URL span scan backtracked for {elapsed:.2f}s"
