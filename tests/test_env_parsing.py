"""Env-int parsing contract: a typo'd numeric env var must not kill the boot.

Two module-import sites parsed ints bare (``int(os.environ.get(...))``):
a typo'd value crashed the whole service at import. ``get_env_int`` logs
and falls back to the default, with optional clamping.
"""

from __future__ import annotations

import logging

from app.validation import get_env_int


class TestGetEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 42) == 42

    def test_valid_value_parses(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", "7")
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 42) == 7

    def test_typo_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", "four")
        with caplog.at_level(logging.WARNING):
            assert get_env_int("BROWSER_CRAWLER_TEST_INT", 42) == 42
        assert caplog.records, "expected an invalid_int_env warning"
        assert caplog.records[0].env_name == "BROWSER_CRAWLER_TEST_INT"
        assert caplog.records[0].fallback == 42

    def test_clamps_to_bounds(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", "0")
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 4, lo=1) == 1
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", "9999")
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 4, hi=100) == 100
