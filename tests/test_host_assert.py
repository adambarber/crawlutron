"""Behavior of the test-only host assertion helper: exact host, never substring."""

from __future__ import annotations

import httpx
import pytest

from safe_url.normalized_url import NormalizedUrl
from tests._host_assert import host_in, host_of
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


@pytest.mark.parametrize(
    "value,expected",
    [
        (NormalizedUrl("https://example.com/hook", policy=UNRESTRICTED_HOST_POLICY), "example.com"),
        (
            NormalizedUrl("http://www.pinterest.com/pin/1", policy=UNRESTRICTED_HOST_POLICY),
            "www.pinterest.com",
        ),
        (httpx.URL("https://webhook.example.com/cb"), "webhook.example.com"),
        ("https://blocked.test/secret", "blocked.test"),
        ("example.com", "example.com"),
        ("notexample.com.evil", "notexample.com.evil"),
    ],
)
def test_host_of_returns_canonical_host(value, expected):
    assert host_of(value) == expected


def test_host_in_matches_exact_host_in_dict():
    cooldowns = {"example.com": 10.0, "other.test": 1.0}
    assert host_in(cooldowns, "example.com") is True
    assert host_in(cooldowns, "notexample.com") is False
    assert host_in(cooldowns, "missing.test") is False


def test_host_in_matches_exact_host_in_set_of_urls():
    urls = {
        NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY),
        NormalizedUrl("http://example.com/a", policy=UNRESTRICTED_HOST_POLICY),
    }
    assert host_in(urls, "example.com") is True
    assert host_in(urls, "evil.com") is False


def test_host_in_matches_exact_host_in_tuple_of_hosts():
    allowed = ("example.com", "trusted.example")
    assert host_in(allowed, "example.com") is True
    assert host_in(allowed, "trusted.example") is True
    assert host_in(allowed, "notexample.com") is False


def test_host_in_rejects_substring_confusion():
    """``example.com`` must not match ``notexample.com.evil``."""
    container = {"notexample.com.evil": 1.0}
    assert host_in(container, "example.com") is False
    assert host_in(container, "notexample.com.evil") is True


def test_host_of_strips_port():
    """A host with a port still yields just the host."""
    assert host_of("https://example.com:8080/hook") == "example.com"
    assert host_of("http://example.com:443/") == "example.com"


def test_host_of_handles_uppercase_host():
    """Hosts are case-insensitive: uppercase spellings canonicalize."""
    assert host_of("HTTPS://EXAMPLE.COM/hook") == "example.com"


def test_host_in_empty_container():
    """An empty container never matches."""
    assert host_in({}, "example.com") is False
    assert host_in((), "example.com") is False
    assert host_in(set(), "example.com") is False


def test_host_in_single_and_many_matches():
    """One match and many matches both return True; no match returns False."""
    urls = [
        NormalizedUrl("https://example.com/a", policy=UNRESTRICTED_HOST_POLICY),
        NormalizedUrl("https://example.com/b", policy=UNRESTRICTED_HOST_POLICY),
        NormalizedUrl("https://other.test/c", policy=UNRESTRICTED_HOST_POLICY),
    ]
    assert host_in(urls, "example.com") is True
    assert host_in(urls, "other.test") is True
    assert host_in(urls, "missing.test") is False


def test_host_of_rejects_anchored_evasion():
    """``example.com.evil`` must never yield host ``example.com``."""
    assert host_of("https://example.com.evil.com/") == "example.com.evil.com"
    assert host_of("https://notexample.com/") == "notexample.com"


def test_host_in_resists_unicode_confusable():
    """Cyrillic-lookalike hosts do not match ASCII ``example.com``."""
    confusable = "https://еxample.com/"  # Cyrillic 'е' (U+0435)
    assert host_of(confusable) != "example.com"
    assert host_in({confusable}, "example.com") is False
