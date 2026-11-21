"""Build, smoke-test, and push a container image for the release version.

This replaces the `image` job from release.yml. The SBOM is generated here
(pre-push, rehearsable) and published by the workflow's attest-sbom step.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

from utils.paths import REPO_ROOT


def _setup_paths() -> pathlib.Path:
    """Expose the test support package and return repo root."""
    _repo_root = REPO_ROOT
    sys.path.insert(0, str(_repo_root / "tests"))
    return _repo_root


_setup_paths()

from scripts.release._common import _require_env, _run
from tests._support._image_types import _HasCookies

SBOM_PATH = pathlib.Path("crawlutron-sbom.cdx.json")


async def _get_cookie_names(context: _HasCookies, target: str) -> list[str]:
    """Get cookie names with proper type hints."""
    cookies = await context.cookies(target)
    return [c["name"] for c in cookies]


async def _get_cookie_jar(context: _HasCookies, target: str) -> dict[str, str]:
    """Get cookie jar with proper type hints."""
    cookies = await context.cookies(target)
    return {c["name"]: c["value"] for c in cookies}


def _validate_env() -> tuple[str, str]:
    native_version = os.environ.get("NATIVE_VERSION", "")
    if not native_version:
        print("root not released; image entrypoint no-op")
        sys.exit(0)
    repository_owner = _require_env("GITHUB_REPOSITORY_OWNER")
    return native_version, repository_owner


def _docker_login() -> None:
    github_actor = _require_env("GITHUB_ACTOR")
    github_token = _require_env("GITHUB_TOKEN")
    login_result = _run(
        [
            "docker",
            "login",
            "ghcr.io",
            "-u",
            github_actor,
            "-p",
            github_token,
        ],
        capture=True,
    )
    if login_result.returncode != 0:
        print(f"Docker login failed: {login_result.stderr}", file=sys.stderr)
        sys.exit(3)


def _build_image(image: str) -> None:
    build_result = _run(
        [
            "docker",
            "build",
            "--target",
            "runtime",
            "--load",
            "--tag",
            image,
            ".",
        ],
        capture=True,
    )
    if build_result.returncode != 0:
        print(f"Docker build failed: {build_result.stderr}", file=sys.stderr)
        sys.exit(3)


def _run_container(name: str, image: str) -> None:
    _run(["docker", "rm", "-f", name], capture=True)
    docker_run = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            "CRAWLUTRON_HOST=0.0.0.0",
            "-p",
            "8765:8765",
            image,
        ],
        capture=True,
    )
    if docker_run.returncode != 0:
        print(f"Docker run failed: {docker_run.stderr}", file=sys.stderr)
        sys.exit(3)


def _wait_for_health(name: str) -> None:
    health_passed = False
    for _ in range(60):
        result = _run(
            ["curl", "--noproxy", "*", "-fsS", "http://127.0.0.1:8765/health"],
            capture=True,
        )
        if result.returncode == 0:
            health_passed = True
            break
        _run(["sleep", "1"], capture=True)

    if not health_passed:
        inspect = _run(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name],
            capture=True,
        )
        logs = _run(["docker", "logs", name], capture=True)
        print("Smoke failed: health check timed out", file=sys.stderr)
        print(f"inspect: {inspect.stdout}", file=sys.stderr)
        print(f"logs: {logs.stdout}", file=sys.stderr)
        sys.exit(3)


def _check_ready() -> None:
    ready_result = _run(
        ["curl", "--noproxy", "*", "-fsS", "http://127.0.0.1:8765/ready"],
        capture=True,
    )
    if ready_result.returncode != 0:
        print("Smoke failed: /ready not accessible", file=sys.stderr)
        sys.exit(3)


def _check_baked_version(name: str, native_version: str) -> None:
    baked_result = _run(
        [
            "docker",
            "exec",
            name,
            "/app/.venv/bin/python",
            "-c",
            "from importlib.metadata import version; print(version('crawlutron'))",
        ],
        capture=True,
    )
    if baked_result.returncode != 0:
        print(f"Smoke failed: could not bake version: {baked_result.stderr}", file=sys.stderr)
        sys.exit(3)

    baked_version = baked_result.stdout.strip()
    if baked_version != native_version:
        msg = f"Smoke failed: version mismatch: baked={baked_version}, expected={native_version}"
        print(msg, file=sys.stderr)
        sys.exit(3)


def _smoke_image(name: str, image: str, native_version: str) -> None:
    _run_container(name, image)
    _wait_for_health(name)
    _check_ready()
    _check_baked_version(name, native_version)


def _generate_sbom(image: str) -> None:
    """Scan the loaded image with syft; the workflow attests the file post-push."""
    syft = shutil.which("syft")
    if syft is None:
        print("syft is not installed; the workflow installs it before this script runs", file=sys.stderr)
        sys.exit(3)
    result = subprocess.run(
        [syft, image, "-o", f"cyclonedx-json={SBOM_PATH}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"SBOM generation failed: {result.stderr}", file=sys.stderr)
        sys.exit(3)


def _push_image(image: str) -> str:
    """Push the smoke-passed image and return its registry digest."""
    push_result = _run(["docker", "push", image], capture=True)
    if push_result.returncode != 0:
        print(f"Docker push failed: {push_result.stderr}", file=sys.stderr)
        sys.exit(4)

    digest_result = _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            "{{.Manifest.Digest}}",
        ],
        capture=True,
    )
    if digest_result.returncode != 0:
        print(f"Attestation digest lookup failed: {digest_result.stderr}", file=sys.stderr)
        sys.exit(4)
    return digest_result.stdout.strip()


def _export_outputs(digest: str) -> None:
    """Hand the digest and SBOM path to the workflow's attest step."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        print("GITHUB_OUTPUT is required so the SBOM attestation can address the push")
        sys.exit(2)
    with open(github_output, "a", encoding="utf-8") as out:
        out.write(f"digest={digest}\n")
        out.write(f"sbom-path={SBOM_PATH}\n")


def _image_tag(native_version: str, repository_owner: str) -> str:
    """The OCI image reference for a released version."""
    tag = native_version.lower().replace("+", "-")
    return f"ghcr.io/{repository_owner}/crawlutron:{tag}"


def main() -> int:
    native_version, repository_owner = _validate_env()
    image = _image_tag(native_version, repository_owner)
    name = "crawlutron-smoke"

    _docker_login()
    _build_image(image)

    try:
        _smoke_image(name, image, native_version)
    finally:
        _run(["docker", "rm", "-f", name], capture=True)

    _generate_sbom(image)
    digest = _push_image(image)
    _export_outputs(digest)
    print(f"image built, smoked, SBOMed, and pushed: {image}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
