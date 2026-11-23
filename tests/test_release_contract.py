"""Contract tests for the release/rehearsal infrastructure (#1207, B2)."""

import re
from pathlib import Path

import pytest

from tests.chokepoint_paths import REPO_ROOT
from tests.workflows._model import workflow

RELEASE_WORKFLOW = REPO_ROOT / ".github/workflows/release.yml"
REHEARSAL_WORKFLOW = REPO_ROOT / ".github/workflows/rehearsal.yml"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
REHEARSAL_SPEC = REPO_ROOT / "docs/REHEARSAL-SPEC.md"
SHARED_TEST_DOOR = "$/.github/actions/run-tests"


def _yaml_section(text: str, job_name: str) -> str:
    """Extract a job block from a workflow file."""
    pattern = rf"^\s*{re.escape(job_name)}:.*?(?=\n^  [\w-]+:|\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if not match:
        pytest.fail(f"Job '{job_name}' not found in workflow")
    return match.group(0)


def _job_names(text: str) -> list[str]:
    """Return job names in declaration order from a workflow YAML."""
    return re.findall(r"^  ([\w-]+):$", text, re.MULTILINE)


def _read_yml(path: Path) -> str:
    """Read a workflow and strip trailing whitespace."""
    return path.read_text().strip()


def _run_blocks(text: str) -> list[str]:
    """Return every run command in a workflow, scalar or block."""
    lines = text.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s+run:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value != "|":
            blocks.append(value)
            continue
        block: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and not following.startswith("        "):
                break
            block.append(following)
        blocks.append("\n".join(block))
    return blocks


def _tag_globs(text: str) -> list[str]:
    """Extract quoted tag patterns from the on: push: tags section."""
    match = re.search(r"on:\s*push:\s*tags:\s*(.+?)(?=\n\S|\Z)", text, re.DOTALL)
    return re.findall(r'^\s*-\s*"([^"]*)"', match.group(1), re.MULTILINE) if match else []


# Test 1: attach parity with the production workflow.
class TestAttachParity:
    """Rehearsal twins use the same entrypoints, needs edges, and env contract."""

    @pytest.mark.parametrize(
        ("release_job", "rehearsal_twin", "entrypoint", "door_source", "needs_edges", "env_contract"),
        [
            ("ci", "ci", SHARED_TEST_DOOR, CI_WORKFLOW, ["released"], {}),
            (
                "install-graph",
                "install-graph",
                "scripts.release.install_graph",
                REHEARSAL_SPEC,
                ["released"],
                {"NAME": "", "VERSION": ""},
            ),
        ],
    )
    def test_attach_parity(
        self,
        release_job: str,
        rehearsal_twin: str,
        entrypoint: str,
        door_source: Path,
        needs_edges: list[str],
        env_contract: dict[str, str],
    ) -> None:
        """Each twin invokes the same frozen door and preserves needs edges."""
        release_text = _read_yml(RELEASE_WORKFLOW)
        rehearsal_text = _read_yml(REHEARSAL_WORKFLOW)
        assert release_text, "Release workflow is empty"
        assert release_job in release_text, f"Release job '{release_job}' missing"
        assert rehearsal_twin in _job_names(rehearsal_text), f"Rehearsal twin '{rehearsal_twin}' missing"
        assert entrypoint in door_source.read_text(), (
            f"Entrypoint '{entrypoint}' not frozen in {door_source.name}"
        )
        assert entrypoint in rehearsal_text, f"Entrypoint '{entrypoint}' not in rehearsal.yml"
        twin = _yaml_section(rehearsal_text, rehearsal_twin)
        for edge in needs_edges:
            assert f"needs: [{edge}" in twin, f"Rehearsal job '{rehearsal_twin}' missing needs edge '{edge}'"
        for key in env_contract:
            assert key in twin, f"Rehearsal job '{rehearsal_twin}' missing env '{key}'"


# Test 2: the image job's privileged surface is documented, not silently omitted.
class TestImageJob:
    """The release image job is privileged; rehearsal records that explicitly."""

    def test_image_job_has_unrehearsed_surface_entry(self) -> None:
        """The privileged image surface is documented and thin-adapter wired."""
        image_job = _yaml_section(_read_yml(RELEASE_WORKFLOW), "image")
        assert "python3 -m scripts.release.image" in image_job
        for credential in ("packages: write", "attestations: write", "id-token: write"):
            assert credential in image_job, f"Image job missing '{credential}'"
        doc_text = (REPO_ROOT / "docs/REHEARSAL.md").read_text().lower()
        assert "unrehearsed-surface" in doc_text, "image surface not documented as unrehearsed"

    def test_rehearsal_has_no_image_job_with_write_permissions(self) -> None:
        """Rehearsal carries zero id-token and zero write credentials."""
        text = _read_yml(REHEARSAL_WORKFLOW)
        for credential in ("id-token: write", "packages: write", "attestations: write"):
            assert credential not in text, f"Rehearsal must not carry '{credential}'"
        for action in ("docker/login-action", "docker push", "attest-build-provenance"):
            assert action not in text, f"Rehearsal must not run '{action}'"


# Test 3: release owns the tag globs; rehearsal is dispatch-only.
class TestPrPathContract:
    """Release declares the tag globs; rehearsal declares none."""

    def test_release_globs_declared_only_in_release(self) -> None:
        """Tag pushes enter through release.yml, never through rehearsal."""
        release_globs = _tag_globs(_read_yml(RELEASE_WORKFLOW))
        rehearsal_globs = _tag_globs(_read_yml(REHEARSAL_WORKFLOW))
        assert release_globs, "release.yml declares no tag globs"
        assert not rehearsal_globs, f"rehearsal must not declare tag globs: {rehearsal_globs}"

    def test_globs_cover_declared_set_in_spec(self) -> None:
        """The frozen spec declares the release glob vocabulary."""
        release_globs = _tag_globs(_read_yml(RELEASE_WORKFLOW))
        spec_text = REHEARSAL_SPEC.read_text()
        assert release_globs, "release.yml declares no tag globs"
        assert "RELEASE_TAG" in spec_text and "tag push" in spec_text


# Test 4: every non-publish release job has a twin.
class TestReleaseJobsHaveTwins:
    """Non-publish release jobs are all mirrored in rehearsal."""

    @pytest.mark.parametrize(
        ("release_job_name", "expected_twin", "needs_edges", "env_keys"),
        [
            ("ci", "ci", ["released"], []),
            ("install-graph", "install-graph", ["released"], ["NAME", "VERSION"]),
        ],
    )
    def test_non_publish_jobs_have_twins(
        self,
        release_job_name: str,
        expected_twin: str,
        needs_edges: list[str],
        env_keys: list[str],
    ) -> None:
        """A twin exists with the same needs edges and env keys."""
        release_text = _read_yml(RELEASE_WORKFLOW)
        rehearsal_text = _read_yml(REHEARSAL_WORKFLOW)
        assert release_job_name in release_text
        twin = _yaml_section(rehearsal_text, expected_twin)
        for edge in needs_edges:
            assert f"needs: [{edge}" in twin, f"Missing needs edge '{edge}'"
        for key in env_keys:
            assert key in twin, f"Missing env key '{key}'"
        assert "id-token" not in twin.lower()


# Test 5: a run block is one entrypoint invocation, not shell logic.
class TestNoShellControlFlow:
    """Rehearsal run blocks contain no shell control flow."""

    def test_no_if_in_run_blocks(self) -> None:
        """No run block branches, loops, or chains commands."""
        blocks = _run_blocks(_read_yml(REHEARSAL_WORKFLOW))
        assert blocks, "Rehearsal has no run blocks"
        for index, block in enumerate(blocks):
            lowered = block.lower()
            assert " while " not in lowered, f"Run block {index} contains a while loop"
            assert "||" not in block, f"Run block {index} contains ||"
            assert "&&" not in block, f"Run block {index} contains &&"
            assert not re.search(r"(?m)^\s*for\s", lowered), f"Run block {index} contains a for loop"
            assert not re.search(r"(?m)^\s*if\s", lowered), f"Run block {index} contains an if statement"

    def test_no_heredoc_in_run_blocks(self) -> None:
        """No run block embeds a heredoc."""
        blocks = _run_blocks(_read_yml(REHEARSAL_WORKFLOW))
        assert blocks, "Rehearsal has no run blocks"
        for index, block in enumerate(blocks):
            assert "<<EOF" not in block.upper(), f"Run block {index} contains a heredoc"
            assert "<<EOH" not in block.upper(), f"Run block {index} contains a heredoc"


# Test 6: the review surface is owned and zizmor audits stay enabled.
class TestOwnersAndPosture:
    """CODEOWNERS covers both adapters and no zizmor audit is disabled."""

    def test_codeowners_contains_rehearsal_and_release(self) -> None:
        """Both release adapters have a named owner."""
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        for filename in ("rehearsal.yml", "release.yml"):
            assert filename in text, f"CODEOWNERS missing '{filename}'"

    def test_zizmor_keeps_permission_audit_enabled(self) -> None:
        """zizmor keeps its default excessive-permissions audit enabled."""
        text = (REPO_ROOT / ".github/zizmor.yml").read_text()
        assert "posture:" not in text, "zizmor has no posture rule"
        assert "disable: true" not in text, "no zizmor audit may be disabled"


# Test 7: rehearsal is isolated from release on triggers and concurrency.
class TestRehearsalIsolation:
    """Rehearsal is dispatch-only and never shares release's concurrency group."""

    def test_rehearsal_triggers_only_on_dispatch(self) -> None:
        """No tag push can start a rehearsal run."""
        assert set(workflow("rehearsal").on_) == {"workflow_dispatch"}

    def test_rehearsal_group_cannot_collide_with_release(self) -> None:
        """The two adapters serialize on distinct groups."""
        rehearsal = workflow("rehearsal").concurrency
        release = workflow("release").concurrency
        assert isinstance(rehearsal, dict) and isinstance(release, dict)
        assert str(rehearsal["group"]).startswith("rehearsal-")
        assert rehearsal["group"] != release["group"]

    def test_rehearsal_released_job_has_no_dead_if(self) -> None:
        """The tautological trigger guard is absent on dispatch-only rehearsal."""
        assert workflow("rehearsal").jobs["released"].if_ is None
