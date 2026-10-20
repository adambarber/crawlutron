from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.executables import require_repo_tool

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = SERVICE_ROOT / ".jscpd.json"
PRECOMMIT_CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"

# A clone's location: repo-relative path and the line range jscpd reported.
LocationKey = tuple[str, int, int]


# Both sides of a clone, sorted so the pair has one representation.
LocationPair = tuple[LocationKey, LocationKey]


#: How a scan root is decided a name belongs to. A module attribute rather than a
#: default argument so the gate's own tests can put a name in a root without
#: putting a file there -- the resolution is against the filesystem by design, and
#: the tests that exercise it need synthetic paths.
_IS_FILE: Callable[[Path], bool] = Path.is_file


# The tree label for a pair whose two sides are in different trees. Its own label
# rather than the first side's tree because the content of a cross-tree fragment
# is the same content a within-tree pair would have, and keying a cross finding
# under `production` would let a sanctioned within-tree entry absorb a cross-tree
# clone of identical text.
CROSS_TREE = "cross"


#: The scan root that sees every tree at once, so its reported names are the real
#: service-relative paths. See ``_cross_tree_scan`` for why the per-root scans
#: cannot substitute for this.
CROSS_TREE_ROOT = "."


# One root per invocation, so that every pair jscpd reports can be attributed to
# the tree it is in: jscpd names files relative to the root it was handed, and
# `app/api/x.py` and `tests/api/x.py` both come back as `x.py`. A
# single invocation over all roots would make that attribution impossible.
#
# This is a real cost and used to be accepted as the price of attribution: it
# means a fragment shared BETWEEN two trees is reported by neither invocation,
# because neither root contains both sides. That is not a consequence of the
# attribution requirement, it is the cost of not noticing the case, and
# ``_cross_tree_scan`` pays the attribution a different way -- one invocation,
# service-relative names, and a tree comparison per path -- so the gap is closed
# rather than documented.
TREE_ROOTS = {
    "production": ("app",),
    "test": ("tests", "scripts"),
}


# `.jscpd.json` decides which files this gate never sees, so it is enforcement
# configuration, and a config nothing validates is a config that can be edited
# into a no-op: one added glob is enough to hide a clone, and `ignore` was read
# from disk by the subprocess below without ever being checked. These are the
# whole sanctioned set -- caches, vendored trees, the scratch directory, and
# schema migrations -- and the gate holds them rather than trusting the file, so
# widening the scan surface is a deliberate edit to this list in a reviewed
# commit instead of a silent edit to a JSON file.
#
# The migrations subtree is excluded because parallel CREATE TABLE scaffolding
# across two migration files is the correct shape, not duplication: migrations
# are replayed by name and each one is a standalone record of one schema step.
# It is a subtree glob so a migration added tomorrow is covered without anyone
# editing this list, which is the only way an exclusion stays true.
PROFILE_PATHS = {"all": tuple(root for roots in TREE_ROOTS.values() for root in roots)}


SANCTIONED_IGNORE_GLOBS = (
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/tmp/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/fixtures/**",
    "**/app/storage/migrations/**",
)


def _tree_of(path: str) -> str:
    """Which tree a reported path belongs to, derived from the path -- so a file cannot
    choose the floor it is judged by; anything outside the known roots is production
    (the tighter floor, so an unrecognised file cannot slip through).
    """
    for tree, roots in TREE_ROOTS.items():
        if any(path == root or path.startswith(root + "/") for root in roots):
            return tree
    return "production"


def _reject_source_masking(ignore: list[str]) -> None:
    """A glob that hides source is never a sanctioned exception.

    Checked independently of the sanctioned list so widening the list cannot itself
    hide a clone: this gate's ignore may skip caches and vendored trees, nothing else.
    """
    for glob in ignore:
        if glob.rstrip("/").endswith(".py") or "*.py" in glob:
            raise ValueError(f"jscpd ignore glob would hide Python source: {glob!r}")
        if not glob.strip("*./"):
            raise ValueError(f"jscpd ignore glob would hide the whole tree: {glob!r}")


def _validate_scan_config(path: Path) -> None:
    """Fail closed unless ``.jscpd.json`` holds only the sanctioned ``ignore`` key.

    Checked before jscpd runs -- a config validated after the scan can no longer
    see what it hid; extra keys (``maxLines``, ``minTokens``...) pass straight through as live.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} must be an object")
    extra = set(document) - {"ignore"}
    if extra:
        raise ValueError(f"{path.name} contains unknown keys: {sorted(extra)}")
    if not isinstance(document.get("ignore"), list):
        raise ValueError(f"{path.name} must have an ignore list")
    ignore = document["ignore"]
    if not all(isinstance(glob, str) and glob for glob in ignore):
        raise ValueError(f"{path.name} ignore entries must be non-empty strings")
    _reject_source_masking(ignore)
    if tuple(ignore) != SANCTIONED_IGNORE_GLOBS:
        added = sorted(set(ignore) - set(SANCTIONED_IGNORE_GLOBS))
        removed = sorted(set(SANCTIONED_IGNORE_GLOBS) - set(ignore))
        raise ValueError(
            f"{path.name} ignore list is not the sanctioned set "
            f"(added={added}, removed={removed}); the sanctioned globs live in "
            f"jscpd_gate.SANCTIONED_IGNORE_GLOBS"
        )


def _location_pair(entries: list[LocationKey]) -> LocationPair:
    first, second = sorted(entries)
    return (first, second)


def _location(container: dict[str, Any], side: str, path_key: str) -> LocationKey:
    return (str(container[side][path_key]), int(container[side]["start"]), int(container[side]["end"]))


def _scanned_roots(profile: str) -> tuple[str, ...]:
    return PROFILE_PATHS[profile]


def _resolve_roots(name: str, profile: str) -> tuple[str, ...]:
    """Which scan root a multi-path report's name belongs to, resolved against the
    filesystem because jscpd strips roots when handed several paths. Returns every root
    that could hold the name -- a name in two roots is undecidable, kept over guessed.
    """
    return tuple(root for root in _scanned_roots(profile) if _IS_FILE(SERVICE_ROOT / root / name))


def _pair_id(duplicate: dict[str, Any], root: str) -> LocationPair:
    """Where jscpd says a clone is, in repository-relative terms: jscpd's ``name`` is
    root-relative, so the root is put back on -- otherwise ``models.py`` from ``app/``
    and ``tests/`` collapse into the same string.
    """
    return _location_pair(
        [_scoped(root, _location(duplicate, side, "name")) for side in ("firstFile", "secondFile")]
    )


def _scoped(root: str, key: LocationKey) -> LocationKey:
    path = key[0]
    scoped = path if root == "." or path == root or path.startswith(root + "/") else f"{root}/{path}"
    return (scoped, key[1], key[2])


def jscpd_report(
    root: str,
    floor: tuple[int, int],
    *,
    threshold: int = 0,
    fail_on_empty: bool = False,
    paths: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Run jscpd over one root at one (lines, tokens) floor and parse the report -- the
    ONE place that knows how to invoke jscpd. ``paths`` scans several roots in one
    invocation (the only way to see a spanning clone); ``fail_on_empty`` refuses an empty result.
    """
    with tempfile.TemporaryDirectory(prefix="jscpd-gate-") as raw_output:
        output = Path(raw_output)
        command = [
            require_repo_tool(SERVICE_ROOT, "jscpd"),
            "--config",
            str(CONFIG_PATH),
            "-f",
            "python",
            "--min-lines",
            str(floor[0]),
            "--min-tokens",
            str(floor[1]),
            "--threshold",
            str(threshold),
            "--reporters",
            "json",
            "--output",
            str(output),
            # The root stays last: it is a positional, and every caller -- the
            # gate, the sweep, and the test stubs that read ``command[-1]`` to
            # tell which root was scanned -- reads it from the end.
            *(["--fail-on-empty"] if fail_on_empty else []),
            *(paths or (root,)),
        ]
        completed = subprocess.run(command, cwd=SERVICE_ROOT, capture_output=True, text=True, check=False)
        report_path = output / "jscpd-report.json"
        if report_path.is_file():
            return json.loads(report_path.read_text(encoding="utf-8"))
        if completed.returncode not in (0, 1):
            raise OSError(f"jscpd failed on {root}: {completed.stdout}{completed.stderr}".strip())
        return None


def _scan(root: str, floor: tuple[int, int], args: argparse.Namespace) -> dict[str, Any]:
    """One jscpd invocation over one root at one floor, scored as a gate."""
    report = jscpd_report(root, floor, threshold=args.threshold, fail_on_empty=True)
    if report is None:
        raise OSError(f"jscpd found no sources under {root}")
    return report


def _cross_tree_scan(floor: tuple[int, int], args: argparse.Namespace) -> dict[str, Any]:
    """Every root in ONE invocation so a pair spanning two of them is visible -- a
    per-root scan names the other root's files relative to the one it was given, so a
    cross-tree fragment is reported by neither. Attributed via ``_resolve_root``.
    """
    report = jscpd_report(
        CROSS_TREE_ROOT,
        floor,
        threshold=args.threshold,
        fail_on_empty=True,
        paths=_scanned_roots(args.profile),
    )
    if report is None:
        raise OSError(f"jscpd found no sources under {CROSS_TREE_ROOT}")
    return report


def _spans_trees(duplicate: dict[str, Any], profile: str) -> bool:
    """True when the two sides of a pair could resolve to different roots ("could",
    not "must"): a single-root pair was scored per-root already; an unattributable
    multi-root name is kept rather than guessed -- bias toward reporting this blind class.
    """
    first = _resolve_roots(duplicate["firstFile"]["name"], profile)
    second = _resolve_roots(duplicate["secondFile"]["name"], profile)
    if not first or not second:
        raise ValueError(
            f"jscpd reported a clone between {duplicate['firstFile']['name']!r} and "
            f"{duplicate['secondFile']['name']!r}, and at least one is under none of the "
            f"scanned roots {list(_scanned_roots(profile))}"
        )
    return len(first) > 1 or len(second) > 1 or first != second


def _cross_pair_id(duplicate: dict[str, Any], profile: str) -> LocationPair:
    """A cross-tree pair's location, re-prefixed so both sides name their root; ``_scoped``
    cannot do it because a multi-path invocation's name has had its root stripped. A name in
    several roots is reported under each -- and declared locations use the same rule.
    """
    sides = [
        [
            (
                f"{root}/{duplicate[side]['name']}",
                int(duplicate[side]["start"]),
                int(duplicate[side]["end"]),
            )
            for root in _resolve_roots(duplicate[side]["name"], profile)
        ]
        for side in ("firstFile", "secondFile")
    ]
    return min(_location_pair([first, second]) for first in sides[0] for second in sides[1])
