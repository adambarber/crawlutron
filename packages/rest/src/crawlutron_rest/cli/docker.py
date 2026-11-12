"""``crawl up``: start the crawl service in Docker.

Explicit image/tag (never latest). Pull-if-missing with confirmation. Start
detached, wait for /health. No secrets in args or logs; no credential injection.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable

__all__ = ["run_up", "DockerUnavailable", "ImageMissing", "DockerStartError"]

DEFAULT_IMAGE = "crawlutron/crawlutron"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_HEALTH_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 1.0

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_SERVER_OR_TRANSPORT = 3


class DockerUnavailable(Exception):
    """The docker CLI is not on PATH."""


class ImageMissing(Exception):
    """The requested image:tag is not present locally."""


class DockerStartError(Exception):
    """``docker run`` failed."""


def _default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _image_present(image_ref: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> bool:
    result = run(["docker", "image", "inspect", image_ref])
    return result.returncode == 0


def _run_or_fail(
    argv: list[str],
    run: Callable[[list[str]], subprocess.CompletedProcess[str]],
    exc_cls: type[Exception],
    message: str,
) -> subprocess.CompletedProcess[str]:
    """Run one docker command and raise the given exception on failure."""
    result = run(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise exc_cls(f"{message}: {detail}")
    return result


def _pull(image_ref: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> None:
    _run_or_fail(
        ["docker", "pull", image_ref],
        run,
        ImageMissing,
        f"pull failed for {image_ref}",
    )


def _start_container(image_ref: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> str:
    return (
        _run_or_fail(
            ["docker", "run", "-d", "-p", "127.0.0.1:8765:8765", image_ref],
            run,
            DockerStartError,
            f"docker run failed for {image_ref}",
        )
    ).stdout.strip()


def _wait_for_health(
    base_url: str, health_timeout: float, run: Callable[[list[str]], subprocess.CompletedProcess[str]]
) -> bool:
    """Poll /health using curl (docker's own tool, avoids leaking headers)."""
    deadline = time.monotonic() + health_timeout
    while time.monotonic() < deadline:
        result = run(["curl", "-fsS", base_url + "/health"])
        if result.returncode == 0:
            return True
        time.sleep(DEFAULT_POLL_INTERVAL)
    return False


def _confirm_pull(image_ref: str) -> bool:
    """Ask once on stdin; return True only for an explicit ``y``."""
    sys.stderr.write(f"image {image_ref} is not present locally; pull it now? [y/N] ")
    sys.stderr.flush()
    line = sys.stdin.readline()
    return line.strip().lower() == "y"


def run_up(
    argv: list[str],
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _default_run,
) -> int:
    """Start the crawl service in Docker and wait for /health."""
    parser = argparse.ArgumentParser(prog="crawl up")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="Image name (default %(default)s).")
    parser.add_argument("--tag", required=True, help="Image tag (required; never 'latest' by default).")
    parser.add_argument("--yes", action="store_true", help="Skip pull confirmation (non-interactive).")
    parser.add_argument("--pull", action="store_true", help="Pull before start (implies --yes).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Health-check base URL.")
    parser.add_argument(
        "--no-wait-for-health",
        action="store_true",
        help="Start detached and return without waiting for /health.",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=DEFAULT_HEALTH_TIMEOUT,
        help="Seconds to wait for /health (default %(default)s).",
    )
    args = parser.parse_args(argv)

    if args.tag == "latest":
        sys.stderr.write("refusing to auto-deploy 'latest'; pass an explicit --tag\n")
        return EXIT_USAGE

    if shutil.which("docker") is None:
        sys.stderr.write("docker is not on PATH\n")
        return EXIT_USAGE

    image_ref = f"{args.image}:{args.tag}"

    if not _image_present(image_ref, run):
        if not (args.yes or args.pull):
            if not _confirm_pull(image_ref):
                return EXIT_USAGE
        _pull(image_ref, run)

    _start_container(image_ref, run)

    if args.no_wait_for_health:
        sys.stdout.write(f"service started: {image_ref}\n")
        return EXIT_SUCCESS

    healthy = _wait_for_health(args.base_url, args.health_timeout, run)
    if not healthy:
        sys.stderr.write(f"service at {args.base_url} did not become healthy\n")
        return EXIT_SERVER_OR_TRANSPORT
    sys.stdout.write("service up and healthy\n")
    return EXIT_SUCCESS
