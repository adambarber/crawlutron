"""Latest-stable freshness: a stale pin is a gate failure, never a quiet warning."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import scripts.pin_verify as pv
from tests._ci_contract import REPORTING_JOBS, WORKFLOW_PATH

_SHA_A = "a" * 40
_SHA_B = "b" * 40


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "versions.toml"
    path.write_text(
        "[actions]\n"
        f'"owner/stale" = {{ version = "1.0.0", sha = "{_SHA_A}" }}\n'
        f'"owner/current" = {{ version = "2.0.0", sha = "{_SHA_B}" }}\n'
        "\n[outdated_waivers]\n"
    )
    monkeypatch.setattr(pv, "REGISTRY_PATH", path)
    return path


def test_numeric_versions_parse_and_prereleases_do_not() -> None:
    assert pv.parse_version("v4.0.3") == (4, 0, 3)
    assert pv.parse_version("4") == (4,)
    assert pv.parse_version("v1.2.3-rc1") is None
    assert pv.parse_version("codeql-bundle-v2.27.5") is None


def test_latest_stable_tag_ignores_prereleases_and_non_version_tags() -> None:
    tags = ["v1.0.0", "v2.0.0", "v2.1.0-rc1", "codeql-bundle-v2.27.5"]
    assert pv.latest_stable_tag(tags) == "v2.0.0"


def test_a_stale_pin_is_named_with_file_line_and_both_versions(registry: Path) -> None:
    latest = {"owner/stale": "1.1.0", "owner/current": "2.0.0"}
    errors = pv.outdated_pins(lambda dependency: latest[dependency])
    assert len(errors) == 1
    assert "versions.toml:2" in errors[0]
    assert "owner/stale" in errors[0]
    assert "1.0.0" in errors[0] and "1.1.0" in errors[0]


def test_current_pins_produce_no_errors(registry: Path) -> None:
    latest = {"owner/stale": "1.0.0", "owner/current": "2.0.0"}
    assert pv.outdated_pins(lambda dependency: latest[dependency]) == []


def test_a_waiver_suppresses_a_known_hold(registry: Path) -> None:
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "[outdated_waivers]\n", '[outdated_waivers]\n"owner/stale" = "intentional hold"\n'
        )
    )
    latest = {"owner/stale": "1.1.0", "owner/current": "2.0.0"}
    assert pv.outdated_pins(lambda dependency: latest[dependency]) == []


def test_an_unregistered_waiver_is_refused(registry: Path) -> None:
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "[outdated_waivers]\n", '[outdated_waivers]\n"owner/ghost" = "why"\n'
        )
    )
    errors = pv.outdated_pins(lambda dependency: "1.0.0")
    assert any("unregistered waiver" in error for error in errors)


def test_an_unknown_latest_version_fails_closed(registry: Path) -> None:
    errors = pv.outdated_pins(lambda _dependency: None)
    assert len(errors) == 2
    assert all("cannot determine latest" in error for error in errors)


def test_pinfreshness_is_a_required_pr_job() -> None:
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert "pin-freshness" in jobs
    assert "pin-freshness" in jobs["required"]["needs"]
    assert "pin-freshness" in REPORTING_JOBS
    runs = " ".join(step.get("run", "") for step in jobs["pin-freshness"]["steps"])
    assert "pin_verify outdated" in runs
