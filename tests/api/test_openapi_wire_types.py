"""The OpenAPI document is a live door: it must actually be servable.

Every URL-valued field on the wire renders as a string -- that is the only
JSON form an admitted URL has. When ``NormalizedUrl`` became a value with a
plain-validator core schema, the OpenAPI generator could no longer infer a
JSON Schema for it, and GET /openapi.json started failing with a 500: the
discovery door every agent is told to read (AGENTS.md) went dark while the
app itself kept running. A consumer sees this only through the endpoint,
so the test goes through the endpoint.
"""

import pytest


@pytest.mark.asyncio
async def test_openapi_document_is_servable_and_describes_url_values_as_strings(async_client):
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200, "the OpenAPI discovery door must serve, not 500"
    spec = resp.json()
    # The crawl result's discovered links carry the URL values; on the wire they are strings, and the document
    # must say so.
    schema_name = "DiscoveredLinkResponse"
    schemas = spec["components"]["schemas"]
    assert schema_name in schemas, "discovered links dropped from the OpenAPI document"
    link = schemas[schema_name]["properties"]
    assert link["normalized_url"]["type"] == "string"
    assert link["source_url"]["type"] == "string"
