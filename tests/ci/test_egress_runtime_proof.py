"""Runtime proof for default-deny egress topology (app internal, proxy dual-homed, origin egress-only)."""

from __future__ import annotations

import json
import subprocess
import time
import uuid

import pytest

from tests._subprocess_helpers import run_cmd
from tests.chokepoint_paths import REPO_ROOT

COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
TEST_COMPOSE_FILE = REPO_ROOT / "docker-compose.test.yml"


def _docker_compose_project(project_name: str, cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run docker compose with the main file and test override, scoped to project."""
    base_cmd = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(TEST_COMPOSE_FILE),
    ]
    return run_cmd(base_cmd + cmd, **kwargs)


def _wait_for_service_healthy(project_name: str, service_name: str, timeout: int = 120) -> bool:
    """Wait for a service to be healthy using project-scoped docker compose."""
    for _ in range(timeout):
        result = _docker_compose_project(project_name, ["ps", "--format", "json", service_name])
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


def _wait_for_container_running(project_name: str, service_name: str, timeout: int = 30) -> bool:
    """Wait for a container to be running (for services without healthcheck)."""
    for _ in range(timeout):
        result = _docker_compose_project(project_name, ["ps", "--format", "json", service_name])
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
    result = _docker_compose_project(project_name, ["ps", "--format", "json", service_name])
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            try:
                info = json.loads(line)
                return info.get("Name", "")
            except json.JSONDecodeError:
                pass
    return ""


@pytest.fixture(scope="module")
def egress_stack():
    """Bring up the egress topology stack for testing with unique project name."""
    project_name = f"crawlutron-egress-test-{uuid.uuid4().hex[:8]}"

    # Build images
    print("Building egress test images...")
    build_cmd = ["build", "crawlutron", "egress-proxy"]
    build_result = _docker_compose_project(project_name, build_cmd, capture=False)
    assert build_result.returncode == 0, "Failed to build images"

    # Pull origin image (pinned python alpine)
    print("Pulling origin image...")
    origin_img = "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"
    pull_result = run_cmd(["docker", "pull", origin_img], capture=False)
    assert pull_result.returncode == 0, "Failed to pull origin image"

    # Bring up the stack
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


@pytest.mark.slow
def test_app_via_proxy_to_origin_succeeds(egress_stack) -> None:
    """App→proxy→origin succeeds using production proxy configuration via crawl API."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    # Use the production crawl API which consumes CRAWLUTRON_EGRESS_PROXY_URL
    result = _crawl_via_api(app_container, "http://origin:80/")
    assert result.returncode == 0, f"Crawl API call failed: {result.stderr}"
    try:
        response_data = json.loads(result.stdout.strip())
        request_id = response_data.get("request_id")
        assert request_id, f"No request_id in response: {result.stdout}"
        print(f"Crawl request submitted: {request_id}")
        # Poll for job completion (bounded)
        for _ in range(60):
            time.sleep(2)
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
                    status_data = json.loads(status_result.stdout.strip())
                except json.JSONDecodeError:
                    continue
                # Check if the crawl is completed (has results array)
                if "results" in status_data and status_data["results"]:
                    # Verify the result contains the origin content
                    result_url = status_data["results"][0].get("url", "")
                    assert result_url == "http://origin:80/", f"Unexpected result URL: {result_url}"
                    # The content should be the directory listing
                    content = status_data["results"][0].get("content", "")
                    assert "Directory listing" in content or "<title>Directory listing" in content, (
                        f"Origin content not found in result: {content[:200]}"
                    )
                    print("SUCCESS: App→proxy→origin works via production crawl API")
                    print("App→proxy→origin correctly succeeded via production crawl API")
                    return
            time.sleep(1)
        raise AssertionError("Crawl job did not complete within timeout")
    except json.JSONDecodeError:
        # If the API returns non-JSON (e.g., error), fail
        raise AssertionError(f"Invalid JSON response from crawl API: {result.stdout[:200]}") from None


@pytest.mark.slow
def test_logical_url_and_pinning_intact(egress_stack) -> None:
    """Logical URL/Host identity preserved through proxy via production crawl API."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    # Use the production crawl API which consumes CRAWLUTRON_EGRESS_PROXY_URL
    result = _crawl_via_api(app_container, "http://origin:80/")
    assert result.returncode == 0, f"Crawl API call failed: {result.stderr}"
    try:
        response_data = json.loads(result.stdout.strip())
        request_id = response_data.get("request_id")
        assert request_id, f"No request_id in response: {result.stdout}"
        print(f"Crawl request submitted: {request_id}")
        # Poll for job completion (bounded)
        for _ in range(60):
            time.sleep(2)
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
                    status_data = json.loads(status_result.stdout.strip())
                except json.JSONDecodeError:
                    continue
                # Check if the crawl is completed (has results array)
                if "results" in status_data and status_data["results"]:
                    # Verify logical URL/Host identity: the result URL should be the logical origin URL
                    result_url = status_data["results"][0].get("url", "")
                    assert result_url == "http://origin:80/", f"Logical URL not preserved: got {result_url}"
                    content = status_data["results"][0].get("content", "")
                    assert "Directory listing" in content or "<title>Directory listing" in content, (
                        f"Origin content not found in result: {content[:200]}"
                    )
                    print("SUCCESS: Logical URL/pinning intact via production crawl API")
                    print("Logical URL/pinning correctly intact via production crawl API")
                    return
            time.sleep(1)
        raise AssertionError("Crawl job did not complete within timeout")
    except json.JSONDecodeError:
        raise AssertionError(f"Invalid JSON response from crawl API: {result.stdout[:200]}") from None


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
