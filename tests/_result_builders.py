from dataclasses import replace
from typing import TypedDict, Unpack
from urllib.parse import urljoin

from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import CrawlResult, EngineCrawlResult
from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none
from crawlutron.models.page_metadata import PageMetadata
from crawlutron.models.registrable_domain import registrable_domain
from crawlutron.models.render_options import RenderOptions
from crawlutron.models.result_factory import terminal_attempt
from crawlutron.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from crawlutron.vocabulary.taxonomy import ResultStatus
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


class Unset:
    """The type of ``final_url``'s default: named-None still means an absent field."""


_UNSET = Unset()


class _EngineResultOverrides(TypedDict, total=False):
    """The subset of EngineCrawlResult fields tests may override."""

    # Submission identity
    original_url: str
    normalized_url: NormalizedUrl | None
    # Strategy selection
    selected_strategy: CrawlStrategyName | None
    attempted_strategies: tuple[CrawlStrategyAttempt, ...]
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
    discovered_links: tuple[DiscoveredLink, ...]
    # Derived, never passed: the builder raises if a caller tries
    discovered_link_count: int
    # Error
    error: CrawlError | None
    # Optional
    markdown: str | None
    diagnostics: CrawlDiagnostics
    llm_metadata: PageMetadata | None
    validators: dict[str, str]


def make_job(index: int, url: str, *, options: RenderOptions | None = None) -> CrawlJob:
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


def _absent_result(
    url: str | NormalizedUrl,
    classification: str,
    *,
    derived: ResultStatus,
    normalized_text: str | NormalizedUrl | None,
    final_url_text: str | NormalizedUrl | None,
    discovered: list[DiscoveredLink],
) -> EngineCrawlResult:
    """The never-got-a-response record one terminal attempt is attached to."""
    return EngineCrawlResult(
        original_url=_text(url),
        normalized_url=canonical(normalized_text) if normalized_text is not None else None,
        final_url=canonical(final_url_text) if final_url_text is not None else None,
        status=derived,
        classification=Classification(classification),
        selected_strategy=None,
        attempted_strategies=(
            terminal_attempt(
                strategy=None,
                status=derived,
                classification=Classification(classification),
                elapsed_ms=0,
                error=None,
            ),
        ),
        http_status=None,
        headers={},
        content_type=None,
        html=None,
        text=None,
        extraction_method=None,
        quality_score=0.0,
        discovered_links=tuple(discovered),
        discovered_link_count=len(discovered),
        error=None,
    )


def engine_result(
    url: str | NormalizedUrl,
    classification: str,
    *,
    status: ResultStatus | None = None,
    links: list[str] | None = None,
    raw_links: list[DiscoveredLink] | None = None,
    final_url: str | NormalizedUrl | None | Unset = _UNSET,
    **overrides: Unpack[_EngineResultOverrides],
):
    """The one ``EngineCrawlResult`` builder: an absent baseline plus what is named.

    The absent baseline is a result that never got a response. Passing ``links``
    (strings, converted through ``_link``) or ``raw_links`` marks the page
    rendered -- ``final_url`` then follows ``canonical(url)`` like a real
    response would, unless named."""
    from crawlutron.vocabulary.taxonomy import status_for_classification

    if "discovered_link_count" in overrides:
        raise TypeError("discovered_link_count is derived from discovered_links; pass only the links")
    links_arg = list(overrides.pop("discovered_links", []) or [])
    # ``normalized_url``/``final_url`` are the record's value fields; callers still name submission text, so
    # admission lives here at the one builder rather than at every call site.
    normalized_text = overrides.pop("normalized_url", url)
    # ``final_url`` binds to the named parameter; a caller-supplied ``final_url=``
    # can never land in ``overrides``, so no override tolerance is needed here.
    final_url_text: str | NormalizedUrl | None = None
    if not isinstance(final_url, Unset):
        # A named ``final_url`` wins, ``None`` included (an absent field).
        final_url_text = _text(final_url) if final_url is not None else None
    elif links is not None or raw_links is not None or links_arg:
        # A rendered page's final_url follows its own URL, as a real response would.
        final_url_text = _text(url)
    derived = status_for_classification(classification) if status is None else status
    discovered = [*links_arg, *(_link(link, _text(url)) for link in links or []), *(raw_links or [])]
    absent = _absent_result(
        url,
        classification,
        derived=derived,
        normalized_text=normalized_text,
        final_url_text=final_url_text,
        discovered=discovered,
    )
    return replace(absent, **overrides) if overrides else absent


def skipped_result(
    url: str,
    classification: str,
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
            classification=Classification(classification),
            error=error,
        )
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
            classification=Classification.rendered,
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


class Spill:
    """The in-RAM spill double; writes carry the record production carries."""

    def __init__(self):
        self.writes: dict[int, EngineCrawlResult] = {}

    async def aput(self, _request_id: str, index: int, result: EngineCrawlResult) -> None:
        self.writes[index] = result

    async def aload_ordered(self, _request_id: str, *, policy: HostPolicy) -> list[EngineCrawlResult]:
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


GRAPH_ROOT = "http://example.test/a"
GRAPH_CHILD = "http://example.test/b"

__all__ = [
    "GRAPH_CHILD",
    "GRAPH_ROOT",
    "Spill",
    "crawl_result",
    "engine_result",
    "make_job",
    "skipped_result",
]
