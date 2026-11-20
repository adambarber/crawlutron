"""Build, smoke-test, push, and attest a container image for the release version.

This replaces the `image` job from release.yml.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    native_version = os.environ.get("NATIVE_VERSION", "")
    repo_owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    repository_owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")

    # If NATIVE_VERSION is empty (root not released), the entrypoint is a no-op
    if not native_version:
        print("root not released; image entrypoint no-op")
        return 0

    # Compute the image tag: lowercase, '+' -> '-'
    tag = native_version.lower().replace("+", "-")

    image = f"ghcr.io/{repository_owner}/crawlutron:{tag}"

    # Login to GHCR
    subprocess.run(
        [
            "docker",
            "login",
            "ghcr.io",
            "-u",
            os.environ.get("GITHUB_ACTOR", ""),
            "-p",
            os.environ.get("GITHUB_TOKEN", ""),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # Build the runtime image locally
    build_result = subprocess.run(
        [
            "docker", "build-push-action",
            "--context", ".",
            "--target", "runtime",
            "--file", "Dockerfile",
            "--no-push",
            f"--tag={image}",
        ],
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        print(f"Docker build failed: {build_result.stderr}")
        return 3

    # Smoke the built image
    name = "crawlutron-smoke"
    rm_result = subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
    )
    # Ignore errors from rm -f

    docker_run = subprocess.run(
        ["docker", "run", "-d", "--name", name,
         "-e", "CRAWLUTRON_HOST=0.0.0.0",
         "-p", "8765:8765", image],
        capture_output=True,
        text=True,
    )
    if docker_run.returncode != 0:
        print(f"Docker run failed: {docker_run.stderr}")
        return 3

    # Poll /health up to 60s
    health_passed = False
    for _ in range(60):
        result = subprocess.run(
            ["curl", "--noproxy", "*", "-fsS", "http://127.0.0.1:8765/health"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            health_passed = True
            break
        subprocess.run(["sleep", "1"], capture_output=True, text=True)

    if not health_passed:
        # Inspect and log for debugging
        inspect = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Status}} {{.State.ExitCode}}", name],
            capture_output=True,
            text=True,
        )
        logs = subprocess.run(
            ["docker", "logs", name],
            capture_output=True,
            text=True,
        )
        print(f"Smoke failed: health check timed out")
        print(f"inspect: {inspect.stdout}")
        print(f"logs: {logs.stdout}")
        return 3

    # Check /ready
    ready_result = subprocess.run(
        ["curl", "--noproxy", "*", "-fsS", "http://127.0.0.1:8765/ready"],
        capture_output=True,
        text=True,
    )
    if ready_result.returncode != 0:
        print(f"Smoke failed: /ready not accessible")
        return 3

    # Bake version from inside the container
    baked_result = subprocess.run(
        ["docker", "exec", name,
         "/app/.venv/bin/python", "-c",
         "from importlib.metadata import version; print(version('crawlutron'))"],
        capture_output=True,
        text=True,
    )
    if baked_result.returncode != 0:
        print(f"Smoke failed: could not bake version: {baked_result.stderr}")
        return 3

    baked_version = baked_result.stdout.strip()
    expected_version = native_version
    if baked_version != expected_version:
        print(f"Smoke failed: version mismatch: baked={baked_version}, expected={expected_version}")
        return 3

    # Push the smoked image
    push_result = subprocess.run(
        ["docker", "push", image],
        capture_output=True,
        text=True,
    )
    if push_result.returncode != 0:
        print(f"Docker push failed: {push_result.stderr}")
        return 4

    # Attest the pushed image
    digest_result = subprocess.run(
        [
            "docker", "buildx", "imagetools",
            "inspect", image,
            "--format",
            "{{.Manifest.Digest}}",
        ],
        capture_output=True,
        text=True,
    )
    if digest_result.returncode != 0:
        print(f"Attestation digest lookup failed: {digest_result.stderr}")
        return 4

    digest = digest_result.stdout.strip()

    attest_result = subprocess.run(
        [
            "actions", "attest-build-provenance",
            "--subject-name",
            f"ghcr.io/{repository_owner}/crawlutron",
            "--subject-digest",
            digest,
            "--push-to-registry",
        ],
        capture_output=True,
        text=True,
    )
    if attest_result.returncode != 0:
        print(f"Attestation failed: {attest_result.stderr}")
        return 4

    print(f"image built, pushed, and attested: {image}")
    return 0


if __name__ == "__main__":
    sys.exit(main())