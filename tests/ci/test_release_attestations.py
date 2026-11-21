"""The release attestation surface: PyPI provenance, image SBOM, boundaries.

Attestations are capability additions at the publish boundary ONLY; build
jobs stay OIDC-free and the publish job stays a logic-free adapter.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import scripts.pin_verify as pv
from tests._ci_contract import RELEASE_WORKFLOW_PATH

SERVICE_ROOT = Path(__file__).resolve().parents[2]
IMAGE_SCRIPT = SERVICE_ROOT / "scripts" / "release" / "image.py"
SUPPORTED_ACTIONS = ("actions/attest-build-provenance", "actions/attest-sbom")


def _workflow() -> dict[str, object]:
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "release.yml is not a workflow"
    return document


def _jobs() -> dict[str, dict[str, object]]:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), "release.yml declares no jobs"
    return {str(name): job for name, job in jobs.items() if isinstance(job, dict)}


def _steps_of(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []  # a reusable `uses:` job declares no steps
    return [step for step in steps if isinstance(step, dict)]


def _permission(job: dict[str, object], scope: str) -> str | None:
    permissions = job.get("permissions")
    if not isinstance(permissions, dict):
        return None
    value = permissions.get(scope)
    return str(value) if value is not None else None


def _oidc_jobs() -> set[str]:
    """Job names granted id-token: write, the credential attestations sign with."""
    return {name for name, job in _jobs().items() if _permission(job, "id-token") == "write"}


def _attestation_step_sites() -> dict[str, list[str]]:
    """Every attestation action as (job name, uses ref) pairs, grouped by job."""
    sites: dict[str, list[str]] = {}
    for name, job in _jobs().items():
        uses = [
            str(step.get("uses"))
            for step in _steps_of(job)
            if any(marker in str(step.get("uses", "")) for marker in SUPPORTED_ACTIONS)
        ]
        if uses:
            sites[name] = uses
    return sites


class TestPublishBoundaryAttestations:
    """PEP 740 attestations ride the publish job, never a build job."""

    def test_pypi_publish_declares_attestations_true(self) -> None:
        """The pypi-publish step opts into PEP 740 attestations explicitly."""
        publish = _jobs().get("publish")
        assert publish is not None, "the publish job is missing"
        publish_steps = [
            step for step in _steps_of(publish) if "pypa/gh-action-pypi-publish" in str(step.get("uses", ""))
        ]
        assert publish_steps, "the publish job must run pypi-publish"
        for step in publish_steps:
            with_block = step.get("with")
            assert isinstance(with_block, dict), "pypi-publish must declare its with block"
            assert with_block.get("attestations") is True, (
                "pypi-publish must set attestations: true (PEP 740 provenance)"
            )

    def test_attest_build_provenance_lives_only_in_the_publish_job(self) -> None:
        """The dist-provenance step is in publish, with subject-path dist/*."""
        sites = _attestation_step_sites()
        holders = {name for name, uses in sites.items() if any("attest-build-provenance" in u for u in uses)}
        assert holders == {"publish"}, (
            f"attest-build-provenance must run only in the publish job, found {sorted(holders)}"
        )
        publish = _jobs()["publish"]
        steps = [
            step for step in _steps_of(publish) if "attest-build-provenance" in str(step.get("uses", ""))
        ]
        assert len(steps) == 1, "one provenance step per publish job"
        with_block = steps[0].get("with")
        assert isinstance(with_block, dict), "attest-build-provenance must declare its inputs"
        assert with_block.get("subject-path") == "dist/*", (
            "the provenance subjects are the verified distributions under dist/"
        )

    def test_publish_job_holds_the_attestations_write_scope(self) -> None:
        """attestations: write is granted only where an attestation step signs."""
        attesters = set(_attestation_step_sites())
        for name, job in _jobs().items():
            grant = _permission(job, "attestations")
            if name in attesters:
                assert grant == "write", f"{name} attests, so it needs attestations: write"
            else:
                assert grant != "write", f"{name} must not hold attestations: write"

    def test_the_oidc_boundary_is_the_publish_boundary(self) -> None:
        """A job able to attest must be a job holding the OIDC credential."""
        holders = set(_attestation_step_sites())
        assert holders, "the release must attest something"
        oidc = _oidc_jobs()
        strays = holders - oidc
        assert not strays, f"attestation steps outside the OIDC boundary: {sorted(strays)}"

    def test_build_jobs_stay_oidc_free(self) -> None:
        """A build backend executes arbitrary code; it never holds id-token."""
        for name in ("publish-build", "released"):
            job = _jobs().get(name)
            assert job is not None, f"the {name} job is missing"
            assert _permission(job, "id-token") != "write", (
                f"{name} must stay OIDC-free; attestation steps never move into it"
            )


class TestImageSbomSurface:
    """The rehearsable image job generates the SBOM pre-push and attests it."""

    def test_image_script_generates_sbom_before_push(self) -> None:
        """syft runs in the image job's script before the registry sees the image."""
        script = IMAGE_SCRIPT.read_text(encoding="utf-8")
        syft_idx = script.find("syft")
        assert syft_idx != -1, "the image script must generate a syft SBOM"
        smoke_idx = script.find("_smoke_image(")
        push_idx = script.find("_push_image(")
        assert smoke_idx != -1 and push_idx != -1, "the smoke/push order pins vanished"
        syft_idx = script.find("_generate_sbom(")
        assert smoke_idx < syft_idx < push_idx, "the SBOM must be generated between smoke and push"

    def test_image_script_exports_sbom_for_the_workflow(self) -> None:
        """The SBOM lands on disk where the attest-sbom step reads it."""
        script = IMAGE_SCRIPT.read_text(encoding="utf-8")
        assert "GITHUB_OUTPUT" in script, "the SBOM path must reach the workflow"
        assert "sbom" in script.lower(), "the export must name the SBOM artifact"

    def test_attest_sbom_publishes_after_smoke_and_push(self) -> None:
        """attest-sbom is a push-to-registry step, sequenced by job needs."""
        sites = _attestation_step_sites()
        holders = {name for name, uses in sites.items() if any("attest-sbom" in u for u in uses)}
        assert holders == {"image"}, f"attest-sbom must run only in the image job, found {sorted(holders)}"
        image = _jobs()["image"]
        needs = image.get("needs")
        assert isinstance(needs, list) and "publish" in {str(item) for item in needs}, (
            "the image job already runs after publish; keep that ordering"
        )
        steps = [step for step in _steps_of(image) if "attest-sbom" in str(step.get("uses", ""))]
        assert len(steps) == 1, "one SBOM attestation step per image job"
        with_block = steps[0].get("with")
        assert isinstance(with_block, dict), "attest-sbom must declare its inputs"
        assert with_block.get("push-to-registry") is True, (
            "the SBOM attestation publishes to the registry beside the image"
        )
        assert with_block.get("subject-name"), "the attested subject must be the pushed image name"
        assert with_block.get("subject-digest"), "the attested subject must be the pushed digest"

    def test_sbom_generation_is_rehearsable(self) -> None:
        """syft runs through the rehearsal entrypoint, not a workflow-only path."""
        script = IMAGE_SCRIPT.read_text(encoding="utf-8")
        assert "GITHUB_OUTPUT" in script and "syft" in script, "the SBOM flow must be scripted"


class TestSupplyChainPosture:
    """Every new action is registered, peeled, owned, and zizmor-clean."""

    def test_every_new_attestation_action_is_registered(self) -> None:
        """pin-verify refuses unregistered dependencies; the registry carries ours."""
        registry = pv.load_registry()
        for action in SUPPORTED_ACTIONS:
            pin = registry.get(action)
            assert pin is not None, f"{action} is missing from .github/versions.toml"
            assert pv.SHA.fullmatch(pv.pin_sha(pin)), f"{action} sha is not a full commit"

    def test_every_workflow_pin_matches_the_registry(self) -> None:
        """The workflow pins and the registry agree (offline check)."""
        assert not pv.verify_pins(), "registry drift"

    def test_new_actions_are_sha_pinned_in_the_workflow(self) -> None:
        """The workflow's new uses: lines carry full-length commit SHAs."""
        for name, job in _jobs().items():
            for step in _steps_of(job):
                uses = str(step.get("uses", ""))
                if any(marker in uses for marker in SUPPORTED_ACTIONS):
                    ref = uses.rsplit("@", 1)[1].split(" ")[0]
                    assert pv.SHA.fullmatch(ref), f"{name}: {uses} is not SHA-pinned"

    def test_codeowners_covers_the_surface(self) -> None:
        """The release-owner boundary still names every touched file."""
        owners = (SERVICE_ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        for path in (".github/workflows/release.yml", ".github/versions.toml"):
            assert path in owners, f"CODEOWNERS missing {path}"
        assert (SERVICE_ROOT / "scripts/release/image.py").exists()
        assert "scripts/release/image.py" in owners, "CODEOWNERS missing the image entrypoint"
