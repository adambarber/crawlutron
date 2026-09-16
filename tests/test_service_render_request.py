"""TestServiceRenderRequest coverage tests (split from tests/test_coverage_gaps.py)."""

from _testkit import *


class TestServiceRenderRequest:
    # Uses the shared _fake_browser worker factory.

    async def test_render_request_success(self):
        from app.settings import CrawlerSettings

        out = await render_request(
            {"url": "http://127.0.0.1:1/x", "options": {"include_html": True}},
            settings=CrawlerSettings.from_mapping(),
            worker=_fake_browser(html="<html></html>", text="t"),
        )
        assert out["submitted_count"] == 1
        assert out["results"][0]["status"] == "success"

    async def test_render_request_disallowed_url(self):
        from app.settings import CrawlerSettings

        out = await render_request(
            {"url": "not a url"},
            settings=CrawlerSettings.from_mapping(),
            worker=_fake_browser(html="<html></html>", text="t"),
        )
        assert out["results"][0]["status"] == "disallowed"

    async def test_render_request_duplicate_urls_return_one_result_each(self):
        """Exact-duplicate URLs in one request collapse to a single submission."""
        from app.settings import CrawlerSettings

        out = await render_request(
            {"urls": ["http://127.0.0.1:1/x", "http://127.0.0.1:1/x"]},
            settings=CrawlerSettings.from_mapping(),
            worker=_fake_browser(html="<html></html>", text="t"),
        )
        assert out["submitted_count"] == 1
        assert len(out["results"]) == 1
        assert out["results"][0]["status"] == "success"

    async def test_render_request_batch_exceeded(self):
        from app.settings import CrawlerSettings

        with pytest.raises(ValueError):
            await render_request(
                # Distinct URLs: the cap counts unique URLs after dedup.
                {"urls": [f"http://127.0.0.1:1/x{i}" for i in range(1000)]},
                settings=CrawlerSettings.from_mapping(),
                worker=_fake_browser(html="<html></html>", text="t"),
            )
