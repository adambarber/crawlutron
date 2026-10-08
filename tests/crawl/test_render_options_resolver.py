"""Resolve_render_options chokepoint.

Effective render options used to be decided in several places that had
already drifted: the engine's browser strategy hard-coded the capture
knobs while policy-derived reads respected the caller elsewhere, and the
``/v1/render`` builder silently dropped ``browser_profile``. Exactly one
place (``app/admission/render_options.py``) now turns request-side options +
server defaults into the effective ``RenderOptions``.

The knob has TWO consumers with different owners, and the split is pinned
here rather than argued per call site:
  * capture (what the browser worker extracts) — the caller legitimately
    controls this on ``/v1/render``; on the crawl path the PIPELINE owns
    it, because ``_browser_strategy_result`` feeds the captured HTML to
    markdown extraction and link discovery BEFORE the caller's output knob
    is applied to the spilled payload (disabling capture there would
    change results, not just save work);
  * payload (what survives into results) — always the caller's
    ``browser_profile.include_html``, read through ``policy_wants_html``.
"""

from __future__ import annotations

import pytest

from app.admission.policy import read_policy
from app.admission.render_options import policy_wants_html, resolve_render_options
from app.api.render import _render_options
from app.config.settings import CrawlerSettings
from app.transport.browser_result import browser_strategy_result
from tests._browser_doubles import _fake_browser
from tests._fetcher_factories import strategy_request

#: The two knobs the resolver reads off settings, set away from the dataclass
#: defaults so a merge assertion cannot pass by coincidence.
#:
#: MAX_RETRIES was 7 here. It is now 4, because 7 is above the deployment
#: ceiling (``MAX_RETRIES``): a deployment-wide default has the blast radius of
#: every caller, so it is capped at 5 and an operator asking for 7 is warned and
#: refused rather than silently handed a different number. 4 is still != the
#: dataclass default, so the merge assertion keeps its teeth. The per-job value
#: of 7 is legal and covered by ``test_caller_retry_count_is_bounded_per_job``;
#: the deployment-side rejection is pinned in
#: ``test_env_parsing.test_an_out_of_ceiling_deployment_value_is_refused``.
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


class TestRenderEndpointConsumesResolver:
    """``/v1/render`` is a resolver consumer, keeping its caller-controlled capture."""

    def test_render_options_forwards_browser_profile(self):
        options = _render_options(
            {"options": {"timeout_seconds": 3, "browser_profile": {"lazy_scroll_steps": 2}}},
            _settings(),
        )
        assert options.timeout_seconds == 3
        assert options.browser_profile == {"lazy_scroll_steps": 2}

    def test_render_options_uses_settings_defaults(self):
        options = _render_options({"options": {}}, _settings())
        assert options.timeout_seconds == 11
        # 4, not 7: 7 is above the deployment's retry ceiling, so it is now
        # refused there. The per-job equivalent of "7 is a real value" is
        # test_caller_retry_count_is_bounded_per_job.
        assert options.max_retries == 4
        assert options.respect_robots is True
        assert options.browser_profile == {}

    def test_render_caller_still_controls_capture(self):
        options = _render_options({"options": {"include_html": False}}, _settings())
        assert options.include_html is False

    def test_invalid_options_shape_still_rejected(self):
        with pytest.raises(ValueError, match="invalid_options"):
            _render_options({"options": "nope"}, _settings())


def _browser_policy() -> dict:
    return {
        "strategies": ["BROWSER_HEADLESS"],
        "limits": {"timeout_seconds": 11, "max_retries": 7, "max_discovered_links": 10},
        "browser_profile": {"include_html": False, "viewport": "mobile"},
    }


class TestEngineConsumesResolver:
    """The crawl path's capture knob is pipeline-owned; the payload knob is the caller's."""

    async def test_capture_pinned_and_profile_reaches_worker(self):
        captured: dict = {}
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
        # The caller asked for no HTML in the payload...
        assert policy_wants_html(read_policy(policy)) is False
        # ...but capture stays pipeline-pinned: the strategy reads the captured
        # HTML into extraction/discovery/diagnostics BEFORE anything strips it,
        # so honoring it here would silently degrade results, not save work.
        assert options.include_html is True
        assert options.include_text is True
        assert options.timeout_seconds == policy["limits"]["timeout_seconds"]
        assert options.max_retries == policy["limits"]["max_retries"]
        assert options.browser_profile == {"include_html": False, "viewport": "mobile"}
        # The captured HTML demonstrably fed the pipeline even though the
        # caller's payload knob is False.
        assert result.diagnostics["content_metrics"]["html_length"] > 0
