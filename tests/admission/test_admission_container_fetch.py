"""fetch_job_sitemap_candidates must route the origin fetch through
EffectContainer.fetch so the admission gate stays pure: it decides yes/no,
the container performs the origin I/O."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import crawlutron.admission.admission as admission
from crawlutron.admission.admission import (
    JobAdmissionContext,
    fetch_job_sitemap_candidates,
)
from crawlutron.admission.policy import PolicyTier
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.service.effects import (
    EffectContainer,
    EffectKind,
    EffectOutcome,
    EffectResult,
)
from tests._policy_factories import canonical


class _FetchContainer(EffectContainer):
    """Container double that records each fetch call and, when ``run_op``,
    runs the effect. One ``fetch`` definition keeps the double clone-free."""

    def __init__(self, *, run_op: bool = False) -> None:
        super().__init__()
        self.run_op = run_op
        self.calls: list[tuple[str, float]] = []

    async def fetch(self, **kwargs) -> EffectResult:
        self.calls.append((kwargs["url"], kwargs["timeout_seconds"]))
        if self.run_op:
            op = cast("Callable[[], Awaitable[object]] | None", kwargs.get("op"))
            if op is not None:
                await op()
        return EffectResult(kind=EffectKind.FETCH, outcome=EffectOutcome.SUCCESS)


def _context() -> JobAdmissionContext:
    return JobAdmissionContext.create(
        CrawlPolicy(), PolicyTier.model_defaults(), CrawlerSettings.from_mapping({})
    )


async def test_fetch_job_sitemap_candidates_delegates_to_container(monkeypatch):
    """fetch_job_sitemap_candidates must call container.fetch, not
    fetch_sitemap_urls directly (admission performs no origin I/O)."""
    context = _context()
    container = _FetchContainer(run_op=False)

    def _refuse_direct_fetch(fetcher, sitemap_url, timeout_seconds, *, host_policy, **_kwargs):
        raise AssertionError("admission called fetch_sitemap_urls directly")

    monkeypatch.setattr(admission, "fetch_sitemap_urls", _refuse_direct_fetch)

    result = await fetch_job_sitemap_candidates("https://example.test/root", context, container)

    assert result == []
    assert container.calls == [("https://example.test/sitemap.xml", context.timeout_seconds)]


async def test_candidates_arrive_only_after_container_fetch_returns(monkeypatch):
    """Candidates are admitted only after the container-bounded fetch returns."""
    context = _context()
    container = _FetchContainer(run_op=True)

    fetched: list[str] = []

    async def _fake_fetch(fetcher, sitemap_url, timeout_seconds, *, host_policy, **_kwargs):
        fetched.append(sitemap_url.raw)
        return {(canonical("http://example.test/a"), None)}

    monkeypatch.setattr(admission, "fetch_sitemap_urls", _fake_fetch)

    result = await fetch_job_sitemap_candidates("https://example.test/root", context, container)

    assert result == ["http://example.test/a"]
    assert container.calls == [("https://example.test/sitemap.xml", context.timeout_seconds)]
    assert fetched == ["https://example.test/sitemap.xml"]
