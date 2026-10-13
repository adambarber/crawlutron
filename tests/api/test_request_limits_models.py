"""Action keys are closed and caller knobs are ceilinged."""

import pytest
from pydantic import ValidationError

from app.models.crawl_strategy import CrawlStrategy


class TestBrowserActionKeysAreClosed:
    """The action *type* vocabulary is closed. The keys inside an action are not.

    ``_close_browser_profile_vocabulary`` refuses a top-level key that is not in
    the table and an action ``type`` that is not in the vocabulary, then hands
    each action dict to the chain, which reads four keys out of it. Nothing
    checks those. So::

        {"type": "fill", "selector": "#q", "val": "hi"}

    is accepted, and the chain fills an empty string -- a 200 on a page that
    loaded with no interaction performed, which is exactly the silent no-op the
    ``type`` check exists to prevent, one level down.

    This is the ``state`` key again (#61) and the ``accept_language``
    description (#64): the same failure, at the third place it can hide.
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

        One test over the table rather than one test per spelling, because the
        two reported cases are the same defect -- a key the consumer never reads
        -- and a second copy of the assertion is a second place to forget one.
        """
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="action keys not permitted"):
            CrawlPolicy(browser_profile={"actions": [action]})

    def test_a_permitted_action_still_validates(self):
        from app.models.crawl_policy import CrawlPolicy

        for action in (
            {"type": "click", "selector": "#x"},
            {"type": "fill", "selector": "#q", "value": "hi"},
            {"type": "wait", "selector": "#x", "timeout": 1000},
            {"type": "wait_seconds", "seconds": 2},
            {"type": "scroll_bottom"},
        ):
            policy = CrawlPolicy(browser_profile={"actions": [action]})
            assert policy.browser_profile["actions"][0]["type"] == action["type"]

    def test_the_chain_reads_exactly_the_declared_keys(self):
        """The drift guard: a reader added without a declaration fails here.

        Read off the chain's own ``action.get(...)`` calls rather than restated,
        because the whole failure of this issue is a key the consumer reads and
        the producer never named. Restating the list here would just be a second
        place to forget.
        """
        import ast

        from app.vocabulary.browser import BROWSER_ACTION_KEYS
        from tests.chokepoint_walks import TRANSPORT_DIR, attribute_calls, string_argument

        source = (TRANSPORT_DIR / "browser_driver.py").read_text()
        read: set[str] = set()
        for node, func in attribute_calls(ast.parse(source)):
            if func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "action":
                read.add(string_argument(node))
        read.discard(None)

        assert read == set(BROWSER_ACTION_KEYS), (
            f"the chain and the table disagree: chain-only={read - set(BROWSER_ACTION_KEYS)}, "
            f"table-only={set(BROWSER_ACTION_KEYS) - read}"
        )


class TestCallerKnobsBehindClosedKeysAreCeilinged:
    """A closed key set answers *which* keys exist. It is silent about how large their values may be.

    The browser profile's vocabulary was closed in aa46380 and the ceiling
    question was not asked, so every knob behind the closed keys took whatever
    the caller sent. Four of them, each with a place in the code that already
    said the value should be bounded:

    - ``_profile_int`` and ``_profile_float`` return ``max(0, value)`` with no
      upper clamp, so ``lazy_scroll_steps: 10**12`` is accepted.
    - ``_lazy_load_scroll`` is called with no budget, immediately below
      ``_run_action_chain``, which is passed ``budget_seconds=navigation_timeout``
      and whose docstring says production must pass a budget. The one call site
      documented as needing a ceiling is the one without one.
    - ``cookie_harvest_timeout_seconds`` is the ``wait_for`` timeout, and the
      comment at that call site says the harvest must not hold the pooled
      renderer for the per-URL budget.
    - ``screenshot_selectors`` has no ``max_length``; each entry appends a
      base64 PNG to the result metadata, so it is a response-size knob too.

    All four returned 200. The 900 s chain budget bounds wall clock and none of
    these.
    """

    #: Longest scroll a lazy-load pass will take, in steps. Deep pages still load in a few dozen; this is past
    #: the point of usefulness, not the point of cost.
    MAX_LAZY_SCROLL_STEPS = 50
    #: Longest per-step scroll pause, in seconds.
    MAX_LAZY_SCROLL_DELAY_SECONDS = 5.0
    #: Longest cookie-harvest wait, in seconds. The harvest runs in a finally block on a pooled renderer, so
    #: this is a hold on a shared resource.
    MAX_COOKIE_HARVEST_TIMEOUT_SECONDS = 5.0
    #: Most element screenshots in one render. Each is a full base64 PNG in the result metadata, so this
    #: bounds the response as well as the render.
    MAX_SCREENSHOT_SELECTORS = 16

    def test_scroll_steps_over_the_ceiling_are_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="lazy_scroll_steps"):
            CrawlPolicy(browser_profile={"lazy_scroll_steps": 10**12})

    def test_a_scroll_delay_over_the_ceiling_is_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="lazy_scroll_delay_seconds"):
            CrawlPolicy(browser_profile={"lazy_scroll_delay_seconds": 3600.0})

    def test_a_cookie_harvest_timeout_over_the_ceiling_is_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError, match="cookie_harvest_timeout_seconds"):
            CrawlPolicy(browser_profile={"cookie_harvest_timeout_seconds": 10**9})

    def test_too_many_screenshot_selectors_are_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        over = [f"#{index}" for index in range(self.MAX_SCREENSHOT_SELECTORS + 1)]
        with pytest.raises(ValidationError, match="screenshot_selectors"):
            CrawlPolicy(browser_profile={"screenshot_selectors": over})

    def test_a_value_at_each_ceiling_is_accepted(self):
        """The ceilings are the boundary, not a lower bound the caller cannot reach."""
        from app.models.crawl_policy import CrawlPolicy

        policy = CrawlPolicy(
            browser_profile={
                "lazy_scroll_steps": self.MAX_LAZY_SCROLL_STEPS,
                "lazy_scroll_delay_seconds": self.MAX_LAZY_SCROLL_DELAY_SECONDS,
                "cookie_harvest_timeout_seconds": self.MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
                "screenshot_selectors": [f"#{i}" for i in range(self.MAX_SCREENSHOT_SELECTORS)],
            }
        )
        assert policy.browser_profile["lazy_scroll_steps"] == self.MAX_LAZY_SCROLL_STEPS

    def test_the_readers_clamp_too_even_when_the_model_is_bypassed(self):
        """Defence in depth, asserted at the call path rather than at a helper.

        The model is a boundary, not the only door: by the time a profile reaches
        a renderer it is a dict, so the validator is not in the path any more.
        The readers are what actually bound the work, so they are what is
        asserted -- through ``_scroll_config_for`` and ``_harvest_timeout_for``,
        which is how the renderer calls them, rather than through
        ``_profile_int`` with no ceiling, whose contract is deliberately to pass a
        value through.
        """
        from app.transport.browser_driver import BrowserDriver
        from app.vocabulary.limits import (
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
        assert driver.scroll_config_for({"lazy_scroll_steps": 7}) == (7, 0.5)
        assert driver.harvest_timeout_for({}) == 2.0

        # Garbage still falls back to the default rather than raising, as before.
        assert driver.scroll_config_for({"lazy_scroll_steps": "not a number"}) == (3, 0.5)
        assert driver.scroll_config_for({"lazy_scroll_steps": -5}) == (0, 0.5)


class TestStrategyLadderIsBounded:
    """``strategies`` is the outbound fan-out per URL, and it was the one collection left uncapped.

    ``urls`` has carried ``max_length=500`` and ``headers`` ``max_length=64`` all
    along. ``strategies`` had ``min_length=1`` and nothing above it, which reads
    as an oversight rather than a decision: it is a list of outbound network
    attempts, and the ladder runs one per entry.

    The ceiling is a ceiling and not a dedupe, deliberately. A duplicate is
    usually pure waste -- if a strategy comes back blocked, the same strategy
    blocks again, and a usable one returns immediately -- but not always: a
    browser render mints a session cookie that a later ``HTTP_CFFI_IMPERSONATE``
    entry replays, so a non-blocked-but-unusable first attempt can leave the jar
    in a state that makes its own repetition succeed. Dedupe would have thrown
    that path away. A ceiling bounds the repetition instead of declaring all of
    it worthless.
    """

    #: Most ladder entries in one request. The vocabulary is three strategies and cookie replay needs a repeat
    #: or two after a browser render, so this is several times more than any useful ladder.
    MAX_STRATEGIES = 16

    def test_a_ladder_at_the_ceiling_is_accepted(self):
        from app.models.crawl_policy import CrawlPolicy

        ladder = [CrawlStrategy.HTTP_DECLARED_CRAWLER] * self.MAX_STRATEGIES
        assert len(CrawlPolicy(strategies=ladder).strategies) == self.MAX_STRATEGIES

    def test_a_ladder_over_the_ceiling_is_refused(self):
        from app.models.crawl_policy import CrawlPolicy

        over = [CrawlStrategy.HTTP_DECLARED_CRAWLER] * (self.MAX_STRATEGIES + 1)
        with pytest.raises(ValidationError, match="at most"):
            CrawlPolicy(strategies=over)

    def test_an_empty_ladder_is_still_refused(self):
        """The lower bound is unchanged: an empty ladder has no strategy to run."""
        from app.models.crawl_policy import CrawlPolicy

        with pytest.raises(ValidationError):
            CrawlPolicy(strategies=[])

    def test_the_ceiling_is_a_ceiling_on_work_not_on_distinct_strategies(self):
        """A repetition the caller means is allowed, up to the ceiling.

        Pins the distinction the ceiling rests on, so a later reader does not
        "fix" this by deduping and break cookie replay.
        """
        from app.models.crawl_policy import CrawlPolicy

        repeated = [
            CrawlStrategy.BROWSER_HEADLESS,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
            CrawlStrategy.HTTP_CFFI_IMPERSONATE,
        ]
        assert len(CrawlPolicy(strategies=repeated).strategies) == 3
