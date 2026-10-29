from collections.abc import Sequence
from dataclasses import replace
from typing import TypedDict, Unpack
from urllib.parse import urljoin

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import CrawlResult, EngineCrawlResult
from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none
from crawlutron.models.registrable_domain import registrable_domain
from crawlutron.models.render_options import RenderOptions
from crawlutron.models.result_factory import terminal_attempt
from crawlutron.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from crawlutron.vocabulary.taxonomy import ResultStatus
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


class _EngineResultOverrides(TypedDict, total=False):
    """The subset of EngineCrawlResult fields tests may override."""

    # Submission identity
    original_url: str
    normalized_url: NormalizedUrl | None
    final_url: NormalizedUrl | None
    # Strategy selection
    selected_strategy: CrawlStrategyName | None
    attempted_strategies: list[CrawlStrategyAttempt]
    # Response metadata
    http_status: int | None
    headers: dict[str, str]
    content_type: str | None
    # Content
    html: str | None
    text: str | None
    extraction_method: str | None
    # Quality
    quality_score: float
    # Discovery
    discovered_links: list[DiscoveredLink]
    discovered_link_count: int
    # Error
    error: CrawlError | None
    # Optional
    markdown: str | None


def make_job(index: int, url: str, *, options: RenderOptions | None = None):
    """A ``CrawlJob`` carrying the default render options.

    ``CrawlJob.options`` has no default; the URL goes in admitted, so
    ``domain`` is derived by production's chokepoint."""
    return CrawlJob(
        index=index,
        url=canonical(url),
        options=RenderOptions() if options is None else options,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


def _text(url: str | NormalizedUrl) -> str:
    """Text form of a URL argument: a value's identity, or text as given.

    For fields declared as submission text (``original_url``) and helpers that
    operate on text (``urljoin``)."""
    return url.raw if isinstance(url, NormalizedUrl) else url


def engine_result(
    url: str | NormalizedUrl,
    classification: Classification,
    *,
    status: ResultStatus | None = None,
    **overrides: Unpack[_EngineResultOverrides],
):
    """An ``EngineCrawlResult`` for `url`: every optional field absent, plus `overrides``.

    The absent baseline is a result that never got a response; named
    arguments, not a dict splat, so the type checker keeps the field names.
    """
    from crawlutron.vocabulary.taxonomy import status_for_classification

    if "discovered_link_count" in overrides:
        raise TypeError("discovered_link_count is derived from discovered_links; pass only the links")
    links = list(overrides.pop("discovered_links", []))
    # ``normalized_url``/``final_url`` are the record's value fields; callers still name submission text, so
    # admission lives here at the one builder rather than at every call site.
    normalized_text = overrides.pop("normalized_url", url)
    final_url_text = overrides.pop("final_url", None)
    derived = status_for_classification(classification) if status is None else status
    absent = EngineCrawlResult(
        original_url=_text(url),
        normalized_url=canonical(normalized_text) if normalized_text is not None else None,
        final_url=canonical(final_url_text) if final_url_text is not None else None,
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
    """A ``skipped_engine_result`` for `url`, built through the real primitive.

    All five arguments must agree, because ``error`` and ``strategy`` once
    drifted from what the production builder emits for a classification."""
    from crawlutron.transport.results import (
        TerminalResult,
        skipped_engine_result,
    )

    return skipped_engine_result(
        TerminalResult(
            original_url=url,
            normalized_url=canonical(normalized_url or url),
            strategy=strategy,
            classification=classification,
            error=error,
        )
    )


def _successful_engine_result(
    url: str | NormalizedUrl,
    *,
    attempted_strategies: list[CrawlStrategyAttempt],
    text: str,
    markdown: str | None = None,
    links: Sequence[DiscoveredLink] | None = None,
):
    return engine_result(
        url,
        "rendered",
        final_url=canonical(url),
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


def crawl_result(url: str | NormalizedUrl, domain: str | None = None) -> CrawlResult:
    """A ``CrawlResult`` -- what the worker hands back.

    Worker-dispatch tests claim identity: the worker's own object comes back,
    not a copy. A string sentinel stood in for it and asserted less."""
    return CrawlResult(
        url=canonical(url),
        attempt=terminal_attempt(
            strategy=None,
            status="success",
            classification="rendered",
            elapsed_ms=0,
            error=None,
        ),
        final_url=canonical(url),
        html="ok",
        text="ok",
        attempts=1,
        domain=domain
        if domain is not None
        else registrable_domain(url.raw if isinstance(url, NormalizedUrl) else url),
    )


def make_engine_result(url: str | NormalizedUrl, links: Sequence[DiscoveredLink] = ()):
    return _successful_engine_result(url, attempted_strategies=[], text="t", links=list(links))


class Spill:
    """The in-RAM ``SpillSink`` double; writes carry the record production carries."""

    def __init__(self):
        self.writes: dict[int, EngineCrawlResult] = {}

    def write(self, index: int, result: EngineCrawlResult) -> None:
        self.writes[index] = result

    def load_ordered(self, *, policy: HostPolicy) -> list[EngineCrawlResult]:
        """Reassembly in write order, as the real sink reads it back out."""
        del policy
        return [self.writes[index] for index in sorted(self.writes)]

    def close(self) -> None:
        """A spill is closable; a no-op here, the writes are already held in RAM."""
        return None


def _link(url: str, source_url: str, *, normalized: str | None = None) -> DiscoveredLink:
    """A discovered link as the record the engine carries.

    A dataclass with ``extract_discovered_links``' own four steps, so the
    double reports links the real producer emits."""
    target = normalized if normalized is not None else urljoin(_text(source_url), url)
    resolved = admit_or_none(target, policy=UNRESTRICTED_HOST_POLICY)
    if resolved is None:
        raise ValueError(f"not a public link target, the producer would drop it: {target}")
    source = admit_or_none(source_url, policy=UNRESTRICTED_HOST_POLICY)
    if source is None:
        raise ValueError(f"the source page must be an admitted URL: {source_url}")
    return DiscoveredLink(
        url=url,
        normalized_url=resolved,
        text="",
        rel=[],
        source_url=source,
        host=resolved.host,
        is_same_host=resolved.host == source.host,
    )


def _canned_result(
    url: str | NormalizedUrl,
    *,
    classification: Classification = "rendered",
    status: ResultStatus | None = None,
    links: list[str] | None = None,
    raw_links: list[DiscoveredLink] | None = None,
) -> EngineCrawlResult:
    """An ``EngineCrawlResult`` for `url`, through the one builder the suite uses.

    Every field the batch tests read is real, so a runner bug cannot hide
    behind a double; ``classification`` is named, ``status`` follows.
    """
    text = _text(url)
    return engine_result(
        text,
        classification,
        status=status,
        final_url=canonical(text),
        discovered_links=[_link(link, text) for link in links or []] + (raw_links or []),
    )


def _failed_result(url: str, classification: Classification) -> EngineCrawlResult:
    """A page that failed for a named reason.

    The status/classification pair is the shape every requeue test drives
    with -- restated as two keywords so they cannot drift apart."""
    return _canned_result(url, status="failed", classification=classification)


def _flaky(
    url: str, *classifications: Classification, lands_on: EngineCrawlResult | None = None
) -> dict[str, list[EngineCrawlResult]]:
    """A page that fails with each of these classifications in turn.

    ``lands_on`` is the final attempt's answer; without it the engine keeps
    failing, which is how the bounded-retry test reaches its cap."""
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
    frontier that drains."""
    return [_canned_result(root, links=list(children)), *(_canned_result(c) for c in children)]


def _small_link_graph_results() -> list[EngineCrawlResult]:
    """The four-page fixture graph, and the URLs it is made of.

    Named here, not inlined: the root a job is submitted against must be the
    same page the fixture describes."""
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
