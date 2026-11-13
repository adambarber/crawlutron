"""CLI flag precedence: webhook means async, and an explicit flag beats its env var.

Batteries of the ``crawl`` CLI arg-resolution door. The crawl client is a recording
stand-in so the resolved base_url/api_key are observable without any network traffic.
"""

from __future__ import annotations

import json

import httpx
import pytest

from crawlutron_rest.cli.__main__ import (
    DEFAULT_BASE_URL,
    EXIT_SUCCESS,
    run_crawl,
)
from crawlutron_rest.cli.client import CrawlClient

SERVICE_BASE = "http://service.example"
DEFAULT_URL = "http://127.0.0.1/page"


class _RecordingCrawlClient:
    """A CrawlClient stand-in that records the resolved constructor arguments."""

    instances: list[_RecordingCrawlClient] = []

    def __init__(
        self, base_url: str, api_key: str | None, timeout: float, client: object | None = None
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        type(self).instances.append(self)

    def crawl(self, payload: dict[str, object]) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    def result(self, request_id: str) -> httpx.Response:
        return httpx.Response(404, content=b"")

    def trace(self, request_id: str) -> httpx.Response:
        return httpx.Response(404, content=b"")

    def close(self) -> None:
        pass


def _run_resolved(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, _RecordingCrawlClient]:
    """Run run_crawl with its OWN client construction, capturing the resolved args."""
    _RecordingCrawlClient.instances.clear()
    monkeypatch.setattr("crawlutron_rest.cli.__main__.CrawlClient", _RecordingCrawlClient)
    code = run_crawl(argv, client=None)
    fake = _RecordingCrawlClient.instances[-1]
    return code, fake


def test_webhook_only_submission_handles_the_202_as_async(capsys) -> None:
    """A --webhook-only submit that gets a 202 must be treated as async, exit 0."""
    envelope = {"status": "accepted", "request_id": "r1", "status_url": "/v1/crawl/r1", "webhook_url": None}
    capture: list[httpx.Request] = []

    def accepted(request: httpx.Request) -> httpx.Response:
        capture.append(request)
        return httpx.Response(202, json=envelope, headers={"content-type": "application/json"})

    http_client = httpx.Client(transport=httpx.MockTransport(accepted))
    with CrawlClient(base_url=SERVICE_BASE, api_key=None, timeout=300.0, client=http_client) as client:
        code = run_crawl([DEFAULT_URL, "--webhook", "https://hook.example"], client=client)
    assert code == EXIT_SUCCESS
    assert capture[0].url == f"{SERVICE_BASE}/v1/crawl"
    sent = json.loads(capture[0].content or b"{}")
    assert sent["webhook_url"] == "https://hook.example"
    assert sent["async_mode"] is True
    assert "r1" in capsys.readouterr().out


def test_explicit_base_url_flag_wins_over_the_env_var(monkeypatch, capsys) -> None:
    """An explicit --base-url flag overrides the CRAWLUTRON_BASE_URL env var."""
    monkeypatch.setenv("CRAWLUTRON_BASE_URL", "http://env.example")
    code, fake = _run_resolved([DEFAULT_URL, "--base-url", "http://flag.example"], monkeypatch)
    assert code == EXIT_SUCCESS
    assert fake.base_url == "http://flag.example"


def test_env_base_url_fills_in_when_no_flag_is_given(monkeypatch, capsys) -> None:
    """Absent --base-url, the env var still wins over the compiled default."""
    monkeypatch.setenv("CRAWLUTRON_BASE_URL", "http://env.example")
    code, fake = _run_resolved([DEFAULT_URL], monkeypatch)
    assert code == EXIT_SUCCESS
    assert fake.base_url == "http://env.example"


def test_default_base_url_when_flag_and_env_are_absent(monkeypatch, capsys) -> None:
    """Neither --base-url nor the env var falls back to the compiled default."""
    monkeypatch.delenv("CRAWLUTRON_BASE_URL", raising=False)
    code, fake = _run_resolved([DEFAULT_URL], monkeypatch)
    assert code == EXIT_SUCCESS
    assert fake.base_url == DEFAULT_BASE_URL


@pytest.mark.parametrize(
    ("flag", "envvar", "flag_value", "env_value"),
    [
        pytest.param("--key", "CRAWLUTRON_API_KEY", "flag-key", "env-key", id="key"),
        pytest.param(
            "--base-url", "CRAWLUTRON_BASE_URL", "http://flag.example", "http://env.example", id="base-url"
        ),
    ],
)
def test_credential_and_target_flags_override_their_env_vars(
    monkeypatch, capsys, flag: str, envvar: str, flag_value: str, env_value: str
) -> None:
    """Contract: every credential/target flag beats its env var when both are set."""
    monkeypatch.setenv(envvar, env_value)
    code, fake = _run_resolved([DEFAULT_URL, flag, flag_value], monkeypatch)
    assert code == EXIT_SUCCESS
    if flag == "--key":
        assert fake.api_key == flag_value
    else:
        assert fake.base_url == flag_value
