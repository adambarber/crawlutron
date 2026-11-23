"""Registry assembling every typed config rule and the live config bundle."""

from __future__ import annotations

from tests._ci_contract import REPORTING_JOBS
from tests.config._bundle import Bundle, Rule
from tests.config._ci_config_rules import (
    coverage_at_prepush,
    jscpd_single_gate,
    precommit_hook_surface,
    prepare_action_env,
)
from tests.config._ci_model import PRECOMMIT, PREPARE
from tests.config._model import CODECOV, PYPROJECT
from tests.config._policy import RuleId
from tests.config._repo_config_rules import (
    codecov_components,
    codecov_informational,
    coverage_measure_once,
    coverage_source_pkgs,
    ruff_known_first_party,
)
from tests.config._script_model import CONFTEST, IMAGE_SCRIPT
from tests.config._script_rules import conftest_bytecode_enabled, image_release_script
from tests.config._workflow_rules import (
    ci_actionlint_in_reporting,
    ci_coverage_download_guards,
    ci_junit_split_order,
    ci_junit_upload_shape,
    ci_no_direct_codecov,
    ci_supply_chain_jobs,
    packaging_triggers,
    packaging_verify_steps,
    release_triggers,
)
from tests.workflows._model import WORKFLOWS

LIVE = Bundle(
    codecov=CODECOV,
    pyproject=PYPROJECT,
    precommit=PRECOMMIT,
    prepare=PREPARE,
    conftest=CONFTEST,
    image=IMAGE_SCRIPT,
    workflows=WORKFLOWS,
    reporting_jobs=frozenset(REPORTING_JOBS),
)

RULES: tuple[Rule, ...] = (
    Rule(RuleId.CODECOV_COMPONENTS, "per-surface measurement names six components", codecov_components),
    Rule(RuleId.CODECOV_INFORMATIONAL, "coverage never blocks a merge", codecov_informational),
    Rule(RuleId.COVERAGE_SOURCE_PKGS, "one package list from the uv members", coverage_source_pkgs),
    Rule(RuleId.COVERAGE_MEASURE_ONCE, "one measure-once coverage surface", coverage_measure_once),
    Rule(RuleId.RUFF_KNOWN_FIRST_PARTY, "isort knows every member import", ruff_known_first_party),
    Rule(RuleId.PRECOMMIT_HOOK_SURFACE, "CI cannot invoke an undeclared hook", precommit_hook_surface),
    Rule(RuleId.JSCPD_SINGLE_GATE, "one whole-repo duplication gate", jscpd_single_gate),
    Rule(RuleId.COVERAGE_AT_PREPUSH, "measurement is a push gate, not a commit gate", coverage_at_prepush),
    Rule(RuleId.PREPARE_ACTION_ENV, "one frozen gate environment", prepare_action_env),
    Rule(
        RuleId.CONFTEST_BYTECODE_ENABLED,
        "bytecode writes stay enabled under xdist",
        conftest_bytecode_enabled,
    ),
    Rule(RuleId.IMAGE_RELEASE_SCRIPT, "a released image matches its tag", image_release_script),
    Rule(RuleId.CI_SUPPLY_CHAIN_JOBS, "audit and secret-scan gates really run", ci_supply_chain_jobs),
    Rule(
        RuleId.CI_ACTIONLINT_IN_REPORTING, "actionlint failure blocks the merge", ci_actionlint_in_reporting
    ),
    Rule(RuleId.PACKAGING_TRIGGERS, "packaging is downstream of CI only", packaging_triggers),
    Rule(
        RuleId.PACKAGING_VERIFY_STEPS, "every distribution is inspected and installed", packaging_verify_steps
    ),
    Rule(
        RuleId.CI_JUNIT_UPLOAD_SHAPE, "each surface names its junit artifact exactly", ci_junit_upload_shape
    ),
    Rule(RuleId.CI_JUNIT_SPLIT_ORDER, "battery junit is split before flagged uploads", ci_junit_split_order),
    Rule(
        RuleId.CI_COVERAGE_DOWNLOAD_GUARDS,
        "skipped surfaces are not read as failures",
        ci_coverage_download_guards,
    ),
    Rule(RuleId.CI_NO_DIRECT_CODECOV, "only coverage reports to Codecov", ci_no_direct_codecov),
    Rule(RuleId.RELEASE_TRIGGERS, "release fires only on named tags and dispatch", release_triggers),
)
