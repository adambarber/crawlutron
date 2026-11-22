"""Generate coverage reports from combined coverage data.

Produces repo-wide and per-package coverage reports (XML, HTML, Markdown).
"""

from __future__ import annotations

# Add scripts directory to path for absolute imports
import sys
from pathlib import Path

from utils.paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from scripts.release._common import _run, roster

_COVERAGE = [sys.executable, "-m", "coverage"]


def _include_patterns() -> dict[str, str]:
    """Map package name -> include pattern for coverage filtering."""
    patterns = {}
    # Root project
    patterns["crawlutron"] = "packages/*/src"
    # Members
    for entry in roster():
        name = entry["name"]
        path = entry["path"]
        if name and path != ".":
            patterns[name] = f"{path}/src"
    return patterns


def _run_coverage(cmd: list[str]) -> None:
    result = _run(cmd)
    if result.returncode != 0:
        print(f"Coverage command failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def _repo_wide_reports(reports_dir: Path) -> None:
    xml_path = reports_dir / "coverage.xml"
    _run([*_COVERAGE, "xml", "-o", str(xml_path), "-i"])

    html_dir = reports_dir / "html"
    _run([*_COVERAGE, "html", "-d", str(html_dir), "-i"])

    md_path = reports_dir / "coverage.md"
    _run(
        [
            *_COVERAGE,
            "report",
            "--format=markdown",
            "-o",
            str(md_path),
            "-i",
        ]
    )


def _per_package_reports(reports_dir: Path, include_patterns: dict[str, str]) -> None:
    for package_name, include_pattern in include_patterns.items():
        pkg_dir = reports_dir / package_name
        pkg_dir.mkdir(parents=True, exist_ok=True)

        pkg_xml = pkg_dir / "coverage.xml"
        _run([*_COVERAGE, "xml", "-o", str(pkg_xml), "--include", f"{include_pattern}/*", "-i"])

        pkg_html = pkg_dir / "html"
        _run([*_COVERAGE, "html", "-d", str(pkg_html), "--include", f"{include_pattern}/*", "-i"])

        pkg_md = pkg_dir / "coverage.md"
        _run(
            [
                *_COVERAGE,
                "report",
                "--format=markdown",
                "--include",
                f"{include_pattern}/*",
                "-o",
                str(pkg_md),
                "-i",
            ]
        )


def main() -> int:
    coverage_file = REPO_ROOT / ".coverage"
    if not coverage_file.exists():
        print(f"ERROR: Coverage data file not found at {coverage_file}", file=sys.stderr)
        return 1

    include_patterns = _include_patterns()
    reports_dir = REPO_ROOT / "coverage_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    _repo_wide_reports(reports_dir)
    _per_package_reports(reports_dir, include_patterns)

    print(f"Coverage reports written to {reports_dir.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
