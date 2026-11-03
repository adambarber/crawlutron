"""The retry's attempts are recorded ONCE (#C9).

retry_cookies routes through execute, so the retry result carries its own
attempt; if the retry path ALSO extends state.attempts again, the ladder's
attempted_strategies counts inflate -- diagnostics lie about how much was tried.
"""

from crawlutron.transport import browser_cookies
from tests._cookie_retry_chain import cffi_retry_chain, cookie_oracle


async def test_retry_attempts_appear_exactly_once():
    """Ladder [http, cffi, browser] with the cookie retry firing: every strategy
    name must appear exactly once in attempted_strategies -- no double-count."""
    browser_cookies.store_browser_cookies("example.com", "incap_ses=abc; visid=def")

    result = await cffi_retry_chain(on_cffi=cookie_oracle("incap_ses"))

    assert result.status == "success"
    names = [a.strategy for a in result.attempted_strategies]
    assert len(names) == len(set(names)), f"a strategy was counted twice in the diagnostics: {names}"
