"""Attach distribution artifacts and checksums to the release.

This replaces the `release-artifacts` job from release.yml.
"""

from __future__ import annotations

import sys

from _common import REPO_ROOT, _gh_release_upload, _release_credentials, _run


def _build_checksums() -> None:
    checksum_cmd = (
        'cd dist && shopt -s nullglob; files=(*.whl *.tar.gz); sha256sum -- "${files[@]}" > SHA256SUMS'
    )
    result = _run(
        ["sh", "-c", checksum_cmd],
        capture=True,
    )
    if result.returncode != 0:
        print(f"Checksum generation failed: {result.stderr}", file=sys.stderr)
        sys.exit(3)


def _collect_files() -> list[str]:
    dist_dir = REPO_ROOT / "dist"
    if not dist_dir.exists():
        print("no dist/ directory found", file=sys.stderr)
        sys.exit(3)

    files = list(dist_dir.glob("*.whl")) + list(dist_dir.glob("*.tar.gz"))
    if not files:
        print("no wheel or sdist found to upload", file=sys.stderr)
        sys.exit(3)

    checksum_file = dist_dir / "SHA256SUMS"
    if not checksum_file.exists():
        print("SHA256SUMS not found", file=sys.stderr)
        sys.exit(3)

    return [str(f) for f in files] + [str(checksum_file)]


def main() -> int:
    tag, gh_token = _release_credentials()

    _build_checksums()
    upload_files = _collect_files()
    _gh_release_upload(tag, gh_token, upload_files, "Artifacts upload failed")

    print(f"Artifacts and checksums attached to release tag: {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
