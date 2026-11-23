"""Surface census: every workflow is governed, waivers are bounded."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from pathlib import Path

from tests.workflows._model import POLICY, WORKFLOWS
from tests.workflows._policy import RuleId
from tests.workflows._registry import CATALOG

import_module("tests.workflows._decision_catalog")  # registration, not collection

WAIVER_CEILINGS = {rule.value: ceiling for rule, ceiling in POLICY.ceilings.items()}

TESTS_DIR = Path(__file__).resolve().parent.parent


def missing_decisions(*, stems: Iterable[str] | None = None) -> set[str]:
    wanted = {item.stem for item in WORKFLOWS} if stems is None else set(stems)
    return wanted - set(CATALOG)


def waiver_counts() -> dict[str, int]:
    return {rule.value: POLICY.count(rule) for rule in RuleId if rule in POLICY.ceilings}


def legacy_ci_tests_remaining() -> set[str]:
    """Top-level legacy CI text-contract tests that must be retired by typed migration."""
    return {path.name for path in TESTS_DIR.glob("test_ci*.py")}
