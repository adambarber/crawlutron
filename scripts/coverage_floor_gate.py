"""Pre-push per-package coverage floor gate (#1358).

Refuses any package whose measured coverage falls below its seeded floor.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

import coverage

from tests._walk_primitives import package_roots
from tests.invariants._coverage_floors import PACKAGE_FLOORS


def measure_packages(cov: coverage.Coverage) -> dict[str, float]:
    """Statement coverage percent for each derived package root."""
    data = cov.get_data()
    roots = sorted(package_roots(), key=len, reverse=True)
    totals = {root: [0, 0] for root in package_roots()}
    for path in data.measured_files():
        for root in roots:
            if path.startswith(root + "/"):
                analysis = cov.analysis2(path)
                statements = set(analysis[1])
                missing = set(analysis[3])
                totals[root][0] += len(statements)
                totals[root][1] += len(statements) - len(missing)
                break
    return {root: (100.0 * covered / total if total else 0.0) for root, (total, covered) in totals.items()}


def check_floors(measured: Mapping[str, float], floors: Mapping[str, float]) -> list[str]:
    """One message per breached floor, naming the package and its floor."""
    breaches = []
    for root in sorted(floors):
        value = measured.get(root, 0.0)
        floor = floors[root]
        if value < floor:
            breaches.append(f"{root}: measured {value:.2f}% below floor {floor:g}%")
    return breaches


def main() -> int:
    """Load the combined data and fail on any per-package floor breach."""
    cov = coverage.Coverage(config_file=True)
    cov.load()
    breaches = check_floors(measure_packages(cov), PACKAGE_FLOORS)
    for breach in breaches:
        print(f"coverage floor: {breach}", file=sys.stderr)
    return 1 if breaches else 0


if __name__ == "__main__":
    raise SystemExit(main())
