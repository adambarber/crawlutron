"""One owner for the envelope: two endpoints passed four fields by name each, so
a field added on one path and forgotten on the other shipped missing and was
caught only by curling live. Adaptation is now one function over one shape.
"""

from app.api.response_shaping import arender_response_from_result, aunified_response_from_result
from app.models.crawl_policy import POLICY_SCHEMA_VERSION
from tests._browser_doubles import _fake_browser
from tests._render_producer import render_stub_request
from tests._result_builders import engine_result

#: The engine's envelope, which is the shape the crawl producer emits.
CRAWL_ENVELOPE = {
    "request_id": "req-crawl",
    "submitted_count": 2,
    "policy_schema_version": "v-engine",
    "results": [
        engine_result("http://example.com/a", "rendered"),
        engine_result("http://example.com/b", "not_found"),
    ],
}


async def _render_envelope() -> dict:
    return await render_stub_request({"url": "http://127.0.0.1:1/x"})


async def test_render_producer_stamps_the_envelope_the_adapter_reads():
    """``/v1/render`` used to substitute the policy version at the ENDPOINT -- a
    second home for the version, correct only because a constant was passed next
    to a dict that never carried it. The producer stamps it now.
    """
    envelope = await _render_envelope()
    assert envelope["policy_schema_version"] == POLICY_SCHEMA_VERSION
    assert {"request_id", "submitted_count", "policy_schema_version", "results"} <= set(envelope)


async def test_the_owner_reports_each_producers_own_version():
    """One adapter, two producers: the version on the wire is the one the
    producer stamped, for render and for crawl alike."""
    rendered = await arender_response_from_result(await _render_envelope())
    assert rendered.policy_schema_version == POLICY_SCHEMA_VERSION
    assert rendered.submitted_count == 1

    crawled = await aunified_response_from_result(CRAWL_ENVELOPE)
    assert crawled.policy_schema_version == "v-engine"
    assert crawled.submitted_count == 2
    assert crawled.request_id == "req-crawl"
    assert [item.original_url for item in crawled.results] == [
        "http://example.com/a",
        "http://example.com/b",
    ]


#: The keys the browser worker puts in ``CrawlResult.metadata``, pinned so a new worker key becomes a
#: deliberate decision here rather than a silent pass-through into ``diagnostics``.
_BROWSER_METADATA_KEYS = frozenset(
    {"title", "content_length", "browser_reused", "challenge_resolved", "extraction_method"}
)


async def test_no_browser_metadata_key_is_dropped_between_worker_and_wire():
    """The adapter once copied metadata by excluding keys BY NAME, re-surfacing
    one and hardcoding the rest, so half the worker's output reached no wire
    field and nothing failed (every result field carries a default).
    """
    envelope = await render_stub_request(
        {"url": "http://127.0.0.1:1/x", "options": {"respect_robots": False}},
        worker=_fake_browser(metadata={key: f"v-{key}" for key in _BROWSER_METADATA_KEYS}),
    )
    item = (await arender_response_from_result(envelope)).results[0]

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
