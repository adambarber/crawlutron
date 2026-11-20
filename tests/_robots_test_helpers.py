"""Shared helpers for robots.txt rule loading tests.

Encapsulates the unrestricted policy builder and standard robots load used by
both robots test files.
"""

from __future__ import annotations

from crawlutron.transport.robots import load_robots_rules
from safe_url.host_policy import HostPolicy
from tests._fetch_doubles import FetcherDouble
from tests._policy_factories import canonical


def unrestricted_policy() -> HostPolicy:
    """An unrestricted host policy for tests that don't exercise host filtering."""
    return HostPolicy.unrestricted()


async def robots_load(origin: str, decode, policy: HostPolicy, *, timeout: int = 5):
    """Load robots rules for one origin with a standard decode double."""
    return await load_robots_rules(
        canonical(origin),
        FetcherDouble(lambda *_: (None, None)),
        timeout,
        fetch_and_decode=decode,
        policy=policy,
    )
