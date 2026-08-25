"""Regression test: uncompressed bodies arrive as bytearray; the document
extraction path must handle them (batch A2 previously broke this)."""

import sys

async def test_document_extraction_with_uncompressed_bytearray_body(base_url):
    """Simulate fetcher delivering CSV body as bytearray (no content-encoding)."""
    from app.crawl.strategies.http import _http_strategy_result
    from app.models import HttpFetchResult

    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        # csv served WITHOUT compression -> decompress_body returns the raw
        # buffer unchanged, which is a bytearray
        return HttpFetchResult(
            url=url, final_url=url, status_code=200,
            headers={"content-type": "text/csv"},
            body=bytearray(b"name,age\nAlice,30\nBob,25"), elapsed_ms=1,
        )

    result = await _http_strategy_result(
        original_url="https://sudbury.ma.us/x.csv", normalized_url="https://sudbury.ma.us/x.csv",
        strategy="HTTP_DECLARED_CRAWLER",
        policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "limits": {"timeout_seconds": 10},
                "headers": {}, "extraction": "document"},
        http_fetcher=fetcher,
    )
    assert result.status == "success", (result.status, result.classification)
    assert "Alice" in (result.text or "")
