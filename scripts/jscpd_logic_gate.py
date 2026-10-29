"""A 2-line/30-token gate over the helper surface, fail-closed on logic clones.

The enforced 3/30 gate misses a two-line clone between helper files; this gate
scans app/, scripts/, tests/_* at 2/30 and fails on cross-file LOGIC clones.
"""

from __future__ import annotations

import argparse
import sys

from scripts import _jscpd_pair_resolution as resolver
from scripts._jscpd_report_types import JscpdDuplicate, JscpdReport
from scripts.jscpd_sweep import classify

FLOOR = (2, 30)


def _helper_files() -> tuple[str, ...]:
    """The ``tests/_*.py`` helper namespace, as service-relative paths."""
    root = resolver.SERVICE_ROOT / "tests"
    return tuple(
        str(path.relative_to(resolver.SERVICE_ROOT))
        for path in sorted(root.glob("_*.py"))
        if path.is_file() and path.suffix == ".py"
    )


def helper_surface() -> tuple[str, ...]:
    """The scan surface: production, scripts, and the helper namespace."""
    return ("crawlutron", "scripts") + _helper_files()


def _is_cross_file(duplicate: JscpdDuplicate) -> bool:
    return duplicate["firstFile"]["name"] != duplicate["secondFile"]["name"]


def _logic_clones(report: JscpdReport) -> list[JscpdDuplicate]:
    return [
        duplicate
        for duplicate in report.get("duplicates") or []
        if _is_cross_file(duplicate) and classify(duplicate.get("fragment", "")) == "logic"
    ]


def _run(floor: tuple[int, int]) -> int:
    report = resolver.jscpd_report(
        resolver.CROSS_TREE_ROOT, floor, fail_on_empty=True, paths=helper_surface()
    )
    if report is None:
        print("jscpd logic gate: scan returned no report", file=sys.stderr)
        return 1
    clones = _logic_clones(report)
    if clones:
        print(f"jscpd logic gate: {len(clones)} cross-file logic clone(s) at {floor[0]}/{floor[1]}:")
        for duplicate in clones:
            first, second = duplicate["firstFile"], duplicate["secondFile"]
            print(
                f"  {first['name']}:{first['start']}  <->  {second['name']}:{second['start']}",
                file=sys.stderr,
            )
        return 1
    print(f"jscpd logic gate: no cross-file logic clones at {floor[0]}/{floor[1]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--floor", default=f"{FLOOR[0]}/{FLOOR[1]}", help="min-lines/min-tokens floor")
    args = parser.parse_args()
    lines, _, tokens = args.floor.partition("/")
    return _run((int(lines), int(tokens)))


if __name__ == "__main__":
    raise SystemExit(main())
