"""Shared rule plumbing for the typed config domain."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from tests.config._ci_model import CompositeAction, Precommit
from tests.config._model import Codecov, Pyproject
from tests.config._policy import Finding, RuleId
from tests.config._script_model import ScriptFacts
from tests.workflows._model import Job, Workflow

STRICT_STAGES = frozenset({"pre-commit", "commit-msg", "pre-push"})


class Bundle(BaseModel):
    """Every typed config object a rule may need, assembled once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    codecov: Codecov
    pyproject: Pyproject
    precommit: Precommit
    prepare: CompositeAction
    conftest: ScriptFacts
    image: ScriptFacts
    workflows: tuple[Workflow, ...]
    reporting_jobs: frozenset[str]


@dataclass(frozen=True)
class Rule:
    id: RuleId
    rationale: str
    check: Callable[[Bundle], Iterator[Finding]]


def _finding(rule: RuleId, target: str, line: int, message: str) -> Finding:
    return Finding(rule, target, line, message)


def _workflow(bundle: Bundle, stem: str) -> Workflow:
    return next(item for item in bundle.workflows if item.stem == stem)


def _job(bundle: Bundle, stem: str, key: str) -> Job | None:
    return _workflow(bundle, stem).jobs.get(key)
