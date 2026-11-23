"""Live config satisfies every typed config rule, and every rule is registered."""

from __future__ import annotations

import pytest

from tests.config._policy import RuleId
from tests.config._rules import LIVE, RULES


def test_every_policy_id_has_a_rule() -> None:
    assert {rule.id for rule in RULES} == set(RuleId), "a policy id has no check"


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.id.value)
def test_live_config_has_no_findings(rule) -> None:
    findings = list(rule.check(LIVE))
    assert findings == [], [finding.message for finding in findings]
