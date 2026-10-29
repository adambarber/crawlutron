"""Sync trafilatura extraction must leave the event loop.

Both strategy result builders call the CPU-bound ``extract_html`` synchronously;
they must go through ``asyncio.to_thread`` like their sibling calls.
"""

from crawlutron.extraction.content import extract_html
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from tests._assertions import install_to_thread_spy
from tests._fetcher_factories import invoke_browser_strategy, invoke_http_strategy
from tests._policy_factories import http_strategy_policy

_HTML_BODY = (
    b"<html><head><title>T</title></head><body><article><p>"
    + b"Hello world, this is enough text to render. " * 20
    + b"</p></article></body></html>"
)


def _resp(url):
    class Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        body = _HTML_BODY
        final_url = url
        elapsed_ms = 3

    return Resp()


async def test_http_trafilatura_extraction_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_http_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="trafilatura"),
        resp=_resp,
    )
    assert result.status == "success"
    assert result.text
    assert "extract_html" in seen


async def test_browser_trafilatura_extraction_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_browser_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="trafilatura"),
        html=_HTML_BODY.decode(),
    )
    assert result.status == "success"
    assert result.text
    assert "extract_html" in seen


async def test_trafilatura_result_unchanged_by_offload(monkeypatch):
    """Offloading must not change the extracted text (same function, new thread)."""
    # Hermetic against trafilatura's process-global body-dedup LRU: extracting the same body twice in a row
    result = await invoke_http_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="trafilatura"),
        resp=_resp,
        dedupe_patch=True,
    )
    direct = extract_html(
        _HTML_BODY.decode(), NormalizedUrl("http://example.com/", policy=HostPolicy.unrestricted())
    )
    assert result.text == (direct.text or "")
