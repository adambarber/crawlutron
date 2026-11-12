"""MCP policy_suggestions tool over the analysis core (the REST door's twin).

The tool is a door over :mod:`crawlutron.service.policy_suggestions` -- zero logic duplication: the same
rollup, the same per-tenant scoping, the same refusal of anonymous callers.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from crawlutron_rest.api.mcp_router import router as mcp_router
from tests._async_harnesses import issue_key
from tests._policy_factories import canonical
from tests._policy_harnesses import stored_trace
from tests._quota_harness import keyed_result_client
from tests.mcp._tool_calls import KeyedToolCaller

if TYPE_CHECKING:
    from httpx import AsyncClient

    from crawlutron.models.json_value import JsonValue
    from crawlutron.storage.caller_key_store import CallerKeyStore
    from crawlutron.storage.result_store import CrawlResultStore

BLOCKED_URL = "https://blocked.test/x"


def _stored_trace(request_id: str, url: str) -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", stored_trace(request_id, url))


class SuggestionHarness(KeyedToolCaller):
    """One keyed harness: the client, the result store, and the caller's secret."""

    def __init__(self, client: AsyncClient, result_store: CrawlResultStore, secret: str) -> None:
        self.client = client
        self.result_store = result_store
        self.secret = secret


async def other_caller(h: SuggestionHarness) -> SuggestionHarness:
    """A second keyed caller on the same harness client (the cross-visibility actor)."""
    return SuggestionHarness(client=h.client, result_store=h.result_store, secret=await issue_key(h.client))


@contextlib.asynccontextmanager
async def _suggestion_app_client(
    tmp_path: Path,
) -> AsyncGenerator[tuple[AsyncClient, CrawlResultStore, CallerKeyStore], None]:
    """An app over /mcp + /v1/policy-suggestions + keys, one isolated triple of stores."""
    from crawlutron_rest.api.keys_router import router as keys_router
    from crawlutron_rest.api.policy_router import router as policy_router

    async with keyed_result_client(tmp_path, (mcp_router, policy_router, keys_router)) as (
        client,
        results,
        keys,
    ):
        yield client, results, keys


@pytest.fixture
async def suggestion_harness(
    tmp_path: Path,
) -> AsyncGenerator[tuple[SuggestionHarness, CallerKeyStore], None]:
    """A keyed client over /mcp + /v1/policy-suggestions sharing one result store."""
    async with _suggestion_app_client(tmp_path) as (client, results, keys):
        secret = await issue_key(client)
        yield SuggestionHarness(client=client, result_store=results, secret=secret), keys


async def _seed_blocked(h: SuggestionHarness, request_id: str, key_id: str) -> None:
    await h.result_store.aput(request_id, _stored_trace(request_id, BLOCKED_URL), owner=key_id)


async def _seeded_for_owner(
    suggestion_harness: tuple[SuggestionHarness, CallerKeyStore],
) -> SuggestionHarness:
    """The harness with one blocked row stored under the caller's key id."""
    h, keys = suggestion_harness
    record = keys.verify_key(h.secret)
    assert record is not None
    await _seed_blocked(h, "crawl_a", record.key_id)
    return h


class TestPolicySuggestionsTool:
    async def test_the_tool_returns_the_same_rollup_as_rest(self, suggestion_harness):
        h = await _seeded_for_owner(suggestion_harness)

        structured = await h.structured("policy_suggestions", {})

        assert structured == await h.rest_json("get", "/v1/policy-suggestions")
        rollup = cast("dict[str, object]", structured)
        suggestions = cast("list[object]", rollup["suggestions"])
        first = cast("dict[str, object]", suggestions[0])
        assert first["host"] == canonical(BLOCKED_URL).site

    async def test_an_empty_store_answers_zero_records_not_an_error(self, suggestion_harness):
        h, _ = suggestion_harness

        structured = await h.structured("policy_suggestions", {})

        assert structured == {"records_examined": 0, "records_skipped": 0, "suggestions": []}


class TestPolicySuggestionsCallerScoping:
    async def test_another_callers_evidence_is_invisible_to_the_tool(self, suggestion_harness):
        h = await _seeded_for_owner(suggestion_harness)
        other = await other_caller(h)

        listed = cast("dict[str, object]", await other.structured("policy_suggestions", {}))

        assert listed["suggestions"] == [], "a foreign caller's evidence leaked into the rollup"
        assert listed["records_examined"] == 0


class TestPolicySuggestionsAuth:
    async def test_an_anonymous_tools_call_is_refused(self, tmp_path: Path):
        async with _suggestion_app_client(tmp_path) as (client, _results, _keys):
            text = await tool_error_text_anonymous(client)

        assert "API key" in text


async def tool_error_text_anonymous(client: AsyncClient) -> str:
    """One unauthenticated tools/call (no credential at all); returns the refusal text."""
    from tests.mcp._tool_calls import META, tool_call

    try:
        result = await tool_call(client, "policy_suggestions", {}, key="anonymous-scope")
        assert result["isError"] is True, result
        return str(result["content"][0]["text"])
    except AssertionError as error:
        if "Unknown or revoked API key" in str(error):
            # Bogus credential refused at the auth layer -- that IS the anonymous refusal;
            # re-send credentialless so the tool-level refusal is the thing asserted.
            from tests.mcp._tool_calls import _mcp_headers

            headers = _mcp_headers(key="x", tool="policy_suggestions")
            del headers["X-API-Key"]
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "policy_suggestions", "arguments": {}, "_meta": META},
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result["isError"] is True, result
            return str(result["content"][0]["text"])
        raise


class TestPolicySuggestionsDiscovery:
    def test_agent_hints_mention_the_policy_suggestions_tool(self):
        from crawlutron_rest.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "policy_suggestions" in guidance

    def test_the_advertised_schema_accepts_the_arguments_the_tool_takes(self):
        """An empty-argument tool advertises an object schema with nothing required."""
        from crawlutron.mcp.registry import TOOLS_BY_NAME

        spec = TOOLS_BY_NAME["policy_suggestions"]
        schema = cast("dict[str, object]", spec.input_schema)
        assert schema["type"] == "object"
        assert cast("list[object]", schema["required"]) == []
