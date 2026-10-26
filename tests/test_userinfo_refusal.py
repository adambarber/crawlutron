"""Userinfo-bearing URLs are refused, not silently stripped (#544/S6)."""

import pytest

from app.models.normalized_url import NormalizedUrl
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


@pytest.mark.parametrize(
    "raw",
    [
        "https://user@evil.com/",
        "https://user:pass@evil.com/",
        "http://victim@example.com/page",
    ],
)
def test_userinfo_is_refused_not_stripped(raw: str) -> None:
    with pytest.raises(ValueError, match="userinfo_refused"):
        NormalizedUrl(raw, policy=UNRESTRICTED_HOST_POLICY)


def test_normal_urls_still_admit() -> None:
    url = NormalizedUrl("https://example.com/page", policy=UNRESTRICTED_HOST_POLICY)
    assert url.raw == "https://example.com/page"
