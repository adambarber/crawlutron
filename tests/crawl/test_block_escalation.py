"""#7 Auto-escalation: a blocked/challenge page must not be returned as a
successful result; the engine must try the next strategy in the chain
(e.g. direct HTTP -> curl_cffi TLS impersonation -> headless browser)."""

import pytest
from tests._assertions import assert_result_success
from tests._fetcher_factories import run_chain, scripted_fetcher, sequenced_fetcher

from app.constants import (
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)


def escalate(base_policy: dict) -> dict:
    """``base_policy`` with the two-step ladder that can answer a block."""
    return dict(base_policy, strategies=[HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE])


class TestBlockAutoEscalation:
    @pytest.mark.parametrize(
        ("challenge_body", "real_body", "blocked_classification"),
        [
            pytest.param(
                b"<html><body>Please complete the captcha to continue</body></html>",
                b"<html><body><a href='/p'>real documentation</a></body></html>",
                "captcha_detected",
                id="captcha",
            ),
            pytest.param(
                b"<html><body>Checking your browser - cf-browser-verification</body></html>",
                b"<html><body>Protected content here</body></html>",
                "bot_challenge_detected",
                id="cloudflare",
            ),
            # The #7 regression guard: a busy CAPTCHA interstitial still carries
            # text, so quality_score may be > 0 and escalation must still happen.
            pytest.param(
                b"<html><body><h1>Verify you are human</h1><p>Some boilerplate text.</p></body></html>",
                b"<html><body>The actual article body.</body></html>",
                "captcha_detected",
                id="captcha-with-content",
            ),
        ],
    )
    async def test_challenge_page_escalates_to_next_strategy(
        self, base_policy, challenge_body, real_body, blocked_classification
    ):
        # First strategy hits a challenge page; second (escalated) gets real
        # content. Stated as a sequence so the transition is the fixture. The
        # three bodies are three shapes of the same page (plain CAPTCHA, provider
        # interstitial, CAPTCHA carrying text), so the escalation rule is stated
        # once and the data says which page it was proved on.
        fetcher = sequenced_fetcher(
            [
                (200, challenge_body, "text/html"),
                (200, real_body, "text/html"),
            ]
        )

        result = await run_chain(fetcher=fetcher, policy=escalate(base_policy))
        assert_result_success(result, classification="rendered")
        assert result.classification != blocked_classification
        assert result.selected_strategy == HTTP_CFFI_IMPERSONATE
        assert len(result.attempted_strategies) == 2
        # The first attempt is recorded as blocked; the second succeeded.
        assert result.attempted_strategies[0].classification == blocked_classification
        assert result.attempted_strategies[1].status == "success"

    async def test_blocked_single_strategy_reports_blocked(self, base_policy):
        fetcher = scripted_fetcher(
            body=b"<html><body>Access denied - please sign in to continue</body></html>"
        )

        result = await run_chain(fetcher=fetcher, policy=base_policy)
        # With no further strategy to escalate to, the blocked result is returned.
        assert result.status == "blocked"
        assert result.classification == "login_required"
        assert result.selected_strategy is None
        assert len(result.attempted_strategies) == 1
