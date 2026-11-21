"""Build distribution and assert version matches the released entry."""

from __future__ import annotations

import email
import sys
import zipfile

from _common import REPO_ROOT, UV, _require_env, _run


def main() -> int:
    selector = _require_env("SELECTOR")
    expected_version = _require_env("EXPECTED_VERSION")

    # Build only this distribution
    build_result = _run(
        [UV, "build", "--package", selector, "--out-dir", "dist"],
        capture=True,
    )
    if build_result.returncode != 0:
        print(f"Build failed: {build_result.stderr}", file=sys.stderr)
        return 3

    wheels = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if not wheels:
        print("no wheel built", file=sys.stderr)
        return 3

    # Assert each wheel carries the release version
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            metadata_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            metadata_bytes = archive.read(metadata_name)
            version = email.message_from_bytes(metadata_bytes)["Version"]
        print(f"{wheel.name}: {version}")
        if version != expected_version:
            print(f"{wheel.name} carries {version}, the release is {expected_version}", file=sys.stderr)
            return 4

    # Run twine check --strict
    twine_result = _run(["uvx", "twine", "check", "--strict", "dist/*"], capture=True)
    if twine_result.returncode != 0:
        for line in twine_result.stdout.strip().split("\n"):
            if line:
                print(f"::error::{line}", file=sys.stderr)
        for line in twine_result.stderr.strip().split("\n"):
            if line:
                print(f"::error::{line}", file=sys.stderr)
        return 5

    print(f"Distribution {selector}=={expected_version} built and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
