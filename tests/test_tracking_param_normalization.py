"""Tracking-parameter normalization dedups URLs that differ only by UTM/tracking tags."""

from functools import partial

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl as _NormalizedUrl
from crawlutron.models.normalized_url import admit_or_none

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


def test_unicode_host_admits_identical_to_punycode():
    """IDN canonicalization is admission's job: the unicode and punycode spellings
    are one identity -- one raw (cache key, dedup), consistent host and site."""
    unicode_form = admit("https://Bücher.example/page")
    punycode_form = admit("https://xn--bcher-kva.example/page")
    assert unicode_form == punycode_form
    assert unicode_form.raw == "https://xn--bcher-kva.example/page"
    assert unicode_form.host == "xn--bcher-kva.example"
    assert unicode_form.site == "xn--bcher-kva.example"


def test_unicode_host_refused_when_unencodable():
    # A host the IDNA grammar cannot encode cannot be canonicalized: admission
    # refuses rather than admitting a value whose identity cannot be trusted.
    assert admit_or_none(f"https://{'a' * 64}.com", policy=HostPolicy.unrestricted()) is None


def test_sorting_still_applied_after_strip():
    a = admit("https://example.gov/p?utm_source=x&z=1&a=2")
    assert a.raw == "https://example.gov/p?a=2&z=1"
