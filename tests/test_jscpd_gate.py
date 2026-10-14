"""The duplication gate wrapper fails closed on thin scans."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from scripts import _jscpd_pair_resolution as res
from scripts import jscpd_gate as gate


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
        fail_on_empty=True,
    )


def _write_expectations(path: Path, pairs: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "pairs": pairs}), encoding="utf-8")


def _strip_root(name: str, root: str) -> str:
    """jscpd's name for a file when several roots are scanned in one invocation.

    A single-root invocation reports paths relative to that root. Handed several,
    it strips the root prefix from each and reports `crawl/recursive.py` for
    `app/orchestration/recursive.py` and `_recursive_harnesses.py` for
    `tests/_recursive_harnesses.py` -- measured, not assumed: a multi-path scan of
    this service returns exactly those two strings, neither of which names the
    tree it came from. The stub has to reproduce that, because the gate resolves
    those names back to a root against the filesystem and a stub that returned
    honest paths would be modelling a tool that does not exist.
    """
    return name[len(root) + 1 :] if name.startswith(root + "/") else name


def _stripped(location: dict, root: str) -> dict:
    return {**location, "name": _strip_root(str(location["name"]), root)}


def _stub_report(
    monkeypatch,
    duplicates: list[dict],
    percentage: float = 0,
    sources: int | dict[str, int] = 200,
) -> list[list[str]]:
    commands: list[list[str]] = []

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

    def run(command, **_kwargs):
        commands.append(command)
        positionals = command[command.index("--fail-on-empty") + 1 :]
        roots = [argument for argument in positionals if argument in _KNOWN_ROOTS]

        def _inside(name: str) -> bool:
            return any(name == root or name.startswith(root + "/") for root in roots)

        def _straddles(duplicate: dict) -> bool:
            sides = [duplicate[side]["name"] for side in ("firstFile", "secondFile")]
            return len({_root_of(side) for side in sides}) > 1

        def _root_of(name: str) -> str:
            return next(root for root in roots if name == root or name.startswith(root + "/"))

        if len(roots) == 1:
            # One root per invocation: only pairs inside it are reported, named relative to it.
            scoped = [
                {
                    **duplicate,
                    "firstFile": _stripped(duplicate["firstFile"], roots[0]),
                    "secondFile": _stripped(duplicate["secondFile"], roots[0]),
                }
                for duplicate in duplicates
                if _inside(duplicate["firstFile"]["name"]) and _inside(duplicate["secondFile"]["name"])
            ]
        else:
            # The cross-tree pass: pairs that SPAN roots, named without their root so they are unattributable
            # until the gate resolves them.
            scoped = [
                {
                    **duplicate,
                    "firstFile": _stripped(duplicate["firstFile"], _root_of(duplicate["firstFile"]["name"])),
                    "secondFile": _stripped(
                        duplicate["secondFile"], _root_of(duplicate["secondFile"]["name"])
                    ),
                }
                for duplicate in duplicates
                if _straddles(duplicate)
            ]
        if isinstance(sources, dict):
            # One root per invocation is scored at that root's own count, which is what makes "this tree
            # scanned nothing" detectable.
            found = sources[roots[0]] if len(roots) == 1 else sources.get(".", max(sources.values()))
        else:
            found = sources
        report = {
            "statistics": {
                "total": {"sources": found, "percentage": percentage, "duplicatedLines": 5 * len(scoped)}
            },
            "duplicates": scoped,
        }
        output = Path(command[command.index("--output") + 1])
        output.mkdir(exist_ok=True)
        (output / "jscpd-report.json").write_text(json.dumps(report), encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(res.subprocess, "run", run)
    return commands


def _run_against_empty_tree(monkeypatch, tmp_path, **kwargs) -> list[list[str]]:
    """Run the gate over a tree with no duplicates; return the commands issued.

    The clean run is the precondition for most of what this file asserts (a
    threshold is honoured, a root is scanned once, a floor is applied to every
    root), so the empty expectations file and the report stub are set up here
    once rather than re-spelled per test.
    """
    _write_expectations(tmp_path / "expectations.json", [])
    monkeypatch.setattr(res, "EXPECTATIONS_PATH", tmp_path / "expectations.json")
    commands = _stub_report(monkeypatch, [], **kwargs)
    assert gate._run(_args()) == 0
    return commands


#: The scan roots this file's stubs understand, read from the gate so a root added to the gate is a root these
#: stubs model without a second edit.
_KNOWN_ROOTS = frozenset(root for roots in gate.PROFILE_PATHS.values() for root in roots)


def _roots_of(command: list[str]) -> list[str]:
    """The scan roots an invocation was handed.

    Everything after the last flag, rather than ``command[-1]``: the cross-tree
    invocation is given every root as a positional, so the last one is not the
    only one and reading it that way would model that invocation as a `scripts`
    scan.
    """
    positionals = command[command.index("--fail-on-empty") + 1 :]
    return [argument for argument in positionals if argument in _KNOWN_ROOTS]


def _duplicate(
    first_path: str = "app/new.py",
    second_path: str = "app/other.py",
    fragment: str = "duplicate",
) -> dict:
    return {
        "firstFile": {"name": first_path, "start": 10, "end": 20},
        "secondFile": {"name": second_path, "start": 30, "end": 40},
        "fragment": fragment,
    }


_LITERAL_GUARD = (
    "tests/invariants/test_taxonomy_construction.py::test_classification_literal_covers_the_enum_exactly"
)


def _valid_pair() -> dict:
    return {
        "id": "new-clone",
        "first": {"path": "app/new.py", "start": 10, "end": 20},
        "second": {"path": "app/other.py", "start": 30, "end": 40},
        "reason": "intentional",
        "guard": _LITERAL_GUARD,
        "fragment_sha256": hashlib.sha256(b"duplicate").hexdigest(),
    }


_PROBE_BASENAME = "zz_probe_clone.py"


# The real module the probe is a copy of, by its path inside the mirrored tree.
PROBE_SOURCE = "app/storage/ids.py"


def _mirrored_service_tree(tmp_path: Path, payload: str) -> Path:
    """A throwaway service tree carrying production's relative layout.

    `app/storage/ids.py` is IN the mirror, because it is the other half of the
    production clone: the reported pair is the planted probe against the real
    module it was copied from, and a mirror holding only the probe would report
    nothing and pass for the wrong reason.

    `jscpd` matches each ignore glob against the path RELATIVE TO ITS OWN CWD,
    not against an absolute path, and that is what makes a tree under pytest's
    `tmp_path` a faithful stand-in: measured by scanning `app/` from a mirror
    rooted under `/tmp`, the `**/tmp/**` glob matched nothing while
    `**/app/storage/migrations/**` excluded the migration clone and left the
    production clone reported. So this holds on a Linux checkout where
    `tmp_path` itself lives under `/tmp`, not only on a macOS tmp dir.
    """
    mirror = tmp_path / "service"
    # Two production files with identical content, plus the same content again under the excluded migrations
    # subtree.
    for relative in (PROBE_SOURCE, f"app/{_PROBE_BASENAME}", f"app/storage/migrations/{_PROBE_BASENAME}"):
        planted = mirror / relative
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(payload, encoding="utf-8")
    # One module per remaining root: enough for the gate's "analyzed 0 sources" check, and a single file per
    # root cannot form a pair of its own.
    for tree in ("tests", "scripts"):
        filler = mirror / tree / "zz_tree_filler.py"
        filler.parent.mkdir(parents=True, exist_ok=True)
        filler.write_text(payload, encoding="utf-8")
    return mirror


_GATE_ARGV = [
    "gate",
    "all",
    "--min-lines",
    str(_CONFIGURED_MIN_LINES),
    "--min-tokens",
    str(_CONFIGURED_MIN_TOKENS),
    "--threshold",
    str(_CONFIGURED_THRESHOLD),
    "--fail-on-empty",
]


def _configure(monkeypatch, tmp_path, pair: dict, duplicate: list[dict]) -> list[list[str]]:
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, [pair])
    monkeypatch.setattr(res, "EXPECTATIONS_PATH", expectations)
    commands = _stub_report(monkeypatch, duplicate)
    monkeypatch.setattr(sys, "argv", _GATE_ARGV)
    return commands


def _expect_nothing_sanctioned(monkeypatch, tmp_path, duplicates, **report) -> None:
    """An attestation file that sanctions nothing, plus a scan report we choose.

    Every gate failure that is NOT about a sanctioned pair needs exactly these
    two things, and was spelling both out at each site. ``_configure`` cannot
    stand in for it: that helper requires a declared pair, which is precisely
    what these tests are rejecting.
    """
    expectations = tmp_path / "expectations.json"
    _write_expectations(expectations, [])
    monkeypatch.setattr(res, "EXPECTATIONS_PATH", expectations)
    _stub_report(monkeypatch, duplicates, **report)
