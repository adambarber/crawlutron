"""Pipeline settings are constructed once per run, not once per extraction.

The old per-call ``settings or CrawlerSettings.from_mapping()`` fallback made
every document extraction re-walk the env and re-read cgroup sysfs.
"""

from unittest.mock import patch

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.json_value import JsonValue
from tests._crawl_runners import build_payload
from tests._fetch_doubles import FetcherDouble


def _payload() -> dict[str, JsonValue]:
    return build_payload(
        url="http://example.com/doc.csv",
        strategies=["HTTP_DECLARED_CRAWLER"],
        limits={"timeout_seconds": 10, "max_retries": 0, "max_depth": 0, "max_pages": 1},
        policy={"extraction": "document"},
    )


async def test_document_extraction_never_constructs_settings_per_call(monkeypatch):
    """The document branch receives the crawl's settings and constructs none.

    The spy rides the pipeline's own ``adocument_extract`` binding (the name it
    actually calls), and the patch on ``from_mapping`` makes any fallback loud."""
    import crawlutron.transport.content_pipeline as cp

    fetcher = FetcherDouble(lambda url, *_: {"status": 200, "ctype": "text/csv", "body": b"name,age\nada,30"})
    settings = CrawlerSettings.from_mapping({})

    with (
        patch("crawlutron.extraction.documents.CrawlerSettings.from_mapping") as per_call,
        patch.object(cp, "adocument_extract", wraps=cp.adocument_extract) as spy,
    ):
        per_call.return_value = settings
        from crawlutron.orchestration.engine import crawl_request

        result = await crawl_request(_payload(), settings=settings, http_fetcher=fetcher, cache=None)
    assert result["results"], "the crawl produced no result"
    assert spy.call_count == 1, "the document branch did not run"
    assert per_call.call_count == 0, f"the extractor constructed settings {per_call.call_count}x in one run"
