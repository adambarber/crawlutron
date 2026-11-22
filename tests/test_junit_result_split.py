"""JUnit results split into behavioral and invariant uploads by test path."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from scripts.split_junit import split


def _write_suite(directory: Path, name: str, cases: list[tuple[str, str, str]]) -> Path:
    suites = etree.Element("testsuites")
    suite = etree.SubElement(suites, "testsuite", name="pytest", tests=str(len(cases)))
    for classname, testname, status in cases:
        case = etree.SubElement(suite, "testcase", classname=classname, name=testname, time="1.0")
        if status == "failure":
            etree.SubElement(case, "failure", message="boom")
        elif status == "skipped":
            etree.SubElement(case, "skipped")
    path = directory / name
    etree.ElementTree(suites).write(str(path), encoding="utf-8", xml_declaration=True)
    return path


def _cases(path: Path) -> list[str]:
    root = etree.parse(str(path)).getroot()
    return [case.get("name", "") for suite in root.findall("testsuite") for case in suite.findall("testcase")]


def test_a_mixed_worker_file_splits_by_classname(tmp_path: Path) -> None:
    source = tmp_path / "junit"
    source.mkdir()
    _write_suite(
        source,
        "junit-1.xml",
        [
            ("tests.invariants.test_z", "inv", "passed"),
            ("tests.orchestration.test_a", "beh", "passed"),
        ],
    )
    dest = tmp_path / "split"
    split(source, dest)
    assert _cases(dest / "behavioral" / "junit-1.xml") == ["beh"]
    assert _cases(dest / "invariants" / "junit-1.xml") == ["inv"]


def test_member_test_home_invariants_are_classified(tmp_path: Path) -> None:
    source = tmp_path / "junit"
    source.mkdir()
    _write_suite(
        source,
        "junit-member.xml",
        [
            ("packages.core.tests.invariants.test_y", "member_inv", "passed"),
            ("tests.orchestration.test_a", "beh", "passed"),
        ],
    )
    dest = tmp_path / "split"
    split(source, dest)
    assert _cases(dest / "invariants" / "junit-member.xml") == ["member_inv"]
    assert _cases(dest / "behavioral" / "junit-member.xml") == ["beh"]


def test_failure_and_skip_counts_follow_their_case(tmp_path: Path) -> None:
    source = tmp_path / "junit"
    source.mkdir()
    _write_suite(
        source,
        "junit-2.xml",
        [
            ("tests.invariants.test_z", "inv_skip", "skipped"),
            ("tests.orchestration.test_a", "beh_fail", "failure"),
        ],
    )
    dest = tmp_path / "split"
    split(source, dest)
    inv = etree.parse(str(dest / "invariants" / "junit-2.xml")).getroot().find("testsuite")
    beh = etree.parse(str(dest / "behavioral" / "junit-2.xml")).getroot().find("testsuite")
    assert inv is not None and inv.get("skipped") == "1" and inv.get("failures") == "0"
    assert beh is not None and beh.get("failures") == "1" and beh.get("skipped") == "0"


def test_every_worker_file_is_processed(tmp_path: Path) -> None:
    source = tmp_path / "junit"
    source.mkdir()
    for index in (1, 2):
        _write_suite(source, f"junit-{index}.xml", [("tests.test_x", f"case_{index}", "passed")])
    dest = tmp_path / "split"
    split(source, dest)
    assert _cases(dest / "behavioral" / "junit-1.xml") == ["case_1"]
    assert _cases(dest / "behavioral" / "junit-2.xml") == ["case_2"]


def test_empty_source_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "junit"
    source.mkdir()
    with pytest.raises(ValueError, match="no JUnit files"):
        split(source, tmp_path / "split")


def test_a_case_without_a_classname_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "junit"
    source.mkdir()
    _write_suite(source, "junit-3.xml", [("", "nameless", "passed")])
    with pytest.raises(ValueError, match="junit-3.xml"):
        split(source, tmp_path / "split")
