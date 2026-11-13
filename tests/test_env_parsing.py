"""Env-int parsing contract: a typo'd numeric env var must not kill the boot.
Two import sites parsed ints bare (`int(os.environ.get(...))`); ``get_env_int``
logs and falls back to the default, with optional clamping.
"""

from __future__ import annotations

import logging

import pytest

from crawlutron.admission.policy import PolicyTier, read_policy, validate_wire_policy
from crawlutron.config.env import get_env_float, get_env_int, parse_bool_env
from crawlutron.config.settings import CrawlerSettings
from crawlutron.vocabulary.limits import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_RETRIES,
    MAX_HTTP_TIMEOUT_SECONDS,
    MAX_JOB_MAX_RETRIES,
    MAX_RETRIES,
)


class TestGetEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_INT", raising=False)
        assert get_env_int("CRAWLUTRON_TEST_INT", 42) == 42

    def test_valid_value_parses(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_TEST_INT", "7")
        assert get_env_int("CRAWLUTRON_TEST_INT", 42) == 7

    def test_env_readers_accept_surrounding_whitespace_consistently(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_TEST_BOOL", " FALSE ")
        monkeypatch.setenv("CRAWLUTRON_TEST_INT", " 7 ")
        monkeypatch.setenv("CRAWLUTRON_TEST_FLOAT", " 0.25 ")
        assert parse_bool_env("CRAWLUTRON_TEST_BOOL", True) is False
        assert get_env_int("CRAWLUTRON_TEST_INT", 42) == 7
        assert get_env_float("CRAWLUTRON_TEST_FLOAT", 0.5) == 0.25


class TestEnvDecisionMemo:
    """The one memoized env-decision primitive the twins must share (#621)."""

    def test_memo_class_exists_and_reuses_unchanged_env(self, monkeypatch):
        from crawlutron.config.env import EnvDecisionMemo

        monkeypatch.setenv("CRAWLUTRON_TEST_MEMO", "1")
        memo = EnvDecisionMemo("CRAWLUTRON_TEST_MEMO", default=False, logger_name=__name__, warning="w %r")
        assert memo.decide() is True
        assert memo.decide() is True

    def test_memo_honors_env_change_immediately(self, monkeypatch):
        from crawlutron.config.env import EnvDecisionMemo

        memo = EnvDecisionMemo("CRAWLUTRON_TEST_MEMO", default=False, logger_name=__name__, warning="w %r")
        monkeypatch.setenv("CRAWLUTRON_TEST_MEMO", "0")
        assert memo.decide() is False

    def test_both_twins_delegate_to_the_shared_memo(self, monkeypatch):
        """safe_url parses the private-target env var inline; patching crawlutron's
        parse_bool_value does not reach it — the twins diverge by design (#998)."""
        from safe_url.host_policy import allow_private_targets, reset_private_target_memo

        import crawlutron.config.env as config_env

        def exploded(raw, default, *, env_name):
            raise AssertionError("twin parsed outside the shared memo")

        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        monkeypatch.setattr(config_env, "parse_bool_value", exploded)
        reset_private_target_memo()
        # safe_url parses inline, not through crawlutron's parse_bool_value:
        # no AssertionError escapes, and the decision is still derived from env.
        assert allow_private_targets() is False

    def test_typo_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("CRAWLUTRON_TEST_INT", "four")
        with caplog.at_level(logging.WARNING):
            assert get_env_int("CRAWLUTRON_TEST_INT", 42) == 42
        assert caplog.records, "expected an invalid_int_env warning"
        assert caplog.records[0].env_name == "CRAWLUTRON_TEST_INT"
        assert caplog.records[0].fallback == 42

    def test_clamps_to_bounds(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_TEST_INT", "0")
        assert get_env_int("CRAWLUTRON_TEST_INT", 4, lo=1) == 1
        monkeypatch.setenv("CRAWLUTRON_TEST_INT", "9999")
        assert get_env_int("CRAWLUTRON_TEST_INT", 4, hi=100) == 100


class TestDefaultCoercionNeverBlowsUp:
    """The chokepoint must ALWAYS return a value of the declared numeric type, never
    raise. ``get_env_float(..., "0.5")`` once returned the string uncast, so
    ``limit * fraction`` became runaway string repetition -- a boot-time hang.
    """

    def test_int_getter_coerces_string_default(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_INT", raising=False)
        value = get_env_int("CRAWLUTRON_TEST_INT", "42")
        assert value == 42 and isinstance(value, int)

    def test_float_getter_coerces_string_default(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_FLOAT", raising=False)
        value = get_env_float("CRAWLUTRON_TEST_FLOAT", "0.5")
        assert value == 0.5 and isinstance(value, float)

    def test_float_default_keeps_arithmetic_numeric(self, monkeypatch):
        # The exact regression: int * fraction must be float math, not str repeat.
        monkeypatch.delenv("CRAWLUTRON_TEST_FLOAT", raising=False)
        fraction = get_env_float("CRAWLUTRON_TEST_FLOAT", "0.5")
        assert 100 * fraction == 50.0

    def test_int_getter_truncates_float_default(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_INT", raising=False)
        value = get_env_int("CRAWLUTRON_TEST_INT", 4.7)
        assert value == 4 and isinstance(value, int)

    def test_uncoercible_default_falls_back_without_raising(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_INT", raising=False)
        monkeypatch.delenv("CRAWLUTRON_TEST_FLOAT", raising=False)
        assert get_env_int("CRAWLUTRON_TEST_INT", None) == 0
        assert get_env_float("CRAWLUTRON_TEST_FLOAT", None) == 0.0
        # "abc" rather than a synthetic object: an unparseable string is a real default (a typo'd env var or a
        # hand-edited mapping) and is the case `_as_int_default` documents by name.
        assert get_env_int("CRAWLUTRON_TEST_INT", "abc") == 0

    def test_valid_env_value_still_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_TEST_FLOAT", "0.25")
        assert get_env_float("CRAWLUTRON_TEST_FLOAT", "0.5") == 0.25
        monkeypatch.setenv("CRAWLUTRON_TEST_INT", "7")
        assert get_env_int("CRAWLUTRON_TEST_INT", "42") == 7

    def test_clamp_bounds_survive_string_default(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_INT", raising=False)
        assert get_env_int("CRAWLUTRON_TEST_INT", 0, lo=1) == 1

    def test_rejects_infinite_env_value(self, monkeypatch):
        # ``float("inf")`` parses cleanly but ``limit * inf`` overflows at int() — a malicious value must fall
        # back, not wedge the boot.
        monkeypatch.setenv("CRAWLUTRON_TEST_FLOAT", "inf")
        assert get_env_float("CRAWLUTRON_TEST_FLOAT", 0.5) == 0.5

    def test_rejects_nan_env_value(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_TEST_FLOAT", "nan")
        assert get_env_float("CRAWLUTRON_TEST_FLOAT", 0.5) == 0.5

    def test_finite_huge_value_clamps_to_hi(self, monkeypatch):
        # 1e308 is finite (passes isfinite) yet ``limit * 1e308`` overflows; only the declared bound keeps the
        # arithmetic safe.
        monkeypatch.setenv("CRAWLUTRON_TEST_FLOAT", "1e308")
        assert get_env_float("CRAWLUTRON_TEST_FLOAT", 0.5, lo=0.0, hi=1.0) == 1.0

    def test_int_default_is_always_int_type(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_TEST_INT", raising=False)
        value = get_env_int("CRAWLUTRON_TEST_INT", 4)
        assert type(value) is int


class TestDeploymentValuesAreRefusedNotClamped:
    """A deployment number under contract must be REFUSED, not rewritten: the comment
    promised a loud fallback while ``lo``/``hi`` silently clamped. The timeout is
    asserted far out of range so refusal (30) beats clamp (120); the ceiling proves nothing.
    """

    def test_out_of_ceiling_timeout_yields_the_default_not_the_ceiling(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS": "999"})
        assert settings.default_timeout_seconds == DEFAULT_HTTP_TIMEOUT_SECONDS  # 30, not 120
        assert settings.default_timeout_seconds != MAX_HTTP_TIMEOUT_SECONDS

    def test_out_of_ceiling_retries_yields_the_default(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_DEFAULT_MAX_RETRIES": "9"})
        assert settings.default_max_retries == DEFAULT_MAX_RETRIES

    def test_in_ceiling_retry_default_is_honoured(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_DEFAULT_MAX_RETRIES": "3"})
        assert settings.default_max_retries == 3

    def test_the_refusal_is_logged_with_the_value_that_was_refused(self, caplog):
        with caplog.at_level(logging.WARNING, logger="crawlutron.safety.validation"):
            CrawlerSettings.from_mapping({"CRAWLUTRON_DEFAULT_MAX_RETRIES": "9"})
        refused = [r for r in caplog.records if r.message == "operator_max_retries_out_of_range"]
        assert refused, "a refused deployment value must be visible at startup"
        assert refused[0].env_value == "9"


class TestRetryAuthorityIsPerJob:
    """The two retry ceilings are different authorities: the deployment default is
    capped at MAX_RETRIES (blast radius: every caller), one job's caller up to
    MAX_JOB_MAX_RETRIES -- binding one to the other made them inseparable.
    """

    @pytest.mark.parametrize("requested", [0, 1, 5, 9, 10])
    def test_caller_retry_count_is_bounded_per_job(self, requested):
        assert (
            validate_wire_policy(
                {"limits": {"max_retries": requested}}, PolicyTier.model_defaults()
            ).limits.max_retries
            == requested
        )

    @pytest.mark.parametrize("requested", [11, 99])
    def test_a_caller_beyond_the_per_job_ceiling_is_rejected(self, requested):
        with pytest.raises(ValueError):
            validate_wire_policy({"limits": {"max_retries": requested}}, PolicyTier.model_defaults())

    def test_an_omitted_limit_resolves_to_the_deployment_default(self):
        # One number for "nobody said": the model's own default and the deployment's must not be able to
        # disagree, or an omitted limit answers differently depending on which read edge resolved it.
        assert read_policy({}).limits.max_retries == DEFAULT_MAX_RETRIES
        assert PolicyTier.model_defaults().max_retries == DEFAULT_MAX_RETRIES
        assert CrawlerSettings.from_mapping({}).default_max_retries == DEFAULT_MAX_RETRIES

    def test_the_deployment_retry_ceiling_is_requestable(self):
        """The invariant ``_within_bounds`` used to enforce at runtime: if the deployment
        ceiling rose above the per-job one, the default would be a value no caller could
        ask for -- a standing check states it instead of clamping it away.
        """
        assert MAX_RETRIES <= MAX_JOB_MAX_RETRIES
        assert DEFAULT_MAX_RETRIES <= MAX_JOB_MAX_RETRIES
        assert 0 <= MAX_RETRIES <= MAX_JOB_MAX_RETRIES
