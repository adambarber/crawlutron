"""Attach distribution artifacts and checksums to the release.

This replaces the `release-artifacts` job from release.yml.
"""

from __future__ import annotations

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

    # Download the verified distribution
    selector = os.environ.get("SELECTOR", "")
    dist_dir = REPO_ROOT / "dist"
    if not dist_dir.exists():
        print("no dist/ directory found")
        return 3

    # Build checksums
    result = subprocess.run(
        ["sh", "-c", "cd dist && shopt -s nullglob; files=(*.whl *.tar.gz); sha256sum -- \"${files[@]}\" > SHA256SUMS"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Checksum generation failed: {result.stderr}")
        return 3

    # Upload artifacts and checksums to the release
    upload_result = subprocess.run(
        [
            "gh", "release",
            "tag",
            *[str(f) for f in dist_dir.glob("*.whl *.tar.gz")],
            "SHA256SUMS",
            "--clobber",
        ],
        capture_output=True,
        text=True,
    )
    if upload_result.returncode != 0:
        print(f"Artifacts upload failed: {upload_result.stderr}")
        return 4

    print(f"Artifacts and checksums attached to release tag: {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())