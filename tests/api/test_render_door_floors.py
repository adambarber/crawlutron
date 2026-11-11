"""The /v1/render door's floor refusals over the real wire (#787, #819).

The model edge refuses below-floor knobs with a typed 422 before the handler
runs; an accepted request resolves through the real resolver with no overrides.
"""

from __future__ import annotations

from tests._async_harnesses import issue_key
from tests.api.test_map_render_quota import _map_render_client


async def _post(client, secret: str, options: dict[str, object]):
    """One keyed /v1/render POST whose options carry the knobs under test."""
    return await client.post(
        "/v1/render",
        headers={"X-API-Key": secret},
        json={"urls": ["http://example.com/1"], "options": dict(options)},
    )


async def test_a_below_floor_timeout_is_a_typed_422(tmp_path):
    """timeout_seconds=-5 (the silent-all-skipped-200 victim) refuses at the door."""
    async with _map_render_client(tmp_path) as client:
        secret = await issue_key(client, max_pages=1)
        response = await _post(client, secret, {"timeout_seconds": -5})
        assert response.status_code == 422, response.text
        assert "timeout_seconds" in response.text


async def test_a_zero_timeout_and_negative_retries_are_422(tmp_path):
    async with _map_render_client(tmp_path) as client:
        secret = await issue_key(client, max_pages=1)
        for bad in ({"timeout_seconds": 0}, {"max_retries": -3}):
            options: dict[str, object] = dict(bad)
            response = await _post(client, secret, options)
            assert response.status_code == 422, response.text


async def test_floor_values_validate_and_resolve_through_the_real_resolver():
    """timeout_seconds=1 with zero retries passes the wire model and resolves
    through the real resolver (no overrides) to a floor-standing RenderOptions."""
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.render_request import RenderRequest
    from crawlutron.service.render import render_options_for

    payload = RenderRequest.model_validate(
        {"urls": ["http://example.com/1"], "options": {"timeout_seconds": 1, "max_retries": 0}}
    )
    options = render_options_for(payload, CrawlerSettings.from_mapping())
    assert options.timeout_seconds == 1
    assert options.max_retries == 0


async def test_an_accepted_floor_request_reaches_a_working_worker():
    """The producer path a validating caller takes: the wire model accepts the
    floor values and the render pipeline answers the envelope with the result."""
    from tests._render_producer import render_stub_request

    envelope = await render_stub_request(
        {
            "urls": ["http://example.com/1"],
            "options": {"timeout_seconds": 1, "max_retries": 0, "respect_robots": False},
        }
    )
    assert envelope["submitted_count"] == 1
    results = envelope["results"]
    assert isinstance(results, list) and results
    assert results[0].status == "success"
    assert results[0].classification == "rendered"
