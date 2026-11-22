"""Start every gate child at once, wait per-hook; wall = slowest, not sum."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from tests._walk_primitives import SCAN_ROOTS
from utils.paths import REPO_ROOT

SERVICE_ROOT = REPO_ROOT
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
STATUS_ROOT = SERVICE_ROOT / ".gate_fanout"
ENGINE_MODULE = "scripts.gate_fanout"

#: Hook scan-scope marker: {{SCAN_ROOTS}} expands to the derived surface.
#: A hook never names a path literal -- a move/landing scans by construction.
SCAN_ROOTS_MARKER = "{{SCAN_ROOTS}}"

#: Seconds a gate child may hold the run before its session is killed.
#: CI overrides via GATE_FANOUT_WALL_SECONDS; default 60s guards commit-admission.
WALL_SECONDS = float(os.environ.get("GATE_FANOUT_WALL_SECONDS", "60"))
#: Any status artifact this old is a dead run's residue: discard it.
STALE_SECONDS = 600.0


def _python() -> str:
    return str(SERVICE_ROOT / ".venv" / "bin" / "python")


def _expand_scan_roots(argv: list[str]) -> list[str]:
    """Replace {{SCAN_ROOTS}} with every non-cached python root, in place order.

    A hook scans the derived surface, never a hand-listed tuple; a package that
    moves or lands under packages/ is picked up without an entry edit."""
    if SCAN_ROOTS_MARKER not in argv:
        return argv
    expanded: list[str] = []
    for token in argv:
        if token != SCAN_ROOTS_MARKER:
            expanded.append(token)
        else:
            expanded.extend(SCAN_ROOTS)
    return expanded


def _gate_argv() -> dict[str, list[str]]:
    """Every pre-commit-stage gate as {hook id: command}, from the live config."""
    document = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    gates: dict[str, list[str]] = {}
    for repo in document["repos"]:
        for hook in repo.get("hooks", ()):
            entry = hook.get("entry", "")
            if "pre-commit" in (hook.get("stages") or []) and ENGINE_MODULE in entry:
                # shlex, not split: the entry after `--` carries shell quoting
                # (addopts=""), which pre-commit itself would consume.
                argv = shlex.split(entry.split(" -- ", 1)[1])
                gates[hook["id"]] = _expand_scan_roots(argv)
    if not gates:
        raise ValueError(f"{CONFIG_PATH.name} declares no {ENGINE_MODULE} gates")
    return gates


def _config_digest() -> str:
    """Digest of the gate config so a config change can't reuse an old verdict."""
    return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()[:16]


def _git_capture(args: list[str], env: dict[str, str] | None = None) -> str:
    """Run a read-only git query and return its stdout."""
    git_bin = shutil.which("git")
    if git_bin is None:
        raise RuntimeError("git not found on PATH: cannot read the gate input tree")
    result = subprocess.run(
        [git_bin, *args],
        env=env,
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _staged_tree_digest(index_file: str) -> str:
    """Digest of the staged tree: blob SHAs, modes, paths."""
    out = _git_capture(["ls-files", "--stage"], env={**os.environ, "GIT_INDEX_FILE": index_file})
    return hashlib.sha256(out.encode()).hexdigest()


def _worktree_digest() -> str:
    """Digest of the worktree gates read with --all-files (no staged index)."""
    parts = (
        _git_capture(["ls-files", "--stage"]),
        _git_capture(["diff", "--no-ext-diff", "--binary", "HEAD"]),
        _git_capture(["status", "--porcelain"]),
    )
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def _run_key(index_file: str | None = None) -> str:
    """Bind verdict identity to gated content, not ref env vars."""
    config_digest = _config_digest()
    index_file = index_file or os.environ.get("PRE_COMMIT_INDEX_FILE")
    if index_file and Path(index_file).is_file():
        staged = _staged_tree_digest(index_file)
        return hashlib.sha256(f"{staged}:{config_digest}".encode()).hexdigest()[:16]
    return hashlib.sha256(f"adhoc:{_worktree_digest()}:{config_digest}".encode()).hexdigest()[:16]


def _status_dir() -> Path:
    path = STATUS_ROOT / _run_key()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _purge(status_dir: Path, entry_start: float) -> None:
    ceiling = min(entry_start - 2.0, time.time() - STALE_SECONDS)
    for path in status_dir.iterdir():
        if path.stat().st_mtime < ceiling:
            path.unlink()


def _spawn(hook_id: str, argv: list[str], status_dir: Path) -> None:
    pidfile = status_dir / f"{hook_id}.pid"
    with (status_dir / f"{hook_id}.log").open("w") as out, pidfile.open("w") as pid_out:
        child = subprocess.Popen(
            [_python(), "-m", ENGINE_MODULE, "__child__", str(status_dir / f"{hook_id}.rc"), *argv],
            cwd=SERVICE_ROOT,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
        pid_out.write(str(child.pid))


def _child_is_running(pidfile: Path) -> bool:
    """True when a live child of ours still owns this pidfile.

    Past the wall it is a hung child (killed, then respawned by the caller); a dead or
    foreign group is dropped, never signalled, because a reused pid is someone else's.
    """
    try:
        pid = int(pidfile.read_text())
    except ValueError:
        return False
    if not _alive(pid):
        return False
    if time.time() - pidfile.stat().st_mtime <= WALL_SECONDS:
        return True
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, 9)
    return False


def _dispatch(status_dir: Path) -> None:
    for hook_id, argv in _gate_argv().items():
        if (status_dir / f"{hook_id}.rc").is_file():
            continue
        pidfile = status_dir / f"{hook_id}.pid"
        if pidfile.is_file() and _child_is_running(pidfile):
            continue
        pidfile.unlink(missing_ok=True)
        _spawn(hook_id, argv, status_dir)


def _wait_own(hook_id: str, status_dir: Path) -> int:
    rc = status_dir / f"{hook_id}.rc"
    pidfile = status_dir / f"{hook_id}.pid"
    deadline = time.monotonic() + WALL_SECONDS
    while time.monotonic() < deadline:
        if rc.is_file():
            # A torn read (child mid-write) surfaces as an empty file; a file
            # present but empty is not a verdict — keep polling until bytes land.
            text = rc.read_text()
            if text.strip():
                return int(text)
        if pidfile.is_file():
            pid_text = pidfile.read_text().strip()
            if pid_text and not _alive(int(pid_text)):
                break
        time.sleep(0.05)
    if pidfile.is_file():
        pid_text = pidfile.read_text().strip()
        if pid_text:
            pid = int(pid_text)
            if _alive(pid):
                os.killpg(pid, 9)
        pidfile.unlink(missing_ok=True)
    print(f"gate fanout: {hook_id} died or hit the wall clock; session killed", file=sys.stderr)
    return 1


def _cleanup(status_dir: Path) -> None:
    gates = _gate_argv()
    if all((status_dir / f"{hook_id}.rc").is_file() for hook_id in gates):
        for hook_id in gates:
            (status_dir / f"{hook_id}.rc").unlink(missing_ok=True)
            (status_dir / f"{hook_id}.pid").unlink(missing_ok=True)


def _run_gate(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one gate command; the only subprocess door in the child path."""
    return subprocess.run(argv, cwd=SERVICE_ROOT, capture_output=True, text=True, check=False)


def _child(rc_path: str, argv: list[str]) -> int:
    completed = _run_gate(argv)
    attempts = [completed]
    if completed.returncode in (-6, 134) and "failed" not in completed.stdout:
        # A gate child can SIGABRT on teardown after a passing report under
        # saturation; retry once after the burst drains, fail-loud on repeat.
        time.sleep(3)
        attempts.append(_run_gate(argv))
    final = attempts[-1]
    tmp = Path(f"{rc_path}.tmp")
    tmp.write_text(str(final.returncode))
    tmp.replace(rc_path)
    print(*[attempt.stdout for attempt in attempts], sep="")
    print(*[attempt.stderr for attempt in attempts], sep="", file=sys.stderr)
    return final.returncode


def main() -> int:
    if "__child__" in sys.argv:
        rest = sys.argv[sys.argv.index("__child__") + 1 :]
        return _child(rest[0], rest[1:])
    hook_id = sys.argv[1]
    status_dir = _status_dir()
    entry_start = time.time()
    _purge(status_dir, entry_start)
    _dispatch(status_dir)
    code = _wait_own(hook_id, status_dir)
    if code != 0:
        log = status_dir / f"{hook_id}.log"
        if log.is_file():
            sys.stderr.write("".join(log.read_text().splitlines(keepends=True)[-80:]))
    _cleanup(status_dir)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
