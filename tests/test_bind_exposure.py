"""A non-loopback bind exposes an API with no caller authentication, so the check
lives at startup (when the operator is watching), warns rather than refusing, and
leaves the shipped ``0.0.0.0`` publish-to-loopback docker mapping intact.
"""

import logging

import pytest

from app.main import warn_if_bound_off_loopback

# Addresses a request can never leave the machine over. The whole 127/8 block qualifies, not just 127.0.0.1: a
# deployment that binds 127.0.0.53 is still loopback-only and should not be told it is exposed.
LOOPBACK = ["127.0.0.1", "127.0.0.53", "::1", "localhost"]

ROUTABLE = ["0.0.0.0", "::", "10.0.0.5", "192.168.1.10", "example.internal"]


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_bind_says_nothing(caplog, host):
    with caplog.at_level(logging.WARNING):
        warn_if_bound_off_loopback(host)
    assert caplog.records == [], f"{host} is loopback-only and warned anyway: {caplog.text}"


@pytest.mark.parametrize("host", ROUTABLE)
def test_routable_bind_warns_that_nothing_authenticates_the_caller(caplog, host):
    with caplog.at_level(logging.WARNING):
        warn_if_bound_off_loopback(host)

    assert caplog.records, f"{host} is routable and startup said nothing"
    message = caplog.text
    assert "anonymous" in message, f"the warning does not say what is exposed: {message}"
    # The mitigation is the part an operator can act on. A warning naming the risk without naming the fix is a
    # worry, not a guard.
    assert host in message, f"the warning does not say which address: {message}"
    assert "127.0.0.1" in message, f"the warning does not say how to keep it local: {message}"


@pytest.mark.parametrize("host", ["", "not-an-address"])
def test_a_bind_the_check_cannot_classify_is_treated_as_exposed(caplog, host):
    """Fail closed: an address this code cannot classify is not one it has any
    reason to believe is private."""
    with caplog.at_level(logging.WARNING):
        warn_if_bound_off_loopback(host)

    assert caplog.records, f"{host!r} was assumed to be loopback"
