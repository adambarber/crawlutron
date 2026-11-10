"""Delta facade parity: MCP schema/tool, CLI flags, SDK pass-through, capabilities.

No network, no browser: the MCP door runs over real stores with a scripted
engine, the CLI and SDK over MockTransport, the capability payload in-process.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from crawlutron.config.settings import CrawlerSettings
from crawlutron.discovery.capabilities import OUTPUT_FIELDS, capabilities_payload
from crawlutron.mcp.handlers import dispatch as tool_dispatch
from crawlutron.mcp.registry import TOOLS_BY_NAME
from crawlutron.models.crawl_request import CrawlRequest
from tests._job_harnesses import ScriptedExecute, make_job_store, scripted_engine
from tests._result_builders import engine_result
from tests.delta._door_harness import assert_removed_summary, delta_stores

URL = "http://example.com/"
CHANGED_SINCE = 1_735_689_600.0


def _sent_body(capture: list[httpx.Request]) -> dict[str, object]:
    """The JSON body the facade sent, read once so every assert stays honest."""
    sent = json.loads(capture[0].content or b"{}")
    assert isinstance(sent, dict)
    return sent


# --- MCP: the advertised schema and the door behavior ---


def test_mcp_crawl_schema_advertises_delta_knobs():
    """The crawl tool's inputSchema offers both delta knobs, derived from the
    request model -- an MCP caller must be able to discover what REST accepts."""
    properties = TOOLS_BY_NAME["crawl"].input_schema["properties"]
    assert isinstance(properties, dict)
    assert "changed_since" in properties
    assert "baseline_request_id" in properties


def _stores(tmpdir: str):
    return delta_stores(tmpdir)


def _seed_baseline(result_store, request_id: str, markdown: str, *, extra_urls=()) -> None:
    results = [
        {
            "original_url": URL,
            "normalized_url": URL,
            "validators": {"etag": '"v1"'},
            "markdown": markdown,
            "html": markdown,
            "text": markdown,
        }
    ]
    results.extend(
        {
            "original_url": url,
            "normalized_url": url,
            "validators": {"etag": '"v1"'},
            "markdown": markdown,
            "html": markdown,
            "text": markdown,
        }
        for url in extra_urls
    )
    result_store.put(
        request_id,
        {"request_id": request_id, "results": results},
        owner="",
    )


def _mcp_engine(monkeypatch, scripted: ScriptedExecute) -> None:
    """The MCP door's patch target: handlers owns the gated engine seam."""
    import crawlutron.mcp.handlers as handlers_mod

    scripted_engine(monkeypatch, handlers_mod, scripted)


async def _mcp_crawl(
    monkeypatch, tmpdir: str, payload: CrawlRequest, *, result_store=None
) -> dict[str, object]:
    """Run the MCP crawl tool dispatch over real stores with a scripted engine."""
    if result_store is None:
        result_store, _ = _stores(tmpdir)
        if payload.baseline_request_id is not None:
            _seed_baseline(result_store, payload.baseline_request_id, "one")
    scripted = ScriptedExecute(
        results=[engine_result(URL, "not_modified", http_status=304, markdown="one", validators={})]
    )
    _mcp_engine(monkeypatch, scripted)
    result = await tool_dispatch(
        TOOLS_BY_NAME["crawl"],
        payload,
        settings=CrawlerSettings.from_mapping(),
        shutdown_event=asyncio.Event(),
        job_store=make_job_store(tmpdir),
        result_store=result_store,
    )
    assert result.is_error is False, result.text
    structured = result.structured
    assert isinstance(structured, dict)
    return {str(k): v for k, v in structured.items()}


def _first_result(response: dict[str, object]) -> dict[str, object]:
    results = response["results"]
    assert isinstance(results, list) and isinstance(results[0], dict)
    return results[0]


async def test_mcp_crawl_classifies_delta_when_knob_set(monkeypatch, tmp_path):
    """The MCP crawl door answers a delta request the same way the REST door
    does: per-result classification and a rolled-up delta_summary."""
    response = await _mcp_crawl(
        monkeypatch, str(tmp_path), CrawlRequest(url=URL, baseline_request_id="baseline-1")
    )
    assert _first_result(response)["delta_classification"] == "unchanged"
    summary = response["delta_summary"]
    assert isinstance(summary, dict)
    assert summary["unchanged"] == 1


async def test_mcp_crawl_without_knobs_stays_delta_free(monkeypatch, tmp_path):
    """No knob, no delta surface: the response is byte-shaped as before."""
    response = await _mcp_crawl(monkeypatch, str(tmp_path), CrawlRequest(url=URL))
    assert _first_result(response)["delta_classification"] is None
    assert response["delta_summary"] is None


async def test_mcp_crawl_reports_removed_baseline_url(monkeypatch, tmp_path):
    """A baseline URL the MCP run never answers is reported removed, matching
    the REST door's removed semantics (one shared shaping home)."""
    tmpdir = str(tmp_path)
    removed_url = "http://example.com/gone/"
    result_store, _ = _stores(tmpdir)
    _seed_baseline(result_store, "baseline-1", "one", extra_urls=(removed_url,))
    response = await _mcp_crawl(
        monkeypatch,
        tmpdir,
        CrawlRequest(url=URL, baseline_request_id="baseline-1"),
        result_store=result_store,
    )
    summary = response["delta_summary"]
    assert isinstance(summary, dict)
    assert_removed_summary(summary, removed_url)


# --- CLI: flags map 1:1 to wire fields, omitted when unset ---


def _capture_handler(capture: list[httpx.Request], response: httpx.Response):
    """A MockTransport handler that records the request and answers ``response``."""

    def handler(request: httpx.Request) -> httpx.Response:
        capture.append(request)
        return response

    return handler


def _run_cli(capture: list[httpx.Request], argv: list[str]) -> int:
    from crawlutron.cli.__main__ import run_crawl
    from crawlutron.cli.client import CrawlClient

    handler = _capture_handler(
        capture, httpx.Response(200, json={"request_id": "r1", "success": True, "results": []})
    )

    client = CrawlClient(
        base_url="http://service.example",
        api_key=None,
        timeout=300.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        return run_crawl(argv, client=client)
    finally:
        client.close()


def test_cli_delta_flags_map_to_wire_fields():
    capture: list[httpx.Request] = []
    code = _run_cli(
        capture,
        [
            "http://127.0.0.1/page",
            "--changed-since",
            "1730000000",
            "--baseline-request-id",
            "req_abc123",
        ],
    )
    assert code == 0
    sent = _sent_body(capture)
    assert sent["changed_since"] == 1730000000.0
    assert sent["baseline_request_id"] == "req_abc123"


def test_cli_omits_delta_fields_when_unset():
    capture: list[httpx.Request] = []
    code = _run_cli(capture, ["http://127.0.0.1/page"])
    assert code == 0
    sent = _sent_body(capture)
    assert "changed_since" not in sent
    assert "baseline_request_id" not in sent


# --- SDK: the client sends the knobs it already models ---


async def test_sdk_client_sends_delta_knobs():
    from crawlutron_sdk import CrawlutronClient
    from crawlutron_sdk.models import CrawlRequest as SdkCrawlRequest

    capture: list[httpx.Request] = []
    handler = _capture_handler(
        capture,
        httpx.Response(
            200,
            json={
                "request_id": "abc",
                "submitted_count": 1,
                "policy_schema_version": "1",
                "success": True,
                "results": [],
                "delta_summary": {"unchanged": 1, "considered": 1},
            },
        ),
    )

    client = CrawlutronClient(transport=httpx.MockTransport(handler), base_url="http://test")
    try:
        response = await client.crawl(
            SdkCrawlRequest(
                url="https://example.com/",
                changed_since=1730000000.0,
                baseline_request_id="req_abc123",
            )
        )
    finally:
        await client.aclose()
    sent = _sent_body(capture)
    assert sent["changed_since"] == 1730000000.0
    assert sent["baseline_request_id"] == "req_abc123"
    assert response.delta_summary == {"unchanged": 1, "considered": 1}


# --- Capabilities/discovery: the flag and the fields are advertised ---


def test_capabilities_advertises_delta_crawl():
    features = capabilities_payload(CrawlerSettings.from_mapping())["features"]
    assert features["delta_crawl"] is True


def test_capabilities_output_fields_name_delta_fields():
    for field in ("delta_classification", "delta_evidence"):
        assert field in OUTPUT_FIELDS


def test_agent_hints_describe_delta_semantics_honestly():
    from crawlutron.discovery.agent_hints import agent_hints_payload

    guidance = " ".join(agent_hints_payload()["guidance"])
    assert "changed_since" in guidance
    assert "baseline_request_id" in guidance
    assert "baseline_expired" in guidance


def test_request_model_docs_no_longer_call_delta_a_future_knob():
    """The slice-1 'stored as a knob only ... changes nothing yet' wording is
    stale now that the semantics are implemented: the schema text must not
    promise that setting the knobs does nothing."""
    schema = CrawlRequest.model_json_schema()
    changed = str(schema["properties"]["changed_since"]["description"])
    baseline = str(schema["properties"]["baseline_request_id"]["description"])
    assert "changes nothing yet" not in changed
    assert "changes nothing yet" not in baseline
