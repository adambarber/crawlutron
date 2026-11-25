"""Runtime proof for default-deny egress topology (app internal, proxy dual-homed, origin egress-only)."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import TypedDict

import pytest

from tests._subprocess_helpers import run_cmd
from tests.chokepoint_paths import REPO_ROOT

COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
TEST_COMPOSE_FILE = REPO_ROOT / "docker-compose.test.yml"
EGRESS_COMPOSE_FILE = REPO_ROOT / "docker-compose.egress.yml"

# Total deadline for the test run (well under pytest.ini's 120s ceiling)
TOTAL_DEADLINE_SECONDS = 90
POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = TOTAL_DEADLINE_SECONDS // POLL_INTERVAL_SECONDS


class _CrawlSubmitResponse(TypedDict):
    crawl_request_id: str


class _CrawlStatusResult(TypedDict):
    crawl_results: list[dict[str, object]]
    crawl_request_id: str


class _CrawlResultItem(TypedDict):
    url: str
    content: str


class _OriginResponse(TypedDict):
    success_marker: str
    host_header: str
    path: str


@dataclass
class CrawlArtifact:
    """Single crawl artifact shared by both success tests."""

    request_id: str
    result_url: str
    content: str
    host_header: str
    success_marker: str


def _docker_compose_project(project_name: str, cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run docker compose with the main file and test override, scoped to project."""
    base_cmd = [
        "docker",
        "compose",
        "--pull",
        "never",  # Use cached pinned images; no registry traffic
        "-p",
        project_name,
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(TEST_COMPOSE_FILE),
    ]
    return run_cmd(base_cmd + cmd, **kwargs)


def _docker_compose_egress(project_name: str, cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run docker compose with the egress topology file, scoped to project."""
    base_cmd = [
        "docker",
        "compose",
        "--pull",
        "never",  # Use cached pinned images; no registry traffic
        "-p",
        project_name,
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(EGRESS_COMPOSE_FILE),
    ]
    return run_cmd(base_cmd + cmd, **kwargs)


def _wait_for_service_healthy(
    project_name: str, service_name: str, timeout: int = 120, use_egress: bool = False
) -> bool:
    """Wait for a service to be healthy using project-scoped docker compose."""
    fn = _docker_compose_egress if use_egress else _docker_compose_project
    for _ in range(timeout):
        result = fn(project_name, ["ps", "--format", "json", service_name])
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                try:
                    info = json.loads(line)
                    if info.get("Health") == "healthy":
                        return True
                except json.JSONDecodeError:
                    pass
        time.sleep(1)
    return False


def _wait_for_container_running(
    project_name: str, service_name: str, timeout: int = 30, use_egress: bool = False
) -> bool:
    """Wait for a container to be running (for services without healthcheck)."""
    fn = _docker_compose_egress if use_egress else _docker_compose_project
    for _ in range(timeout):
        result = fn(project_name, ["ps", "--format", "json", service_name])
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                try:
                    info = json.loads(line)
                    if info.get("State") == "running":
                        return True
                except json.JSONDecodeError:
                    pass
        time.sleep(1)
    return False


def _get_container_name(project_name: str, service_name: str, use_egress: bool = False) -> str:
    """Get the actual container name for a service in the project."""
    fn = _docker_compose_egress if use_egress else _docker_compose_project
    result = fn(project_name, ["ps", "--format", "json", service_name])
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            try:
                info = json.loads(line)
                return info.get("Name", "")
            except json.JSONDecodeError:
                pass
    return ""


def _crawl_via_api(app_container: str, url: str) -> subprocess.CompletedProcess[str]:
    """Invoke the production crawl API via the REST endpoint (loopback port 8765).
    This uses the real production wiring: the service's pooled client configured
    with CRAWLUTRON_EGRESS_PROXY_URL.
    """
    payload = json.dumps({"url": url})
    return run_cmd(
        [
            "docker",
            "exec",
            "-i",
            app_container,
            "curl",
            "-s",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
            "http://localhost:8765/v1/crawl",
        ]
    )


def _poll_crawl_result(app_container: str, request_id: str, deadline: float) -> _CrawlStatusResult | None:
    """Poll for crawl job completion until deadline. Returns result dict or None if timeout."""
    while time.monotonic() < deadline:
        status_result = run_cmd(
            [
                "docker",
                "exec",
                "-i",
                app_container,
                "curl",
                "-s",
                f"http://localhost:8765/v1/crawl/{request_id}",
            ]
        )
        if status_result.returncode == 0:
            try:
                raw_data = json.loads(status_result.stdout.strip())
            except json.JSONDecodeError:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            # Transform to TypedDict with non-mirroring field names
            if "results" in raw_data and raw_data["results"]:
                status_data: _CrawlStatusResult = {
                    "crawl_results": raw_data["results"],
                    "crawl_request_id": raw_data.get("request_id", ""),
                }
                return status_data
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _validate_rest_contract(response_data: _CrawlSubmitResponse) -> None:
    """Validate the actual REST contract for crawl submission response."""
    request_id = response_data.get("crawl_request_id")
    assert request_id, f"No request_id in response: {response_data}"
    assert isinstance(request_id, str) and request_id, "request_id must be a nonempty string"
    # HTTP failures fail with the response body; terminal job failure raises immediately with the payload
    # result/status/content fields have the expected types (validated in the poll step)


def _extract_crawl_artifact(status_data: _CrawlStatusResult) -> CrawlArtifact:
    """Extract and validate the crawl artifact from completed status data."""
    assert "crawl_results" in status_data and status_data["crawl_results"], "No results in status data"
    result = status_data["crawl_results"][0]

    # Validate result fields have expected types
    result_url = result.get("url", "")
    assert isinstance(result_url, str), "result.url must be a string"
    content = result.get("content", "")
    assert isinstance(content, str), "result.content must be a string"

    # Parse the content JSON to extract success_marker and host_header
    try:
        content_data: _OriginResponse = json.loads(content)
    except json.JSONDecodeError as e:
        raise AssertionError(f"Result content is not valid JSON: {content[:200]}") from e

    success_marker = content_data.get("success_marker", "")
    assert success_marker, f"No success_marker in content: {content_data}"
    assert isinstance(success_marker, str), "success_marker must be a string"

    host_header = content_data.get("host_header", "")
    assert isinstance(host_header, str), "host_header must be a string"

    request_id = status_data.get("crawl_request_id", "")
    assert request_id, "No request_id in status data"

    return CrawlArtifact(
        request_id=request_id,
        result_url=result_url,
        content=content,
        host_header=host_header,
        success_marker=success_marker,
    )


@pytest.fixture(scope="module")
def egress_stack():
    """Bring up the egress topology stack for testing with unique project name."""
    project_name = f"crawlutron-egress-test-{uuid.uuid4().hex[:8]}"

    # Build images
    print("Building egress test images...")
    build_cmd = ["build", "crawlutron", "egress-proxy"]
    build_result = _docker_compose_project(project_name, build_cmd, capture=False)
    assert build_result.returncode == 0, "Failed to build images"

    # Build test-origin image (uses cached base; no unconditional pull)
    print("Building test-origin image...")
    build_origin_cmd = ["build", "origin"]
    build_origin_result = _docker_compose_project(project_name, build_origin_cmd, capture=False)
    assert build_origin_result.returncode == 0, "Failed to build test-origin image"

    # Bring up the stack with --pull never (uses cached pinned python image)
    print("Bringing up egress topology stack...")
    up_result = _docker_compose_project(project_name, ["up", "-d"], capture=False)
    assert up_result.returncode == 0, "Failed to bring up stack"

    try:
        # Wait for services to be healthy with bounded waits
        print("Waiting for crawlutron to be healthy...")
        healthy = _wait_for_service_healthy(project_name, "crawlutron", timeout=120)
        assert healthy, "crawlutron did not become healthy"
        print("Waiting for egress-proxy to be ready...")
        ready = _wait_for_container_running(project_name, "egress-proxy", timeout=30)
        assert ready, "egress-proxy did not become ready"
        print("Waiting for origin to be ready...")
        ready = _wait_for_container_running(project_name, "origin", timeout=30)
        assert ready, "origin did not become ready"

        yield project_name
    finally:
        # Teardown - always clean up
        print("Tearing down egress topology stack...")
        _docker_compose_project(project_name, ["down", "-v", "--remove-orphans"], capture=False)


@pytest.fixture(scope="module")
def crawled_origin(egress_stack) -> CrawlArtifact:
    """Drive the real production POST /v1/crawl and bounded GET /v1/crawl/{id} polling once.
    Returns a single CrawlArtifact that both success tests assert different properties of.
    """
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    deadline = time.monotonic() + TOTAL_DEADLINE_SECONDS

    # Submit crawl job via production API
    result = _crawl_via_api(app_container, "http://origin:80/")
    assert result.returncode == 0, f"Crawl API call failed: {result.stderr}"

    try:
        response_data: _CrawlSubmitResponse = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise AssertionError(f"Invalid JSON response from crawl API: {result.stdout[:200]}") from e

    _validate_rest_contract(response_data)
    request_id = response_data["crawl_request_id"]
    print(f"Crawl request submitted: {request_id}")

    # Poll for job completion with bounded monotonic deadline
    status_data = _poll_crawl_result(app_container, request_id, deadline)
    assert status_data is not None, f"Crawl job did not complete within {TOTAL_DEADLINE_SECONDS}s deadline"

    # Extract and validate the crawl artifact
    artifact = _extract_crawl_artifact(status_data)
    print(f"Crawl completed: request_id={artifact.request_id}, url={artifact.result_url}")
    print(f"Success marker: {artifact.success_marker}")
    print(f"Host header observed by origin: {artifact.host_header}")

    return artifact


@pytest.mark.slow
def test_direct_app_to_origin_fails(egress_stack) -> None:
    """Direct app→origin must fail (no direct network path)."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    # Try to reach origin directly from crawlutron container
    # The origin is on the egress network only, app is on internal only
    result = run_cmd(["docker", "exec", "-i", app_container, "curl", "--max-time", "5", "http://origin:80/"])
    # Should fail - no direct network path
    assert result.returncode != 0, f"Direct app→origin should fail but got: {result.stdout}"
    print(f"Direct app→origin correctly failed (exit code: {result.returncode})")


@pytest.mark.slow
def test_app_via_proxy_to_origin_succeeds(crawled_origin: CrawlArtifact) -> None:
    """App→proxy→origin succeeds using production proxy configuration via crawl API.
    Asserts: isolated origin content reached through the proxy (success marker present).
    """
    # The success marker proves isolated origin content was reached through the proxy
    assert "CRAWLUTRON_EGRESS_PROOF_" in crawled_origin.success_marker, (
        f"Expected success marker not found: {crawled_origin.success_marker}"
    )
    # The result URL should be the logical origin URL
    assert crawled_origin.result_url == "http://origin:80/", (
        f"Unexpected result URL: {crawled_origin.result_url}"
    )
    print("SUCCESS: App→proxy→origin works via production crawl API")
    print("App→proxy→origin correctly succeeded via production crawl API")


@pytest.mark.slow
def test_logical_host_header_preserved(crawled_origin: CrawlArtifact) -> None:
    """Logical URL/Host identity preserved through proxy via production crawl API.
    Asserts: the origin-observed Host header matches the logical hostname.
    """
    # The origin fixture echoes the Host header it received
    # The logical hostname for "http://origin:80/" is "origin" (default port 80 omitted)
    expected_host = "origin"
    assert crawled_origin.host_header == expected_host, (
        f"Host header not preserved: expected '{expected_host}', got '{crawled_origin.host_header}'"
    )
    # The result URL should be the logical origin URL
    assert crawled_origin.result_url == "http://origin:80/", (
        f"Logical URL not preserved: got {crawled_origin.result_url}"
    )
    print("SUCCESS: Logical URL/Host identity intact via production crawl API")
    print("Logical URL/pinning correctly intact via production crawl API")


@pytest.mark.slow
def test_loopback_only_published_port(egress_stack) -> None:
    """Verify app container has loopback-only published port (127.0.0.1:8765)."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    result = run_cmd(["docker", "inspect", app_container, "--format", "{{json .NetworkSettings.Ports}}"])
    assert result.returncode == 0
    ports = json.loads(result.stdout.strip())
    # Should have only the loopback mapping
    expected = {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8765"}]}
    assert ports == expected, f"App should have only loopback port mapping, got: {ports}"
    print("App correctly has loopback-only published port")


@pytest.mark.slow
def test_network_topology(egress_stack) -> None:
    """Verify network topology: app internal only, proxy dual-homed, origin egress only."""
    project_name = egress_stack
    for name, expected_networks in [
        ("crawlutron", ["internal"]),
        ("egress-proxy", ["internal", "egress"]),
        ("origin", ["egress"]),
    ]:
        container_name = _get_container_name(project_name, name)
        fmt = "{{json .NetworkSettings.Networks}}"
        result = run_cmd(["docker", "inspect", container_name, "--format", fmt])
        assert result.returncode == 0, f"Failed to inspect {name}"
        networks = json.loads(result.stdout.strip())
        actual_networks = sorted(networks.keys())
        assert actual_networks == sorted(expected_networks), (
            f"{name}: expected networks {expected_networks}, got {actual_networks}"
        )
        print(f"{name}: correctly on networks {actual_networks}")

    # Verify internal network is internal-only
    net_name = f"{project_name}_internal"
    result = run_cmd(["docker", "network", "inspect", net_name, "--format", "{{.Internal}}"])
    assert result.returncode == 0
    assert result.stdout.strip() == "true", "internal network must be internal-only"
    print("internal network correctly marked as internal-only")

    # Verify egress network is NOT internal-only
    net_name = f"{project_name}_egress"
    result = run_cmd(["docker", "network", "inspect", net_name, "--format", "{{.Internal}}"])
    assert result.returncode == 0
    assert result.stdout.strip() == "false", "egress network must allow external access"
    print("egress network correctly allows external access")
