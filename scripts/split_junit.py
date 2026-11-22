"""Split pytest JUnit reports into behavioral and invariant upload sets."""

from __future__ import annotations

import sys
from functools import cache
from pathlib import Path

from lxml import etree

from utils.paths import REPO_ROOT, get_test_roots

_INVARIANT_SEGMENT = "invariants"
_BUCKETS = ("behavioral", "invariants")
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _resolve(path: Path) -> Path:
    """Repo-relative arguments resolve through the one path door."""
    return path if path.is_absolute() else REPO_ROOT / path


@cache
def _invariant_prefixes() -> tuple[str, ...]:
    """Every first-class test home contributes an invariant prefix."""
    return tuple(f"{home.replace('/', '.')}.{_INVARIANT_SEGMENT}." for home in get_test_roots())


def _is_invariant(classname: str) -> bool:
    """Classify a JUnit classname by every derived invariant test home."""
    return any(classname.startswith(prefix) for prefix in _invariant_prefixes())


def _suite_attributes(suite: etree._Element, cases: list[etree._Element]) -> dict[str, str]:
    """Recompute counts so Codecov never reads a stale suite total."""
    attrs = dict(suite.attrib)
    attrs["tests"] = str(len(cases))
    attrs["failures"] = str(sum(case.find("failure") is not None for case in cases))
    attrs["errors"] = str(sum(case.find("error") is not None for case in cases))
    attrs["skipped"] = str(sum(case.find("skipped") is not None for case in cases))
    attrs["time"] = f"{sum(float(case.get('time', '0')) for case in cases):.3f}"
    return attrs


def _bucket_cases(root: etree._Element, bucket: str) -> list[tuple[etree._Element, list[etree._Element]]]:
    """The suites and cases belonging to one flagged upload."""
    selected: list[tuple[etree._Element, list[etree._Element]]] = []
    for suite in root.findall("testsuite"):
        cases = [
            case
            for case in suite.findall("testcase")
            if _is_invariant(case.get("classname", "")) == (bucket == "invariants")
        ]
        if cases:
            selected.append((suite, cases))
    return selected


def _write_bucket(root: etree._Element, bucket: str, path: Path) -> None:
    """Write one bucket's suites, skipping families with no cases."""
    out = etree.Element("testsuites", dict(root.attrib))
    for suite, cases in _bucket_cases(root, bucket):
        written = etree.SubElement(out, "testsuite", _suite_attributes(suite, cases))
        written.extend(cases)
    etree.ElementTree(out).write(str(path), encoding="utf-8", xml_declaration=True)


def split(source: Path, destination: Path) -> tuple[int, int]:
    """Split every worker report into the two flagged upload directories."""
    src = _resolve(source)
    dest = _resolve(destination)
    files = sorted(src.glob("junit-*.xml"))
    if not files:
        raise ValueError(f"no JUnit files in {src}")
    totals = [0, 0]
    for bucket in _BUCKETS:
        (dest / bucket).mkdir(parents=True, exist_ok=True)
    for path in files:
        root = etree.parse(str(path), _PARSER).getroot()
        for case in root.iter("testcase"):
            classname = case.get("classname", "")
            if not classname:
                raise ValueError(f"{path.name}: testcase has no classname")
        for index, bucket in enumerate(_BUCKETS):
            _write_bucket(root, bucket, dest / bucket / path.name)
            totals[index] += sum(len(cases) for _suite, cases in _bucket_cases(root, bucket))
    return totals[0], totals[1]


def main(argv: list[str] | None = None) -> int:
    """CLI entry: split <source-dir> <destination-dir>."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m scripts.split_junit <source-dir> <destination-dir>", file=sys.stderr)
        return 2
    behavioral, invariant = split(Path(args[0]), Path(args[1]))
    print(f"split {behavioral} behavioral and {invariant} invariant test cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
