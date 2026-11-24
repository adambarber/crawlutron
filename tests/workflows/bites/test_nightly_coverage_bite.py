"""Nightly coverage handoff, derived from the live test-door definition."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tests.chokepoint_paths import REPO_ROOT
from tests.workflows._model import Step, parse_document, workflow

_DOOR = REPO_ROOT / ".github" / "actions" / "run-tests" / "action.yml"


def _door_step() -> Step:
    raw, index = parse_document(_DOOR)
    if not isinstance(raw, Mapping):
        raise AssertionError(f"{_DOOR.name} is not a mapping")
    runs = raw.get("runs")
    if not isinstance(runs, Mapping):
        raise AssertionError(f"{_DOOR.name} has no runs block")
    steps = runs.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        raise AssertionError(f"{_DOOR.name} has no composite steps")
    for number, item in enumerate(steps):
        if not isinstance(item, Mapping):
            continue
        line = index.get(("runs", "steps", str(number)), 0)
        admitted = Step.admit_or_none({**item, "line": line})
        if admitted is not None and admitted.run and "pytest" in admitted.run:
            return admitted
    raise AssertionError(f"{_DOOR.name} has no pytest door step")


def _door_fragment() -> str:
    for key, value in _door_step().env.items():
        if str(key) == "COVERAGE_FILE":
            return str(value)
    raise AssertionError(f"{_DOOR.name} has no COVERAGE_FILE")


def test_door_emits_a_hidden_coverage_fragment() -> None:
    """No hand-maintained argv: the fact is read from the door itself."""
    run = _door_step().run or ""
    assert "--cov=crawlutron" in run and "--cov-report=" in run
    assert "-o addopts=" not in run
    assert _door_fragment().startswith(".coverage.")


def test_producer_uploads_the_door_fragment() -> None:
    producer = workflow("nightly").jobs["test"]
    assert any(step.uses and "run-tests" in step.uses for step in producer.steps)
    uploads = [
        step
        for step in producer.steps
        if step.uses and "upload-artifact" in step.uses and step.with_.get("name") == "coverage-data-nightly"
    ]
    assert len(uploads) == 1
    upload = uploads[0]
    assert str(upload.with_.get("path")) == ".coverage*"
    assert upload.with_.get("include-hidden-files") is True
    assert upload.with_.get("if-no-files-found") == "error"
    assert "always()" in str(upload.if_)


def test_producer_does_not_report_from_an_uncombined_fragment() -> None:
    producer = workflow("nightly").jobs["test"]
    assert not [step for step in producer.steps if step.run and "scripts.coverage_reports" in step.run]


def test_consumer_combines_before_reporting() -> None:
    consumer = workflow("nightly").jobs["coverage"]
    runs = [step.run or "" for step in consumer.steps]
    combine = next(i for i, run in enumerate(runs) if "coverage combine" in run)
    reports = next(i for i, run in enumerate(runs) if "scripts.coverage_reports" in run)
    assert combine < reports
    downloads = [step for step in consumer.steps if step.uses and "download-artifact" in step.uses]
    assert any(step.with_.get("name") == "coverage-data-nightly" for step in downloads)
