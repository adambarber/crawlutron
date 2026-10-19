"""cache_control directive parser: no-store bans persisting, no-cache/must-revalidate
and pragma: no-cache force revalidation. Pins the seam the content cache honors (#247).
"""

from app.transport.cache_control import forbids_store, requires_revalidation


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
