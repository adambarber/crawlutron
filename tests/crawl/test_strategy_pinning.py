"""Tests for per-job per-domain strategy pinning (escalation ladder + pinning)."""

from __future__ import annotations

from app.constants import (
    BROWSER_HEADLESS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)
from app.crawl.strategy_chain import _starting_strategies
from app.models.crawl_strategy import CrawlStrategy
from app.scheduler import DomainScheduler

# The policy ladder as the enum members `_starting_strategies` validates, not as
# the bare name strings. It takes `Iterable[CrawlStrategy]` because it calls
# `CrawlStrategy(member)` on each entry to reject anything the policy could not
# name; handing it strings worked only because the enum is a StrEnum, and the
# validation it performs is the thing under test here.
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


class TestExecuteCrawlChainPinning:
    """Where ``_starting_strategies`` puts the ladder start.

    These call the production slice rather than restating it. A restatement is
    a tautology: it keeps passing when production's pinning is deleted, which is
    exactly the regression this class exists to catch.
    """

    def test_chain_starts_at_pinned_strategy(self) -> None:
        """When a domain is pinned, the chain should start at that strategy."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        assert _starting_strategies(LADDER, scheduler, "example.com") == [BROWSER_HEADLESS]

    def test_chain_starts_at_cffi_when_pinned(self) -> None:
        """When pinned to CFFI, chain should start at CFFI (skip HTTP_DECLARED_CRAWLER)."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", HTTP_CFFI_IMPERSONATE)
        assert _starting_strategies(LADDER, scheduler, "example.com") == [
            HTTP_CFFI_IMPERSONATE,
            BROWSER_HEADLESS,
        ]

    def test_no_pin_starts_at_beginning(self) -> None:
        """When no pin, chain starts at the beginning."""
        scheduler = _scheduler()
        assert _starting_strategies(LADDER, scheduler, "example.com") == LADDER

    def test_pin_not_in_policy_starts_at_beginning(self) -> None:
        """When pinned strategy is not in policy, chain starts at beginning."""
        scheduler = _scheduler()
        scheduler.set_pinned_strategy("example.com", BROWSER_HEADLESS)
        # Policy without browser (e.g., HTTP-only job)
        http_only = [CrawlStrategy(HTTP_DECLARED_CRAWLER), CrawlStrategy(HTTP_CFFI_IMPERSONATE)]
        assert _starting_strategies(http_only, scheduler, "example.com") == http_only

    def test_no_scheduler_starts_at_beginning(self) -> None:
        """With no scheduler there is no pin to honour."""
        assert _starting_strategies(LADDER, None, "example.com") == LADDER


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
