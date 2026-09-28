from typing import Any

from tests._policy_factories import UNRESTRICTED_HOST_POLICY

from app.constants import HTTP_DECLARED_CRAWLER


def make_job(index: int, url: str):
    from app.models import CrawlJob

    return CrawlJob(
        index=index,
        url=url,
        domain=url.split("/")[2],
        options=None,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


def engine_result(url: str, classification: str, **overrides: Any):
    """An ``EngineCrawlResult`` for `url`: every optional field absent, plus `overrides`.

    The absent baseline is what a result that never got a response looks like --
    no status, no body, no strategy, zero quality, no links. A test that needs a
    success restates that shape, because those fields ARE the difference between
    the two, and spelling them per test is how they drift.

    Two things are derived rather than accepted, because each is a property of
    something else and two arguments that must agree should not both be the
    caller's: ``status`` comes from the production ``status_for_classification``
    mapping, so a test cannot assert a status that contradicts the classification
    it named; and ``discovered_link_count`` comes from ``discovered_links``.
    """
    from app.constants import status_for_classification
    from app.models import EngineCrawlResult

    if "discovered_link_count" in overrides:
        raise TypeError("discovered_link_count is derived from discovered_links; pass only the links")
    links = list(overrides.pop("discovered_links", []))
    fields: dict[str, Any] = {
        "original_url": url,
        "normalized_url": url,
        "final_url": None,
        "status": status_for_classification(classification),
        "classification": classification,
        "selected_strategy": None,
        "attempted_strategies": [],
        "http_status": None,
        "headers": {},
        "content_type": None,
        "html": None,
        "text": None,
        "markdown": None,
        "extraction_method": None,
        "quality_score": 0.0,
        "discovered_link_count": len(links),
        "discovered_links": links,
        "error": None,
    }
    fields.update(overrides)
    return EngineCrawlResult(**fields)


def skipped_result(
    url: str,
    classification: str,
    *,
    strategy: str,
    error: str,
    normalized_url: str | None = None,
):
    """A ``_skipped_engine_result`` for `url`, built through the real primitive.

    The skipped-result builders are one per classification site in production, and
    a test that wants to check the builder's status against the classification
    table needs all five of its arguments to agree. Written out, ``error`` and
    ``strategy`` drifted: one suite kept asserting a skipped verdict while naming
    an error string the production builder never emits for that classification,
    which is exactly the drift the table exists to catch.
    """
    from app.crawl.result_helpers import TerminalResult, _skipped_engine_result

    return _skipped_engine_result(
        TerminalResult(
            original_url=url,
            normalized_url=normalized_url or url,
            strategy=strategy,
            classification=classification,
            error=error,
        )
    )


def _successful_engine_result(
    url: str,
    *,
    attempted_strategies: list[Any],
    text: str,
    markdown: str | None = None,
    links: list[Any] | None = None,
):
    return engine_result(
        url,
        "rendered",
        final_url=url,
        selected_strategy=HTTP_DECLARED_CRAWLER,
        attempted_strategies=attempted_strategies,
        http_status=200,
        content_type="text/html",
        html="<html></html>",
        text=text,
        markdown=markdown,
        extraction_method="http_html",
        quality_score=1.0,
        discovered_links=list(links or []),
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


def _failed_result(url: str, classification: str) -> dict:
    """A page that failed for a named reason.

    The status/classification pair is the shape every requeue test drives with,
    and restating it as two keywords is what let the classification and the
    status drift apart in one of them.
    """
    return _canned_result(url, status="failed", classification=classification)


def _flaky(url: str, *classifications: str, lands_on: dict | None = None) -> dict[str, list[dict]]:
    """A page that fails with each of these classifications in turn.

    ``lands_on`` is what the final attempt returns; without it the engine keeps
    answering failure, which is how the bounded-retry test reaches its cap.
    Stating the whole sequence in one call is what stopped the two retry tests
    from each inventing a way of spelling the same run.
    """
    entries = [_failed_result(url, classification) for classification in classifications]
    if lands_on is not None:
        entries.append(lands_on)
    return {url: entries}


def _scripted_result_map(urls, *, links: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    link_map = links or {}
    return {url: [_canned_result(url, links=link_map.get(url, []))] for url in urls}


GRAPH_ROOT = "http://example.test/a"
GRAPH_CHILD = "http://example.test/b"


def _root_child_results(root: str = GRAPH_ROOT, child: str = GRAPH_CHILD) -> list[dict]:
    """The two-page graph: root discovers child, child is a leaf.

    The smallest graph that still exercises discovery, and the one most of the
    runner tests want -- one expansion, one frontier drain, nothing to cap.
    """
    return [_canned_result(root, links=[child]), _canned_result(child)]


def _root_fans_out(root: str, *children: str) -> list[dict]:
    """A root that discovers every one of ``children``, each of them a leaf.

    The three-page shape most discovery tests want: one expansion, N leaves, a
    frontier that drains, and nothing else to configure. When a leaf has to
    discover something back, that is a different graph and gets written out.
    """
    return [_canned_result(root, links=list(children)), *(_canned_result(c) for c in children)]


def _small_link_graph_results() -> list[dict]:
    """The four-page fixture graph, and the URLs it is made of.

    The URLs are named here rather than inlined at each test because the graph
    and the root a job is submitted against have to be the same page: a test
    that crawls a different ``/a`` than the fixture describes gets an unscripted
    URL back, and the scripted engine answers it as a success by default.
    """
    return [
        _canned_result(
            GRAPH_ROOT,
            links=[GRAPH_CHILD, "http://example.test/c", "http://example.test/d"],
        ),
        _canned_result(GRAPH_CHILD, links=["http://example.test/d"]),
        _canned_result("http://example.test/c"),
        _canned_result("http://example.test/d"),
    ]


__all__ = [
    "GRAPH_CHILD",
    "GRAPH_ROOT",
    "Spill",
    "_canned_result",
    "_failed_result",
    "_flaky",
    "_link",
    "_root_child_results",
    "_root_fans_out",
    "_scripted_result_map",
    "_small_link_graph_results",
    "engine_result",
    "make_engine_result",
    "make_job",
    "make_result",
]
