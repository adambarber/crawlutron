"""README quickstart curl stays derived from the live crawl route and request model."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

import httpx
from fastapi.routing import APIRoute

from crawlutron.models.crawl_request import CrawlRequest
from crawlutron_rest.api.crawl_router import router
from crawlutron_sdk import CrawlRequest as SdkCrawlRequest
from crawlutron_sdk import CrawlutronClient
from tests.chokepoint_paths import REPO_ROOT

_CURL = re.compile(r"curl\s+[^\n]*?(https?://[^\s'\"\\]+)")
_BODY = re.compile(r"-d\s+'([^']+)'")


def _readme_text() -> str:
    """The live README, read on every call so a planted edit is seen."""
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _quickstart(text: str) -> list[tuple[int, str]]:
    """Numbered lines of the quickstart section, or empty when it is absent."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## Quickstart")), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [(i, lines[i]) for i in range(start, end)]


def _commands(section: list[tuple[int, str]]):
    """Yield (line index, joined command) for each curl command in a section."""
    first = None
    command = ""
    for number, raw in section:
        line = raw.strip()
        if first is None and line.startswith("curl "):
            first, command = number, line
        elif first is not None:
            command += " " + line
        if first is not None and not line.endswith("\\"):
            yield first, command
            first, command = None, ""


def _live_route() -> tuple[str, set[str]]:
    """The path and verbs of the registered crawl handler, not a hand mirror."""
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if getattr(route.endpoint, "__name__", None) == "crawl":
            return route.path, set(route.methods or ())
    raise AssertionError("the live crawl route is not registered")


def _offenders(text: str) -> list[str]:
    """Every quickstart curl claim that no longer matches the live contract."""
    section = _quickstart(text)
    if not section:
        return ["README.md: quickstart section is missing"]
    failures: list[str] = []
    live_path, methods = _live_route()
    fields = set(CrawlRequest.model_fields)
    commands = list(_commands(section))
    if not commands:
        failures.append("README.md: quickstart has no curl command")
    for number, command in commands:
        url_match = _CURL.search(command)
        if url_match is None:
            failures.append(f"README.md:{number + 1}: curl has no URL to derive")
            continue
        path = urlparse(url_match.group(1)).path
        if path != live_path:
            failures.append(f"README.md:{number + 1}: curl path {path!r} != live route {live_path!r}")
        if "POST" not in methods:
            failures.append(f"README.md:{number + 1}: live route {live_path!r} is not POST")
        body_match = _BODY.search(command)
        if body_match is None:
            failures.append(f"README.md:{number + 1}: curl has no JSON body to derive")
            continue
        payload = json.loads(body_match.group(1))
        unknown = set(payload) - fields
        if unknown:
            failures.append(
                f"README.md:{number + 1}: body keys {sorted(unknown)} are not live request fields"
            )
    return failures


def _with_changed_curl(text: str, old: str, new: str) -> str:
    """One curl-command token replaced anywhere in the quickstart section."""
    line = next(row for _, row in _quickstart(text) if old in row)
    return text.replace(line, line.replace(old, new), 1)


def test_readme_quickstart_curl_matches_live_route():
    assert not (failures := _offenders(_readme_text())), "; ".join(failures)


def test_quickstart_contract_bites_a_stale_path():
    stale = _with_changed_curl(_readme_text(), "/v1/crawl", "/v1/crawlx")
    failures = _offenders(stale)
    assert any(
        re.match(r"^README\.md:\d+:", item) and "curl path '/v1/crawlx'" in item for item in failures
    ), failures


def test_quickstart_contract_bites_an_unknown_request_field():
    stale = _with_changed_curl(_readme_text(), '"url"', '"urlz"')
    failures = _offenders(stale)
    assert any(re.match(r"^README\.md:\d+:", item) and "body keys ['urlz']" in item for item in failures), (
        failures
    )


def test_documented_sdk_chain_runs_against_the_wire_shape():
    """The README install/construct/submit/poll example runs on the live SDK."""
    section = "\n".join(row for _, row in _quickstart(_readme_text()))
    assert "pip install crawlutron-sdk" in section
    assert "CrawlutronClient()" in section
    assert "client.crawl_async(CrawlRequest(url=" in section
    assert "client.wait_for(accepted.request_id)" in section

    def reply(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "request_id": "crawl_demo",
                    "status_url": "/v1/crawl/crawl_demo",
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "crawl_demo",
                "submitted_count": 1,
                "policy_schema_version": "2026-06-23",
                "success": True,
                "results": [],
            },
        )

    transport = httpx.MockTransport(reply)

    async def run():
        async with CrawlutronClient(transport=transport) as client:
            accepted = await client.crawl_async(SdkCrawlRequest(url="https://example.com/"))
            return await client.wait_for(accepted.request_id, poll_interval=0)

    result = asyncio.run(run())
    assert result.success is True
    assert result.submitted_count == 1
