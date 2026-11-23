"""Rule identities and findings for the typed config domain."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuleId(StrEnum):
    CODECOV_COMPONENTS = "codecov-components"
    CODECOV_INFORMATIONAL = "codecov-informational"
    COVERAGE_SOURCE_PKGS = "coverage-source-pkgs"
    COVERAGE_MEASURE_ONCE = "coverage-measure-once"
    RUFF_KNOWN_FIRST_PARTY = "ruff-known-first-party"
    PRECOMMIT_HOOK_SURFACE = "precommit-hook-surface"
    JSCPD_SINGLE_GATE = "jscpd-single-gate"
    COVERAGE_AT_PREPUSH = "coverage-at-prepush"
    PREPARE_ACTION_ENV = "prepare-action-env"
    CONFTEST_BYTECODE_ENABLED = "conftest-bytecode-enabled"
    IMAGE_RELEASE_SCRIPT = "image-release-script"
    CI_SUPPLY_CHAIN_JOBS = "ci-supply-chain-jobs"
    CI_ACTIONLINT_IN_REPORTING = "ci-actionlint-in-reporting"
    CI_JUNIT_UPLOAD_SHAPE = "ci-junit-upload-shape"
    CI_JUNIT_SPLIT_ORDER = "ci-junit-split-order"
    CI_COVERAGE_DOWNLOAD_GUARDS = "ci-coverage-download-guards"
    CI_NO_DIRECT_CODECOV = "ci-no-direct-codecov"
    RELEASE_TRIGGERS = "release-triggers"
    RELEASE_NO_FAIL_FAST_MATRIX = "release-no-fail-fast-matrix"
    RELEASE_VALIDATE_BEFORE_UPLOAD = "release-validate-before-upload"
    PACKAGING_TRIGGERS = "packaging-triggers"
    PACKAGING_VERIFY_STEPS = "packaging-verify-steps"


@dataclass(frozen=True)
class Finding:
    rule: RuleId
    target: str
    line: int
    message: str
