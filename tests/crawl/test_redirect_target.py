"""Unit tests for the shared manual-redirect decision helper (both fetchers)."""

from functools import partial

from app.crawl.strategies.http import _MAX_REDIRECT_HOPS
from app.crawl.strategies.http import _redirect_target as _redirect_target_with_policy
from app.validation import HostPolicy

_redirect_target = partial(
    _redirect_target_with_policy,
    policy=HostPolicy.unrestricted(),
)


def test_redirect_target_follows_in_budget_redirect():
    result = _redirect_target("http://a.com/x", {"Host": "a.com"}, 302, "/y", 0)
    assert result == ("http://a.com/y", {"Host": "a.com"})


def test_redirect_target_none_on_terminal_status():
    assert _redirect_target("http://a.com/", {}, 200, None, 0) is None
    assert _redirect_target("http://a.com/", {}, 404, "/elsewhere", 0) is None


def test_redirect_target_none_when_hop_budget_exhausted():
    assert _redirect_target("http://a.com/", {}, 301, "/y", _MAX_REDIRECT_HOPS) is None


def test_redirect_target_strips_sensitive_headers_cross_host():
    headers = {"Host": "a.com", "Cookie": "s=1", "Authorization": "Bearer t", "Accept": "*/*"}
    result = _redirect_target("http://a.com/", headers, 301, "http://b.com/z", 0)
    new_url, new_headers = result
    assert new_url == "http://b.com/z"
    assert "Cookie" not in new_headers and "Authorization" not in new_headers
    assert new_headers["Accept"] == "*/*"
    # original dict must not be mutated (caller keeps per-hop copy semantics)
    assert headers["Cookie"] == "s=1"


def test_redirect_target_keeps_sensitive_headers_same_host():
    headers = {"Cookie": "s=1"}
    _, new_headers = _redirect_target("http://a.com/a", headers, 307, "/b", 2)
    assert new_headers["Cookie"] == "s=1"
