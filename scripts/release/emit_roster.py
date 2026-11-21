"""Emit the released packages roster as compact GITHUB_OUTPUT and a one-line summary.

This replaces the inline python in release.yml's `released` job plus the
separate `python3 -` block that sets GITHUB_OUTPUT.
"""

from __future__ import annotations

import json
import os
import sys

from scripts.release._common import _require_env, _run


def main() -> int:
    # Resolve RELEASE_TAG: empty string means "tags at HEAD"
    release_tag = os.environ.get("RELEASE_TAG")

    # Get released entries (or all roster if RELEASE_TAG not set)
    if release_tag is not None:
        result = _run([sys.executable, "-m", "scripts.release_roster", "--released"])
    else:
        result = _run([sys.executable, "-m", "scripts.release_roster"])

    if result.returncode != 0:
        print(f"Roster resolution failed: {result.stderr}", file=sys.stderr)
        return 3

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse roster JSON: {e}", file=sys.stderr)
        return 3

    # Resolve primary via release_roster.py --primary (never hardcoded)
    primary_result = _run([sys.executable, "-m", "scripts.release_roster", "--primary"])
    if primary_result.returncode != 0:
        print(f"Primary resolution failed: {primary_result.stderr}", file=sys.stderr)
        return 3
    primary = primary_result.stdout.strip() == "true"

    # Write GITHUB_OUTPUT
    github_output = _require_env("GITHUB_OUTPUT")

    try:
        with open(github_output, "a", encoding="utf-8") as out:
            # packages=<JSON array> — compact, matching release.yml:92-94
            out.write(f"packages={json.dumps(entries)}\n")
            # root_version=<string> — version of the crawlutron entry, or ""
            root_entry = next((e for e in entries if e["name"] == "crawlutron"), None)
            root_version = root_entry["version"] if root_entry else ""
            out.write(f"root_version={root_version}\n")
            # primary=<true|false> — result of release_roster.py --primary
            out.write(f"primary={'true' if primary else 'false'}\n")
    except OSError as e:
        print(f"Failed to write GITHUB_OUTPUT: {e}", file=sys.stderr)
        return 2

    # Stdout: one-line summary
    root_version = root_entry["version"] if root_entry else ""
    primary_str = "primary=true" if primary else "primary=false"
    print(f"{len(entries)} package(s) tagged; root={root_version or 'not released'}; {primary_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
