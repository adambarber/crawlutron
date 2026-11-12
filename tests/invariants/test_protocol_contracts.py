"""Protocol constants are produced; guards and rejections are authored once."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    app_trees,
    attribute_reads,
    file_tree,
    lineno,
    name_reads,
    offender,
    relative_posix,
)
from tests.invariants._shared import _declared_public_symbols, _enclosing_calls, _enclosing_nodes
from tests.rules import Bites, Rule

_PROTOCOL_CONSTANTS_MODULE = "crawlutron/mcp/constants.py"


def _module_level_assignment_lines(tree: ast.Module, names: Iterable[str]) -> dict[str, int]:
    """Line of each named module-level assignment, for the offender label.

    Distinct from the surface declaration reader; this locates a name the
    surface already vouches for so the failure names the line to delete.
    """
    wanted = set(names)
    lines: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                lines[target.id] = node.lineno
    return lines


def _unproduced_protocol_constants(
    owner_tree: ast.Module | None,
    trees: Iterable[tuple[Path, ast.Module]],
    *,
    owner: str = _PROTOCOL_CONSTANTS_MODULE,
) -> list[str]:
    """Declared protocol constants that no other module under ``app/`` reads.

    An unread value is a claim that reads as coverage; reachability across
    ``app/`` is the check, against the ``__all__`` surface statement.
    """
    if owner_tree is None:
        return [f"{owner}: the protocol registry module could not be read, so it cannot be swept"]
    declared = _declared_public_symbols(owner_tree)
    if declared is None:
        return [f"{owner}: the protocol registry is not one literal __all__, so it cannot be swept"]
    referenced: set[str] = set()
    for path, tree in trees:
        if relative_posix(path) == owner:
            continue
        referenced.update(node.id for node in name_reads(tree))
        referenced.update(node.attr for node in attribute_reads(tree))
    lines = _module_level_assignment_lines(owner_tree, declared)
    offenders: list[str] = []
    for name in sorted(declared):
        if name in referenced:
            continue
        detail = f"{name} is declared but nothing in app/ reads it"
        if name in lines:
            offenders.append(offender(REPO_ROOT / owner, lines[name], detail))
        else:
            offenders.append(f"{owner} {detail}")
    return offenders


def test_every_protocol_constant_is_produced():
    """crawlutron/mcp/constants.py declares only codes and keys the server can reach."""
    _, owner_tree = file_tree(_PROTOCOL_CONSTANTS_MODULE)
    offenders = _unproduced_protocol_constants(owner_tree, app_trees())
    assert not offenders, "protocol constants with no production reader: " + ", ".join(offenders)


_CYCLE_GUARD_CALL = "_entered"


_CYCLE_GUARD_OWNER = "_build_guarded"


def _cycle_guard_offenders(
    tree: ast.AST, path: Path, *, call: str = _CYCLE_GUARD_CALL, owner: str = _CYCLE_GUARD_OWNER
) -> list[str]:
    """Calls to the cycle guard made from anywhere but the one function that owns it.

    ``_entered`` was single-sourced then inlined at every use, and the guard is
    not type-required on a new branch; one helper makes the check structural.
    """
    return [
        offender(path, node.lineno, f"{call}() is called from {enclosing!r}, not {owner}()")
        for node, enclosing in _enclosing_calls(tree, call)
        if enclosing != owner
    ]


CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE = Rule(
    name="cycle-guard-is-consulted-in-exactly-one-place",
    find=_cycle_guard_offenders,
    summary=(
        "crawlutron/models/taxonomy.py consults the cycle guard in one function; a call from "
        "an arm re-inlines the 'descend, give up if already on the path' pair it "
        "was deduped away from, so route every arm through the owner"
    ),
    proof_path=APP_ROOT / "models.py",
    bite=lambda: (
        Bites(
            "def arm(value, seen):\n    entered = _entered(value, seen)\n    return entered\n",
            "is called from 'arm', not _build_guarded()",
        ),
        Bites("entered = _entered(value, seen)\n", "is called from '<module>'"),
    ),
    clean=lambda: (
        f"def {_CYCLE_GUARD_OWNER}(value, seen, build):\n"
        "    entered = _entered(value, seen)\n"
        "    return build(value, entered)\n",
    ),
)


def test_the_cycle_guard_is_applied_in_exactly_one_place():
    """crawlutron/models/taxonomy.py consults the cycle guard in one function, not once per container arm."""
    CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE.scan()


def test_cycle_guard_detector_respects_the_owner_parameter():
    # An owner by another name is still a second place, so the rule is about being the one sanctioned function
    # rather than about a literal spelling.
    assert _cycle_guard_offenders(
        ast.parse("def other(value, seen):\n    return _entered(value, seen)\n"),
        APP_ROOT / "models.py",
        owner="different_owner",
    ), "a guard call must fire when the sanctioned owner is renamed"


_META_REASON_OWNER = "_require_meta_key"


_META_WRONG_TYPE_FRAGMENT = "Invalid _meta key (must be "


def _meta_key_reason_offenders(tree: ast.AST, path: Path, *, owner: str = _META_REASON_OWNER) -> list[str]:
    """The wrong-type half of the ``_meta`` key rejection authored outside its owner.

    The absent/wrong-type reason was spelled per key, and that distinction is
    the caller's only way to learn which failure it hit -- so it must be one sentence.
    """

    def is_reason(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _META_WRONG_TYPE_FRAGMENT in node.value
        )

    return [
        offender(path, lineno(node), f"the _meta key reason is authored in {enclosing!r}, not {owner}()")
        for node, enclosing in _enclosing_nodes(tree, is_reason)
        if enclosing != owner
    ]


THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE = Rule(
    name="the-meta-key-rejection-reason-is-authored-once",
    find=_meta_key_reason_offenders,
    summary=(
        "the wrong-type _meta reason is a contract the caller reads to learn it "
        "forgot a key rather than sent the wrong shape, so it is authored once, in "
        "the function both keys check through"
    ),
    bite=lambda: (
        Bites(
            "def validate_meta(meta, request_id):\n"
            f'    reason = f"{{key}} "\n'
            f'    reason += "{_META_WRONG_TYPE_FRAGMENT}{{key}})"\n',
            "authored in 'validate_meta'",
        ),
        Bites(
            f'WHY = "{_META_WRONG_TYPE_FRAGMENT}protocolVersion)"\n',
            "authored in '<module>'",
        ),
    ),
    clean=lambda: (
        # The one place the sentence legitimately lives: the owner function both keys check through.
        f"def {_META_REASON_OWNER}(meta, key, expected, label, request_id):\n"
        f'    return f"{_META_WRONG_TYPE_FRAGMENT}{{label}}): {{key}})"\n',
        # Unrelated reasons are not the contract, so they must not be swept up with it.
        'def validate_meta(meta):\n    raise ProtocolValidationError("Missing _meta block")\n',
        'def validate_meta(meta):\n    reason = f"Missing required _meta key: {key}"\n',
    ),
    proof_path=APP_ROOT / "mcp" / "protocol.py",
)


def test_the_meta_key_rejection_reason_is_authored_once():
    """crawlutron/mcp/protocol.py spells the wrong-type _meta reason in one function."""
    THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE.scan()
