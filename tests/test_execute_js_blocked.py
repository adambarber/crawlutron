"""TDD tests for CRITICAL #2: arbitrary JavaScript execution via the API.

The browser action chain must never execute caller-supplied JavaScript. Two
layers are enforced:

1. ``BrowserRenderer._run_action_chain`` does not run ``execute_js`` actions.
2. ``CrawlPolicy`` rejects ``execute_js`` actions at the API boundary.

Both are tested here; the fix lives in ``app/crawl/strategies/browser.py`` and
``app/api_models.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.api_models import CrawlPolicy
from app.crawl.strategies.browser import BrowserRenderer


def _make_mock_page() -> MagicMock:
    page = MagicMock()
    for name in ("click", "fill", "hover", "query_selector", "wait_for_selector", "evaluate", "close"):
        setattr(page, name, AsyncMock())
    # scroll's query_selector should resolve to an element
    page.query_selector.return_value = MagicMock()
    return page


class TestExecuteJsBlocked:
    async def test_run_action_chain_does_not_execute_user_js(self):
        """A user-supplied ``execute_js`` script must never reach page.evaluate."""
        page = _make_mock_page()
        renderer = BrowserRenderer()
        await renderer._run_action_chain(
            page,
            [
                {"type": "execute_js", "script": "malicious()"},
                {"type": "scroll_bottom"},
            ],
        )
        called_scripts = [str(c.args[0]) for c in page.evaluate.call_args_list]
        assert "malicious()" not in called_scripts
        # The hardcoded, safe scroll_bottom evaluate must still run.
        assert any("window.scrollTo" in c for c in called_scripts)

    def test_crawl_policy_rejects_execute_js_action(self):
        """The API must reject execute_js actions before any crawl runs."""
        with pytest.raises(ValidationError):
            CrawlPolicy(browser_profile={"actions": [{"type": "execute_js", "script": "x()"}]})

    def test_crawl_policy_allows_benign_actions_and_profile(self):
        """Benign browser_profile keys/actions are still accepted."""
        policy = CrawlPolicy(
            browser_profile={
                "viewport": "mobile",
                "resource_mode": "reduced",
                "actions": [
                    {"type": "click", "selector": "#a"},
                    {"type": "wait_seconds", "seconds": 1},
                ],
            }
        )
        assert policy.browser_profile["viewport"] == "mobile"
        assert policy.browser_profile["actions"][0]["type"] == "click"
