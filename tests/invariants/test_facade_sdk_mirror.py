"""SDK knob-mirror clause of the facade knob-parity gate.

The SDK is a thin facade (sdk_thin_by_design; #846 owns completeness): every
accepted field must ride a mirror, a declared knob row, or a thin row.
"""

from __future__ import annotations

from tests.invariants._facade_tables import (
    SDK_KNOB_ROWS,
    SDK_THIN_ROWS,
    accepted_knobs,
)


def _sdk_mirror_fields() -> dict[tuple[str, str], frozenset[str]]:
    """Fields each SDK request mirror carries, keyed by its covered route."""
    from crawlutron_sdk import models as sdk

    return {
        ("POST", "/v1/crawl"): frozenset(sdk.CrawlRequest.model_fields),
        ("POST", "/v1/admit"): frozenset(sdk.AdmitRequest.model_fields),
        ("POST", "/v1/search"): frozenset(sdk.SearchRequest.model_fields),
        ("POST", "/v1/jobs"): frozenset(sdk.JobCreateRequest.model_fields),
        ("POST", "/v1/watches"): frozenset(sdk.WatchCreateRequest.model_fields),
        ("POST", "/v1/jobs/{job_id}/control"): frozenset(sdk.JobControlRequest.model_fields),
        ("POST", "/v1/map"): frozenset(sdk.MapRequest.model_fields),
        ("POST", "/v1/render"): frozenset(sdk.RenderRequest.model_fields),
        ("GET", "/v1/jobs"): frozenset({"limit", "offset"}),
    }


def test_sdk_mirror_covers_every_knob():
    """Each accepted field rides an SDK mirror or a declared exclusion row."""
    thin = set(SDK_THIN_ROWS)
    gaps = [
        str(knob)
        for knob in accepted_knobs()
        if (knob.method, knob.path) not in thin
        and knob.field not in SDK_KNOB_ROWS.get((knob.method, knob.path), frozenset())
        and knob.field not in _sdk_mirror_fields().get((knob.method, knob.path), frozenset())
    ]
    assert not gaps, f"fields the SDK neither mirrors nor excludes: {gaps}"


def test_sdk_exclusion_rows_stay_live():
    """A stale SDK row (route deleted, or mirrored after all) fails the gate."""
    routes = {(k.method, k.path) for k in accepted_knobs()}
    for row in SDK_THIN_ROWS:
        assert row in routes, f"SDK exclusion row {row} matches no accepted route"
    mirrored = _sdk_mirror_fields()
    for row in SDK_THIN_ROWS:
        assert row not in mirrored, f"SDK exclusion row {row} duplicates an existing mirror"
