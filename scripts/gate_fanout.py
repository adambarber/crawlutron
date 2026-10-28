"""Start every gate child at once, wait per-hook; wall = slowest, not sum."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"
STATUS_ROOT = SERVICE_ROOT / ".gate_fanout"
ENGINE_MODULE = "scripts.gate_fanout"

#: Seconds a gate child may hold the run before its session is killed.
WALL_SECONDS = 60.0
#: Any status artifact this old is a dead run's residue: discard it.
STALE_SECONDS = 600.0


def _python() -> str:
    return str(SERVICE_ROOT / ".venv" / "bin" / "python")


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
                gates[hook["id"]] = shlex.split(entry.split(" -- ", 1)[1])
    if not gates:
        raise ValueError(f"{CONFIG_PATH.name} declares no {ENGINE_MODULE} gates")
    return gates


def _run_key() -> str:
    pair = f"{os.environ.get('PRE_COMMIT_FROM_REF', '')} {os.environ.get('PRE_COMMIT_TO_REF', '')}"
    if not pair.strip():
        return "adhoc"
    return hashlib.sha256(pair.encode()).hexdigest()[:16]


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


def _dispatch(status_dir: Path) -> None:
    for hook_id, argv in _gate_argv().items():
        if (status_dir / f"{hook_id}.rc").is_file():
            continue
        pidfile = status_dir / f"{hook_id}.pid"
        if pidfile.is_file():
            pid = int(pidfile.read_text())
            if _alive(pid):
                if time.time() - pidfile.stat().st_mtime > WALL_SECONDS:
                    os.killpg(pid, 9)  # a hung child from an earlier run
                else:
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
        if pidfile.is_file() and not _alive(int(pidfile.read_text())):
            break
        time.sleep(0.05)
    if pidfile.is_file():
        pid = int(pidfile.read_text())
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


def _child(rc_path: str, argv: list[str]) -> int:
    completed = subprocess.run(argv, cwd=SERVICE_ROOT, capture_output=True, text=True, check=False)
    attempts = [completed]
    if completed.returncode in (-6, 134) and "failed" not in completed.stdout:
        # A pyright teardown aborts (SIGABRT) after a passing report under
        # saturation; retry once after the burst drains, fail-loud on repeat.
        time.sleep(3)
        attempts.append(subprocess.run(argv, cwd=SERVICE_ROOT, capture_output=True, text=True, check=False))
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
