"""Action keys are closed and caller knobs are ceilinged."""

import pytest
from pydantic import ValidationError

from crawlutron.models.browser_profile_record import BrowserProfileRecord
from crawlutron.models.crawl_strategy import CrawlStrategy


class TestBrowserActionKeysAreClosed:
    """Inside an action the key set is closed too: ``val`` for ``value`` is a
    key nothing reads, so the chain fills an empty string -- the silent no-op
    #61's ``state`` and #64's ``accept_language`` each hid, at a third spot.
    """

    @pytest.mark.parametrize(
        "action",
        [
            pytest.param({"type": "click", "selector": "#x", "url": "http://e/"}, id="stray-url"),
            pytest.param({"type": "fill", "selector": "#q", "val": "hi"}, id="misspelled-value"),
            pytest.param(
                {"type": "wait", "selector": "#x", "timeout": 1000, "wait_for": "#y"}, id="stray-wait-for"
            ),
        ],
    )
    def test_a_key_the_chain_does_not_read_is_refused(self, action):
        """``val`` for ``value`` is the reported case: the action then does nothing.
        One test over the table, not one per spelling -- a second copy is a
        second place to forget one key.
        """
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="action keys not permitted"):
            CrawlPolicy(browser_profile={"actions": [action]})

    def test_a_permitted_action_still_validates(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        for action in (
            {"type": "click", "selector": "#x"},
            {"type": "fill", "selector": "#q", "value": "hi"},
            {"type": "wait", "selector": "#x", "timeout": 1000},
            {"type": "wait_seconds", "seconds": 2},
            {"type": "scroll_bottom"},
        ):
            policy = CrawlPolicy(browser_profile={"actions": [action]})
            actions = policy.browser_profile["actions"]
            assert isinstance(actions, list) and isinstance(actions[0], dict)
            assert actions[0]["type"] == action["type"]

    def test_the_chain_reads_exactly_the_declared_keys(self):
        """Drift guard: read off the chain's own ``action.get`` calls, because the
        whole failure is a key the consumer reads and the producer never named.
        """
        import ast

        from crawlutron.vocabulary.browser import BROWSER_ACTION_KEYS
        from tests.chokepoint_walks import TRANSPORT_DIR, attribute_calls, string_argument

        source = (TRANSPORT_DIR / "browser_driver.py").read_text()
        read: set[str] = set()
        for node, func in attribute_calls(ast.parse(source)):
            if func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "action":
                key = string_argument(node)
                if key is not None:
                    read.add(key)

        assert read == set(BROWSER_ACTION_KEYS), (
            f"the chain and the table disagree: chain-only={read - set(BROWSER_ACTION_KEYS)}, "
            f"table-only={set(BROWSER_ACTION_KEYS) - read}"
        )


class TestCallerKnobsBehindClosedKeysAreCeilinged:
    """Closing the key set answered WHICH keys, not how large: four knobs took
    whatever the caller sent, so each now has a ceiling plus a clamping reader
    for when the model is bypassed (900 s chain budget bounds wall clock only).
    """

    def test_scroll_steps_over_the_ceiling_are_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="lazy_scroll_steps"):
            CrawlPolicy(browser_profile={"lazy_scroll_steps": 10**12})

    def test_a_scroll_delay_over_the_ceiling_is_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="lazy_scroll_delay_seconds"):
            CrawlPolicy(browser_profile={"lazy_scroll_delay_seconds": 3600.0})

    def test_a_cookie_harvest_timeout_over_the_ceiling_is_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="cookie_harvest_timeout_seconds"):
            CrawlPolicy(browser_profile={"cookie_harvest_timeout_seconds": 10**9})

    def test_too_many_screenshot_selectors_are_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy
        from crawlutron.vocabulary.limits import MAX_SCREENSHOT_SELECTORS

        over = [f"#{index}" for index in range(MAX_SCREENSHOT_SELECTORS + 1)]
        with pytest.raises(ValidationError, match="screenshot_selectors"):
            CrawlPolicy(browser_profile={"screenshot_selectors": over})

    def test_a_value_at_each_ceiling_is_accepted(self):
        """The ceilings are the boundary, not a lower bound the caller cannot reach."""
        from crawlutron.models.crawl_policy import CrawlPolicy
        from crawlutron.vocabulary.limits import (
            MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
            MAX_LAZY_SCROLL_DELAY_SECONDS,
            MAX_LAZY_SCROLL_STEPS,
            MAX_SCREENSHOT_SELECTORS,
        )

        policy = CrawlPolicy(
            browser_profile={
                "lazy_scroll_steps": MAX_LAZY_SCROLL_STEPS,
                "lazy_scroll_delay_seconds": MAX_LAZY_SCROLL_DELAY_SECONDS,
                "cookie_harvest_timeout_seconds": MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
                "screenshot_selectors": [f"#{i}" for i in range(MAX_SCREENSHOT_SELECTORS)],
            }
        )
        assert policy.browser_profile["lazy_scroll_steps"] == MAX_LAZY_SCROLL_STEPS

    def test_the_readers_clamp_too_even_when_the_model_is_bypassed(self):
        """Defence in depth, asserted at the call path: by render time the profile
        is a dict and the validator is gone, so the readers (never ``_profile_int``,
        whose contract is to pass a value through) are what actually bound work.
        """
        from crawlutron.transport.browser_driver import BrowserDriver
        from crawlutron.vocabulary.limits import (
            MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
            MAX_LAZY_SCROLL_DELAY_SECONDS,
            MAX_LAZY_SCROLL_STEPS,
        )

        driver = BrowserDriver(
            lazy_scroll_steps=3, lazy_scroll_delay_seconds=0.5, harvest_timeout_seconds=2.0
        )

        steps, delay = driver.scroll_config_for(
            {"lazy_scroll_steps": 10**12, "lazy_scroll_delay_seconds": 10**9}
        )
        assert (steps, delay) == (MAX_LAZY_SCROLL_STEPS, MAX_LAZY_SCROLL_DELAY_SECONDS)
        assert driver.harvest_timeout_for({"cookie_harvest_timeout_seconds": 10**9}) == (
            MAX_COOKIE_HARVEST_TIMEOUT_SECONDS
        )

        # The knob still moves inside the ceiling: a clamp that pinned every value to the ceiling would pass
        # the same assertions.
        profile: BrowserProfileRecord = {"lazy_scroll_steps": 7, "lazy_scroll_delay_seconds": 0.5}
        assert driver.scroll_config_for(profile) == (7, 0.5)
        assert driver.harvest_timeout_for({}) == 2.0

        # Garbage still falls back to the default rather than raising, as before.
        garbage: dict[str, object] = {"lazy_scroll_steps": "not a number", "lazy_scroll_delay_seconds": 0.5}
        assert driver.scroll_config_for(garbage) == (3, 0.5)
        negative: dict[str, object] = {"lazy_scroll_steps": -5, "lazy_scroll_delay_seconds": 0.5}
        assert driver.scroll_config_for(negative) == (0, 0.5)


class TestProfileStringsAreCeilinged:
    """#444: closed keys answered WHICH keys; string VALUES were unsized.

    An 8 KiB ceiling (#441's header-value number) bounds a caller's per-request
    memory at the boundary, with the resolver as the reader-side refusal.
    """

    #: Longest string anywhere in a browser_profile, in characters. The #441
    #: header-value ceiling: the common 8 KB interop limit origins accept.
    MAX_PROFILE_STRING = 8192

    def test_an_oversized_action_selector_is_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="browser_profile actions"):
            CrawlPolicy(
                browser_profile={
                    "actions": [{"type": "click", "selector": "#" + "a" * (self.MAX_PROFILE_STRING + 1)}]
                }
            )

    def test_an_oversized_top_level_string_is_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="browser_profile storage_state"):
            CrawlPolicy(browser_profile={"storage_state": "s" * (self.MAX_PROFILE_STRING + 1)})

    def test_an_oversized_screenshot_selector_is_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="browser_profile screenshot_selectors"):
            CrawlPolicy(browser_profile={"screenshot_selectors": ["#" + "s" * (self.MAX_PROFILE_STRING + 1)]})

    def test_a_string_at_the_ceiling_is_accepted(self):
        """The ceiling is the boundary, not a lower bound the caller cannot reach."""
        from crawlutron.models.crawl_policy import CrawlPolicy

        policy = CrawlPolicy(
            browser_profile={
                "storage_state": "s" * self.MAX_PROFILE_STRING,
                "actions": [{"type": "click", "selector": "c" * self.MAX_PROFILE_STRING}],
            }
        )
        assert len(policy.browser_profile["storage_state"]) == self.MAX_PROFILE_STRING


class TestStrategyLadderIsBounded:
    """The one collection left uncapped: the ladder runs one outbound attempt
    per entry, and the ceiling beats a dedupe because a browser render mints a
    session cookie a later CFFI entry replays -- repetition can be the win.
    """

    #: Most ladder entries in one request. The vocabulary is three strategies and cookie replay needs a repeat
    #: or two after a browser render, so this is several times more than any useful ladder.
    MAX_STRATEGIES = 16

    def test_a_ladder_at_the_ceiling_is_accepted(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        ladder = [CrawlStrategy.HTTP_DECLARED_CRAWLER] * self.MAX_STRATEGIES
        assert len(CrawlPolicy(strategies=ladder).strategies) == self.MAX_STRATEGIES

    def test_a_ladder_over_the_ceiling_is_refused(self):
        from crawlutron.models.crawl_policy import CrawlPolicy

        over = [CrawlStrategy.HTTP_DECLARED_CRAWLER] * (self.MAX_STRATEGIES + 1)
        with pytest.raises(ValidationError, match="at most"):
            CrawlPolicy(strategies=over)

    def test_an_empty_ladder_is_still_refused(self):
        """The lower bound is unchanged: an empty ladder has no strategy to run."""
        from crawlutron.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(strategies=[])

    def test_the_ceiling_is_a_ceiling_on_work_not_on_distinct_strategies(self):
        """A repetition the caller means is allowed, up to the ceiling.

        Pins the distinction the ceiling rests on, so a later reader does not
        "fix" this by deduping and break cookie replay.
        """
        from crawlutron.models.crawl_policy import CrawlPolicy

        repeated = [
            CrawlStrategy.BROWSER_HEADLESS,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
        ]
        assert len(CrawlPolicy(strategies=repeated).strategies) == 3


class TestRequestLimitConstantsArePinned:
    """One wire-contract pin per limit constant: the number on the wire is the
    named constant, not a restated literal that can drift from its home."""

    @pytest.mark.parametrize(
        "constant,value",
        [
            pytest.param("MAX_LAZY_SCROLL_STEPS", 50, id="lazy-scroll-steps"),
            pytest.param("MAX_LAZY_SCROLL_DELAY_SECONDS", 5.0, id="lazy-scroll-delay"),
            pytest.param("MAX_COOKIE_HARVEST_TIMEOUT_SECONDS", 5.0, id="cookie-harvest"),
            pytest.param("MAX_SCREENSHOT_SELECTORS", 16, id="screenshot-selectors"),
        ],
    )
    def test_the_vocabulary_limit_holds_its_wire_value(self, constant, value):
        import crawlutron.vocabulary.limits as limits

        assert getattr(limits, constant) == value

    @pytest.mark.parametrize(
        "module_name,constant,value",
        [
            pytest.param(
                "crawlutron.models.single_url_source_request", "MAX_URLS_PER_REQUEST", 500, id="urls"
            ),
            pytest.param(
                "crawlutron.models.crawl_request", "MAX_VALIDATORS_PER_REQUEST", 64, id="validators"
            ),
            pytest.param(
                "crawlutron.models.crawl_policy_vocabulary", "MAX_HEADERS_PER_REQUEST", 64, id="headers"
            ),
        ],
    )
    def test_the_model_limit_holds_its_wire_value(self, module_name, constant, value):
        import importlib

        module = importlib.import_module(module_name)
        assert getattr(module, constant) == value
