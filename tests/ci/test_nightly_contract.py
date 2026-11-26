"""Residual contract tests for nightly.yml not covered by the typed workflow rules."""

from __future__ import annotations

import pathlib

import pytest

from tests.chokepoint_paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "nightly.yml"
CODEOWNERS_PATH = SERVICE_ROOT / ".github" / "CODEOWNERS"
PYPROJECT_PATH = SERVICE_ROOT / "pyproject.toml"
RUN_TESTS_ACTION_PATH = WORKFLOW_PATH.parent.parent / "actions" / "run-tests" / "action.yml"


def workflow_document() -> dict[object, object]:
    """Load and parse the nightly workflow."""
    import yaml

    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "nightly.yml is not a workflow"
    return document


def test_nightly_workflow_exists() -> None:
    """nightly.yml must exist."""
    assert WORKFLOW_PATH.exists(), "nightly.yml does not exist"


def test_nightly_xvfb_prepare_browser_camoufox() -> None:
    """nightly.yml has prepare-browser; the door action handles camoufox/fpgen/xvfb."""
    doc = workflow_document()
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "jobs must be a mapping"

    test_job = jobs.get("test")
    assert isinstance(test_job, dict), "test job required"

    steps = test_job.get("steps")
    assert isinstance(steps, list), "test job must have steps"

    prepare_browser = any(
        "prepare-browser" in str(step.get("uses", "")) for step in steps if isinstance(step, dict)
    )
    door = any("run-tests" in str(step.get("uses", "")) for step in steps if isinstance(step, dict))
    assert prepare_browser, "prepare-browser action required"
    assert door, "nightly test job must call the run-tests door"

    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    assert "cache" in action and "camoufox" in action, "the door must cache camoufox"
    assert "camoufox fetch" in action, "the door must fetch camoufox"
    assert "ensure_fpgen_model" in action, "the door must install the fpgen model"
    assert "xvfb-run" in action, "the door must run pytest under xvfb-run"


def test_nightly_env_no_proxy_headless_false() -> None:
    """nightly.yml must set NO_PROXY and CRAWLUTRON_HEADLESS=false for browser tests."""
    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    assert "NO_PROXY: 127.0.0.1,localhost" in action, "bad NO_PROXY in door action"
    assert 'CRAWLUTRON_HEADLESS: "false"' in action, "bad headless in door action"


def test_nightly_coverage_source_pkgs_not_overridden() -> None:
    """nightly.yml must not override coverage source_pkgs; the door uses --cov=crawlutron only."""
    import re
    import tomllib

    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    expected_source_pkgs = data.get("tool", {}).get("coverage", {}).get("run", {}).get("source_pkgs", [])
    assert isinstance(expected_source_pkgs, list), "source_pkgs must be a list"
    assert len(expected_source_pkgs) == 7, (
        f"expected 7 source_pkgs, got {len(expected_source_pkgs)}: {expected_source_pkgs}"
    )

    action = RUN_TESTS_ACTION_PATH.read_text(encoding="utf-8")
    cov_matches = re.findall(r"--cov=([^\s]+)", action)
    assert set(cov_matches) == {"crawlutron"}, f"expected --cov=crawlutron only, got {cov_matches}"
    assert "--cov-source" not in action, "must not override --cov-source"
    assert "--cov-config" not in action, "must not override --cov-config"


def test_nightly_codeowners_entry_exists() -> None:
    """nightly.yml must have a CODEOWNERS entry."""
    content = CODEOWNERS_PATH.read_text(encoding="utf-8")
    assert ".github/workflows/nightly.yml" in content, "nightly.yml missing from CODEOWNERS"
    line = [ln for ln in content.splitlines() if ".github/workflows/nightly.yml" in ln][0]
    assert "@adambarber" in line, "nightly.yml must have @adambarber owner"


def test_egress_default_deny_contract() -> None:
    """Compose/CI contract: egress is default-deny; only the loopback proxy is the
    sanctioned dial path. Required exceptions: proxy DNS resolution, loopback
    healthcheck, published local ports are exempted and sinkholed."""

    compose_path = REPO_ROOT / "docker-compose.yml"
    dockerfile_path = REPO_ROOT / "Dockerfile"

    compose = compose_path.read_text(encoding="utf-8")
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    # Service binds to loopback only; exact mapping with COMPOSE_HOST_PORT
    assert "127.0.0.1:${COMPOSE_HOST_PORT-8765}:8765" in compose, (
        "compose must bind the service port to loopback only with COMPOSE_HOST_PORT"
    )
    assert "0.0.0.0:8765:8765" not in compose, "compose must not bind the service port to all interfaces"
    assert '\n      - "8765:8765"' not in compose, (
        "compose must not expose the service port without a loopback bind"
    )

    # Private targets are blocked by default: the secure default is on.
    assert 'CRAWLUTRON_ALLOW_PRIVATE_TARGETS: "0"' in compose, "compose must block private targets by default"

    # Telemetry sinkholes are declared for the sinks that have no opt-out flag.
    assert "extra_hosts:" in compose, "compose must declare telemetry sinkholes"
    for sink in (
        "mobile.events.data.microsoft.com",
        "browser.events.data.microsoft.com",
        "dc.services.visualstudio.com",
    ):
        assert sink in compose, f"compose must sinkhole telemetry host {sink}"

    # Healthcheck targets loopback only, never 0.0.0.0 or an external host.
    assert "http://localhost:8765/health" in compose, "healthcheck must reference the loopback address"
    assert "0.0.0.0:8765/health" not in compose, "healthcheck must not target all interfaces"

    # Dockerfile installs curl solely for the loopback healthcheck (compose-verified).
    assert "curl" in dockerfile, "Dockerfile must provide curl for the loopback healthcheck"

    # No baked-outbound proxy: the proxy is runtime-managed through the egress door,
    # not baked into the image as an ambient proxy that could bypass the door.
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ARG ", "ENV ")) and "proxy" in stripped.lower():
            assert "CRAWLUTRON_BASE_IMAGE" in stripped or "PYTHON" in stripped, (
                f"Dockerfile bakes a proxy setting that could bypass the egress door: {stripped}"
            )


_TOPOLOGY_SERVICES = frozenset({"crawlutron", "egress-proxy", "api-ingress", "origin"})
_TOPOLOGY_NETWORKS = frozenset({"internal", "egress", "ingress"})
_ROLE_NETWORKS = {
    "crawlutron": frozenset({"internal"}),
    "egress-proxy": frozenset({"internal", "egress"}),
    "api-ingress": frozenset({"internal", "ingress"}),
    "origin": frozenset({"egress"}),
}


def _compose_files() -> tuple[pathlib.Path, ...]:
    """Every compose layer the fast topology gate owns, derived not hand-listed."""
    from tests._walk_primitives import discovered_files

    return discovered_files(REPO_ROOT, "docker-compose*.yml")


def test_topology_contract_covers_every_discovered_compose_file() -> None:
    """The fast topology gate names every overlay, not just base + egress."""
    from tests._walk_primitives import discovered_files

    attached = getattr(test_egress_topology_contract, "pytestmark", ())
    marks = [mark for mark in attached if mark.name == "parametrize"]
    covered = {path.name for mark in marks for path in mark.args[1]}
    discovered = {path.name for path in discovered_files(REPO_ROOT, "docker-compose*.yml")}
    assert covered == discovered, f"topology gate misses {sorted(discovered - covered)}"


@pytest.mark.parametrize("compose_path", _compose_files(), ids=lambda path: path.name)
def test_egress_topology_contract(compose_path: pathlib.Path) -> None:
    """Each discovered layer independently obeys the default-deny topology."""
    import yaml

    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = document.get("services", {})
    networks = document.get("networks", {})
    assert services, f"{compose_path.name}: no services declared"
    assert set(services) <= _TOPOLOGY_SERVICES, f"{compose_path.name}: unknown service declared"
    assert set(networks) <= _TOPOLOGY_NETWORKS, f"{compose_path.name}: unknown network declared"

    for name, definition in services.items():
        declared = definition.get("networks")
        if name == "crawlutron" and declared == []:
            continue  # the dev layer detaches the app for standalone use
        if declared:
            assert set(declared) == _ROLE_NETWORKS[name], (
                f"{compose_path.name}: {name} on {declared}, not {sorted(_ROLE_NETWORKS[name])}"
            )
        if name in {"crawlutron", "origin"}:
            assert "ports" not in definition, f"{compose_path.name}: {name} must not publish ports"
        env = definition.get("environment", {})
        if name == "crawlutron" and env.get("CRAWLUTRON_EGRESS_PROXY_URL"):
            assert env["CRAWLUTRON_EGRESS_PROXY_URL"] == "http://egress-proxy:8888"
        if name == "api-ingress" and "ports" in definition:
            assert definition["ports"] == ["127.0.0.1:${COMPOSE_HOST_PORT-8765}:8765"]

    for name, definition in networks.items():
        if "internal" in definition:
            assert (definition["internal"] is True) is (name == "internal"), (
                f"{compose_path.name}: {name} carries the wrong internal flag"
            )
