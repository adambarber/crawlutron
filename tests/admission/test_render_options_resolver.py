"""Resolve_render_options chokepoint.

Exactly one place now decides effective RenderOptions; the two consumers split:
capture is the pipeline's on the crawl path, payload is the caller's include_html.
"""

import pytest

from app.admission.policy import read_policy
from app.admission.render_options import policy_wants_html, resolve_render_options
from app.api.render import render_options_for
from app.config.settings import CrawlerSettings
from app.models.render_options import RenderOptions
from app.transport.browser_result import browser_strategy_result
from app.vocabulary.limits import MAX_HEADER_VALUE_BYTES, MAX_HTTP_TIMEOUT_SECONDS, MAX_RETRIES
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
        profile = {"viewport": "mobile"}
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
        and a pooled renderer holds -- an absurd user_agent is not accepted.
        """
        with pytest.raises(ValueError, match="browser_profile user_agent"):
            resolve_render_options(
                {"browser_profile": {"user_agent": "A" * (MAX_HEADER_VALUE_BYTES + 1)}}, {}
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
            {"browser_profile": {"user_agent": "A" * MAX_HEADER_VALUE_BYTES}}, {}
        )

        assert options.browser_profile["user_agent"] == "A" * MAX_HEADER_VALUE_BYTES


class TestRenderEndpointConsumesResolver:
    """``/v1/render`` is a resolver consumer, keeping its caller-controlled capture."""

    def test_render_options_forwards_browser_profile(self):
        options = render_options_for(
            {"options": {"timeout_seconds": 3, "browser_profile": {"lazy_scroll_steps": 2}}},
            _settings(),
        )
        assert options.timeout_seconds == 3
        assert options.browser_profile == {"lazy_scroll_steps": 2}

    def test_render_options_uses_settings_defaults(self):
        options = render_options_for({"options": {}}, _settings())
        assert options.timeout_seconds == 11
        # 4, not 7: 7 is above the deployment's retry ceiling, so it is now refused there. The per-job
        # equivalent of "7 is a real value" is test_caller_retry_count_is_bounded_per_job.
        assert options.max_retries == 4
        assert options.respect_robots is True
        assert options.browser_profile == {}

    def test_render_caller_still_controls_capture(self):
        options = render_options_for({"options": {"include_html": False}}, _settings())
        assert options.include_html is False

    def test_invalid_options_shape_still_rejected(self):
        with pytest.raises(ValueError, match="invalid_options"):
            render_options_for({"options": "nope"}, _settings())

    def test_the_endpoint_clamps_absurd_caller_knobs(self):
        """#444: the /v1/render funnel ceilinged its caller's knobs -- the door
        a raw-dict caller reaches, so the clamp must sit on this path too.
        """
        options = render_options_for({"options": {"timeout_seconds": 10**9, "max_retries": 999}}, _settings())

        assert options.timeout_seconds == MAX_HTTP_TIMEOUT_SECONDS
        assert options.max_retries == MAX_RETRIES

    def test_the_endpoint_refuses_an_oversized_profile_string(self):
        """#444: the oversized-string refusal reaches the endpoint funnel."""
        with pytest.raises(ValueError, match="browser_profile user_agent"):
            render_options_for(
                {"options": {"browser_profile": {"user_agent": "B" * (MAX_HEADER_VALUE_BYTES + 1)}}},
                _settings(),
            )


def _browser_policy() -> dict[str, object]:
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
