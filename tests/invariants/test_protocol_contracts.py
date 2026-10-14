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

_PROTOCOL_CONSTANTS_MODULE = "app/mcp/constants.py"


def _module_level_assignment_lines(tree: ast.Module, names: Iterable[str]) -> dict[str, int]:
    """Line of each named module-level assignment, for the offender label.

    Distinct from :func:`_declared_public_symbols`, which reads the *declaration*
    of the surface; this locates a name the surface already vouches for, so a
    reader of the failure is handed the line to delete rather than a name to go
    looking for.
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

    This surface is a set of *values other code can rely on*: a JSON-RPC code a
    caller can be sent, a ``_meta`` key the server looks for. A declared value
    nothing reads is not a spare part of that surface, it is a claim about it --
    and an untested error code is worse than an absent one precisely because it
    reads as coverage. Two sat in that state: ``MISSING_REQUIRED_CLIENT_CAPABILITY``,
    which nothing ever raised, and ``META_CLIENT_INFO``, which nothing ever read,
    because the 2026-07-28 spec requires only ``protocolVersion`` and
    ``clientCapabilities`` in ``_meta``.

    Reachability is the check rather than a reviewer's memory, because the
    registry is where a spec addition goes to be *recorded* and the recording is
    not the same act as implementing it. The reference sweep is the whole
    ``app/`` tree -- a constant read from a module this test never heard of is
    produced -- with the declaring module excluded from the reference side,
    because its own assignments would otherwise satisfy the check for every
    name in it, which is the defect spelled backwards.

    ``__all__`` rather than every module-level assignment, reusing
    :func:`_declared_public_symbols`, because ``__all__`` is the module's own
    statement of the surface it offers and the claim under test is about that
    surface. An unreadable declaration is itself a finding: a registry the sweep
    cannot read is a registry nothing holds.
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
    """app/mcp/constants.py declares only codes and keys the server can reach."""
    _, owner_tree = file_tree(_PROTOCOL_CONSTANTS_MODULE)
    offenders = _unproduced_protocol_constants(owner_tree, app_trees())
    assert not offenders, "protocol constants with no production reader: " + ", ".join(offenders)


def test_protocol_constant_production_detector_bites():
    """An unread constant fires; reading it by name, or as an attribute, stays clean."""
    owner = ast.parse(
        "HEADER_MISMATCH = -32020\n"
        "META_CLIENT_INFO = 'x'\n"
        "__all__ = ['HEADER_MISMATCH', 'META_CLIENT_INFO']\n"
    )
    elsewhere = APP_ROOT / "mcp" / "protocol.py"
    silent = _unproduced_protocol_constants(
        owner, [(elsewhere, ast.parse("raise ProtocolError(HEADER_MISMATCH)\n"))]
    )
    assert len(silent) == 1, silent
    assert "constants.py:2 META_CLIENT_INFO is declared but nothing in app/ reads it" in silent[0], silent
    both = _unproduced_protocol_constants(
        owner,
        [
            (
                elsewhere,
                ast.parse("def check(meta):\n    return meta, HEADER_MISMATCH, err.META_CLIENT_INFO\n"),
            )
        ],
    )
    assert not both, both
    # The vacuous direction: a registry the sweep cannot read is not a clean bill.
    unreadable = ast.parse("__all__ = compute()\n")
    for source, reason in (
        (unreadable, "the protocol registry is not one literal __all__"),
        (None, "the protocol registry module could not be read"),
    ):
        offenders = _unproduced_protocol_constants(source, [(elsewhere, ast.parse(""))])
        assert len(offenders) == 1 and f"{_PROTOCOL_CONSTANTS_MODULE}: {reason}" in offenders[0], offenders


_CYCLE_GUARD_CALL = "_entered"


_CYCLE_GUARD_OWNER = "_build_guarded"


def _cycle_guard_offenders(
    tree: ast.AST, path: Path, *, call: str = _CYCLE_GUARD_CALL, owner: str = _CYCLE_GUARD_OWNER
) -> list[str]:
    """Calls to the cycle guard made from anywhere but the one function that owns it.

    ``_entered`` was single-sourced as a *function* and then inlined back at
    every use: three container branches each carried the two-step "descend, and
    give up if already on the path" sequence, so the guard existed in four
    places. The function's own docstring already claimed the dedup -- "One
    function rather than three copies of the four lines" -- which made the gap
    worse, not better: a reader checking that claim would have believed it.

    The hazard is what a *fourth* branch does. Adding a container kind means
    writing a new ``isinstance`` arm, and the guard is not something the type
    system requires there. Routing every arm through one helper is what makes
    the check structural instead of remembered, so the rule under test is
    positional rather than structural: the guard is consulted in exactly one
    function in ``app/``, and that function is the one the arms call.
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
        "app/models/taxonomy.py consults the cycle guard in one function; a call from "
        "an arm re-inlines the 'descend, give up if already on the path' pair it "
        "was deduped away from, so route every arm through the owner"
    ),
    proof_path=REPO_ROOT / "app" / "models.py",
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
    """app/models/taxonomy.py consults the cycle guard in one function, not once per container arm."""
    CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE.scan()


def test_cycle_guard_detector_bites():
    """A guard call inlined at an arm fires; the same call in the owner stays clean."""
    CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE.check_bites()


def test_cycle_guard_detector_respects_the_owner_parameter():
    # An owner by another name is still a second place, so the rule is about being the one sanctioned function
    # rather than about a literal spelling.
    assert _cycle_guard_offenders(
        ast.parse("def other(value, seen):\n    return _entered(value, seen)\n"),
        REPO_ROOT / "app" / "models.py",
        owner="different_owner",
    ), "a guard call must fire when the sanctioned owner is renamed"


_META_REASON_OWNER = "_require_meta_key"


_META_WRONG_TYPE_FRAGMENT = "Invalid _meta key (must be "


def _meta_key_reason_offenders(tree: ast.AST, path: Path, *, owner: str = _META_REASON_OWNER) -> list[str]:
    """The wrong-type half of the ``_meta`` key rejection authored outside its owner.

    ``validate_meta`` checked two required ``_meta`` keys and, for each, spelled
    out the same two-part reason -- absent versus wrong type -- and raised the
    same error around it. The distinction is the caller's only way to learn it
    forgot a key rather than sent the wrong shape, so it is a contract, and a
    duplicated one can drift in both directions at once: the two arms can stop
    agreeing on wording, or one can stop distinguishing the two cases at all,
    with the suite green either way.

    Matching the *literal* rather than the shape is deliberate. A second arm that
    built the same sentence a different way -- an f-string split across lines, a
    helper, a new noun -- would be precisely the drift, and a test that recognised
    the shape would wave it through. The rule is that the sentence is authored
    once, in the function both keys check through.
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
    proof_path=REPO_ROOT / "app" / "mcp" / "protocol.py",
)


def test_the_meta_key_rejection_reason_is_authored_once():
    """app/mcp/protocol.py spells the wrong-type _meta reason in one function."""
    THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE.scan()


def test_meta_key_reason_detector_bites():
    """A second authoring of the sentence fires; the one in the owner stays clean."""
    THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE.check_bites()
