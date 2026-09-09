"""TestServiceRenderRequest coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestServiceRenderRequest:
    # Uses the module-level _worker factory.

    async def test_render_request_success(self):
        from app.settings import CrawlerSettings

        out = await render_request(
            {"url": "http://127.0.0.1:1/x", "options": {"include_html": True}},
            settings=CrawlerSettings.from_mapping(),
            worker=_worker(),
        )
        assert out["submitted_count"] == 1
        assert out["results"][0]["status"] == "success"

    async def test_render_request_disallowed_url(self):
        from app.settings import CrawlerSettings

        out = await render_request(
            {"url": "not a url"},
            settings=CrawlerSettings.from_mapping(),
            worker=_worker(),
        )
        assert out["results"][0]["status"] == "disallowed"

    async def test_render_request_duplicate_urls_return_one_result_each(self):
        from app.settings import CrawlerSettings

        out = await render_request(
            {"urls": ["http://127.0.0.1:1/x", "http://127.0.0.1:1/x"]},
            settings=CrawlerSettings.from_mapping(),
            worker=_worker(),
        )
        assert out["submitted_count"] == 2
        assert len(out["results"]) == 2
        assert all(item["status"] == "success" for item in out["results"])

    async def test_render_request_batch_exceeded(self):
        from app.settings import CrawlerSettings

        with pytest.raises(ValueError):
            await render_request(
                {"urls": ["http://127.0.0.1:1/x"] * 1000},
                settings=CrawlerSettings.from_mapping(),
                worker=_worker(),
            )
