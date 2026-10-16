"""Every URL-valued wire field renders as a string. When ``NormalizedUrl`` gained
core plain-validator schema the generator could no longer infer JSON Schema, and
GET /openapi.json 500'd -- a door agents read, seen only via the endpoint.
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
