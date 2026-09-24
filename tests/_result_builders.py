from typing import Any

from app.constants import HTTP_DECLARED_CRAWLER
from app.validation import HostPolicy

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()


def make_job(index: int, url: str):
    from app.models import CrawlJob

    return CrawlJob(
        index=index,
        url=url,
        domain=url.split("/")[2],
        options=None,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


def _successful_engine_result(
    url: str,
    *,
    attempted_strategies: list[Any],
    text: str,
    markdown: str | None = None,
    links: list[Any] | None = None,
):
    from app.models import EngineCrawlResult

    discovered_links = list(links or [])
    return EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=url,
        status="success",
        classification="rendered",
        selected_strategy=HTTP_DECLARED_CRAWLER,
        attempted_strategies=attempted_strategies,
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html></html>",
        text=text,
        markdown=markdown,
        extraction_method="http_html",
        quality_score=1.0,
        discovered_link_count=len(discovered_links),
        discovered_links=discovered_links,
    )


def make_result(url: str, domain: str):
    from app.models import CrawlStrategyAttempt

    return _successful_engine_result(
        url,
        attempted_strategies=[
            CrawlStrategyAttempt(
                strategy=HTTP_DECLARED_CRAWLER,
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=0,
                error=None,
            )
        ],
        text="ok",
        markdown="ok",
    )


def make_engine_result(url: str, links=()):
    return _successful_engine_result(url, attempted_strategies=[], text="t", links=list(links))


class Spill:
    def __init__(self):
        self.writes: dict[int, Any] = {}

    def write(self, index: int, result: Any) -> None:
        self.writes[index] = result

    def load_ordered(self) -> list[Any]:
        return [self.writes[i] for i in sorted(self.writes)]


def _link(url: str, source_url: str, *, normalized: str | None = None) -> dict:
    return {
        "url": url,
        "normalized_url": normalized if normalized is not None else url,
        "text": "",
        "rel": [],
        "source_url": source_url,
        "host": (normalized or url).split("//", 1)[-1].split("/", 1)[0],
        "is_same_host": True,
    }


def _canned_result(
    url: str,
    *,
    status: str = "success",
    links: list[str] | None = None,
    raw_links: list[dict] | None = None,
    classification: str | None = None,
) -> dict:
    return {
        "original_url": url,
        "normalized_url": url,
        "final_url": url,
        "status": status,
        "classification": classification or ("rendered" if status == "success" else "site_error"),
        "discovered_links": [_link(link, url) for link in links or []] + (raw_links or []),
    }


def _scripted_result_map(urls, *, links: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    link_map = links or {}
    return {url: [_canned_result(url, links=link_map.get(url, []))] for url in urls}


def _small_link_graph_results() -> list[dict]:
    root = "http://example.test/a"
    return [
        _canned_result(
            root,
            links=[
                "http://example.test/b",
                "http://example.test/c",
                "http://example.test/d",
            ],
        ),
        _canned_result("http://example.test/b", links=["http://example.test/d"]),
        _canned_result("http://example.test/c"),
        _canned_result("http://example.test/d"),
    ]


__all__ = [
    "Spill",
    "_canned_result",
    "_link",
    "_scripted_result_map",
    "_small_link_graph_results",
    "make_engine_result",
    "make_job",
    "make_result",
]
