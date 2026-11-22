"""Bites for the typed workflow primitives."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.workflows._names import DisplayName, EnvName, JobId, StepName, WorkflowStem


@pytest.mark.parametrize(
    ("adapter", "value"),
    [
        (TypeAdapter(StepName), "   "),
        (TypeAdapter(StepName), ""),
        (TypeAdapter(StepName), "x" * 121),
        (TypeAdapter(StepName), "line one\nline two"),
        (TypeAdapter(StepName), "Use ${{ github.event.x }}"),
        (TypeAdapter(DisplayName), "   "),
        (TypeAdapter(JobId), "Bad_Name"),
        (TypeAdapter(JobId), "-leading"),
        (TypeAdapter(WorkflowStem), "BadName"),
        (TypeAdapter(EnvName), "lower_case"),
    ],
)
def test_a_name_that_violates_its_grammar_is_unrepresentable(adapter, value) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize(
    ("adapter", "value"),
    [
        (TypeAdapter(StepName), "Run pytest (browser, PRs only)"),
        (TypeAdapter(DisplayName), "Build ${{ matrix.package.name }} for PyPI"),
        (TypeAdapter(JobId), "real-pbkdf2"),
        (TypeAdapter(WorkflowStem), "agent-review"),
        (TypeAdapter(EnvName), "PYTEST_ADDOPTS"),
    ],
)
def test_a_name_from_the_real_tree_is_accepted(adapter, value) -> None:
    assert adapter.validate_python(value) == value
