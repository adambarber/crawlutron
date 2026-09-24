"""TestServiceRenderRequest coverage tests (split from tests/test_coverage_gaps.py)."""

import pytest
from _fetcher_factories import _fake_browser

from app.api.render import render_request


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

    async def test_render_request_disallowed_url_is_not_accepted(self):
        from app.settings import CrawlerSettings

        with pytest.raises(ValueError, match="invalid_url"):
            await render_request(
                {"url": "not a url"},
                settings=CrawlerSettings.from_mapping(),
                worker=_fake_browser(html="<html></html>", text="t"),
            )

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

    async def test_render_worker_exception_does_not_leak_raw_error_text(self):
        """A worker failure must surface a stable code, not the exception text.

        The missing-slot result used to carry ``error=str(error)`` from the
        caught worker exception, and ``to_dict`` is pure serialization -- so
        raw exception text (which can carry DNS names, paths, or secrets)
        reached the /v1/render response body verbatim. This is exactly the
        leak the shared exception->result primitive exists to prevent.
        """
        from app.settings import CrawlerSettings

        secret = "SECRET_TOKEN_ABC123_INTERNAL_PATH=/etc"

        def worker(job):
            raise RuntimeError(f"boom: {secret}")

        out = await render_request(
            {"url": "http://127.0.0.1:1/x"},
            settings=CrawlerSettings.from_mapping(),
            worker=worker,
        )
        error = out["results"][0]["error"]
        assert secret not in error, f"raw exception text leaked into the response: {error!r}"
        # RuntimeError classifies to the stable network_error code.
        assert error == "network_error"
