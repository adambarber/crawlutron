"""Real tests for main.py endpoint logic (no mocks)."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "browser-crawler-test.jsonl"))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as crawler_main  # noqa: E402
from app.main import build_condensed_crawl_response  # noqa: E402

class TestMainCrawlTransform:
    def test_build_condensed_handles_html_result(self) -> None:
        results = [
            {
                "original_url": "http://example.com/a",
                "normalized_url": "http://example.com/a",
                "final_url": "http://example.com/a",
                "status": "success",
                "classification": "rendered",
                "http_status": 200,
                "content_type": "text/html",
                "html": "<html><head><title>T</title></head><body><p>Body</p></body></html>",
            }
        ]
        response = build_condensed_crawl_response(results)
        assert response.success is True
        item = response.data[0]
        assert item.url == "http://example.com/a"
        assert item.warning is None  # classification == rendered -> no warning
        assert item.metadata.source_url == "http://example.com/a"

    def test_build_condensed_handles_text_only_result(self) -> None:
        results = [
            {
                "original_url": "http://example.com/doc.pdf",
                "normalized_url": "http://example.com/doc.pdf",
                "final_url": "http://example.com/doc.pdf",
                "status": "success",
                "classification": "binary_no_text",
                "http_status": 200,
                "content_type": "application/pdf",
                "text": "extracted pdf text",
            }
        ]
        response = build_condensed_crawl_response(results)
        item = response.data[0]
        assert item.markdown == "extracted pdf text"
        assert item.warning == "binary_no_text"
        assert item.metadata.source_url == "http://example.com/doc.pdf"

class TestMainRenderValidation:
    def test_render_rejects_invalid_options_with_400(self) -> None:
        client = TestClient(crawler_main.app)
        response = client.post(
            "/v1/render",
            json={"url": "http://example.com/", "options": "not-a-dict"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["detail"]["code"] == "invalid_options"
