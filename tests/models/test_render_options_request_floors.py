"""Floors on the /v1/render caller timeout knobs (#787, #819). timeout_seconds below 1 and a negative
max_retries used to cross validation silently: a negative timeout fired gather_deadline_bounded's deadline
immediately, so every URL failed closed as robots-refused behind a 200.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlutron.models.render_options_request import RenderOptionsRequest
from crawlutron.vocabulary.limits import MAX_HTTP_TIMEOUT_SECONDS, MAX_RETRIES


def _options(**overrides: object) -> RenderOptionsRequest:
    return RenderOptionsRequest(**overrides)


class TestTimeoutSecondsFloor:
    """The timeout knob's floor: refuse below 1, accept 1 and the ceiling."""

    @pytest.mark.parametrize("bad", [-5, -1, 0])
    def test_a_timeout_below_one_is_refused(self, bad: int):
        with pytest.raises(ValidationError, match="timeout_seconds"):
            _options(timeout_seconds=bad)

    def test_a_timeout_of_one_is_accepted(self):
        assert _options(timeout_seconds=1).timeout_seconds == 1

    def test_a_timeout_at_the_ceiling_is_accepted(self):
        assert _options(timeout_seconds=MAX_HTTP_TIMEOUT_SECONDS).timeout_seconds == MAX_HTTP_TIMEOUT_SECONDS

    def test_a_timeout_above_the_ceiling_still_reaches_the_resolver_clamp(self):
        """The model carries the FLOOR only; the ceiling stays the resolver's
        clamp (#444) -- an over-ceiling value validates here and clamps there.
        """
        assert _options(timeout_seconds=MAX_HTTP_TIMEOUT_SECONDS + 1).timeout_seconds == (
            MAX_HTTP_TIMEOUT_SECONDS + 1
        )

    def test_non_finite_timeouts_are_refused(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValidationError, match="timeout_seconds"):
                _options(timeout_seconds=bad)


class TestMaxRetriesFloor:
    """Negative retries are nonsense, not infinite; zero retries is legal."""

    @pytest.mark.parametrize("bad", [-3, -1])
    def test_a_negative_retry_count_is_refused(self, bad: int):
        with pytest.raises(ValidationError, match="max_retries"):
            _options(max_retries=bad)

    @pytest.mark.parametrize("good", [0, 1, MAX_RETRIES])
    def test_retry_counts_from_zero_to_the_ceiling_stand(self, good: int):
        assert _options(max_retries=good).max_retries == good
