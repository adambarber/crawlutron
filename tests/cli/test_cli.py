"""CLI behavior: exit codes, output modes, async/wait, error passthrough, Docker lifecycle.

All service calls are hermetic: httpx.MockTransport stands in for the crawl
service, and subprocess is scripted for ``crawl up``. No real Docker daemon,
no network, no SSRF/policy imports in the CLI package.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
from pathlib import Path

import httpx

from crawlutron.cli.__main__ import (
    EXIT_PARTIAL,
    EXIT_SERVER_OR_TRANSPORT,
    EXIT_SIGINT,
    EXIT_SUCCESS,
    EXIT_USAGE,
    run_crawl,
)
from crawlutron.cli.client import CrawlClient
from crawlutron.cli.docker import run_up

SERVICE_BASE = "http://service.example"
DEFAULT_URL = "http://127.0.0.1/page"


def _transport(handler):
    return httpx.MockTransport(handler)


def _json_handler(status_code: int, payload: object, capture: list[httpx.Request] | None = None):
    """Return a MockTransport handler that answers ``status_code`` with ``payload``."""

    def handle(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        return httpx.Response(status_code, content=body, headers={"content-type": "application/json"})

    return handle


def _failing_handler(exc: BaseException):
    def handle(request: httpx.Request) -> httpx.Response:
        raise exc

    return handle


def _default_transport(capture: list[httpx.Request]):
    """The default successful crawl service: 200 with a minimal result envelope."""
    return _transport(_json_handler(200, {"request_id": "r1", "success": True, "results": []}, capture))


def _success_response() -> httpx.Response:
    """A terminal successful crawl result as an httpx response."""
    return httpx.Response(
        200,
        content=json.dumps({"request_id": "r1", "success": True, "results": []}).encode(),
        headers={"content-type": "application/json"},
    )


def _exit_on_result(body: dict[str, object], exit_on: str, expected: int) -> None:
    """Submit a completed crawl body and assert the ``--exit-on`` code."""
    transport = _transport(_json_handler(200, body))
    code = _run([], [DEFAULT_URL, "--exit-on", exit_on], transport=transport)
    assert code == expected


def _up(run, args: list[str]) -> int:
    """Run ``crawl up`` with the scripted docker runner."""
    return run_up(args, run=run)


def _up_ok(run, args: list[str]) -> None:
    """Run ``crawl up`` and assert it exits successfully."""
    code = _up(run, args)
    assert code == EXIT_SUCCESS


GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"


def _golden_body(name: str) -> bytes:
    return (GOLDEN_DIR / name).read_bytes()


def _crawl_client(transport, api_key: str | None = None) -> CrawlClient:
    http_client = httpx.Client(transport=transport)
    return CrawlClient(base_url=SERVICE_BASE, api_key=api_key, timeout=300.0, client=http_client)


def _run(capture: list[httpx.Request], argv: list[str], api_key: str | None = None, transport=None):
    if transport is None:
        transport = _default_transport(capture)
    with _crawl_client(transport, api_key) as client:
        return run_crawl(argv, client=client)


def test_cli_package_has_no_admission_policy_or_ssrf_imports():
    """Contract pin: the CLI package imports no admission/SSRF/policy validation internals."""
    import crawlutron.cli

    package = os.path.dirname(crawlutron.cli.__file__)
    forbidden = (
        "crawlutron.admission",
        "crawlutron.models.host_policy",
        "crawlutron.models.normalized_url",
        "crawlutron.safety",
        "validate_wire_policy",
    )
    for root, _dirs, files in os.walk(package):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert node.module is None or not any(
                        node.module.startswith(forbidden_mod) for forbidden_mod in forbidden
                    ), f"{path} imports {node.module}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not any(alias.name.startswith(forbidden_mod) for forbidden_mod in forbidden), (
                            f"{path} imports {alias.name}"
                        )


def test_exit_code_200_returns_zero_and_prints_body(capsys):
    capture: list[httpx.Request] = []
    code = _run(capture, [DEFAULT_URL])
    assert code == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "r1" in out
    assert capture[0].url == f"{SERVICE_BASE}/v1/crawl"
    assert json.loads(capture[0].content or b"{}")["url"] == DEFAULT_URL


def test_exit_code_422_returns_two_and_surfaces_envelope(capsys):
    envelope = {"code": "validation_error", "message": "Request validation failed", "details": {}}
    transport = _transport(_json_handler(422, envelope))
    code = _run([], [DEFAULT_URL, "--output", "csv"], transport=transport)
    assert code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "validation_error" in err


def test_exit_code_401_returns_two(capsys):
    envelope = {"code": "unauthenticated", "message": "Invalid API key", "details": {}}
    transport = _transport(_json_handler(401, envelope))
    code = _run([], [DEFAULT_URL], transport=transport)
    assert code == EXIT_USAGE


def test_exit_code_500_returns_three(capsys):
    transport = _transport(_json_handler(500, {"code": "internal_error", "message": "boom", "details": {}}))
    code = _run([], [DEFAULT_URL], transport=transport)
    assert code == EXIT_SERVER_OR_TRANSPORT


def test_exit_code_connect_error_returns_three(capsys):
    request = httpx.Request("POST", f"{SERVICE_BASE}/v1/crawl")
    transport = _transport(_failing_handler(httpx.ConnectError("refused", request=request)))
    code = _run([], [DEFAULT_URL], transport=transport)
    assert code == EXIT_SERVER_OR_TRANSPORT


def test_exit_code_timeout_returns_three(capsys):
    request = httpx.Request("POST", f"{SERVICE_BASE}/v1/crawl")
    transport = _transport(_failing_handler(httpx.ReadTimeout("timed out", request=request)))
    code = _run([], [DEFAULT_URL], transport=transport)
    assert code == EXIT_SERVER_OR_TRANSPORT


def test_exit_code_keyboard_interrupt_returns_130(capsys):
    transport = _transport(_failing_handler(KeyboardInterrupt()))
    code = _run([], [DEFAULT_URL], transport=transport)
    assert code == EXIT_SIGINT


def test_output_mode_markdown_prints_body_byte_for_byte(capsys):
    body = b"---\nmarkdown content\n"
    transport = _transport(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "text/markdown; charset=utf-8"}
        )
    )
    code = _run([], [DEFAULT_URL, "--output", "markdown"], transport=transport)
    assert code == EXIT_SUCCESS
    assert capsys.readouterr().out == "---\nmarkdown content\n"


def test_output_mode_csv_prints_body_byte_for_byte(capsys):
    body = b"original_url,status\nhttp://127.0.0.1/page,ok\n"
    transport = _transport(
        lambda request: httpx.Response(200, content=body, headers={"content-type": "text/csv; charset=utf-8"})
    )
    code = _run([], [DEFAULT_URL, "--output", "csv"], transport=transport)
    assert code == EXIT_SUCCESS
    assert capsys.readouterr().out == "original_url,status\nhttp://127.0.0.1/page,ok\n"


def test_golden_body_printed_byte_for_byte(capsys):
    """The CLI echoes the service body faithfully; golden body lives under
    tests/cli/fixtures/golden/ (referenced by the issue brief)."""
    golden = _golden_body("crawl_success.json")
    transport = _transport(
        lambda request: httpx.Response(200, content=golden, headers={"content-type": "application/json"})
    )
    code = _run([], [DEFAULT_URL], transport=transport)
    assert code == EXIT_SUCCESS
    assert capsys.readouterr().out == golden.decode("utf-8")


def test_api_key_sent_as_x_api_key_header(capsys):
    capture: list[httpx.Request] = []
    transport = _default_transport(capture)
    with _crawl_client(transport, api_key="secret-key") as client:
        code = run_crawl([DEFAULT_URL, "--key", "secret-key"], client=client)
    assert code == EXIT_SUCCESS
    assert capture[0].headers.get("x-api-key") == "secret-key"


def test_api_key_from_env(monkeypatch, capsys):
    monkeypatch.setenv("CRAWLUTRON_API_KEY", "env-key")
    capture: list[httpx.Request] = []
    transport = _default_transport(capture)
    with _crawl_client(transport, api_key="env-key") as client:
        code = run_crawl([DEFAULT_URL], client=client)
    assert code == EXIT_SUCCESS
    assert capture[0].headers.get("x-api-key") == "env-key"


def test_flags_map_to_wire_fields(capsys):
    capture: list[httpx.Request] = []
    transport = _default_transport(capture)
    with _crawl_client(transport) as client:
        code = run_crawl(
            [
                DEFAULT_URL,
                "--timeout",
                "10",
                "--max-retries",
                "3",
                "--max-pages",
                "25",
                "--max-depth",
                "2",
                "--strategy",
                "HTTP_DECLARED_CRAWLER",
                "--extraction",
                "trafilatura",
                "--no-robots",
                "--deep-crawl",
                "bfs",
            ],
            client=client,
        )
    assert code == EXIT_SUCCESS
    sent = json.loads(capture[0].content or b"{}")
    policy = sent["policy"]
    assert policy["strategies"] == ["HTTP_DECLARED_CRAWLER"]
    assert policy["extraction"] == "trafilatura"
    limits = policy["limits"]
    assert limits["timeout_seconds"] == 10
    assert limits["max_retries"] == 3
    assert limits["max_pages"] == 25
    assert limits["max_depth"] == 2
    assert limits["respect_robots"] is False


def test_limits_only_flags_reach_policy_without_strategy(capsys):
    """--max-pages/--timeout/--max-retries/--no-robots reach policy.limits with no --strategy."""
    captured: list[httpx.Request] = []
    assert (
        _run(
            captured,
            [DEFAULT_URL, "--max-pages", "50", "--timeout", "60", "--max-retries", "7", "--no-robots"],
        )
        == EXIT_SUCCESS
    )
    body = json.loads(captured[0].content or b"{}")
    policy = body["policy"]
    assert "strategies" not in policy
    assert "extraction" not in policy
    limits = policy["limits"]
    assert limits["timeout_seconds"] == 60
    assert limits["max_retries"] == 7
    assert limits["max_pages"] == 50
    assert limits["respect_robots"] is False


def test_async_returns_202_envelope_and_exits_zero(capsys):
    capture: list[httpx.Request] = []
    envelope = {"status": "accepted", "request_id": "r1", "status_url": "/v1/crawl/r1", "webhook_url": None}
    transport = _transport(_json_handler(202, envelope, capture))
    with _crawl_client(transport) as client:
        code = run_crawl([DEFAULT_URL, "--async"], client=client)
    assert code == EXIT_SUCCESS
    assert capture[0].url == f"{SERVICE_BASE}/v1/crawl"
    assert json.loads(capture[0].content or b"{}")["async_mode"] is True
    out = capsys.readouterr().out
    assert "r1" in out


def test_wait_polls_until_terminal_and_returns_result(capsys):
    state = {"first": True}

    def handle(request: httpx.Request) -> httpx.Response:
        if state["first"]:
            state["first"] = False
            return httpx.Response(
                202,
                content=json.dumps(
                    {
                        "status": "accepted",
                        "request_id": "r1",
                        "status_url": "/v1/crawl/r1",
                        "webhook_url": None,
                    }
                ).encode(),
                headers={"content-type": "application/json"},
            )
        return _success_response()

    transport = _transport(handle)
    with _crawl_client(transport) as client:
        code = run_crawl([DEFAULT_URL, "--wait"], client=client)
    assert code == EXIT_SUCCESS


def test_exit_on_partial_success_false_returns_one():
    _exit_on_result({"request_id": "r1", "success": False, "results": []}, "partial", EXIT_PARTIAL)


def test_exit_on_partial_success_true_returns_zero():
    _exit_on_result({"request_id": "r1", "success": True, "results": []}, "partial", EXIT_SUCCESS)


def test_exit_on_any_result_error_returns_one():
    body = {"request_id": "r1", "success": True, "results": [{"url": DEFAULT_URL, "error": "not_found"}]}
    _exit_on_result(body, "any", EXIT_PARTIAL)


def test_exit_on_any_no_errors_returns_zero():
    body = {"request_id": "r1", "success": True, "results": [{"url": DEFAULT_URL}]}
    _exit_on_result(body, "any", EXIT_SUCCESS)


def test_exit_on_none_ignores_partial_results():
    body = {"request_id": "r1", "success": False, "results": [{"url": DEFAULT_URL, "error": "not_found"}]}
    _exit_on_result(body, "none", EXIT_SUCCESS)


def test_trace_prints_trace_after_crawl(capsys):
    trace_capture: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "/trace" in request.url.path:
            trace_capture.append(request)
        return _success_response()

    transport = _transport(handle)
    with _crawl_client(transport) as client:
        code = run_crawl([DEFAULT_URL, "--trace"], client=client)
    assert code == EXIT_SUCCESS
    assert trace_capture


def test_main_with_no_args_returns_usage():
    from crawlutron.cli.__main__ import main

    assert main([]) == EXIT_USAGE


def test_main_help_returns_zero():
    from crawlutron.cli.__main__ import main

    assert main(["--help"]) == EXIT_SUCCESS


def _docker_run_script(image_present: bool = True):
    calls = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if len(argv) >= 3 and argv[1] == "image" and argv[2] == "inspect":
            return subprocess.CompletedProcess(argv, 0 if image_present else 1, "", "")
        if len(argv) >= 2 and argv[1] == "pull":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "curl":
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        return subprocess.CompletedProcess(argv, 0, "container-1", "")

    return run, calls


def test_run_up_image_present_starts_container_and_waits_for_health():
    run, calls = _docker_run_script()
    _up_ok(run, ["--image", "crawlutron/crawlutron", "--tag", "1.2.3", "--yes"])
    assert any(c[1:3] == ["image", "inspect"] for c in calls)
    assert any(c[1] == "run" for c in calls)
    assert any(c[0] == "curl" for c in calls)


def test_run_up_missing_image_without_yes_returns_usage(monkeypatch):
    monkeypatch.setattr("crawlutron.cli.docker.sys.stdin", io.StringIO("n\n"))
    run, calls = _docker_run_script(image_present=False)
    code = _up(run, ["--image", "crawlutron/crawlutron", "--tag", "1.2.3"])
    assert code == EXIT_USAGE
    assert not any(c[1] == "pull" for c in calls)


def test_run_up_missing_image_with_yes_pulls_and_starts():
    run, calls = _docker_run_script(image_present=False)
    _up_ok(run, ["--image", "crawlutron/crawlutron", "--tag", "1.2.3", "--yes"])
    assert any(c[1] == "pull" for c in calls)
    assert any(c[1] == "run" for c in calls)


def test_run_up_refuses_latest_tag():
    run, calls = _docker_run_script()
    code = _up(run, ["--image", "crawlutron/crawlutron", "--tag", "latest"])
    assert code == EXIT_USAGE
    assert not any(c[1] == "pull" for c in calls)
    assert not any(c[1] == "run" for c in calls)


def test_run_up_no_wait_for_health_starts_without_curl():
    run, calls = _docker_run_script()
    code = _up(
        run,
        ["--image", "crawlutron/crawlutron", "--tag", "1.2.3", "--yes", "--no-wait-for-health"],
    )
    assert code == EXIT_SUCCESS
    assert any(c[1] == "run" for c in calls)
    assert not any(c[0] == "curl" for c in calls)
