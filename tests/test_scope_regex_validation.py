"""scope_regex validation contract: ``compile_untrusted`` RAISES on unsafe patterns,
and the API field validator must turn that into a pydantic ValidationError (a 422),
not a 500 or a dropped filter -- pinning the fix for the dead ``result is None`` branch.
"""

import pytest
from pydantic import ValidationError

from app.models.crawl_policy import CrawlPolicy


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
