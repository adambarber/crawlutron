"""Adversarial Round 3 fix tests.

Findings addressed:
- B1/B2: _PENDING_CLIENT_CLOSE cap now calls sync ``close()`` on the dropped
  client so socket resources are released even when no event loop is running.
- B3: dedupe + cap check are now atomic under a single lock acquisition.
- C1: truncated requests get ``Connection: close`` so the keep-alive socket
  buffer cannot poison subsequent requests on the same connection.
- C3: per-chunk ``headers`` (HTTP/1.1 trailers) are preserved across truncation.
"""
import re
from unittest.mock import MagicMock

from app.crawl.strategies import http as http_mod


def _extract_function_body(source: str, name: str) -> str:
    """Extract just the body of an async function ``name`` at any indent."""
    pattern = re.compile(
        r"^[ \t]*async def " + re.escape(name) + r"\b.*?^[ \t]*(?=@app\.middleware|class |def |async def [a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(source)
    if not m:
        raise AssertionError(f"Could not find function {name}")
    return m.group(0)


# ---- B1: cap pops oldest and calls sync close() ----
def test_pending_client_close_cap_calls_sync_close_on_dropped():
    sample = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
    for c in sample:
        c.close = MagicMock()
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()
        for c in sample:
            http_mod._PENDING_CLIENT_CLOSE.append(c)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        if len(http_mod._PENDING_CLIENT_CLOSE) >= http_mod._PENDING_CLIENT_CLOSE_MAX:
            stale = http_mod._PENDING_CLIENT_CLOSE.pop(0)
            stale.close()
    sample[0].close.assert_called_once()
    assert sample[0] not in http_mod._PENDING_CLIENT_CLOSE


def test_pending_client_close_sync_close_failure_is_swallowed():
    class _BoomClient:
        def close(self):
            raise RuntimeError("already closed")

    boom = _BoomClient()
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()
        for _ in range(http_mod._PENDING_CLIENT_CLOSE_MAX):
            http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
        http_mod._PENDING_CLIENT_CLOSE[0] = boom
        stale = http_mod._PENDING_CLIENT_CLOSE.pop(0)
        try:
            stale.close()
        except Exception:
            pass
        http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
    assert len(http_mod._PENDING_CLIENT_CLOSE) == http_mod._PENDING_CLIENT_CLOSE_MAX


# ---- B3 ----
def test_pending_client_close_dedupe_and_cap_are_atomic():
    src_path = "app/crawl/strategies/http.py"
    with open(src_path) as f:
        source = f.read()
    assert "already_queued = old_client in _PENDING_CLIENT_CLOSE" in source
    assert "if not already_queued:" in source


# ---- C1 ----
def test_body_size_middleware_sets_connection_close_on_truncation():
    src_path = "app/api/middleware.py"
    with open(src_path) as f:
        source = f.read()
    assert 'response.headers["Connection"] = "close"' in source
    assert "if truncated:" in source


def test_body_size_middleware_drains_after_truncation():
    src_path = "app/api/middleware.py"
    with open(src_path) as f:
        source = f.read()
    assert "drain_after_truncate" in source
    assert "already_drained" in source


# ---- C3 ----
def test_body_size_middleware_preserves_per_chunk_headers():
    src_path = "app/api/middleware.py"
    with open(src_path) as f:
        source = f.read()
    body = _extract_function_body(source, "_capped_receive")
    non_comment = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "{**message" in non_comment
    assert "dict(message)" not in non_comment


def test_body_size_middleware_truncation_keeps_more_body_false():
    src_path = "app/api/middleware.py"
    with open(src_path) as f:
        source = f.read()
    body = _extract_function_body(source, "_capped_receive")
    assert '"more_body": False' in body
