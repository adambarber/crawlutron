"""Run allowlisted review probes and write structured JSON artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from scripts import executables as _executables
    from scripts import review_registry as _registry
    from scripts import review_support as _support
else:
    if __package__:
        from scripts import executables as _executables
        from scripts import review_registry as _registry
        from scripts import review_support as _support
    else:
        import executables as _executables
        import review_registry as _registry
        import review_support as _support

from crawlutron.review.probes import ProbeArtifact
from crawlutron.review.support import ProbeResultRecord, probe_exit_code

SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REPOSITORY_SEGMENT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]+")


def is_valid_repository(repository: str) -> bool:
    """Accept only owner/repo names with safe, single-level segments."""
    parts = repository.split("/")
    if len(parts) != 2:
        return False
    for part in parts:
        if not part or not _REPOSITORY_SEGMENT.fullmatch(part):
            return False
        if part in {".", ".."} or part.startswith(".") or part.endswith("."):
            return False
    return True


INVARIANT_TESTS: Final[tuple[str, ...]] = (
    "tests/invariants/test_no_inline_suppressions.py",
    "tests/invariants/test_file_size.py",
    "tests/invariants/test_comment_length.py",
    "tests/invariants/test_scan_scope_completeness.py",
)
_GIT: Final[str] = _executables.require_executable("git")
_PROBE_TIMEOUT_SECONDS: Final[int] = 120


def _sha256(path: Path) -> str:
    """Compute a file's SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path, output_dir: Path) -> str:
    """Render an output path relative to its artifact directory."""
    try:
        return str(path.relative_to(output_dir))
    except ValueError:
        return path.name


def _artifact_paths(output_path: Path) -> tuple[Path, Path]:
    """Create and return the stdout and stderr paths for an artifact."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path.with_suffix(".stdout.txt"), output_path.with_suffix(".stderr.txt")


def _result_record(
    name: str,
    argv: tuple[str, ...],
    stdout_path: Path,
    stderr_path: Path,
    started: float,
    exit_code: int,
    output_dir: Path,
) -> ProbeResultRecord:
    """Build one result record with output hashes."""
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "name": name,
        "argv": list(argv),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_path": _display_path(stdout_path, output_dir),
        "stderr_path": _display_path(stderr_path, output_dir),
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
    }


def _run_command(
    name: str,
    argv: tuple[str, ...],
    stdout_path: Path,
    stderr_path: Path,
) -> ProbeResultRecord:
    """Run a fixed argument vector without a shell."""
    started = time.perf_counter()
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_file,
        stderr_path.open("w", encoding="utf-8") as stderr_file,
    ):
        try:
            completed = subprocess.run(
                argv,
                cwd=_support.REPO_ROOT,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            stderr_file.write(f"probe timed out after {_PROBE_TIMEOUT_SECONDS} seconds\n")
            exit_code = 124
    return _result_record(name, argv, stdout_path, stderr_path, started, exit_code, stdout_path.parent)


def _changed_files(base_sha: str, head_sha: str) -> list[str]:
    """List files changed between two commits."""
    completed = subprocess.run(
        (_GIT, "diff", "--name-only", "-z", base_sha, head_sha, "--"),
        cwd=_support.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not list changed files: {completed.stderr.strip()}")
    return [path for path in completed.stdout.split("\0") if path]


def probe_structural_diff(base_sha: str, head_sha: str, output_path: Path) -> ProbeArtifact:
    """Collect the complete patch and changed-file list for a pull request."""
    if not SHA_PATTERN.fullmatch(base_sha) or not SHA_PATTERN.fullmatch(head_sha):
        raise ValueError("base and head must be full commit SHAs")
    stdout_path, stderr_path = _artifact_paths(output_path)
    argv = (_GIT, "diff", "--no-ext-diff", "--no-color", base_sha, head_sha, "--")
    result = _run_command("structural-diff", argv, stdout_path, stderr_path)
    succeeded = probe_exit_code(result) == 0
    changed = _changed_files(base_sha, head_sha) if succeeded else []
    diff = stdout_path.read_text(encoding="utf-8") if succeeded else ""
    data: dict[str, object] = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": changed,
        "diff": diff,
    }
    artifact = ProbeArtifact(name="structural-diff", result=result, data=data)
    _write_artifact(output_path, artifact)
    return artifact


def probe_ci_gates(run_id: str, output_path: Path) -> ProbeArtifact:
    """Collect job conclusions from the triggering CI workflow run."""
    if not run_id.isdigit():
        raise ValueError("run id must be numeric")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not is_valid_repository(repository):
        raise ValueError("GITHUB_REPOSITORY must name an owner and repository")
    stdout_path, stderr_path = _artifact_paths(output_path)
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
    started = time.perf_counter()
    status, body, transport_error = _support.http_get_text(
        url,
        {"Accept": "application/vnd.github+json"},
        30.0,
    )
    stdout_path.write_text(body, encoding="utf-8")
    stderr_path.write_text(transport_error or (body if status != 200 else ""), encoding="utf-8")
    result = _result_record(
        "ci-gates",
        ("GET", url),
        stdout_path,
        stderr_path,
        started,
        0 if status == 200 else 1,
        stdout_path.parent,
    )
    gates: dict[str, object] = {}
    if status == 200:
        payload = _support.load_json_object(stdout_path, "GitHub jobs response")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise RuntimeError("GitHub jobs response does not contain a jobs list")
        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                raise RuntimeError("GitHub jobs response contains a non-object job")
            name = raw_job.get("name")
            if not isinstance(name, str):
                raise RuntimeError("GitHub jobs response contains an unnamed job")
            gates[name] = {
                "conclusion": raw_job.get("conclusion"),
                "status": raw_job.get("status"),
            }
    data: dict[str, object] = {"run_id": run_id, "gates": gates}
    artifact = ProbeArtifact(name="ci-gates", result=result, data=data)
    _write_artifact(output_path, artifact)
    return artifact


def probe_invariants(output_path: Path) -> ProbeArtifact:
    """Run the bounded invariant subset used by the review agent."""
    stdout_path, stderr_path = _artifact_paths(output_path)
    argv = (sys.executable, "-m", "pytest", *INVARIANT_TESTS, "-q", "--tb=line", "-x")
    result = _run_command("invariants", argv, stdout_path, stderr_path)
    data: dict[str, object] = {
        "passed": probe_exit_code(result) == 0,
        "tests": list(INVARIANT_TESTS),
    }
    artifact = ProbeArtifact(name="invariants", result=result, data=data)
    _write_artifact(output_path, artifact)
    return artifact


def _write_artifact(path: Path, artifact: ProbeArtifact) -> None:
    """Write one probe artifact as strict JSON."""
    payload = {
        "name": artifact.name,
        "result": artifact.result,
        "data": artifact.data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _run_probe(
    probe: _registry.ProbeDefinition,
    output_path: Path,
    base_sha: str,
    head_sha: str,
    run_id: str,
) -> ProbeArtifact:
    """Run one registered probe command."""
    if probe.command == "structural-diff":
        return probe_structural_diff(base_sha, head_sha, output_path)
    if probe.command == "ci-gates":
        return probe_ci_gates(run_id, output_path)
    if probe.command == "invariants":
        return probe_invariants(output_path)
    raise ValueError(f"unsupported probe command {probe.command!r}")


def _changed_paths(artifact: ProbeArtifact) -> tuple[str, ...]:
    """Read changed paths from a structural-diff artifact."""
    value = artifact.data.get("changed_files")
    if not isinstance(value, list):
        return ()
    return tuple(path for path in value if isinstance(path, str))


def run_registered_probes(
    registry: _registry.ReviewRegistry,
    output_dir: Path,
    base_sha: str,
    head_sha: str,
    run_id: str,
) -> list[ProbeArtifact]:
    """Run the routed agent's registered probe allowlist."""
    output_dir.mkdir(parents=True, exist_ok=True)
    structural = probe_structural_diff(base_sha, head_sha, output_dir / "structural-diff.json")
    agent = _registry.select_agent(registry, _changed_paths(structural))
    artifacts = [structural]
    for probe_name in agent.probes:
        if probe_name == "structural-diff":
            continue
        probe = registry.probes[probe_name]
        artifact = _run_probe(probe, output_dir / f"{probe_name}.json", base_sha, head_sha, run_id)
        artifacts.append(artifact)
    return artifacts


def _build_parser() -> argparse.ArgumentParser:
    """Build the probe command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="probe", required=True)
    run_parser = commands.add_parser("run", help="Run registered probes")
    run_parser.add_argument(
        "--registry", type=Path, default=_support.REPO_ROOT / ".github/review-agents.toml"
    )
    run_parser.add_argument("--output-dir", type=Path, default=_support.REPO_ROOT / "probes")
    run_parser.add_argument("--base", required=True)
    run_parser.add_argument("--head", required=True)
    run_parser.add_argument("--run-id", required=True)
    diff_parser = commands.add_parser("structural-diff", help="Collect a structural diff")
    diff_parser.add_argument("--base", required=True)
    diff_parser.add_argument("--head", required=True)
    diff_parser.add_argument("--output", type=Path, required=True)
    gates_parser = commands.add_parser("ci-gates", help="Collect CI gate results")
    gates_parser.add_argument("--run-id", required=True)
    gates_parser.add_argument("--output", type=Path, required=True)
    invariant_parser = commands.add_parser("invariants", help="Run invariant tests")
    invariant_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested probe command."""
    args = _build_parser().parse_args(argv)
    try:
        if args.probe == "run":
            registry = _registry.load_registry(args.registry)
            artifacts = run_registered_probes(
                registry,
                args.output_dir,
                args.base,
                args.head,
                args.run_id,
            )
        elif args.probe == "structural-diff":
            artifacts = [probe_structural_diff(args.base, args.head, args.output)]
        elif args.probe == "ci-gates":
            artifacts = [probe_ci_gates(args.run_id, args.output)]
        else:
            artifacts = [probe_invariants(args.output)]
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    failed = [artifact.name for artifact in artifacts if probe_exit_code(artifact.result) != 0]
    if failed:
        print(f"ERROR: probes failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"probes completed: {', '.join(artifact.name for artifact in artifacts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
