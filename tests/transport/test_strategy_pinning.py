"""Tests for per-job per-domain strategy pinning (escalation ladder + pinning)."""

from __future__ import annotations

from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.orchestration.strategy_chain import starting_strategies
from crawlutron.transport.scheduler import DomainScheduler
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_CFFI_IMPERSONATE, HTTP_DECLARED_CRAWLER

# The policy ladder as the enum members `starting_strategies` validates, not as the bare name strings.
LADDER: list[CrawlStrategy] = [
    CrawlStrategy(HTTP_DECLARED_CRAWLER),
    CrawlStrategy(HTTP_CFFI_IMPERSONATE),
    CrawlStrategy(BROWSER_HEADLESS),
]


def _scheduler() -> DomainScheduler:
    return DomainScheduler(global_limit=10, per_domain_limit=2, delay_seconds=0.1)


class TestStrategyPinning:
    """Tests for strategy pinning in DomainScheduler."""

    def test_pinned_strategy_get_set(self) -> None:
        """Test getting and setting pinned strategy."""
        scheduler = _scheduler()
        # Initially no pin
        assert scheduler.get_pinned_strategy("example.com") is None
        # Set a pin
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert scheduler.get_pinned_strategy("example.com") == BROWSER_HEADLESS
        # Different domain has no pin
        assert scheduler.get_pinned_strategy("other.com") is None

    def test_pin_overwrites(self) -> None:
        """Test that setting a pin overwrites the previous one."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", HTTP_CFFI_IMPERSONATE)
        assert scheduler.get_pinned_strategy("example.com") == HTTP_CFFI_IMPERSONATE
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert scheduler.get_pinned_strategy("example.com") == BROWSER_HEADLESS

    def test_expired_pin_is_no_longer_consulted(self) -> None:
        """A pinned strategy expires after its TTL, so a domain pins that later
        loses that capability re-evaluates rather than staying on a stale pin."""
        times = [1_000.0]
        scheduler = DomainScheduler(
            global_limit=10,
            per_domain_limit=2,
            delay_seconds=0.1,
            clock=lambda: times[0],
        )
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert scheduler.get_pinned_strategy("example.com") == BROWSER_HEADLESS

        times[0] += 6 * 60  # past the pin TTL: the stale pin re-evaluates
        assert scheduler.get_pinned_strategy("example.com") is None


class TestExecuteCrawlChainPinningFailureUnpin:
    """A strategy that fails with a capacity signal is unpinned so the fallback
    strategy is tried instead of a per-fetch failure loop (#303)."""

    async def test_browser_pool_exhaustion_unpins_and_falls_back_to_http(self) -> None:
        """When the pinned browser strategy exhausts its pool, the chain clears the
        pin and serves the page over the HTTP fallback."""
        from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
        from tests._browser_doubles import _fake_browser
        from tests._chain_policies import chain_policy
        from tests._fetch_doubles import FetcherDouble
        from tests._fetcher_factories import run_chain

        http_fetcher = FetcherDouble(lambda _url, _sent_headers, *_: (200, b"<html>ok</html>", "text/html"))
        scheduler = DomainScheduler(global_limit=8, per_domain_limit=8, delay_seconds=0.0)
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)

        result = await run_chain(
            fetcher=http_fetcher,
            scheduler=scheduler,
            policy=chain_policy(HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS),
            browser_worker=_fake_browser(status="failed", classification="browser_pool_exhausted"),
        )

        # The HTTP fallback served the page, and the browser-only pin is gone
        # (unpinned on the capacity failure, then re-pinned to the HTTP win).
        assert result.status == "success"
        assert scheduler.get_pinned_strategy("example.com") != BROWSER_HEADLESS


class TestExecuteCrawlChainPinning:
    """Where ``starting_strategies`` puts the ladder start.

    These call the production slice rather than restating it; a restatement is
    a tautology that keeps passing when production's pinning is deleted.
    """

    def test_chain_starts_at_pinned_strategy(self) -> None:
        """When a domain is pinned, the chain should start at that strategy."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert starting_strategies(LADDER, scheduler, "example.com") == [BROWSER_HEADLESS]

    def test_chain_starts_at_cffi_when_pinned(self) -> None:
        """When pinned to CFFI, chain should start at CFFI (skip HTTP_DECLARED_CRAWLER)."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", HTTP_CFFI_IMPERSONATE)
        assert starting_strategies(LADDER, scheduler, "example.com") == [
            HTTP_CFFI_IMPERSONATE,
            BROWSER_HEADLESS,
        ]

    def test_no_pin_starts_at_beginning(self) -> None:
        """When no pin, chain starts at the beginning."""
        scheduler = _scheduler()
        assert starting_strategies(LADDER, scheduler, "example.com") == LADDER

    def test_pin_not_in_policy_starts_at_beginning(self) -> None:
        """When pinned strategy is not in policy, chain starts at beginning."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        # Policy without browser (e.g., HTTP-only job)
        http_only = [CrawlStrategy(HTTP_DECLARED_CRAWLER), CrawlStrategy(HTTP_CFFI_IMPERSONATE)]
        assert starting_strategies(http_only, scheduler, "example.com") == http_only

    def test_no_scheduler_starts_at_beginning(self) -> None:
        """With no scheduler there is no pin to honour."""
        assert starting_strategies(LADDER, None, "example.com") == LADDER


class TestSchedulerPinningIsolation:
    """Verify pinning is per-job (scheduler instance) and doesn't leak."""

    def test_pins_are_per_scheduler_instance(self) -> None:
        """Pins in one scheduler should not affect another."""
        scheduler1 = DomainScheduler(
            global_limit=10,
            per_domain_limit=2,
            delay_seconds=0.1,
        )
        scheduler2 = DomainScheduler(
            global_limit=10,
            per_domain_limit=2,
            delay_seconds=0.1,
        )

        scheduler1.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert scheduler1.get_pinned_strategy("example.com") == BROWSER_HEADLESS
        assert scheduler2.get_pinned_strategy("example.com") is None

    def test_pins_are_per_domain(self) -> None:
        """Pins for one domain should not affect another."""
        scheduler = DomainScheduler(
            global_limit=10,
            per_domain_limit=2,
            delay_seconds=0.1,
        )

        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert scheduler.get_pinned_strategy("example.com") == BROWSER_HEADLESS
        assert scheduler.get_pinned_strategy("other.com") is None
