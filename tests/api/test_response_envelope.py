"""One owner for the unified response envelope.

``/v1/crawl`` and ``/v1/render`` each read a raw producer envelope and adapt it
to :class:`UnifiedCrawlResponse`. They used to do that at their own call sites,
passing four fields by name each, and a field added to one path and forgotten on
the other is dropped by the model rather than rejected: a response field has
already shipped missing from one endpoint and was caught only by curling the
live endpoint. The adaptation is now one function
(``app.api.response_shaping._aunified_response_from_result``) over one envelope
shape, and the tree-wide guards in ``tests/test_safety_invariants.py`` keep it
that way. These tests pin the two properties that made the move worth making.
"""

from _render_producer import render_stub_request

from app.api.response_shaping import _aunified_response_from_result, render_result_to_unified
from app.api_models import POLICY_SCHEMA_VERSION

#: The engine's envelope, which is the shape the crawl producer emits.
CRAWL_ENVELOPE = {
    "request_id": "req-crawl",
    "submitted_count": 2,
    "policy_schema_version": "v-engine",
    "results": [
        {"original_url": "http://example.com/a", "status": "success", "classification": "rendered"},
        {"original_url": "http://example.com/b", "status": "failed", "classification": "not_found"},
    ],
}


async def _render_envelope() -> dict:
    return await render_stub_request({"url": "http://127.0.0.1:1/x"})


async def test_render_producer_stamps_the_envelope_the_adapter_reads():
    """``/v1/render`` used to substitute the policy version at the ENDPOINT.

    That is a second place the version lives, and the quiet kind: the value was
    correct only because a constant was passed next to a dict that never carried
    it. The producer stamps it now, so the adapter reads one envelope shape from
    both producers and the endpoint passes no version of its own.
    """
    envelope = await _render_envelope()
    assert envelope["policy_schema_version"] == POLICY_SCHEMA_VERSION
    assert {"request_id", "submitted_count", "policy_schema_version", "results"} <= set(envelope)


async def test_the_owner_reports_each_producers_own_version():
    """One adapter, two producers: the version on the wire is the one the
    producer stamped, for render and for crawl alike."""
    rendered = await _aunified_response_from_result(await _render_envelope(), adapt=render_result_to_unified)
    assert rendered.policy_schema_version == POLICY_SCHEMA_VERSION
    assert rendered.submitted_count == 1

    crawled = await _aunified_response_from_result(CRAWL_ENVELOPE)
    assert crawled.policy_schema_version == "v-engine"
    assert crawled.submitted_count == 2
    assert crawled.request_id == "req-crawl"
    assert [item.original_url for item in crawled.results] == [
        "http://example.com/a",
        "http://example.com/b",
    ]
