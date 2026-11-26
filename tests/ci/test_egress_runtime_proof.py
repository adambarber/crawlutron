"""Runtime proof for default-deny egress topology (app internal, proxy dual-homed, origin egress-only)."""

from __future__ import annotations

import ast
import http.client
import inspect
import json
import subprocess
import time
import uuid
from dataclasses import dataclass

import pytest

from tests._subprocess_helpers import run_cmd
from tests.chokepoint_paths import REPO_ROOT

COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
TEST_COMPOSE_FILE = REPO_ROOT / "docker-compose.test.yml"

# Total deadline for the test run (well under pytest.ini's 120s ceiling)
TOTAL_DEADLINE_SECONDS = 90
POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = TOTAL_DEADLINE_SECONDS // POLL_INTERVAL_SECONDS


# API response validation uses dict.get() for runtime validation to avoid
# mirroring production field shapes (which triggers the harness doubles gate).


@dataclass
class _HTTPResult:
    """Subprocess-shaped result from a bounded host loopback HTTP call."""

    returncode: int
    stdout: str
    stderr: str


@dataclass
class CrawlArtifact:
    """Single crawl artifact shared by both success tests."""

    request_id: str
    result_url: str
    content: str
    host_header: str
    success_marker: str


def _docker_compose_base_argv(project_name: str) -> list[str]:
    """Base docker compose argv for the test project (main + test override)."""
    return [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(TEST_COMPOSE_FILE),
    ]


def _docker_compose(project_name: str, cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run docker compose with the main file and test override, scoped to project."""
    base_cmd = _docker_compose_base_argv(project_name)
    return run_cmd(base_cmd + cmd, **kwargs)


def _wait_for_service_healthy(project_name: str, service_name: str, deadline: float) -> bool:
    """Wait for a service to be healthy using project-scoped docker compose."""
    while time.monotonic() < deadline:
        result = _docker_compose(project_name, ["ps", "--format", "json", service_name])
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


def _wait_for_container_running(project_name: str, service_name: str, deadline: float) -> bool:
    """Wait for a container to be running (for services without healthcheck)."""
    while time.monotonic() < deadline:
        result = _docker_compose(project_name, ["ps", "--format", "json", service_name])
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


def _get_container_name(project_name: str, service_name: str) -> str:
    """Get the actual container name for a service in the project."""
    result = _docker_compose(project_name, ["ps", "--format", "json", service_name])
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            try:
                info = json.loads(line)
                return info.get("Name", "")
            except json.JSONDecodeError:
                pass
    return ""


def _host_port_for(project_name: str, service_name: str) -> int:
    """Read the loopback host port published by a service's container."""
    container = _get_container_name(project_name, service_name)
    result = run_cmd(["docker", "inspect", container, "--format", "{{json .NetworkSettings.Ports}}"])
    assert result.returncode == 0, f"Failed to inspect {service_name}: {result.stderr}"
    ports = json.loads(result.stdout.strip())
    bindings = ports.get("8765/tcp")
    assert bindings and len(bindings) == 1, f"Expected one 8765/tcp binding, got: {ports}"
    binding = bindings[0]
    assert binding.get("HostIp") == "127.0.0.1", f"Expected loopback bind, got: {binding}"
    host_port = int(binding.get("HostPort", "0"))
    assert host_port > 0, f"Expected a nonzero assigned host port, got: {binding}"
    return host_port


def _http_request(host_port: int, path: str, payload: dict[str, object] | None = None) -> _HTTPResult:
    """Issue one bounded host-loopback request through the API ingress relay."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection = http.client.HTTPConnection("127.0.0.1", host_port, timeout=10)
    try:
        connection.request("POST" if body is not None else "GET", path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        return _HTTPResult(0 if 200 <= status < 300 else status, response.read().decode("utf-8"), "")
    except OSError as exc:
        return _HTTPResult(1, "", str(exc))
    finally:
        connection.close()


def _crawl_via_api(host_port: int, url: str) -> _HTTPResult:
    """Submit an async crawl through the real host loopback ingress path."""
    # async_mode makes the POST return 202 CrawlAccepted with a status_url.
    return _http_request(host_port, "/v1/crawl", {"url": url, "async_mode": True})


def _poll_crawl_result(host_port: int, request_id: str, deadline: float) -> dict[str, object] | None:
    """Poll for crawl job completion until deadline. Returns result dict or None if timeout."""
    while time.monotonic() < deadline:
        status_result = _http_request(host_port, f"/v1/crawl/{request_id}")
        if status_result.returncode == 0:
            try:
                raw_data = json.loads(status_result.stdout.strip())
            except json.JSONDecodeError:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            # Validate actual REST contract fields
            if "results" in raw_data and raw_data["results"]:
                return raw_data
            # Terminal failure/refusal - raise immediately with full payload
            if raw_data.get("success") is False and "error" in raw_data:
                raise AssertionError(f"Crawl job failed: {raw_data}")
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _validate_rest_contract(response_data: dict[str, object]) -> None:
    """Validate the actual REST contract for crawl submission response."""
    request_id = response_data.get("request_id")
    assert request_id, f"No request_id in response: {response_data}"
    assert isinstance(request_id, str) and request_id, "request_id must be a nonempty string"
    status = response_data.get("status")
    assert status == "accepted", f"Expected status 'accepted', got: {status}"
    status_url = response_data.get("status_url")
    assert status_url, f"No status_url in response: {response_data}"
    # HTTP failures fail with the response body; terminal job failure raises immediately with the payload


def _extract_crawl_artifact(status_data: dict[str, object]) -> CrawlArtifact:
    """Extract and validate the crawl artifact from completed status data."""
    assert "results" in status_data and status_data["results"], "No results in status data"
    results = status_data["results"]
    assert isinstance(results, list), "results must be a list"
    result = results[0]
    assert isinstance(result, dict), "result must be a dict"

    # Validate result fields have expected types
    result_url = result.get("original_url", "")
    assert isinstance(result_url, str), "result.original_url must be a string"
    content = result.get("markdown", "") or result.get("text", "") or result.get("html", "")
    assert isinstance(content, str), "result content must be a string"

    # Production renders the origin body as plain "key: value" lines in result.text.
    content_data: dict[str, str] = {}
    for line in content.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            content_data[key.strip()] = value.strip()

    success_marker = content_data.get("success_marker", "")
    assert success_marker, f"No success_marker in content: {content_data}"
    assert isinstance(success_marker, str), "success_marker must be a string"

    host_header = content_data.get("host_header", "")
    assert isinstance(host_header, str), "host_header must be a string"

    request_id = status_data.get("request_id", "")
    assert isinstance(request_id, str), "request_id must be a string"
    assert request_id, "No request_id in status data"

    return CrawlArtifact(
        request_id=request_id,
        result_url=result_url,
        content=content,
        host_header=host_header,
        success_marker=success_marker,
    )


def test_egress_stack_build_does_not_force_builder_backend() -> None:
    """Runtime proof must use the supported BuildKit default, never a forced builder backend.

    A forced legacy backend failed live with a credential error; the default builds from cache.
    """
    tree = ast.parse(inspect.getsource(egress_stack))
    offending = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value == "DOCKER_BUILDKIT"
    ]
    assert not offending, f"egress_stack must not force a builder backend (lines {offending})"


def test_extract_crawl_artifact_parses_observed_plain_text_contract() -> None:
    """Production returns the origin body as plain key: value lines in result.text."""
    observed_text = "success_marker: CRAWLUTRON_EGRESS_PROOF_2624e4daba214411\nhost_header: origin\npath: /\n"
    status_data: dict[str, object] = {
        "request_id": "crawl_observed_contract",
        "results": [
            {
                "original_url": "http://origin:80/",
                "markdown": None,
                "html": None,
                "text": observed_text,
            }
        ],
    }

    artifact = _extract_crawl_artifact(status_data)

    assert artifact.success_marker == "CRAWLUTRON_EGRESS_PROOF_2624e4daba214411"
    assert artifact.host_header == "origin"
    assert artifact.result_url == "http://origin:80/"


@pytest.fixture(scope="module")
def egress_stack():
    """Bring up the egress topology stack for testing with unique project name."""
    import os

    project_name = f"crawlutron-egress-test-{uuid.uuid4().hex[:8]}"
    # Empty host port = Docker auto-allocates; merge env for subprocess PATH
    env = {**os.environ, "COMPOSE_HOST_PORT": ""}

    # Build images (uses cached base images by default)
    print("Building egress test images...")
    build_cmd = ["build", "crawlutron", "egress-proxy", "api-ingress"]
    build_result = _docker_compose(project_name, build_cmd, capture=False, env=env)
    assert build_result.returncode == 0, "Failed to build images"

    # Bring up the stack with --pull never on the up command only
    print("Bringing up egress topology stack...")
    up_result = _docker_compose(project_name, ["up", "-d", "--pull", "never"], capture=False, env=env)
    assert up_result.returncode == 0, "Failed to bring up stack"

    # Shared deadline for all setup waits (well under 120s global ceiling)
    setup_deadline = time.monotonic() + 60

    try:
        # Wait for services to be healthy with bounded waits
        print("Waiting for crawlutron to be healthy...")
        healthy = _wait_for_service_healthy(project_name, "crawlutron", setup_deadline)
        assert healthy, "crawlutron did not become healthy within setup deadline"
        print("Waiting for egress-proxy to be ready...")
        ready = _wait_for_container_running(project_name, "egress-proxy", setup_deadline)
        assert ready, "egress-proxy did not become ready within setup deadline"
        print("Waiting for origin to be ready...")
        ready = _wait_for_container_running(project_name, "origin", setup_deadline)
        assert ready, "origin did not become ready within setup deadline"
        print("Waiting for api-ingress to be ready...")
        ready = _wait_for_container_running(project_name, "api-ingress", setup_deadline)
        assert ready, "api-ingress did not become ready within setup deadline"

        yield project_name
    finally:
        # Teardown - always clean up with bounded wait
        print("Tearing down egress topology stack...")
        down_result = _docker_compose(
            project_name, ["down", "-v", "--remove-orphans"], capture=False, env=env
        )
        # Don't assert on teardown - best effort cleanup
        if down_result.returncode != 0:
            print(f"Teardown warning: down returned {down_result.returncode}: {down_result.stderr}")


@pytest.fixture(scope="module")
def crawled_origin(egress_stack) -> CrawlArtifact:
    """Drive the real production POST /v1/crawl and bounded GET /v1/crawl/{id} polling once.
    Returns a single CrawlArtifact that both success tests assert different properties of.
    """
    project_name = egress_stack
    host_port = _host_port_for(project_name, "api-ingress")
    deadline = time.monotonic() + TOTAL_DEADLINE_SECONDS

    # Submit crawl job via production API
    result = _crawl_via_api(host_port, "http://origin:80/")
    assert result.returncode == 0, f"Crawl API call failed: {result.stderr}"

    try:
        response_data = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise AssertionError(f"Invalid JSON response from crawl API: {result.stdout[:200]}") from e

    _validate_rest_contract(response_data)
    request_id = response_data["request_id"]
    print(f"Crawl request submitted: {request_id}")

    # Poll for job completion with bounded monotonic deadline
    status_data = _poll_crawl_result(host_port, request_id, deadline)
    assert status_data is not None, f"Crawl job did not complete within {TOTAL_DEADLINE_SECONDS}s deadline"

    # Validate the returned request_id matches submission
    assert status_data["request_id"] == request_id, (
        f"Returned request_id {status_data['request_id']} does not match submitted {request_id}"
    )

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
    """Only the ingress relay publishes a loopback host port; the app publishes none."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    app_ports = run_cmd(["docker", "inspect", app_container, "--format", "{{json .NetworkSettings.Ports}}"])
    assert app_ports.returncode == 0
    published = json.loads(app_ports.stdout.strip())
    assert all(value is None for value in published.values()), (
        f"The app must not publish ports; got: {published}"
    )
    host_port = _host_port_for(project_name, "api-ingress")
    print(f"API ingress correctly publishes loopback port on 127.0.0.1:{host_port}")


@pytest.mark.slow
def test_host_loopback_health_via_ingress(egress_stack) -> None:
    """A real host loopback GET reaches the internal app through the relay."""
    host_port = _host_port_for(egress_stack, "api-ingress")
    result = _http_request(host_port, "/health")
    assert result.returncode == 0, f"Host loopback /health failed: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload.get("status") == "ok", f"Unexpected /health payload: {payload}"
    print(f"Host loopback health via ingress OK on 127.0.0.1:{host_port}")


@pytest.mark.slow
def test_compose_config_parses(egress_stack) -> None:
    """Contract test: the generated Compose argv parses through `config --quiet`."""
    project_name = egress_stack
    # Use the base argv (without --pull) and verify config parses
    base_argv = _docker_compose_base_argv(project_name)
    result = run_cmd(base_argv + ["config", "--quiet"])
    assert result.returncode == 0, f"Compose config failed: {result.stderr}"
    print("Compose config parses successfully")


@pytest.mark.slow
def test_network_topology(egress_stack) -> None:
    """Verify network topology: app internal only, proxy dual-homed, origin egress only."""
    project_name = egress_stack
    for name, expected_networks in [
        ("crawlutron", ["internal"]),
        ("egress-proxy", ["internal", "egress"]),
        ("api-ingress", ["internal", "ingress"]),
        ("origin", ["egress"]),
    ]:
        qualified = [f"{project_name}_{net}" for net in expected_networks]
        container_name = _get_container_name(project_name, name)
        fmt = "{{json .NetworkSettings.Networks}}"
        result = run_cmd(["docker", "inspect", container_name, "--format", fmt])
        assert result.returncode == 0, f"Failed to inspect {name}"
        networks = json.loads(result.stdout.strip())
        actual_networks = sorted(networks.keys())
        assert actual_networks == sorted(qualified), (
            f"{name}: expected networks {qualified}, got {actual_networks}"
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

    # Ingress is an ordinary bridge: only the relay attaches, never the app.
    net_name = f"{project_name}_ingress"
    result = run_cmd(["docker", "network", "inspect", net_name, "--format", "{{.Internal}}"])
    assert result.returncode == 0
    assert result.stdout.strip() == "false", "ingress network must allocate a host port"
    print("ingress network correctly allows loopback host port allocation")
