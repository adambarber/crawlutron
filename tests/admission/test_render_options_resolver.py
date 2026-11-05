"""Resolve_render_options chokepoint.

Exactly one place now decides effective RenderOptions; the two consumers split:
capture is the pipeline's on the crawl path, payload is the caller's include_html.
"""

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from crawlutron.admission.policy import read_policy
from crawlutron.admission.render_options import policy_wants_html, resolve_render_options
from crawlutron.api.render import render_options_for
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.json_value import JsonValue
from crawlutron.models.render_options import RenderOptions
from crawlutron.models.render_request import RenderRequest
from crawlutron.transport.browser_result import browser_strategy_result
from crawlutron.vocabulary.limits import (
    MAX_COOKIE_HARVEST_TIMEOUT_SECONDS,
    MAX_HEADER_VALUE_BYTES,
    MAX_HTTP_TIMEOUT_SECONDS,
    MAX_LAZY_SCROLL_DELAY_SECONDS,
    MAX_LAZY_SCROLL_STEPS,
    MAX_RETRIES,
)
from tests._browser_doubles import _fake_browser
from tests._fetcher_factories import strategy_request

#: The two knobs the resolver reads off settings, set away from the dataclass defaults so a merge assertion
#: cannot pass by coincidence.
SETTINGS_OVERRIDES = {
    "CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS": "11",
    "CRAWLUTRON_DEFAULT_MAX_RETRIES": "4",
}


def _settings() -> CrawlerSettings:
    return CrawlerSettings.from_mapping(SETTINGS_OVERRIDES)


def _request(options: object) -> RenderRequest:
    """The wire boundary the endpoint itself validates: options are declared or refused."""
    # ``options`` is deliberately untyped here: the boundary under test rejects
    # non-dict shapes, so the caller must be able to hand one over.
    bad_body: Callable[..., RenderRequest] = RenderRequest.model_validate
    return bad_body({"url": "http://example.com/", "options": options})


class TestResolveRenderOptions:
    """The resolver's merge semantics: caller wins, defaults fill the rest."""

    def test_caller_values_win_over_server_defaults(self):
        options = resolve_render_options(
            {
                "timeout_seconds": 5,
                "max_retries": 0,
                "include_html": False,
                "include_text": False,
                "respect_robots": False,
            },
            {"timeout_seconds": 11, "max_retries": 7, "respect_robots": True},
        )
        assert options.timeout_seconds == 5
        assert options.max_retries == 0
        assert options.include_html is False
        assert options.include_text is False
        assert options.respect_robots is False

    def test_absent_and_null_keys_fall_back_to_defaults(self):
        """Explicit None is "absent", not a crash: raw-dict callers send it."""
        options = resolve_render_options(
            {"timeout_seconds": None, "browser_profile": None},
            {"timeout_seconds": 11, "max_retries": 7},
        )
        assert options.timeout_seconds == 11
        assert options.max_retries == 7
        assert options.browser_profile == {}

    def test_unknown_keys_are_ignored(self):
        """The removed-wait_until contract: a stale client key must not break."""
        options = resolve_render_options({"wait_until": "load"}, {"timeout_seconds": 11})
        assert options.timeout_seconds == 11

    def test_browser_profile_is_carried(self):
        """Worker knobs must reach the worker, not die in the builder."""
        options = resolve_render_options({"browser_profile": {"viewport": "mobile"}}, {})
        assert options.browser_profile == {"viewport": "mobile"}

    def test_browser_profile_is_copied_defensively(self):
        profile: dict[str, JsonValue] = {"viewport": "mobile"}
        options = resolve_render_options({"browser_profile": profile}, {})
        profile["viewport"] = "desktop"
        assert options.browser_profile == {"viewport": "mobile"}

    def test_an_absurd_timeout_and_retry_count_are_clamped(self):
        """#444: the caller's render knobs are ceilinged at the shared constants.

        A stalling target under a 1e9-second timeout holds a pooled renderer
        (~700 MiB) for as long as it stalls, retried without bound.
        """
        options = resolve_render_options({"timeout_seconds": 1_000_000_000, "max_retries": 1_000_000_000}, {})

        assert options.timeout_seconds == MAX_HTTP_TIMEOUT_SECONDS
        assert options.max_retries == MAX_RETRIES

    def test_caller_values_under_the_ceilings_stand(self):
        """The clamp only bites above the ceiling; a clamp that pinned every
        value to the ceiling would pass the same assertions.
        """
        options = resolve_render_options({"timeout_seconds": 45, "max_retries": 2}, {})

        assert options.timeout_seconds == 45
        assert options.max_retries == 2

    def test_policy_defaults_pass_through_uncapped(self):
        """#444 scopes the clamp to the caller's side: the defaults side is
        operator/policy-owned and already ceilinged by its own validators
        (CrawlPolicyLimits allows up to MAX_JOB_MAX_RETRIES per job).
        """
        options = resolve_render_options(None, {"timeout_seconds": 30, "max_retries": 7})

        assert options.timeout_seconds == 30
        assert options.max_retries == 7

    def test_an_oversized_profile_string_is_refused(self):
        """#444: an unsized profile string is per-request memory a caller sets
        and a pooled renderer holds -- an absurd storage_state is not accepted.
        """
        with pytest.raises(ValueError, match="browser_profile storage_state"):
            resolve_render_options(
                {"browser_profile": {"storage_state": "A" * (MAX_HEADER_VALUE_BYTES + 1)}}, {}
            )

    def test_a_nested_oversized_action_string_is_refused(self):
        """#444: the cap walks nested shapes -- an action's selector rides the
        same profile and the same pooled renderer.
        """
        with pytest.raises(ValueError, match="browser_profile actions"):
            resolve_render_options(
                {
                    "browser_profile": {
                        "actions": [{"type": "click", "selector": "#" + "a" * (MAX_HEADER_VALUE_BYTES + 1)}]
                    }
                },
                {},
            )

    def test_a_profile_string_at_the_cap_is_accepted(self):
        """The cap is a ceiling, not a lower bound a caller cannot reach."""
        options = resolve_render_options(
            {"browser_profile": {"storage_state": "A" * MAX_HEADER_VALUE_BYTES}}, {}
        )

        assert options.browser_profile["storage_state"] == "A" * MAX_HEADER_VALUE_BYTES

    def test_an_unknown_profile_key_is_refused(self):
        """The resolver is the render path's validation boundary: the same
        vocabulary authority the crawl path answers to refuses here too -- a
        key nothing reads is inert, and inert means a caller mistake.
        """
        with pytest.raises(ValueError, match="bogus_key"):
            resolve_render_options({"browser_profile": {"bogus_key": 1}}, {})

    def test_an_over_ceiling_lazy_scroll_knob_is_refused(self):
        """The vocabulary refuses what the consumers would only clamp: a
        caller-side knob above its ceiling never crosses as the record.
        """
        with pytest.raises(ValueError, match="lazy_scroll_steps"):
            resolve_render_options({"browser_profile": {"lazy_scroll_steps": 999_999}}, {})

    @pytest.mark.parametrize("key", ["lazy_scroll_steps", "lazy_scroll_delay_seconds"])
    def test_a_non_numeric_string_knob_is_refused(self, key):
        """A string knob that parses no number is caller garbage: it refuses
        like every other off-boundary value instead of crossing raw and
        silently defaulting at the consumer.
        """
        with pytest.raises(ValueError, match=key):
            resolve_render_options({"browser_profile": {key: "not_a_number"}}, {})

    @pytest.mark.parametrize(
        ("key", "ceiling"),
        [
            ("lazy_scroll_steps", MAX_LAZY_SCROLL_STEPS),
            ("lazy_scroll_delay_seconds", MAX_LAZY_SCROLL_DELAY_SECONDS),
            ("cookie_harvest_timeout_seconds", MAX_COOKIE_HARVEST_TIMEOUT_SECONDS),
        ],
    )
    def test_caller_profile_knobs_at_the_ceiling_stand(self, key, ceiling):
        """The clamp pins the ceiling, not a lower number: values at the
        ceiling stand and values above it are refused, so the assertions
        cannot pass by coincidence in either direction.
        """
        profile = {key: ceiling}
        assert resolve_render_options({"browser_profile": profile}, {}).browser_profile == profile


class TestRenderEndpointConsumesResolver:
    """``/v1/render`` is a resolver consumer, keeping its caller-controlled capture."""

    def test_render_options_forwards_browser_profile(self):
        """The door refuses caller browser_profile in options (extra=forbid): the
        profile knob is policy-owned, carried on the crawl path, not this one.
        """
        with pytest.raises(ValidationError):
            _request({"browser_profile": {"lazy_scroll_steps": 2}})

    def test_render_options_uses_settings_defaults(self):
        options = render_options_for(_request({}), _settings())
        assert options.timeout_seconds == 11
        # 4, not 7: 7 is above the deployment's retry ceiling, so it is now refused there. The per-job
        # equivalent of "7 is a real value" is test_caller_retry_count_is_bounded_per_job.
        assert options.max_retries == 4
        assert options.respect_robots is True
        assert options.browser_profile == {}

    def test_render_caller_still_controls_capture(self):
        options = render_options_for(_request({"include_html": False}), _settings())
        assert options.include_html is False

    def test_invalid_options_shape_still_rejected(self):
        with pytest.raises(ValidationError):
            _request("nope")

    def test_the_endpoint_clamps_absurd_caller_knobs(self):
        """#444: the /v1/render funnel ceilinged its caller's knobs -- the door
        a validated caller reaches, so the clamp must sit on this path too.
        """
        options = render_options_for(_request({"timeout_seconds": 10**9, "max_retries": 999}), _settings())

        assert options.timeout_seconds == MAX_HTTP_TIMEOUT_SECONDS
        assert options.max_retries == MAX_RETRIES


def _browser_policy() -> dict[str, JsonValue]:
    return {
        "strategies": ["BROWSER_HEADLESS"],
        "limits": {"timeout_seconds": 11, "max_retries": 7, "max_discovered_links": 10},
        "browser_profile": {"include_html": False, "viewport": "mobile"},
    }


class TestEngineConsumesResolver:
    """The crawl path's capture knob is pipeline-owned; the payload knob is the caller's."""

    async def test_capture_pinned_and_profile_reaches_worker(self):
        captured: dict[str, object] = {}
        rendered = _fake_browser(
            html="<html><head><title>T</title></head><body>some page content</body></html>",
            text="some page content",
        )

        async def worker(job):
            captured["options"] = job.options
            return rendered(job)

        policy = _browser_policy()
        result = await browser_strategy_result(
            request=strategy_request("http://example.com/", policy, "BROWSER_HEADLESS"),
            browser_worker=worker,
        )
        options = captured["options"]
        assert isinstance(options, RenderOptions)
        # The caller asked for no HTML in the payload...
        assert policy_wants_html(read_policy(policy)) is False
        assert options.include_html is True
        assert options.include_text is True
        limits = policy["limits"]
        assert isinstance(limits, dict)
        assert options.timeout_seconds == limits["timeout_seconds"]
        assert options.max_retries == limits["max_retries"]
        assert options.browser_profile == {"include_html": False, "viewport": "mobile"}
        # The captured HTML demonstrably fed the pipeline even though the caller's payload knob is False.
        assert result.diagnostics["content_metrics"]["html_length"] > 0
