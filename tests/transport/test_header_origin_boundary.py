"""Header authority at the origin boundary: caller headers must not ride a
cross-origin redirect, service-owned identity headers survive, and same-origin
replay is preserved.
"""

from __future__ import annotations

import pytest

from crawlutron.transport.cookie_jar import global_cookie_jar
from crawlutron.transport.pinned_transport import redirect_target
from tests._policy_factories import canonical


@pytest.mark.asyncio
async def test_cross_origin_hop_drops_caller_api_key_keeps_service_identity():
    """RED 2: a caller ``X-Api-Key`` never crosses an origin, while service
    identity headers survive and the destination origin's jar cookie is
    re-attached."""
    jar = global_cookie_jar()
    jar.add_cookie_header(canonical("http://other.example/"), "session=abc")

    result = await redirect_target(
        canonical("http://example.com/"),
        {"X-Api-Key": "caller-secret", "User-Agent": "test-agent/1.0"},
        302,
        "http://other.example/",
        0,
    )
    assert result is not None
    new_url, headers = result

    assert new_url == canonical("http://other.example/")
    assert "X-Api-Key" not in headers, headers
    assert headers["User-Agent"] == "test-agent/1.0", headers
    assert headers["Cookie"] == "session=abc", headers


@pytest.mark.asyncio
async def test_cross_origin_hop_drops_every_non_owned_name():
    """Any caller name outside the closed identity allowlist is dropped on a
    cross-origin hop, not just the three credential names the old table knew."""
    result = await redirect_target(
        canonical("http://example.com/"),
        {"X-Tenant": "acme", "X-Api-Key": "k", "Authorization": "Bearer t"},
        302,
        "http://other.example/",
        0,
    )
    assert result is not None
    _, headers = result

    assert "X-Tenant" not in headers, headers
    assert "X-Api-Key" not in headers, headers
    assert "Authorization" not in headers, headers


@pytest.mark.asyncio
async def test_same_origin_hop_preserves_caller_headers():
    """RED 3: the fix must not break same-origin replay -- caller headers ride a
    same-origin redirect untouched."""
    result = await redirect_target(
        canonical("http://example.com/"),
        {"X-Api-Key": "caller-secret", "X-Tenant": "acme"},
        302,
        "http://example.com/other",
        0,
    )
    assert result is not None
    new_url, headers = result

    assert new_url == canonical("http://example.com/other")
    assert headers["X-Api-Key"] == "caller-secret", headers
    assert headers["X-Tenant"] == "acme", headers


def test_survives_cross_origin_is_closed() -> None:
    """The propagation allowlist is closed by construction: only service-owned
    identity names cross an origin."""
    from crawlutron.safety.header_authority import survives_cross_origin

    assert survives_cross_origin("user-agent") is True
    assert survives_cross_origin("accept") is True
    assert survives_cross_origin("x-api-key") is False
    assert survives_cross_origin("authorization") is False
    assert survives_cross_origin("cookie") is False
