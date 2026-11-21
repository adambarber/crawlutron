"""Emit the manifest-derived PyPI release roster as JSON.

Root project plus every ``[tool.uv.workspace].members`` package, each carrying
its declared name, version, and directory.
"""

from __future__ import annotations

import argparse
import json
import os

# Add scripts directory to path for absolute imports
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from release._common import (
    primary_tag,
    released,
    roster,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--released", action="store_true", help="only packages tagged at HEAD")
    parser.add_argument("--primary", action="store_true", help="is this event the publishing one")
    arguments = parser.parse_args()
    if arguments.primary:
        print(
            "true"
            if primary_tag(
                event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
                ref_name=os.environ.get("GITHUB_REF_NAME", ""),
            )
            else "false"
        )
        return
    entries = released(tag=os.environ.get("RELEASE_TAG") or None) if arguments.released else roster()
    print(json.dumps(entries, indent=2))


if __name__ == "__main__":
    main()
