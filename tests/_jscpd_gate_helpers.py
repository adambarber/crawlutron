"""The duplication gate wrapper fails closed on thin scans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts import _jscpd_pair_resolution as res
from scripts import executables as ex
from scripts import jscpd_gate as gate
from scripts._jscpd_report_types import JscpdDuplicate, JscpdLocation


def stub_resolver_probe(monkeypatch) -> None:
    """Accept the resolver's venv preprobe; these stubs model the scan, not resolution."""
    monkeypatch.setattr(ex, "_probe", lambda candidate: True)


def answer_version_preprobe(command: list[str]) -> object | None:
    """Answer the resolver's ``--version`` preprobe so the scan stub is the only jscpd run."""
    if command[1:] != ["--version"]:
        return None
    return type("Completed", (), {"returncode": 0, "stdout": "jscpd 5.3.2", "stderr": ""})()


def scan_run_stub(monkeypatch, commands: list[list[str]], scan) -> None:
    """subprocess.run that records commands, answers the preprobe, delegates scans to ``scan``."""

    def run(command, **_kwargs):
        commands.append(command)
        answered = answer_version_preprobe(command)
        if answered is not None:
            return answered
        return scan(command)

    monkeypatch.setattr(res.subprocess, "run", run)


def _args(threshold: float = 0) -> argparse.Namespace:
    return _args_for(threshold=threshold)


_CONFIGURED_MIN_LINES, _CONFIGURED_MIN_TOKENS, _CONFIGURED_THRESHOLD = gate.configured_floor()


"""The floor under test, read from the hook rather than restated here.

This file used to spell 4/39 in ``_args_for`` and again in ``_GATE_ARGV``, and
``.pre-commit-config.yaml`` spelled it a third time in the entry and a fourth in
the hook's name. Four copies of a number, with nothing comparing them, is how the
tests kept passing against a floor the hook no longer ran at -- the suite would
have gone on asserting behaviour at 4/39 while every commit was gated at whatever
the config said. ``gate.configured_floor()`` is the one read, so tightening the
hook tightens the tests in the same edit.
"""


def _args_for(
    threshold: float = _CONFIGURED_THRESHOLD,
    *,
    min_lines: int = _CONFIGURED_MIN_LINES,
    min_tokens: int = _CONFIGURED_MIN_TOKENS,
) -> argparse.Namespace:
    return argparse.Namespace(
        profile="all",
        min_lines=min_lines,
        min_tokens=min_tokens,
        threshold=threshold,
    )


def _strip_root(name: str, root: str) -> str:
    """jscpd's name for a file in a multi-root invocation: it strips the root prefix,
    reporting `crawl/recursive.py` for `crawlutron/orchestration/recursive.py` -- a pair
    the gate resolves back against the filesystem, so the stub must reproduce it.
    """
    return name[len(root) + 1 :] if name.startswith(root + "/") else name


def _stripped(location: JscpdLocation, root: str) -> JscpdLocation:
    return {**location, "name": _strip_root(str(location["name"]), root)}


def _intercept_declared_files(monkeypatch, duplicates: list[JscpdDuplicate]) -> None:
    """Pin res._IS_FILE to the declared files plus the real filesystem."""
    # Anything the test did not declare falls through to the real filesystem, which is what keeps an ambiguity
    # like `zz_tree_filler.py` a real one.
    declared = {
        tuple(duplicate[side]["name"].split("/"))
        for duplicate in duplicates
        for side in ("firstFile", "secondFile")
    }

    def _is_file(path: Path) -> bool:
        try:
            relative = tuple(path.relative_to(gate.SERVICE_ROOT).parts)
        except ValueError:
            return False
        return relative in declared or Path.is_file(path)

    monkeypatch.setattr(res, "_IS_FILE", _is_file)


def _scoped_duplicates(duplicates: list[JscpdDuplicate], roots: list[str]) -> list[JscpdDuplicate]:
    """The duplicates the one whole-repo invocation reports, named as jscpd names
    them: handed every root at once, jscpd strips each side's own root from its
    path, leaving a name the gate resolves back against the filesystem.
    """

    def _root_of(name: str) -> str | None:
        return next((root for root in roots if name == root or name.startswith(root + "/")), None)

    named: list[JscpdDuplicate] = []
    for duplicate in duplicates:
        first_root = _root_of(duplicate["firstFile"]["name"])
        second_root = _root_of(duplicate["secondFile"]["name"])
        if first_root is None or second_root is None:
            continue
        named.append(
            {
                **duplicate,
                "firstFile": _stripped(duplicate["firstFile"], first_root),
                "secondFile": _stripped(duplicate["secondFile"], second_root),
            }
        )
    return named


def _sources_found(sources: int | dict[str, int], roots: list[str]) -> int:
    """The sources count one invocation sees, per-root or global."""
    if isinstance(sources, dict):
        # One root per invocation is scored at that root's own count, which is what makes "this tree
        # scanned nothing" detectable.
        return sources[roots[0]] if len(roots) == 1 else sources.get(".", max(sources.values()))
    return sources


def _stub_report(
    monkeypatch,
    duplicates: list[JscpdDuplicate],
    percentage: float = 0,
    sources: int | dict[str, int] = 200,
) -> list[list[str]]:
    commands: list[list[str]] = []
    _intercept_declared_files(monkeypatch, duplicates)
    stub_resolver_probe(monkeypatch)

    def scan(command) -> object:
        positionals = command[command.index("--fail-on-empty") + 1 :]
        roots = [argument for argument in positionals if argument in _KNOWN_ROOTS]
        scoped = _scoped_duplicates(duplicates, roots)
        report = {
            "statistics": {
                "total": {
                    "sources": _sources_found(sources, roots),
                    "percentage": percentage,
                    "duplicatedLines": 5 * len(scoped),
                }
            },
            "duplicates": scoped,
        }
        output = Path(command[command.index("--output") + 1])
        output.mkdir(exist_ok=True)
        (output / "jscpd-report.json").write_text(json.dumps(report), encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    scan_run_stub(monkeypatch, commands, scan)
    return commands


def _run_against_empty_tree(monkeypatch, **kwargs) -> list[list[str]]:
    """Run the gate over a tree with no duplicates; return the commands issued. The
    clean run is the precondition for most of this file, so the report stub is set
    up here once rather than re-spelled per test.
    """
    commands = _stub_report(monkeypatch, [], **kwargs)
    assert gate._run(_args()) == 0
    return commands


#: The scan roots this file's stubs understand, read from the gate so a root added to the gate is a root these
#: stubs model without a second edit.
_KNOWN_ROOTS = frozenset(root for roots in gate.PROFILE_PATHS.values() for root in roots)


def _roots_of(command: list[str]) -> list[str]:
    """The scan roots an invocation was handed: everything after the last flag, not
    ``command[-1]``, because the cross-tree invocation gets every root and reading
    the last one alone would model it as a `scripts` scan.
    """
    positionals = command[command.index("--fail-on-empty") + 1 :]
    return [argument for argument in positionals if argument in _KNOWN_ROOTS]


def _duplicate(
    first_path: str = "packages/core/crawlutron/new.py",
    second_path: str = "packages/core/crawlutron/other.py",
    fragment: str = "duplicate",
) -> JscpdDuplicate:
    return {
        "firstFile": {"name": first_path, "start": 10, "end": 20},
        "secondFile": {"name": second_path, "start": 30, "end": 40},
        "fragment": fragment,
    }


_GATE_ARGV = [
    "gate",
    "all",
    "--min-lines",
    str(_CONFIGURED_MIN_LINES),
    "--min-tokens",
    str(_CONFIGURED_MIN_TOKENS),
    "--threshold",
    str(_CONFIGURED_THRESHOLD),
]
