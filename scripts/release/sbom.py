"""Build and attach the dependency SBOM to the release.

This replaces the `sbom` job from release.yml.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    tag = os.environ.get("TAG", "")
    gh_token = os.environ.get("GH_TOKEN", "")

    if not tag:
        print("ERROR: TAG environment variable is required")
        return 2

    # Build sbom.cdx.json from the frozen lock
    lock_path = REPO_ROOT / "uv.lock"
    if not lock_path.exists():
        # Try to export the lock
        subprocess.run(
            [UV, "lock", "--frozen"],
            capture_output=True,
            text=True,
            check=True,
        )
        lock_path = REPO_ROOT / "uv.lock"

    # Export locked requirements to a temp file
    lock_txt = Path("/tmp/crawlutron-lock.txt")
    export_result = subprocess.run(
        [UV, "export", "--frozen", "--no-dev", "--no-emit-workspace", "--format", "requirements-txt", "-o", str(lock_txt)],
        capture_output=True,
        text=True,
    )
    if export_result.returncode != 0:
        print(f"Lock export failed: {export_result.stderr}")
        return 3

    # Generate SBOM using cyclonedx-bom
    sbom_path = Path("sbom.cdx.json")
    sbom_result = subprocess.run(
        [
            "uv", "tool",
            "run",
            "--from",
            "cyclonedx-bom==8.0.1",
            "cyclonedx-py",
            "requirements",
            str(lock_txt),
            "-o",
            str(sbom_path),
            "--of",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if sbom_result.returncode != 0:
        print(f"SBOM generation failed: {sbom_result.stderr}")
        return 3

    # Upload SBOM to the release
    upload_result = subprocess.run(
        [
            "gh", "release",
            "upload",
            tag,
            str(sbom_path),
            "--clobber",
        ],
        capture_output=True,
        text=True,
    )
    if upload_result.returncode != 0:
        print(f"SBOM upload failed: {upload_result.stderr}")
        return 4

    print(f"SBOM attached to release tag: {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())