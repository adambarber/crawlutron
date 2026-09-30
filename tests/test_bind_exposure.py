"""A non-loopback bind exposes an API that has no caller authentication.

The service defaults to loopback precisely because nothing authenticates a
caller: every ``Authorization`` header in the tree is outbound, attached to a
crawled origin rather than presented by one. The shipped compose file keeps
the container on ``0.0.0.0`` and publishes only to ``127.0.0.1``, which is
also the only configuration Docker port mapping can reach.

The gap both leave is the operator who changes one of those and gets an
unauthenticated crawl, job-control and webhook surface on a network, with
nothing in the log to say so. Startup is the only moment the exposure exists
and the operator is watching, which is why the check is there and not in a
request path.

A warning and not a refusal. A refusal would reject the container's own
``0.0.0.0`` bind -- the configuration this project ships.
"""

import logging

import pytest

from app.main import _warn_if_bound_off_loopback

# Addresses a request can never leave the machine over. The whole 127/8 block
# qualifies, not just 127.0.0.1: a deployment that binds 127.0.0.53 is still
# loopback-only and should not be told it is exposed.
LOOPBACK = ["127.0.0.1", "127.0.0.53", "::1", "localhost"]

# Everything a network peer can reach, including the ranges that are not valid
# public egress targets -- which is the whole point of asking the bind question
# separately instead of reusing the egress verdict.
ROUTABLE = ["0.0.0.0", "::", "10.0.0.5", "192.168.1.10", "example.internal"]


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_bind_says_nothing(caplog, host):
    with caplog.at_level(logging.WARNING):
        _warn_if_bound_off_loopback(host)
    assert caplog.records == [], f"{host} is loopback-only and warned anyway: {caplog.text}"


@pytest.mark.parametrize("host", ROUTABLE)
def test_routable_bind_warns_that_nothing_authenticates_the_caller(caplog, host):
    with caplog.at_level(logging.WARNING):
        _warn_if_bound_off_loopback(host)

    assert caplog.records, f"{host} is routable and startup said nothing"
    message = caplog.text
    assert "no caller authentication" in message, f"the warning does not say what is exposed: {message}"
    # The mitigation is the part an operator can act on. A warning naming the
    # risk without naming the fix is a worry, not a guard.
    assert host in message, f"the warning does not say which address: {message}"
    assert "127.0.0.1" in message, f"the warning does not say how to keep it local: {message}"


@pytest.mark.parametrize("host", ["", "not-an-address"])
def test_a_bind_the_check_cannot_classify_is_treated_as_exposed(caplog, host):
    """Fail closed: an address this code cannot classify is not one it has any
    reason to believe is private."""
    with caplog.at_level(logging.WARNING):
        _warn_if_bound_off_loopback(host)

    assert caplog.records, f"{host!r} was assumed to be loopback"
