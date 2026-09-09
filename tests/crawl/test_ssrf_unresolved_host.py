"""HIGH fix (F9): an unresolvable hostname must NOT route the crawler to
loopback. Previously _check_ssrf_protection silently passed on socket.gaierror
and _resolve_and_pin_host fell back to pinning '127.0.0.1', letting an attacker
submit a non-resolving URL (e.g. http://nonexistent.invalid/) to reach local
services. This also covers the webhook path, which validates/_pins via the same
helpers (_validate_webhook_url -> _check_ssrf_protection; _deliver_webhook ->
_resolve_and_pin_host).
"""

import socket

import pytest

import app.crawl.strategies.http as http_mod


def _fail_resolution(host, *args, **kwargs):
    raise socket.gaierror("name or service not known")


def test_check_ssrf_protection_blocks_unresolvable_host(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    with pytest.raises(ValueError):
        http_mod._check_ssrf_protection("http://nonexistent.invalid/")


def test_resolve_and_pin_host_does_not_fall_back_to_loopback(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    hostname, pinned_ip = http_mod._resolve_and_pin_host("http://nonexistent.invalid/")
    assert pinned_ip != "127.0.0.1", "unresolvable host must not pin to loopback"
    assert pinned_ip == "nonexistent.invalid"


def test_build_pinned_request_never_targets_loopback_for_unresolvable(monkeypatch):
    # Defense-in-depth: even if a caller skips the SSRF check, pinning must not
    # point at 127.0.0.1 for an attacker-influenced, unresolvable hostname.
    monkeypatch.setattr(socket, "getaddrinfo", _fail_resolution)
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    # _build_pinned_request raises now (check fails closed), so assert it raises.
    with pytest.raises(ValueError):
        http_mod._build_pinned_request("http://nonexistent.invalid/", {})
