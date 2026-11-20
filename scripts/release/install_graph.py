"""Install-graph probe + retry for released packages.

This replaces the `install-graph` job from release.yml.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    name = os.environ.get("NAME", "")
    version = os.environ.get("VERSION", "")

    if not name or not version:
        print("ERROR: NAME and VERSION environment variables are required")
        return 2

    # Create a clean venv
    probe_dir = Path("/tmp/probe")
    if probe_dir.exists():
        subprocess.run(["rm", "-rf", str(probe_dir)], capture_output=True, text=True)
    probe_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [sys.executable, "-m", "venv", str(probe_dir)],
        capture_output=True,
        text=True,
    )

    # Retry pip install up to 30 attempts, sleeping 10s between attempts
    installed = False
    for attempt in range(1, 31):
        result = subprocess.run(
            [str(probe_dir / "bin" / "pip"), "install", "--quiet", "--no-cache-dir", f"{name}=={version}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            installed = True
            break
        if attempt == 30:
            print(f"::error::{name}=={version} never resolved from the published index")
            return 3
        subprocess.run(["sleep", "10"], capture_output=True, text=True)

    if not installed:
        return 3

    # Run pip check
    pip_check = subprocess.run(
        [str(probe_dir / "bin" / "pip"), "check"],
        capture_output=True,
        text=True,
    )
    if pip_check.returncode != 0:
        print(f"pip check failed: {pip_check.stderr}")
        return 4

    # If NAME == "crawlutron", import all member packages
    if name == "crawlutron":
        member_packages = [
            "crawlutron_extract",
            "crawlutron_mcp",
            "crawlutron_rest",
            "crawlutron_sdk",
            "safe_url",
        ]
        for pkg in member_packages:
            import_result = subprocess.run(
                [str(probe_dir / "bin" / "python"), "-c", f"import {pkg}"],
                capture_output=True,
                text=True,
            )
            if import_result.returncode != 0:
                print(f"Import failed for {pkg}: {import_result.stderr}")
                return 4

    print(f"Package {name}=={version} installed and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())