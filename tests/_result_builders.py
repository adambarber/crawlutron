from dataclasses import replace
from typing import Any
from urllib.parse import urljoin, urlparse

from app.constants import HTTP_DECLARED_CRAWLER, ResultStatus
from app.crawl.result_helpers import terminal_attempt
from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt
from app.models.discovered_link import DiscoveredLink
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.registrable_domain import registrable_domain
from app.models.render_options import RenderOptions
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.validation import safe_public_url_or_none
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def make_job(index: int, url: str, *, options: RenderOptions | None = None):
    """A ``CrawlJob`` carrying the default render options.

    ``CrawlJob.options`` has no default and the renderer reads
    ``options.max_retries`` off it, so the ``None`` this used to pass was not a
    value the job could hold -- it only went unnoticed because no test rendered
    one. The absent baseline is ``RenderOptions()`` itself, every field at its
    default.

    ``domain`` comes from ``registrable_domain``, the chokepoint production uses.
    The ``url.split("/")[2]`` this replaced kept the port, any userinfo, and the
    ``www.``, so a job built here could carry a domain no production job does.
    """
    return CrawlJob(
        index=index,
        url=url,
        domain=registrable_domain(url),
        options=RenderOptions() if options is None else options,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


def engine_result(
    url: str,
    classification: Classification,
    *,
    status: ResultStatus | None = None,
    **overrides: Any,
):
    """An ``EngineCrawlResult`` for `url`: every optional field absent, plus `overrides``.

    The absent baseline is what a result that never got a response looks like --
    no body, no strategy, zero quality, no links. A test that needs a success
    restates that shape, because those fields ARE the difference between the two,
    and spelling them per test is how they drift.

    The baseline is named as arguments rather than assembled in a ``dict`` and
    splatted. A splat through ``dict[str, Any]`` erases the field names from the
    type checker, so a required field added to the record, or one of these
    fourteen renamed or retyped, would fail here at runtime and nowhere before --
    which is why a field this table never mentioned was constructible for months.
    Every field the record declares a default for is left out and takes the
    record's own: a second copy of those defaults is a second thing to keep in
    step, and the copy is what drifts.

    Two things are derived rather than accepted, because each is a property of
    something else and two arguments that must agree should not both be the
    caller's. ``discovered_link_count`` comes from ``discovered_links``, always.
    ``status`` comes from the production ``status_for_classification`` mapping
    unless a caller names one, which :func:`_failed_result` does deliberately:
    a context-varying classification legitimately takes a status other than the
    table default, and the record's own ``__post_init__`` is what rejects a pair
    that disagrees with the taxonomy.

    One attempt, never none: ``build_engine_result`` is the only place an
    ``EngineCrawlResult`` exists and it always records one, so ``[]`` was a shape
    no runtime result has -- and nothing type-checks emptiness, so it survived.
    """
    from app.constants import status_for_classification

    if "discovered_link_count" in overrides:
        raise TypeError("discovered_link_count is derived from discovered_links; pass only the links")
    links = list(overrides.pop("discovered_links", []))
    derived = status_for_classification(classification) if status is None else status
    absent = EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=None,
        status=derived,
        classification=classification,
        selected_strategy=None,
        attempted_strategies=[
            terminal_attempt(
                strategy=None,
                status=derived,
                classification=classification,
                elapsed_ms=0,
                error=None,
            )
        ],
        http_status=None,
        headers={},
        content_type=None,
        html=None,
        text=None,
        extraction_method=None,
        quality_score=0.0,
        discovered_links=links,
        discovered_link_count=len(links),
        error=None,
    )
    return replace(absent, **overrides) if overrides else absent


def skipped_result(
    url: str,
    classification: Classification,
    *,
    strategy: CrawlStrategyName,
    error: CrawlError,
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
    attempted_strategies: list[CrawlStrategyAttempt],
    text: str,
    markdown: str | None = None,
    links: list[str] | None = None,
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


def crawl_result(url: str, domain: str | None = None) -> CrawlResult:
    """A ``CrawlResult`` -- the object ``app.crawl.worker.call_worker`` hands back.

    Worker-dispatch tests need a real result because the claim they make is
    identity: that the worker's own object is what comes back, rather than a copy
    or a discarded side effect. A string sentinel stood in for it, which made
    the assertion weaker than the thing being asserted and was not a
    ``CrawlResult`` at all.
    """
    return CrawlResult(
        url=url,
        attempt=terminal_attempt(
            strategy=None,
            status="success",
            classification="rendered",
            elapsed_ms=0,
            error=None,
        ),
        final_url=url,
        html="ok",
        text="ok",
        attempts=1,
        domain=domain if domain is not None else registrable_domain(url),
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


def _link(url: str, source_url: str, *, normalized: str | None = None) -> DiscoveredLink:
    """A discovered link as the record the engine carries.

    A dataclass rather than the dict ``to_dict`` used to produce: the runner
    under test now reads engine results as results, and a double that hands it
    a dict would be testing the old wire shape instead of the behavior.

    The four steps are ``extract_discovered_links``' own, because this is the
    same record and a double that skips them is not a looser producer -- it is
    one that reports links the real producer never emits. It resolved nothing,
    so every relative href raised instead of joining; it reached past
    ``safe_public_url_or_none``, the chokepoint every egress door is routed
    through, to the raising normalizer beside it; it read ``host`` off a string
    split rather than the parsed URL; and it answered ``is_same_host`` with a
    constant, so a cross-host link claimed to be same-host and nothing checked.
    """
    target = normalized if normalized is not None else urljoin(source_url, url)
    resolved = safe_public_url_or_none(target, policy=UNRESTRICTED_HOST_POLICY)
    if resolved is None:
        raise ValueError(f"not a public link target, the producer would drop it: {target}")
    host = urlparse(str(resolved)).hostname or ""
    source_host = urlparse(source_url).hostname or ""
    return DiscoveredLink(
        url=url,
        normalized_url=resolved,
        text="",
        rel=[],
        source_url=source_url,
        host=host,
        is_same_host=host.lower() == source_host.lower(),
    )


def _canned_result(
    url: str,
    *,
    classification: Classification = "rendered",
    status: ResultStatus | None = None,
    links: list[str] | None = None,
    raw_links: list[DiscoveredLink] | None = None,
) -> EngineCrawlResult:
    """An ``EngineCrawlResult`` for `url`, through the one builder the suite uses.

    Every field the batch tests read is real, so a runner bug that misreads a
    result cannot hide behind a double whose shape the runner never sees.

    ``classification`` is the axis a caller names and ``status`` follows from it,
    which is the direction production derives them in. Naming a status instead
    used to mean handing over a classification too, invented by a ternary here:
    a second copy of the taxonomy table, keyed the other way round, and one that
    could not have been wrong loudly -- it agreed with production until the
    taxonomy moved and this table did not.
    """
    return engine_result(
        url,
        classification,
        status=status,
        final_url=url,
        discovered_links=[_link(link, url) for link in links or []] + (raw_links or []),
    )


def _failed_result(url: str, classification: Classification) -> EngineCrawlResult:
    """A page that failed for a named reason.

    The status/classification pair is the shape every requeue test drives with,
    and restating it as two keywords is what let the classification and the
    status drift apart in one of them.
    """
    return _canned_result(url, status="failed", classification=classification)


def _flaky(
    url: str, *classifications: Classification, lands_on: EngineCrawlResult | None = None
) -> dict[str, list[EngineCrawlResult]]:
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


def _scripted_result_map(
    urls, *, links: dict[str, list[str]] | None = None
) -> dict[str, list[EngineCrawlResult]]:
    link_map = links or {}
    return {url: [_canned_result(url, links=link_map.get(url, []))] for url in urls}


GRAPH_ROOT = "http://example.test/a"
GRAPH_CHILD = "http://example.test/b"


def _root_child_results(root: str = GRAPH_ROOT, child: str = GRAPH_CHILD) -> list[EngineCrawlResult]:
    """The two-page graph: root discovers child, child is a leaf.

    The smallest graph that still exercises discovery, and the one most of the
    runner tests want -- one expansion, one frontier drain, nothing to cap.
    """
    return [_canned_result(root, links=[child]), _canned_result(child)]


def _root_fans_out(root: str, *children: str) -> list[EngineCrawlResult]:
    """A root that discovers every one of ``children``, each of them a leaf.

    The three-page shape most discovery tests want: one expansion, N leaves, a
    frontier that drains, and nothing else to configure. When a leaf has to
    discover something back, that is a different graph and gets written out.
    """
    return [_canned_result(root, links=list(children)), *(_canned_result(c) for c in children)]


def _small_link_graph_results() -> list[EngineCrawlResult]:
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
