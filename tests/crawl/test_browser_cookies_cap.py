import pytest

pytestmark = pytest.mark.browser

"""Browser cookie cache must be bounded (#6)."""


from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies.browser import _BROWSER_COOKIES_MAX, _store_browser_cookies, get_browser_cookies_for


def _with_clean_cache(fn):
    saved = dict(browser_mod._BROWSER_COOKIES_BY_DOMAIN)
    browser_mod._BROWSER_COOKIES_BY_DOMAIN.clear()
    try:
        fn()
    finally:
        browser_mod._BROWSER_COOKIES_BY_DOMAIN.clear()
        browser_mod._BROWSER_COOKIES_BY_DOMAIN.update(saved)

def test_cookie_cache_capped():
    def run():
        for i in range(600):
            _store_browser_cookies(f"d{i}.example", f"k{i}=v")
        assert len(browser_mod._BROWSER_COOKIES_BY_DOMAIN) == _BROWSER_COOKIES_MAX
        # Oldest entries were evicted; newest retained.
        assert get_browser_cookies_for("d599.example") == "k599=v"
        assert get_browser_cookies_for("d0.example") is None

    _with_clean_cache(run)

def test_lru_keeps_recently_used():
    def run():
        for i in range(_BROWSER_COOKIES_MAX + 50):
            _store_browser_cookies(f"d{i}.example", f"k{i}=v")
        # Touch an older-but-not-oldest entry; it should survive the next eviction.
        get_browser_cookies_for(f"d{_BROWSER_COOKIES_MAX}.example")
        _store_browser_cookies("new.example", "new=v")
        assert get_browser_cookies_for(f"d{_BROWSER_COOKIES_MAX}.example") == f"k{_BROWSER_COOKIES_MAX}=v"

    _with_clean_cache(run)

