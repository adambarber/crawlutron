"""#7 Auto-escalation: a blocked/challenge page must not be returned as a
successful result; the engine must try the next strategy in the chain
(e.g. direct HTTP -> curl_cffi TLS impersonation -> headless browser)."""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "bc-block-test.jsonl"))

from _testkit import assert_result_success, make_fetch

from app.constants import (
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)
from app.crawl.engine import _execute_crawl_chain
from app.validation import HostPolicy


class TestBlockAutoEscalation:
    async def _run_chain(self, strategies, fetcher, base_policy, **kwargs):
        return await _execute_crawl_chain(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            policy=dict(base_policy, strategies=list(strategies)),
            http_fetcher=fetcher,
            cffi_fetcher=fetcher,
            browser_worker=None,
            host_policy=HostPolicy.unrestricted(),
            **kwargs,
        )

    async def test_captcha_page_escalates_to_next_strategy(self, base_policy):
        calls = {"n": 0}

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            calls["n"] += 1
            if calls["n"] == 1:
                # First strategy hits a CAPTCHA challenge.
                return make_fetch(body=b"<html><body>Please complete the captcha to continue</body></html>")
            # Second strategy (escalated) gets real content.
            return make_fetch(body=b"<html><body><a href='/p'>real documentation</a></body></html>")

        result = await self._run_chain([HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE], fetcher, base_policy)
        assert_result_success(result, classification="rendered")
        assert result.classification != "captcha_detected"
        assert result.selected_strategy == HTTP_CFFI_IMPERSONATE
        assert len(result.attempted_strategies) == 2
        # The first attempt is recorded as blocked; the second succeeded.
        assert result.attempted_strategies[0].classification == "captcha_detected"
        assert result.attempted_strategies[1].status == "success"

    async def test_cloudflare_challenge_escalates(self, base_policy):
        calls = {"n": 0}

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            calls["n"] += 1
            if calls["n"] == 1:
                return make_fetch(
                    body=b"<html><body>Checking your browser - cf-browser-verification</body></html>"
                )
            return make_fetch(body=b"<html><body>Protected content here</body></html>")

        result = await self._run_chain([HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE], fetcher, base_policy)
        assert result.status == "success"
        assert result.selected_strategy == HTTP_CFFI_IMPERSONATE
        assert result.attempted_strategies[0].classification == "bot_challenge_detected"

    async def test_blocked_single_strategy_reports_blocked(self, base_policy):
        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            return make_fetch(body=b"<html><body>Access denied - please sign in to continue</body></html>")

        result = await self._run_chain([HTTP_DECLARED_CRAWLER], fetcher, base_policy)
        # With no further strategy to escalate to, the blocked result is returned.
        assert result.status == "blocked"
        assert result.classification == "login_required"
        assert result.selected_strategy is None
        assert len(result.attempted_strategies) == 1

    async def test_block_escalates_even_when_blocked_page_has_content(self, base_policy):
        # A busy CAPTCHA interstitial still carries some text, so quality_score
        # may be > 0; escalation must still happen (#7 regression guard).
        calls = {"n": 0}

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            calls["n"] += 1
            if calls["n"] == 1:
                return make_fetch(
                    body=b"<html><body><h1>Verify you are human</h1><p>Some boilerplate text.</p></body></html>"
                )
            return make_fetch(body=b"<html><body>The actual article body.</body></html>")

        result = await self._run_chain([HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE], fetcher, base_policy)
        assert_result_success(result)
        assert result.selected_strategy == HTTP_CFFI_IMPERSONATE
        assert result.attempted_strategies[0].classification == "captcha_detected"
