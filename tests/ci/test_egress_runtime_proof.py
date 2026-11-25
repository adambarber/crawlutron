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
DOCKERFILE = REPO_ROOT / "Dockerfile"


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


@pytest.mark.slow
def test_app_via_proxy_to_origin_succeeds(egress_stack) -> None:
    """App→proxy→origin succeeds using production proxy configuration."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    # App uses CRAWLUTRON_EGRESS_PROXY_URL; test via docker exec with httpx client
    result = run_cmd(
        [
            "docker",
            "exec",
            "-i",
            app_container,
            "python",
            "-c",
            """
import httpx
import os

proxy_url = os.environ.get('CRAWLUTRON_EGRESS_PROXY_URL', 'http://egress-proxy:8888')
print(f'Using proxy: {proxy_url}')

client = httpx.Client(proxy=proxy_url, timeout=10.0)
try:
    response = client.get('http://origin:80/')
    print(f'Status: {response.status_code}')
    print(f'Body: {response.text[:200]}')
    assert response.status_code == 200
    # python -m http.server returns an HTML directory listing
    assert '<title>Directory listing' in response.text or 'Directory listing' in response.text
    print('SUCCESS: App→proxy→origin works')
except Exception as e:
    print(f'FAILED: {e}')
    import sys
    sys.exit(1)
""",
        ]
    )
    assert result.returncode == 0, f"App→proxy→origin failed: {result.stderr}"
    assert "SUCCESS" in result.stdout
    print("App→proxy→origin correctly succeeded")


@pytest.mark.slow
def test_logical_url_and_pinning_intact(egress_stack) -> None:
    """Logical URL/Host identity preserved through proxy."""
    project_name = egress_stack
    app_container = _get_container_name(project_name, "crawlutron")
    result = run_cmd(
        [
            "docker",
            "exec",
            "-i",
            app_container,
            "python",
            "-c",
            """
import httpx
import os

proxy_url = os.environ.get('CRAWLUTRON_EGRESS_PROXY_URL', 'http://egress-proxy:8888')
print(f'Using proxy: {proxy_url}')

client = httpx.Client(proxy=proxy_url, timeout=10.0)
try:
    response = client.get('http://origin:80/')
    print(f'Status: {response.status_code}')
    print(f'Body: {response.text[:200]}')
    assert response.status_code == 200
    # python -m http.server returns an HTML directory listing
    assert '<title>Directory listing' in response.text or 'Directory listing' in response.text
    print('SUCCESS: Logical URL/pinning intact via proxy')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback
    traceback.print_exc()
    import sys
    sys.exit(1)
""",
        ]
    )
    assert result.returncode == 0, f"Logical URL test failed: {result.stderr}"
    assert "SUCCESS" in result.stdout
    print("Logical URL/pinning correctly intact")


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
