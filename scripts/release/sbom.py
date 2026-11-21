"""Build and attach the dependency SBOM to the release.

This replaces the `sbom` job from release.yml.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _common import UV, _gh_release_upload, _release_credentials, _run


def _export_lock(lock_txt: Path) -> None:
    export_result = _run(
        [
            UV,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
            "-o",
            str(lock_txt),
        ],
        capture=True,
    )
    if export_result.returncode != 0:
        print(f"Lock export failed: {export_result.stderr}", file=sys.stderr)
        sys.exit(3)


def _generate_sbom(lock_txt: Path, sbom_path: Path) -> None:
    sbom_result = _run(
        [
            UV,
            "tool",
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
        capture=True,
    )
    if sbom_result.returncode != 0:
        print(f"SBOM generation failed: {sbom_result.stderr}", file=sys.stderr)
        sys.exit(3)


def main() -> int:
    tag, gh_token = _release_credentials()

    with tempfile.NamedTemporaryFile(mode="w", suffix="-lock.txt", delete=False) as f:
        lock_txt = Path(f.name)
    sbom_path = Path("sbom.cdx.json")

    _export_lock(lock_txt)
    _generate_sbom(lock_txt, sbom_path)
    _gh_release_upload(tag, gh_token, [str(sbom_path)], "SBOM upload failed")

    print(f"SBOM attached to release tag: {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
