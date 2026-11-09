"""MCP read_crawl_result / read_crawl_trace over the stored crawl record.

The tools answer from the same stored rows the REST replay doors read, never
re-validating URL fields; a still-running job is a STRUCTURED NON-ERROR result
(status: running), not an is_error tool result.
"""

from __future__ import annotations

import pytest
from typing_extensions import TypedDict

from crawlutron.api.crawl_endpoint import read_crawl_result_record, read_crawl_trace_record
from crawlutron.mcp.handlers import ToolResult
from crawlutron.mcp.handlers import dispatch as tool_dispatch
from crawlutron.mcp.registry import TOOLS_BY_NAME
from crawlutron.models.crawl_result_request import CrawlResultRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
from crawlutron.storage.job_store import JobStatus
from tests._assertions import completed_crawl
from tests._fetcher_factories import make_crawl_payload
from tests._policy_factories import canonical
from tests.mcp.test_mcp import _META, _body


class _ToolCallRecord(TypedDict):
    """The wire shape of one successful ``tools/call`` result."""

    isError: bool
    structuredContent: JsonValue


class _ToolArguments(TypedDict):
    """The arguments of one read tool's ``tools/call``."""

    request_id: str


async def _tool_call(client, name: str, arguments: _ToolArguments) -> _ToolCallRecord:
    """One tools/call over the full app; returns its result object."""
    body = _body(
        "tools/call",
        params={
            "name": name,
            "arguments": dict[str, JsonValue](**arguments),
            "_meta": _META,
        },
    )
    from tests.mcp.test_mcp import _assert_complete_tool

    result: JsonValue = await _assert_complete_tool(client, body)
    assert isinstance(result, dict)
    structured: JsonValue = result.get("structuredContent")
    return {"isError": bool(result.get("isError", False)), "structuredContent": structured}


async def _tool_call_matches_rest(
    async_client,
    name: str,
    request_id: str,
    *,
    trace: bool = False,
) -> _ToolCallRecord:
    """The tool's structured answer must equal the REST door's JSON for the same row."""
    rest_path = f"/v1/crawl/{request_id}" + ("/trace" if trace else "")
    rest = await async_client.get(rest_path)
    assert rest.status_code == 200, rest.text
    tool = await _tool_call(async_client, name, {"request_id": request_id})
    assert tool["isError"] is False
    assert tool["structuredContent"] == rest.json()
    return tool


#: The minimal stored envelope a completed single-shot job row carries.
_EMPTY_ENVELOPE = UnifiedCrawlResponse(
    request_id="REPLACED",
    results=[],
    submitted_count=0,
    policy_schema_version="",
    success=True,
)


async def _seed_completed_job(job_store, request_id: str, stored: UnifiedCrawlResponse) -> JsonValue:
    """Persist a completed single-shot job whose stored envelope is the model's dump."""
    await job_store.acreate_single_shot(request_id, "http://example.com/", {})
    job = await job_store.aload(request_id)
    record = {**stored.model_dump(), "request_id": request_id}
    job.results = [record]
    await job_store.asave(job)
    await job_store.aset_status(request_id, JobStatus.COMPLETED, expected=JobStatus.PENDING)
    return record


async def _read_result(request_id: str, job_store) -> ToolResult:
    return await tool_dispatch(
        TOOLS_BY_NAME["read_crawl_result"],
        CrawlResultRequest(request_id=request_id),
        settings=None,
        shutdown_event=None,
        job_store=job_store,
    )


async def _read_trace(request_id: str, job_store) -> ToolResult:
    return await tool_dispatch(
        TOOLS_BY_NAME["read_crawl_trace"],
        CrawlResultRequest(request_id=request_id),
        settings=None,
        shutdown_event=None,
        job_store=job_store,
    )


def _assert_running(result: ToolResult, request_id: str) -> None:
    """P1's shape: a running job is a structured non-error, never is_error."""
    assert result.is_error is False
    assert result.structured == {"status": "running", "request_id": request_id, "crawl_status": "pending"}


async def _completed_request_id(async_client, base_url) -> str:
    """Crawl a helper-server page synchronously; return its request_id."""
    crawled = await completed_crawl(async_client, make_crawl_payload(base_url))
    request_id = crawled["request_id"]
    assert isinstance(request_id, str)
    return request_id


class TestReadCrawlResultTool:
    async def test_the_tool_replays_the_same_stored_response_as_rest(self, async_client, base_url):
        await _tool_call_matches_rest(
            async_client, "read_crawl_result", await _completed_request_id(async_client, base_url)
        )

    async def test_a_running_job_is_a_structured_running_result_not_an_error(self, job_store):
        await job_store.acreate_single_shot("crawl_running", "http://example.com/", {})

        _assert_running(await _read_result("crawl_running", job_store), "crawl_running")

    async def test_a_completed_single_shot_job_answers_from_the_durable_row(self, job_store):
        await _seed_completed_job(job_store, "crawl_done", _EMPTY_ENVELOPE)

        result = await _read_result("crawl_done", job_store)

        assert result.is_error is False
        assert result.structured == {**_EMPTY_ENVELOPE.model_dump(), "request_id": "crawl_done"}

    async def test_an_unknown_request_id_is_a_tool_error(self, job_store):
        result = await _read_result("crawl_never_happened", job_store)

        assert result.is_error is True
        assert "crawl_never_happened" in result.text


class TestReadCrawlTraceTool:
    async def test_the_tool_returns_the_same_trace_as_rest(self, async_client, base_url):
        request_id = await _completed_request_id(async_client, base_url)

        result = await _tool_call_matches_rest(async_client, "read_crawl_trace", request_id, trace=True)

        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        results = structured["results"]
        assert isinstance(results, list) and len(results) > 0
        page = results[0]
        assert isinstance(page, dict)
        for absent in ("html", "text", "markdown"):
            assert absent not in page

    async def test_a_running_job_trace_is_a_structured_running_result_not_an_error(self, job_store):
        await job_store.acreate_single_shot("crawl_running", "http://example.com/", {})

        _assert_running(await _read_trace("crawl_running", job_store), "crawl_running")

    async def test_an_unknown_request_id_trace_is_a_tool_error(self, job_store):
        result = await _read_trace("crawl_never_happened", job_store)

        assert result.is_error is True


class TestNoRevalidationOnTheReplayPath:
    async def test_the_core_serves_a_payload_the_models_would_refuse(self, job_store):
        """The stored row is read as stored: a URL string the NormalizedUrl
        constructor would refuse must still replay untouched (admission is the
        constructor's job, not the cache's)."""
        seed = await _seed_completed_job(
            job_store,
            "crawl_odd",
            UnifiedCrawlResponse(
                request_id="REPLACED",
                results=[
                    UnifiedCrawlResult(
                        original_url="http://admitted.example/",
                        normalized_url=canonical("http://admitted.example/"),
                        status="success",
                        classification="rendered",
                        attempted_strategies=[],
                        classification_evidence=[],
                    )
                ],
                submitted_count=1,
                policy_schema_version="",
                success=True,
            ),
        )
        # The stored row is then mutated the way a constructor-refusing string would
        # arrive from an older build: the read must serve it as stored, unchanged.
        seed_map = seed
        assert isinstance(seed_map, dict)
        seed_results = seed_map["results"]
        assert isinstance(seed_results, list) and len(seed_results) > 0
        first_seed = seed_results[0]
        assert isinstance(first_seed, dict)
        first_seed["normalized_url"] = "http://stored-as-written.example/"
        job = await job_store.aload("crawl_odd")
        job.results = [seed]
        await job_store.asave(job)

        record = await read_crawl_result_record("crawl_odd", job_store, None)

        assert isinstance(record, dict)
        record_results = record["results"]
        assert isinstance(record_results, list) and len(record_results) > 0
        first = record_results[0]
        assert isinstance(first, dict) and isinstance(first.get("normalized_url"), str)
        assert first["normalized_url"] == "http://stored-as-written.example/"
        trace = await read_crawl_trace_record("crawl_odd", job_store, None)
        assert trace.results[0].normalized_url == "http://stored-as-written.example/"


class TestDiscoveryAndParity:
    def test_capabilities_name_the_full_mcp_tool_set(self):
        from crawlutron.config.settings import CrawlerSettings
        from crawlutron.discovery.capabilities import capabilities_payload
        from crawlutron.mcp.registry import TOOL_SPECS

        mcp = capabilities_payload(CrawlerSettings.from_mapping({}))["mcp"]
        assert mcp["tools"] == [spec.name for spec in TOOL_SPECS]

    def test_agent_hints_mention_the_read_tools_and_polling(self):
        from crawlutron.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "read_crawl_result" in guidance
        assert "read_crawl_trace" in guidance

    def test_tool_descriptions_cross_reference_polling(self):
        from crawlutron.mcp.registry import TOOLS_BY_NAME

        assert "read_crawl_result" in TOOLS_BY_NAME["crawl"].description
        assert "read_crawl_result" in TOOLS_BY_NAME["read_crawl_result"].description
        assert "read_crawl_result" in TOOLS_BY_NAME["read_crawl_trace"].description


@pytest.mark.parametrize("request_id", ["crawl_missing"])
async def test_the_read_cores_raise_the_typed_pending_error_for_running_jobs(job_store, request_id):
    from crawlutron.models.crawl_pending import CrawlPendingError

    await job_store.acreate_single_shot(request_id, "http://example.com/", {})

    with pytest.raises(CrawlPendingError) as raised:
        await read_crawl_result_record(request_id, job_store, None)

    assert raised.value.job_status == "pending"
    assert raised.value.request_id == request_id
