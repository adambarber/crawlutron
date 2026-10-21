"""cache_control directive parser: no-store bans persisting, no-cache/must-revalidate
and pragma: no-cache force revalidation, private bans SHARED persistence, max-age
bounds replay. Pins the seam the content cache honors (#247, #385).
"""

import pytest

from app.transport.cache_control import (
    forbids_shared_store,
    forbids_store,
    max_age_seconds,
    requires_revalidation,
)


def test_forbids_store_detects_no_store():
    assert forbids_store({"cache-control": "max-age=60, no-store"})
    assert not forbids_store({"cache-control": "max-age=60"})
    assert not forbids_store({})


def test_no_store_is_case_and_whitespace_insensitive():
    assert forbids_store({"cache-control": "public, NO-STORE ; max-age=0"})
    assert forbids_store({"Cache-Control": "no-store"})


def test_requires_revalidation_covers_no_cache_and_must_revalidate():
    assert requires_revalidation({"cache-control": "no-cache"})
    assert requires_revalidation({"cache-control": "must-revalidate"})
    assert requires_revalidation({"pragma": "no-cache"})
    assert not requires_revalidation({"cache-control": "max-age=60"})
    assert not requires_revalidation({})


def test_no_store_is_separate_from_no_cache():
    assert not requires_revalidation({"cache-control": "no-store"})
    assert not forbids_store({"cache-control": "no-cache"})


# RFC 9111 5.2.2.6: private marks a response single-user; a shared cache stores nothing.


def test_private_forbids_shared_store_but_not_plain_store_ban():
    assert forbids_shared_store({"cache-control": "private"})
    assert forbids_shared_store({"cache-control": "private, max-age=3600"})
    assert forbids_shared_store({"cache-control": "no-store"})
    assert not forbids_shared_store({"cache-control": "max-age=3600"})
    assert not forbids_shared_store({})
    # plain forbids_store stays a no-store-only question (pins #247's seam)
    assert not forbids_store({"cache-control": "private"})


# RFC 9111 5.2.2.1/5.2.2.9: max-age sets freshness; s-maxage wins in a shared cache.


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"cache-control": "max-age=60"}, 60.0),
        ({"cache-control": "public, max-age=10"}, 10.0),
        ({"cache-control": "max-age=0"}, 0.0),
        ({"cache-control": "s-maxage=5, max-age=999"}, 5.0),
        ({"cache-control": "max-age=abc"}, None),
        ({"cache-control": "no-store"}, None),
        ({}, None),
    ],
)
def test_max_age_seconds_parses_the_origin_freshness(headers, expected):
    assert max_age_seconds(headers) == expected


# RFC 9111 5.3: Expires is the older form of the same freshness directive (#392).


_NOW = 1_500_000_000.0  # 2017: between the past and future header instants
_FUTURE = "Sun, 22 Aug 2032 00:00:00 GMT"
_PAST = "Sun, 22 Aug 1982 00:00:00 GMT"


def test_expires_seconds_parses_the_origin_instant():
    from app.transport.cache_control import expires_seconds

    seconds = expires_seconds({"expires": _FUTURE}, now=_NOW)
    assert seconds is not None and seconds > 0
    assert expires_seconds({"expires": _PAST}, now=_NOW) is not None
    assert expires_seconds({"expires": _PAST}, now=_NOW) <= 0.0  # already stale
    assert expires_seconds({"expires": "not a date"}, now=_NOW) is None
    assert expires_seconds({}, now=_NOW) is None
