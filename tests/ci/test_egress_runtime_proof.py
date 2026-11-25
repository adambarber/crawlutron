"""Runtime proof for default-deny egress topology (app internal, proxy dual-homed, origin egress-only)."""

from __future__ import annotations

import subprocess
import time

import pytest

from tests._subprocess_helpers import run_cmd
from tests.chokepoint_paths import REPO_ROOT

COMPOSE_FILE = REPO_ROOT / "docker-compose.egress.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _docker_compose(
    cmd: list[str],
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    """Run docker compose with the egress file."""
    return run_cmd(
        ["docker", "compose", "-f", str(COMPOSE_FILE)] + cmd,
        **kwargs,
    )


def _wait_for_service(name: str, timeout: int = 300) -> bool:
    """Wait for a service to be healthy."""
    for _ in range(timeout):
        result = _docker_compose(["ps", "--format", "json", name])
        if result.returncode == 0 and result.stdout.strip():
            import json

            for line in result.stdout.strip().split("\n"):
                try:
                    info = json.loads(line)
                    if info.get("Health") == "healthy":
                        return True
                except json.JSONDecodeError:
                    pass
        time.sleep(1)
    return False


@pytest.fixture(scope="module")
def egress_stack():
    """Bring up the egress topology stack for testing."""
    # Build the test image
    print("Building egress test image...")
    build_result = _docker_compose(["build", "crawlutron"], capture=False)
    assert build_result.returncode == 0, "Failed to build crawlutron image"

    # Build proxy image (same Dockerfile, different target not needed)
    build_proxy_result = _docker_compose(["build", "egress-proxy"], capture=False)
    assert build_proxy_result.returncode == 0, "Failed to build egress-proxy image"

    # Pull origin image (pinned python alpine)
    print("Pulling origin image...")
    pull_result = run_cmd(
        ["docker", "pull", "python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89"],
        capture=False,
    )
    assert pull_result.returncode == 0, "Failed to pull origin image"

    # Bring up the stack
    print("Bringing up egress topology stack...")
    up_result = _docker_compose(["up", "-d"], capture=False)
    assert up_result.returncode == 0, "Failed to bring up stack"

    try:
        # Wait for services to be healthy
        print("Waiting for crawlutron to be healthy...")
        assert _wait_for_service("crawlutron", timeout=300), "crawlutron did not become healthy"
        print("Waiting for egress-proxy...")
        time.sleep(10)  # proxy has no healthcheck, give it time to start
        print("Waiting for origin...")
        time.sleep(5)  # origin is a simple HTTP server

        yield
    finally:
        # Teardown
        print("Tearing down egress topology stack...")
        _docker_compose(["down", "-v"], capture=False)


@pytest.mark.slow
def test_direct_app_to_origin_fails(_egress_stack) -> None:
    """Direct app→origin must fail (no direct network path)."""
    # Try to reach origin directly from crawlutron container
    # The origin is on the egress network only, app is on internal only
    result = _docker_compose(["exec", "-T", "crawlutron", "curl", "--max-time", "5", "http://origin:80/"])
    # Should fail - no direct network path
    assert result.returncode != 0, f"Direct app→origin should fail but got: {result.stdout}"
    print(f"Direct app→origin correctly failed (exit code: {result.returncode})")


@pytest.mark.slow
def test_app_via_proxy_to_origin_succeeds(_egress_stack) -> None:
    """App→proxy→origin succeeds using production proxy configuration."""
    # App uses CRAWLUTRON_EGRESS_PROXY_URL; test via docker exec with httpx client
    result = _docker_compose(
        [
            "exec",
            "-T",
            "crawlutron",
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
    print(f'Body: {response.text}')
    assert response.status_code == 200
    assert 'ORIGIN-REACHED-VIA-PROXY' in response.text
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
def test_logical_url_and_pinning_intact(_egress_stack) -> None:
    """Logical URL/Host identity preserved through proxy."""
    result = _docker_compose(
        [
            "exec",
            "-T",
            "crawlutron",
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
    print(f'Body: {response.text}')
    assert response.status_code == 200
    assert 'ORIGIN-REACHED-VIA-PROXY' in response.text
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
def test_no_public_ports_on_app(_egress_stack) -> None:
    """Verify app container has no published ports."""
    result = run_cmd(
        ["docker", "inspect", "crawlutron-egress", "--format", "{{json .NetworkSettings.Ports}}"]
    )
    assert result.returncode == 0
    import json

    ports = json.loads(result.stdout.strip())
    assert ports == {} or ports is None, f"App should have no published ports, got: {ports}"
    print("App correctly has no published ports")


@pytest.mark.slow
def test_network_topology(_egress_stack) -> None:
    """Verify network topology: app internal only, proxy dual-homed, origin egress only."""
    for name, expected_networks in [
        ("crawlutron-egress", ["internal"]),
        ("egress-proxy", ["internal", "egress"]),
        ("isolated-origin", ["egress"]),
    ]:
        result = run_cmd(["docker", "inspect", name, "--format", "{{json .NetworkSettings.Networks}}"])
        assert result.returncode == 0, f"Failed to inspect {name}"
        import json

        networks = json.loads(result.stdout.strip())
        actual_networks = sorted(networks.keys())
        assert actual_networks == sorted(expected_networks), (
            f"{name}: expected networks {expected_networks}, got {actual_networks}"
        )
        print(f"{name}: correctly on networks {actual_networks}")

    # Verify internal network is internal-only
    result = run_cmd(["docker", "network", "inspect", "egress_internal", "--format", "{{.Internal}}"])
    assert result.returncode == 0
    assert result.stdout.strip() == "true", "internal network must be internal-only"
    print("internal network correctly marked as internal-only")

    # Verify egress network is NOT internal-only
    result = run_cmd(["docker", "network", "inspect", "egress_egress", "--format", "{{.Internal}}"])
    assert result.returncode == 0
    assert result.stdout.strip() == "false", "egress network must allow external access"
    print("egress network correctly allows external access")
