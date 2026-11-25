"""The argument-validation branch of tools/call returns one coded tool error."""

from __future__ import annotations

import json

from crawlutron.models.json_value import JsonValue
from crawlutron_mcp.server import handle_mcp_request
from tests.mcp._tool_calls import META, PROTOCOL


async def test_invalid_arguments_carry_structured_validation_error():
    """Kills the hand-built isError dict: the coded envelope must ride along, redacted."""
    body: dict[str, JsonValue] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "list_jobs",
            "arguments": {"limit": "https://user:s3cret@example.com/x"},
            "_meta": META,
        },
    }
    status, payload = await handle_mcp_request(
        body,
        protocol_version_header=PROTOCOL,
        method_header="tools/call",
        name_header="list_jobs",
        settings=None,
        shutdown_event=None,
    )
    assert status == 200
    assert isinstance(payload, dict)
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    content = result["content"]
    assert isinstance(content, list) and content
    assert isinstance(content[0], dict)
    assert "limit:" in str(content[0]["text"])
    structured = result["structuredContent"]
    assert isinstance(structured, dict)
    assert structured["code"] == "validation_error"
    assert structured["details"]
    wire = json.dumps(result)
    assert "s3cret" not in wire
    assert "[REDACTED]" in wire
