"""Named product decisions, registered at import from the catalog module."""

from __future__ import annotations

from tests.workflows._decision_catalog import (
    test_codeql_uploads_findings_with_the_narrow_write_scope,
    test_nightly_runs_the_full_marker_set,
    test_only_the_publish_job_holds_the_publish_credential,
    test_packaging_cannot_publish,
    test_release_please_is_owner_dispatched,
    test_scorecard_runs_on_a_schedule_and_writes_findings,
    test_the_coverage_job_does_not_rerun_pytest,
    test_the_probe_plane_holds_no_trusted_credential,
    test_the_rehearsal_workflow_has_no_write_scope,
    test_the_roster_resolver_exposes_its_three_outputs,
    test_weekly_mutation_job_is_timeboxed,
)

__all__ = [
    "test_codeql_uploads_findings_with_the_narrow_write_scope",
    "test_the_coverage_job_does_not_rerun_pytest",
    "test_nightly_runs_the_full_marker_set",
    "test_only_the_publish_job_holds_the_publish_credential",
    "test_packaging_cannot_publish",
    "test_release_please_is_owner_dispatched",
    "test_scorecard_runs_on_a_schedule_and_writes_findings",
    "test_the_probe_plane_holds_no_trusted_credential",
    "test_the_rehearsal_workflow_has_no_write_scope",
    "test_the_roster_resolver_exposes_its_three_outputs",
    "test_weekly_mutation_job_is_timeboxed",
]
