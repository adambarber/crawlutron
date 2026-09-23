"""Env-int parsing contract: a typo'd numeric env var must not kill the boot.

Two module-import sites parsed ints bare (``int(os.environ.get(...))``):
a typo'd value crashed the whole service at import. ``get_env_int`` logs
and falls back to the default, with optional clamping.
"""

from __future__ import annotations

import logging

from app.validation import get_env_float, get_env_int, parse_bool_env


class TestGetEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 42) == 42

    def test_valid_value_parses(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", "7")
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 42) == 7

    def test_env_readers_accept_surrounding_whitespace_consistently(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_BOOL", " FALSE ")
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", " 7 ")
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_FLOAT", " 0.25 ")
        assert parse_bool_env("BROWSER_CRAWLER_TEST_BOOL", True) is False
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 42) == 7
        assert get_env_float("BROWSER_CRAWLER_TEST_FLOAT", 0.5) == 0.25

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


class TestDefaultCoercionNeverBlowsUp:
    """The chokepoint must ALWAYS return a value of the declared numeric type,
    no matter what default literal a caller passes, and must never raise.

    Regression that motivated this: ``get_env_float(..., "0.5")`` returned the
    *string* ``"0.5"`` uncast, so ``limit * fraction`` became runaway string
    repetition -- a boot-time hang that froze every test importing the module.
    """

    def test_int_getter_coerces_string_default(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        value = get_env_int("BROWSER_CRAWLER_TEST_INT", "42")
        assert value == 42 and isinstance(value, int)

    def test_float_getter_coerces_string_default(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_FLOAT", raising=False)
        value = get_env_float("BROWSER_CRAWLER_TEST_FLOAT", "0.5")
        assert value == 0.5 and isinstance(value, float)

    def test_float_default_keeps_arithmetic_numeric(self, monkeypatch):
        # The exact regression: int * fraction must be float math, not str repeat.
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_FLOAT", raising=False)
        fraction = get_env_float("BROWSER_CRAWLER_TEST_FLOAT", "0.5")
        assert 100 * fraction == 50.0

    def test_int_getter_truncates_float_default(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        value = get_env_int("BROWSER_CRAWLER_TEST_INT", 4.7)
        assert value == 4 and isinstance(value, int)

    def test_uncoercible_default_falls_back_without_raising(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_FLOAT", raising=False)
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", None) == 0
        assert get_env_float("BROWSER_CRAWLER_TEST_FLOAT", None) == 0.0
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", object()) == 0

    def test_valid_env_value_still_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_FLOAT", "0.25")
        assert get_env_float("BROWSER_CRAWLER_TEST_FLOAT", "0.5") == 0.25
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_INT", "7")
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", "42") == 7

    def test_clamp_bounds_survive_string_default(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        assert get_env_int("BROWSER_CRAWLER_TEST_INT", 0, lo=1) == 1

    def test_rejects_infinite_env_value(self, monkeypatch):
        # ``float("inf")`` parses cleanly but ``limit * inf`` overflows at
        # int() — a malicious value must fall back, not wedge the boot.
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_FLOAT", "inf")
        assert get_env_float("BROWSER_CRAWLER_TEST_FLOAT", 0.5) == 0.5

    def test_rejects_nan_env_value(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_FLOAT", "nan")
        assert get_env_float("BROWSER_CRAWLER_TEST_FLOAT", 0.5) == 0.5

    def test_finite_huge_value_clamps_to_hi(self, monkeypatch):
        # 1e308 is finite (passes isfinite) yet ``limit * 1e308`` overflows;
        # only the declared bound keeps the arithmetic safe.
        monkeypatch.setenv("BROWSER_CRAWLER_TEST_FLOAT", "1e308")
        assert get_env_float("BROWSER_CRAWLER_TEST_FLOAT", 0.5, lo=0.0, hi=1.0) == 1.0

    def test_int_default_is_always_int_type(self, monkeypatch):
        monkeypatch.delenv("BROWSER_CRAWLER_TEST_INT", raising=False)
        value = get_env_int("BROWSER_CRAWLER_TEST_INT", 4)
        assert type(value) is int
