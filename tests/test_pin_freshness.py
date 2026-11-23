"""Latest-stable freshness: a stale pin is a gate failure, never a quiet warning."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

import scripts.pin_verify as pv
from tests._ci_contract import REPORTING_JOBS, WORKFLOW_PATH

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_NEW_SHA = "c" * 40
_NEW_SHA_C = "d" * 40


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


@pytest.fixture
def bump_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    registry = tmp_path / ".github" / "versions.toml"
    registry.write_text(
        "[actions]\n"
        f'"owner/stale" = {{ version = "1.0.0", sha = "{_SHA_A}" }}\n'
        f'"owner/current" = {{ version = "2.0.0", sha = "{_SHA_B}" }}\n'
        "\n[outdated_waivers]\n",
        encoding="utf-8",
    )
    stale = f"      - uses: owner/stale@{_SHA_A} # v1.0.0\n"
    current = f"      - uses: owner/current@{_SHA_B} # v2.0.0\n"
    (workflows / "ci.yml").write_text("steps:\n" + stale + current, encoding="utf-8")
    (workflows / "nightly.yml").write_text("steps:\n" + stale, encoding="utf-8")
    monkeypatch.setattr(pv, "REGISTRY_PATH", registry)
    monkeypatch.setattr(pv, "ROOT", tmp_path)
    return tmp_path


def _fake_latest(**versions: str) -> Callable[[str], str | None]:
    return versions.get


def _fake_peel(**shas: str) -> Callable[[str, str], str | None]:
    return lambda name, version: shas.get(name)


def test_bump_rewrites_registry_and_every_site(bump_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    errors = pv.bump(
        _fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.0.0"}),
        _fake_peel(**{"owner/stale": _NEW_SHA}),
    )
    assert errors == []
    assert pv.load_registry()["owner/stale"] == ("1.1.0", _NEW_SHA)
    assert pv.load_registry()["owner/current"] == ("2.0.0", _SHA_B)
    for name in ("ci.yml", "nightly.yml"):
        text = (bump_tree / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert f"owner/stale@{_NEW_SHA} # v1.1.0" in text
    ci = (bump_tree / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"owner/current@{_SHA_B} # v2.0.0" in ci
    assert "owner/stale" in capsys.readouterr().out


def test_bump_self_verifies_after_writing(bump_tree: Path) -> None:
    latest = _fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.0.0"})
    assert pv.bump(latest, _fake_peel(**{"owner/stale": _NEW_SHA})) == []
    assert pv.verify_pins() == []
    assert pv.outdated_pins(latest) == []


def test_bump_leaves_waived_dependency_untouched(bump_tree: Path) -> None:
    registry = bump_tree / ".github" / "versions.toml"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "[outdated_waivers]\n", '[outdated_waivers]\n"owner/stale" = "hold"\n'
        ),
        encoding="utf-8",
    )
    latest = _fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.1.0"})
    assert pv.bump(latest, _fake_peel(**{"owner/current": _NEW_SHA_C})) == []
    assert pv.load_registry()["owner/stale"] == ("1.0.0", _SHA_A)
    assert pv.load_registry()["owner/current"] == ("2.1.0", _NEW_SHA_C)
    ci = (bump_tree / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"owner/stale@{_SHA_A} # v1.0.0" in ci


def test_failed_peel_aborts_without_any_write(bump_tree: Path) -> None:
    registry = bump_tree / ".github" / "versions.toml"
    ci = bump_tree / ".github" / "workflows" / "ci.yml"
    before_registry = registry.read_text(encoding="utf-8")
    before_ci = ci.read_text(encoding="utf-8")
    errors = pv.bump(_fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.0.0"}), _fake_peel())
    assert any("cannot peel" in error for error in errors)
    assert registry.read_text(encoding="utf-8") == before_registry
    assert ci.read_text(encoding="utf-8") == before_ci


def test_unregistered_waiver_errors_without_writes(bump_tree: Path) -> None:
    registry = bump_tree / ".github" / "versions.toml"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "[outdated_waivers]\n", '[outdated_waivers]\n"owner/ghost" = "why"\n'
        ),
        encoding="utf-8",
    )
    before = registry.read_text(encoding="utf-8")
    errors = pv.bump(
        _fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.0.0"}),
        _fake_peel(**{"owner/stale": _NEW_SHA}),
    )
    assert any("unregistered waiver" in error for error in errors)
    assert registry.read_text(encoding="utf-8") == before


def test_dry_run_mutates_nothing(bump_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    registry = bump_tree / ".github" / "versions.toml"
    ci = bump_tree / ".github" / "workflows" / "ci.yml"
    before_registry = registry.read_text(encoding="utf-8")
    before_ci = ci.read_text(encoding="utf-8")
    assert (
        pv.bump(
            _fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.0.0"}),
            _fake_peel(**{"owner/stale": _NEW_SHA}),
            dry_run=True,
        )
        == []
    )
    assert registry.read_text(encoding="utf-8") == before_registry
    assert ci.read_text(encoding="utf-8") == before_ci
    assert "1.1.0" in capsys.readouterr().out


def test_main_accepts_bump_with_dry_run(bump_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pv, "latest_version", _fake_latest(**{"owner/stale": "1.1.0", "owner/current": "2.0.0"})
    )
    monkeypatch.setattr(pv, "peel_to_commit", _fake_peel(**{"owner/stale": _NEW_SHA}))
    assert pv.main(["bump", "--dry-run"]) == 0
