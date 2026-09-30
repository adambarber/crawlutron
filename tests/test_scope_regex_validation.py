"""Scope_regex validation contract.

compile_untrusted RAISES UnsafePattern on over-long/invalid patterns; the
API field validator must convert that into a pydantic ValidationError (which
FastAPI renders as a 422 validation_error), not let it escape as a 500 or
silently drop the filter. Pins the fix for the dead `result is None` branch.
"""

import pytest
from pydantic import ValidationError

from app.api_models import CrawlPolicy


def test_overlong_scope_regex_raises_validation_error():
    overlong = "a" * 257
    with pytest.raises(ValidationError, match="scope_regex"):
        CrawlPolicy(scope_regex=overlong)


def test_invalid_scope_regex_raises_validation_error():
    with pytest.raises(ValidationError, match="scope_regex"):
        CrawlPolicy(scope_regex="(((")


def test_reasonable_scope_regex_accepted():
    policy = CrawlPolicy(scope_regex=r"^https://example\.com")
    assert policy.scope_regex == r"^https://example\.com"
