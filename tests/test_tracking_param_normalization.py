"""Tracking-parameter normalization dedups URLs that differ only by UTM/tracking tags."""

from functools import partial

from app.models.normalized_url import NormalizedUrl as _NormalizedUrl
from app.safety.validation import HostPolicy

admit = partial(
    _NormalizedUrl,
    policy=HostPolicy.unrestricted(),
)


def test_utm_params_stripped():
    a = admit("https://example.gov/page?utm_source=twitter&utm_medium=social")
    b = admit("https://example.gov/page?utm_source=facebook&utm_medium=email")
    assert a == b and a.raw == "https://example.gov/page"


def test_gclid_fbclid_stripped():
    a = admit("https://example.gov/p?gclid=ABC&id=5")
    b = admit("https://example.gov/p?fbclid=XYZ&id=5")
    assert a == b and a.raw == "https://example.gov/p?id=5"


def test_legitimate_params_preserved():
    # 'ref' that is NOT a tracking ref, and other query keys, survive
    a = admit("https://example.gov/p?category=news&page=2")
    b = admit("https://example.gov/p?page=2&category=news")
    assert a == b and a.raw == "https://example.gov/p?category=news&page=2"


def test_sorting_still_applied_after_strip():
    a = admit("https://example.gov/p?utm_source=x&z=1&a=2")
    assert a.raw == "https://example.gov/p?a=2&z=1"
