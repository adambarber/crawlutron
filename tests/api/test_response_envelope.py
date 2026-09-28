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

from tests._browser_doubles import _fake_browser
from tests._render_producer import render_stub_request

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


#: The keys the browser worker puts in ``CrawlResult.metadata``, pinned so a new
#: worker key becomes a deliberate decision here rather than a silent
#: pass-through into ``diagnostics``.
_BROWSER_METADATA_KEYS = frozenset(
    {"title", "content_length", "browser_reused", "challenge_resolved", "extraction_method"}
)


async def test_no_browser_metadata_key_is_dropped_between_worker_and_wire():
    """The property that was false for four of these six keys.

    The render adapter copied ``CrawlResult.metadata`` into ``diagnostics`` by
    excluding five keys BY NAME, re-surfaced exactly one of them, and hardcoded
    ``metadata: {}``. So ``content_length``, ``browser_reused`` and
    ``challenge_resolved`` were produced by the worker and read by nothing, and
    ``PageMetadata.title`` -- a declared field -- could not be filled on this
    endpoint by any input. Nothing failed at the time: an exclusion set says
    which keys are going elsewhere without saying where, and every field of
    ``UnifiedCrawlResult`` carries a default, so a dict that omitted one was a
    perfectly valid dict.

    The adapter constructs the model now, so the assertion is the whole
    property rather than a list of the keys someone remembered: everything the
    worker emits must be readable off the shaped item somewhere.
    """
    envelope = await render_stub_request(
        {"url": "http://127.0.0.1:1/x"},
        worker=_fake_browser(metadata={key: f"v-{key}" for key in _BROWSER_METADATA_KEYS}),
    )
    item = (await _aunified_response_from_result(envelope, adapt=render_result_to_unified)).results[0]

    populated = {key for key, value in item.metadata.model_dump().items() if value is not None}
    surfaced = set(item.diagnostics) | populated
    if item.extraction_method is not None:
        surfaced.add("extraction_method")
    assert _BROWSER_METADATA_KEYS <= surfaced

    assert item.metadata.title == "v-title"
    assert item.diagnostics["content_length"] == "v-content_length"
    assert item.diagnostics["browser_reused"] == "v-browser_reused"
    assert item.diagnostics["challenge_resolved"] == "v-challenge_resolved"
    assert item.extraction_method == "v-extraction_method"
