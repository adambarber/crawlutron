"""A duplication profile that rejects every pair not declared in the expectations baseline.

The expectations file is a DECLARED BASELINE of known, reviewed duplication,
not a record of past approvals. Each pair carries a reason and a fragment hash
that prove the list and the code agree at the time of the assertion — a hash
proves the list and the code agree, not that a human approved the duplication.
The baseline changes through a reviewed commit; GitHub rulesets return 403 on
this account, so the trust boundary is this gate plus a CODEOWNERS entry
covering `jscpd-expectations.json`.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from scripts.executables import require_repo_tool

SERVICE_ROOT = Path(__file__).resolve().parents[1]
# A clone's location: repo-relative path and the line range jscpd reported.
LocationKey = tuple[str, int, int]
# Both sides of a clone, sorted so the pair has one representation.
LocationPair = tuple[LocationKey, LocationKey]
EXPECTATIONS_PATH = SERVICE_ROOT / "jscpd-expectations.json"
CONFIG_PATH = SERVICE_ROOT / ".jscpd.json"
PRECOMMIT_CONFIG_PATH = SERVICE_ROOT / ".pre-commit-config.yaml"


def configured_floor() -> tuple[int, int, float]:
    """The floor the pre-commit hook actually runs at, read from the hook itself.

    ``.pre-commit-config.yaml`` is the threshold source, because the hook's
    ``entry`` has to carry the numbers as text for pre-commit to invoke it -- YAML
    has no interpolation and an anchor cannot inject a flag, so the literal cannot
    live anywhere else. Everything that needs to know the floor reads it from
    there: the gate's own test suite builds its argv from this, and
    :func:`_require_configured_floor` refuses a run that was handed a laxer pair.

    That refusal is the part worth having. A duplication gate whose number is a
    command-line argument is one keystroke away from being run at a floor that
    hides today's clones and reports green, and the report would be true of the
    floor that was asked for rather than of the one the hook enforces.
    """
    document = yaml.safe_load(PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8"))
    hooks = [
        hook
        for repo_hooks in document.get("repos", ())
        for hook in repo_hooks.get("hooks", ())
        if hook.get("id") == "jscpd"
    ]
    if len(hooks) != 1:
        raise ValueError(f"expected exactly one jscpd hook to read a floor from, found {len(hooks)}")
    entry = hooks[0]["entry"]
    numbers = {}
    for flag in ("min-lines", "min-tokens", "threshold"):
        match = re.search(rf"--{flag}\s+(\S+)", entry)
        if match is None:
            raise ValueError(f"the jscpd hook entry declares no --{flag}, so the floor is unreadable")
        numbers[flag] = match.group(1)
    return int(numbers["min-lines"]), int(numbers["min-tokens"]), float(numbers["threshold"])


def _require_configured_floor(args: argparse.Namespace) -> None:
    """Refuse a run whose thresholds are laxer than the hook's.

    Only laxer is rejected. Scanning *stricter* than the hook is how a new floor
    gets measured before it is adopted -- that is the point of running the gate by
    hand -- so it is allowed, and adopting the number is a reviewed edit to the
    hook plus the baseline it produced.
    """
    min_lines, min_tokens, threshold = configured_floor()
    laxer = (args.min_lines > min_lines, args.min_tokens > min_tokens, args.threshold > threshold)
    if any(laxer):
        raise ValueError(
            f"scanning at ({args.min_lines} lines / {args.min_tokens} tokens / {threshold}%) is "
            f"laxer than the enforced floor ({min_lines} / {min_tokens} / {threshold}%) in "
            f"{PRECOMMIT_CONFIG_PATH.name}: a clean result would only mean the clone is "
            f"below the number asked for. Scan stricter to measure a candidate floor."
        )


MIN_SOURCES = {"all": 200}
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
# `app/crawl/x.py` and `tests/crawl/x.py` both come back as `crawl/x.py`. A
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
PROFILE_PATHS = {"all": tuple(root for roots in TREE_ROOTS.values() for root in roots)}
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
SANCTIONED_IGNORE_GLOBS = (
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/tmp/**",
    "**/node_modules/**",
    "**/.git/**",
    "**/fixtures/**",
    "**/app/storage/migrations/**",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=tuple(PROFILE_PATHS))
    # One floor, applied to every root. The split it replaced (app 7/35, tests
    # 8/40) existed because a single loose floor hid production duplication
    # behind a number picked for test scaffolding. At 4/39 both trees report
    # zero, so there is nothing left to hide and a second number is a second
    # thing to ratchet.
    parser.add_argument("--min-lines", type=int, required=True)
    parser.add_argument("--min-tokens", type=int, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--fail-on-empty", action="store_true", required=True)
    return parser.parse_args()


def _tree_of(path: str) -> str:
    """Which tree a reported path belongs to.

    Derived from the path, not declared by the expectation, so a file cannot
    choose the floor it is judged by. Anything outside the known roots is
    production: the tighter floor, so an unrecognised file cannot slip through
    on the looser one.
    """
    for tree, roots in TREE_ROOTS.items():
        if any(path == root or path.startswith(root + "/") for root in roots):
            return tree
    return "production"


def _reject_source_masking(ignore: list[str]) -> None:
    """A glob that hides source is never a sanctioned exception.

    Checked independently of the sanctioned list so that widening the list is
    not itself a way to hide a clone: the rule is that this gate's ignore list
    may skip caches and vendored trees, and nothing else.
    """
    for glob in ignore:
        if glob.rstrip("/").endswith(".py") or "*.py" in glob:
            raise ValueError(f"jscpd ignore glob would hide Python source: {glob!r}")
        if not glob.strip("*./"):
            raise ValueError(f"jscpd ignore glob would hide the whole tree: {glob!r}")


def _validate_scan_config(path: Path) -> None:
    """Fail closed unless `.jscpd.json` contains only the sanctioned `ignore` key.

    Checked before jscpd runs, not after: a scan taken under an unvalidated
    config has already decided what the gate cannot see, so there is nothing
    left to check afterwards. Only `ignore` is allowed; extra keys like
    `maxLines`, `skipLocal`, `minLines`, `minTokens` pass straight through
    to jscpd via `--config` and are live. Measured: `maxLines: 500` shrinks
    production coverage from 94 sources/21506 lines to 83/13560 (37% of
    production unscanned) while the gate still exits 0 because the expectations
    file is satisfied by the reduced pair set.
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
            f"_jscpd_gate.SANCTIONED_IGNORE_GLOBS"
        )


def _fragment_hash(duplicate: dict[str, Any]) -> str:
    fragment = duplicate.get("fragment")
    if not isinstance(fragment, str):
        raise ValueError("jscpd report is missing a duplicate fragment")
    return hashlib.sha256(fragment.encode()).hexdigest()


def _test_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as error:
        raise ValueError(f"guard file cannot be parsed: {path}") from error

    names: set[str] = set()

    def collect(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                names.add(f"{prefix}{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                collect(node.body, f"{prefix}{node.name}::")

    collect(tree.body)
    return names


def _validate_guard(guard: Any) -> None:
    if not isinstance(guard, str) or not guard.strip():
        raise ValueError("jscpd expectation guard must name a test")
    parts = guard.split("::")
    if len(parts) < 2 or not parts[0] or not parts[-1]:
        raise ValueError(f"jscpd expectation guard has an invalid node id: {guard!r}")
    relative = Path(parts[0])
    if relative.is_absolute() or relative.parts[:1] != ("tests",) or ".." in relative.parts:
        raise ValueError(f"jscpd expectation guard must point inside tests/: {guard!r}")
    path = SERVICE_ROOT / relative
    if not path.is_file() or path.suffix != ".py":
        raise ValueError(f"jscpd expectation guard file does not exist: {guard!r}")
    node = "::".join(parts[1:])
    if node not in _test_names(path):
        raise ValueError(f"jscpd expectation guard test does not exist: {guard!r}")


def _validate_expectation(pair: Any) -> None:
    if not isinstance(pair, dict):
        raise ValueError("each jscpd expectation must be an object")
    pair_id = pair.get("id")
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise ValueError("each jscpd expectation needs a non-empty id")
    if "profiles" in pair:
        raise ValueError(f"jscpd expectation {pair_id!r} uses the legacy profiles shape")
    reason = pair.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"jscpd expectation {pair_id!r} needs a non-empty reason")
    _validate_guard(pair.get("guard"))
    fragment_hash = pair.get("fragment_sha256")
    if not isinstance(fragment_hash, str) or len(fragment_hash) != 64:
        raise ValueError(f"jscpd expectation {pair_id!r} needs a fragment_sha256")
    try:
        int(fragment_hash, 16)
    except ValueError as error:
        raise ValueError(f"jscpd expectation {pair_id!r} has an invalid fragment_sha256") from error
    for side in ("first", "second"):
        location = pair.get(side)
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            raise ValueError(f"jscpd expectation {pair_id!r} has an invalid {side} location")
        path = Path(location["path"])
        if path.is_absolute() or ".." in path.parts or not location["path"]:
            raise ValueError(f"jscpd expectation {pair_id!r} has an unsafe {side} path")
        if not all(
            isinstance(location.get(field), int) and not isinstance(location.get(field), bool)
            for field in ("start", "end")
        ):
            raise ValueError(f"jscpd expectation {pair_id!r} has invalid {side} bounds")
        if location["start"] < 1 or location["end"] < location["start"]:
            raise ValueError(f"jscpd expectation {pair_id!r} has invalid {side} bounds")


def _expected_pairs() -> list[dict[str, Any]]:
    document = json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    if document.get("version") != 1 or not isinstance(document.get("pairs"), list):
        raise ValueError("jscpd-expectations.json must have version 1 and a pairs list")
    seen_ids: set[str] = set()
    seen_locations: dict[LocationPair, str] = {}
    for pair in document["pairs"]:
        _validate_expectation(pair)
        pair_id = pair["id"]
        if pair_id in seen_ids:
            raise ValueError(f"duplicate jscpd expectation id: {pair_id!r}")
        seen_ids.add(pair_id)
        # Two expectations on the same lines is not a duplicate the gate can
        # resolve: at most one can ever be satisfied, and the loser is reported
        # as a missing clone, which sends the reader after the duplication
        # instead of after this file. Rejecting it names the actual mistake.
        locations = _expected_locations(pair)
        if locations in seen_locations:
            raise ValueError(
                f"jscpd expectations {seen_locations[locations]!r} and {pair_id!r} "
                f"declare the same location: {list(locations)}"
            )
        seen_locations[locations] = pair_id
    return document["pairs"]


def _location_pair(entries: list[LocationKey]) -> LocationPair:
    first, second = sorted(entries)
    return (first, second)


def _location(container: dict[str, Any], side: str, path_key: str) -> LocationKey:
    return (str(container[side][path_key]), int(container[side]["start"]), int(container[side]["end"]))


def _expected_locations(pair: dict[str, Any]) -> LocationPair:
    return _location_pair([_location(pair, side, "path") for side in ("first", "second")])


def _scanned_roots(profile: str) -> tuple[str, ...]:
    return PROFILE_PATHS[profile]


def _resolve_roots(name: str, profile: str) -> tuple[str, ...]:
    """Which scan root a multi-path report's name belongs to.

    jscpd strips the root prefix when it is handed several paths, so one
    invocation over `app tests` returns `crawl/recursive.py` for
    `app/crawl/recursive.py` AND `_recursive_harnesses.py` for
    `tests/_recursive_harnesses.py` -- names that name no tree, which is the
    whole reason attribution was delegated to one-root-per-invocation scans in
    the first place. There is no flag to turn that off, so attribution is
    resolved here instead, against the filesystem.

    Returns every root that could hold the name, not just the first, because a
    name in two roots (two files of the same name, as the gate's own fixtures
    have) is genuinely undecidable. The caller treats that as possibly-spanning
    rather than picking one, which keeps the failure mode a report a human can
    dismiss rather than a dropped clone nobody sees.
    """
    return tuple(root for root in _scanned_roots(profile) if _IS_FILE(SERVICE_ROOT / root / name))


def _expected_tree(pair: dict[str, Any]) -> str:
    """The tree label a declared pair is scored under.

    Derived from the two declared paths the same way the cross-tree pass derives
    it from the two reported ones, so a pair whose sides straddle two trees is
    keyed ``cross`` whether it was declared or observed. Keying a cross-tree
    declaration under its first side's tree would let a within-tree entry of
    identical content satisfy it -- the sanctioned fragment would be found in the
    other tree and the cross-tree clone would go unreported.
    """
    first, second = _expected_locations(pair)
    if _tree_of(first[0]) != _tree_of(second[0]):
        return CROSS_TREE
    return _tree_of(first[0])


def _pair_id(duplicate: dict[str, Any], root: str) -> LocationPair:
    """Where jscpd says a clone is, in repository-relative terms.

    jscpd's `name` is relative to the scan root, so the root is put back on.
    Without it, `models.py` scanned from `app/` and `models.py` scanned from
    `tests/` are the same string, and a single whole-tree scan cannot say which
    tree a pair belongs to.
    """
    return _location_pair(
        [_scoped(root, _location(duplicate, side, "name")) for side in ("firstFile", "secondFile")]
    )


def _scoped(root: str, key: LocationKey) -> LocationKey:
    path = key[0]
    scoped = path if root == "." or path == root or path.startswith(root + "/") else f"{root}/{path}"
    return (scoped, key[1], key[2])


def _short(fragment_hash: str) -> str:
    return fragment_hash[:12]


def _scan(root: str, floor: tuple[int, int], args: argparse.Namespace) -> dict[str, Any]:
    """One jscpd invocation over one root at one floor, scored as a gate."""
    report = jscpd_report(root, floor, threshold=args.threshold, fail_on_empty=True)
    if report is None:
        raise OSError(f"jscpd found no sources under {root}")
    return report


def _cross_tree_scan(floor: tuple[int, int], args: argparse.Namespace) -> dict[str, Any]:
    """Every root in ONE invocation, so a pair that spans two of them is visible.

    The per-root scans are the reason this exists. Handed one root, jscpd names
    the other root's files relative to the one it was given, so a fragment shared
    between `app/` and `tests/` is reported by neither invocation -- not at a
    higher floor, not with a different threshold, not at all. The gap is not a
    near miss, and the shape it hides is the one that matters most: a test
    harness re-spelling production's truth looks like one legitimate copy on each
    side, so the per-tree gate has nothing to object to even in principle.

    Scanned as one invocation with every root as a positional, and attributed
    through :func:`_resolve_root` because the reported names are root-relative and
    name no tree.
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
    """True when the two sides of a pair could resolve to different scan roots.

    Decided as "could", not "must". A name that resolves to one root on both
    sides is within one tree and was already scored by the per-root pass; a name
    that resolves to several is unattributable and is kept rather than guessed.
    Keeping it costs a report a human can dismiss, dropping it could hide a real
    cross-tree clone, and the asymmetry is the point -- this pass exists to catch
    the one class the per-root scans are blind to, so its bias is toward
    reporting.
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
    """A cross-tree pair's location, re-prefixed so both sides name their root.

    The per-root scans cannot leave this to ``_scoped``, because the name a
    multi-path invocation returns has already had its root stripped and no longer
    says which file it was. A name that resolves to several roots is reported
    under each of them, since which copy was the one compared is exactly what is
    unknown -- and the declared locations use the same rule, so an ambiguous pair
    can still be sanctioned.
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


def jscpd_report(
    root: str,
    floor: tuple[int, int],
    *,
    threshold: int = 0,
    fail_on_empty: bool = False,
    paths: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Run jscpd over one root at one (lines, tokens) floor and parse the report.

    The one place that knows how to invoke jscpd. The gate scores what comes
    back and the frontier sweep enumerates the whole staircase of floors, and
    a second copy of this command line is a second answer to "what did jscpd
    see" -- which is the question the whole gate exists to keep one answer to.

    ``paths`` scans several roots in ONE invocation, which is the only way to see
    a clone that spans two of them: handed one root, jscpd reports the other
    root's files under names relative to the one it was given, so a pair is
    either invisible or attributed to a path that names neither tree. Used only
    by the cross-tree scan, with ``root="."`` so the reported names are the real
    service-relative paths.

    ``fail_on_empty`` is a gate concern, not a measurement one: it makes jscpd
    refuse an empty result so a mis-pointed scan root cannot read as clean.
    A sweep wants the report either way. Returns None only when jscpd produced
    no report at all, which is the one failure a caller cannot interpret.
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


_IMPORT_LINE = re.compile(r"^(?:import|from)\s")


def _is_dependency_declaration(fragment: str) -> bool:
    """True when a fragment is nothing but import statements.

    Two modules that both import the same four names produce byte-identical
    text, and no third module can hold those imports for them -- Python resolves
    each module's own namespace. So a fragment of this shape cannot drift from
    its twin, cannot be extracted into a chokepoint, and cannot be a defect. It
    is the one place in a codebase where identical text means identical intent
    rather than a second copy of a decision, which is exactly what this gate
    exists to find. At the configured floor four of these lines clear the
    threshold, so a package where every record module imports its own taxonomy
    reports clones forever.

    A fragment qualifies only if EVERY logical line is an import: one line of
    real code between two import runs is enough to keep the pair under
    judgement, because that is where the duplication the gate wants to see
    begins. The fragment is NOT required to end with balanced parens. jscpd
    reports a line range, so a run that happens to be cut on the opening line of
    a wrapped import is still nothing but declarations, and asking whether the
    report stopped mid-statement is a question about where jscpd stopped rather
    than about what the text says. A continuation line inherits the
    classification of the statement it continues, so an unclosed tail stays an
    import; a fragment made ONLY of continuation lines has no statement to judge
    and stays visible, which is the safe direction.
    """
    logical: list[str] = []
    open_parens = 0
    for line in fragment.splitlines():
        stripped = line.strip()
        # A line inside an open paren continues the statement above it, so it is
        # not a statement of its own and must not be judged as one.
        continuation = open_parens > 0
        open_parens += line.count("(") - line.count(")")
        if not stripped or stripped.startswith("#"):
            continue
        if not continuation:
            logical.append(stripped)
    return bool(logical) and all(_IMPORT_LINE.match(line) for line in logical)


def _footprint(duplicate: dict[str, Any]) -> int:
    return min(
        int(duplicate["firstFile"]["end"]) - int(duplicate["firstFile"]["start"]) + 1,
        int(duplicate["secondFile"]["end"]) - int(duplicate["secondFile"]["start"]) + 1,
    )


def _run(args: argparse.Namespace) -> int:
    _validate_scan_config(CONFIG_PATH)
    _require_configured_floor(args)
    floor = (args.min_lines, args.min_tokens)
    # Keyed by (tree, content). The tree is part of the identity because the
    # same fragment in `app/` and in `tests/` is two separate findings against
    # two separate scan roots, and jscpd reports root-relative names, so a
    # single whole-tree scan keyed on content alone cannot tell them apart.
    observed: dict[tuple[str, str], set[LocationPair]] = {}
    spans: dict[tuple[str, str], int] = {}
    totals: dict[str, dict[str, int]] = {}
    sources = 0
    # Counted, not silently dropped: a filter that hides its own hits is
    # indistinguishable from a filter that hides too much. The lines are
    # subtracted from the duplication statistic too, because jscpd counted them
    # and leaving them in would hold the percentage over the threshold for
    # fragments this pass has already judged.
    declarations = 0
    declaration_lines = 0
    for tree, roots in TREE_ROOTS.items():
        for root in roots:
            report = _scan(root, floor, args)
            statistics = report["statistics"]["total"]
            found_sources = int(statistics["sources"])
            sources += found_sources
            if found_sources < 1:
                print(
                    f"jscpd {args.profile}: [{tree}] {root} analyzed {found_sources} sources; "
                    f"the scan root is wrong or the tree is empty",
                    file=sys.stderr,
                )
                return 1
            tree_totals = totals.setdefault(
                tree, {"lines": 0, "duplicatedLines": 0, "pairs": 0, "sources": 0}
            )
            tree_totals["lines"] += int(statistics.get("lines", 0))
            tree_totals["duplicatedLines"] += int(statistics["duplicatedLines"])
            tree_totals["sources"] += found_sources
            for duplicate in report["duplicates"]:
                if _is_dependency_declaration(str(duplicate.get("fragment", ""))):
                    declarations += 1
                    declaration_lines += _footprint(duplicate)
                    continue
                fragment_hash = _fragment_hash(duplicate)
                key = (tree, fragment_hash)
                observed.setdefault(key, set()).add(_pair_id(duplicate, root))
                spans.setdefault(key, _footprint(duplicate))
                tree_totals["pairs"] += 1
            tree_totals["duplicatedLines"] -= declaration_lines
            declaration_lines = 0

    # The cross-tree pass runs after the per-root ones and deliberately does NOT
    # fold its source count into `sources`: it reads the same files a second
    # time, so counting them twice would let the MIN_SOURCES floor be met with
    # half the sources it names.
    cross = _cross_tree_scan(floor, args)
    cross_sources = int(cross["statistics"]["total"]["sources"])
    if cross_sources < 1:
        print(
            f"jscpd {args.profile}: [cross] {CROSS_TREE_ROOT} analyzed {cross_sources} sources; "
            f"the scan root is wrong or every tree is empty",
            file=sys.stderr,
        )
        return 1
    cross_totals = {"lines": 0, "duplicatedLines": 0, "pairs": 0, "sources": cross_sources}
    for duplicate in cross["duplicates"]:
        if _is_dependency_declaration(str(duplicate.get("fragment", ""))):
            declarations += 1
            continue
        # Only pairs that SPAN two roots are this pass's business. A pair inside
        # one tree was already scored above against the same floor, and reporting
        # it twice would charge one clone against two expectations.
        if not _spans_trees(duplicate, args.profile):
            continue
        fragment_hash = _fragment_hash(duplicate)
        key = (CROSS_TREE, fragment_hash)
        observed.setdefault(key, set()).add(_cross_pair_id(duplicate, args.profile))
        spans.setdefault(key, _footprint(duplicate))
        cross_totals["pairs"] += 1
    totals[CROSS_TREE] = cross_totals

    if sources < MIN_SOURCES[args.profile]:
        print(f"jscpd {args.profile}: analyzed only {sources} sources", file=sys.stderr)
        return 1

    # A sanctioned clone is identified by its CONTENT, within its tree. Identity
    # used to be (path, start, end), and `missing` was tested before
    # `mismatched`, so inserting one comment line above a sanctioned clone
    # reported it as a disappearance and the fix-forward was to copy the
    # coordinates out of the error message into the expectation -- an
    # expectation whose stated location nothing ever checked. Content identity
    # makes a moved clone the same clone, which turns that into drift: a failure
    # that names the expectation and states where the content is now, so
    # correcting it is a reviewed edit rather than a copy of what was printed.
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for pair in _expected_pairs():
        fragment_hash = str(pair["fragment_sha256"])
        key = (_expected_tree(pair), fragment_hash)
        if key in expected:
            raise ValueError(
                f"two jscpd expectations ({expected[key]['id']!r} and {pair['id']!r}) "
                f"claim the same fragment {_short(fragment_hash)}"
            )
        expected[key] = pair

    unsanctioned = sorted(set(observed) - set(expected))
    missing = sorted(set(expected) - set(observed))
    drifted: list[tuple[dict[str, Any], tuple[LocationPair, ...]]] = []
    undeclared: list[tuple[dict[str, Any], list[LocationKey]]] = []
    for key, pair in expected.items():
        found = observed.get(key)
        if not found:
            continue
        found_keys = {location for location_pair in found for location in location_pair}
        declared = _expected_locations(pair)
        if not set(declared) <= found_keys:
            drifted.append((pair, tuple(sorted(found))))
            continue
        extra = sorted(found_keys - set(declared))
        if extra:
            undeclared.append((pair, extra))

    if unsanctioned or missing or drifted or undeclared:
        for key in unsanctioned:
            for location in sorted(observed[key]):
                print(
                    f"jscpd {args.profile}: [{key[0]}] unsanctioned clone: {location} "
                    f"sha256={_short(key[1])}",
                    file=sys.stderr,
                )
        for key in missing:
            print(
                f"jscpd {args.profile}: [{key[0]}] expected clone is missing: "
                f"id={expected[key]['id']} sha256={_short(key[1])}. "
                f"Either the duplication was fixed, in which case delete the expectation on "
                f"purpose, or its content changed, in which case it is an unsanctioned clone above.",
                file=sys.stderr,
            )
        for pair, drifted_at in drifted:
            print(
                f"jscpd {args.profile}: expected clone drifted: id={pair['id']} declares "
                f"{_expected_locations(pair)} but its content is now at {list(drifted_at)}. "
                f"Update the expectation to match, in this commit, on purpose.",
                file=sys.stderr,
            )
        for pair, undeclared_at in undeclared:
            print(
                f"jscpd {args.profile}: sanctioned clone also appears undeclared: "
                f"id={pair['id']} at {undeclared_at}",
                file=sys.stderr,
            )
        return 1

    if declarations:
        print(
            f"jscpd {args.profile}: skipped {declarations} fragment(s) that are nothing but "
            f"import statements. Two modules importing the same names is a shared vocabulary, "
            f"not a second copy of a decision; :func:`_is_dependency_declaration` says why.",
            file=sys.stderr,
        )

    for tree, tree_totals in totals.items():
        sanctioned_lines = sum(spans[key] for key in expected if key[0] == tree)
        unsanctioned_lines = max(0, tree_totals["duplicatedLines"] - sanctioned_lines)
        total_lines = tree_totals["lines"]
        percentage = 100 * unsanctioned_lines / total_lines if total_lines else 0.0
        if percentage > args.threshold:
            print(
                f"jscpd {args.profile}: [{tree}] {percentage:.6f}% unsanctioned "
                f"exceeds {args.threshold:.6f}%",
                file=sys.stderr,
            )
            return 1
        print(
            f"jscpd {args.profile}: [{tree}] {tree_totals['pairs']} pairs at "
            f"{floor[0]} lines/{floor[1]} tokens, "
            f"{sum(1 for key in expected if key[0] == tree)} sanctioned, "
            f"{tree_totals['duplicatedLines']} duplicated lines of {total_lines} "
            f"({tree_totals['sources']} sources)"
        )
    return 0


def main() -> int:
    try:
        return _run(_parse_args())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"jscpd gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
