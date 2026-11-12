"""A gate's docstring must not claim roots its walk never scans (the #876 audit)."""

from __future__ import annotations

import ast
import re

from tests._walk_primitives import REPO_ROOT, SCAN_ROOTS

#: A scope claim is a root name in TREE position; a bare mention is a subject. Claim shapes: verb-led
#: and list continuation; a deeper path is a reference, not a claim -- the slash must end the segment.
_TREE_CLAIM = re.compile(
    r"(?:sweep|scan|cover|walk|over|across|scanning|sweeping|covering|walking|"
    r"in)s?\s+(?:``)?(crawlutron|crawlutron_sdk|tests|scripts)(?:``)?/(?![\w.])"
    r"|(?:,|and|or)\s+(?:``)?(crawlutron|crawlutron_sdk|tests|scripts)(?:``)?/(?![\w.])",
    re.IGNORECASE,
)

#: The scope law's own declaration file: its exact-tuple pin IS the claim.
_DECLARATION = "test_scan_roots.py"


def _docstrings(tree: ast.Module) -> list[str]:
    """Every docstring in the module (module, class, function)."""
    found: list[str] = []
    for node in [tree] + [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]:
        text = ast.get_docstring(node)
        if text is not None:
            found.append(text)
    return found


def _claimed_roots(text: str) -> set[str]:
    """Root names the text claims to scan, in tree position."""
    return {m for hit in _TREE_CLAIM.findall(text) for m in hit if m}


def _root_bindings(tree: ast.Module) -> set[str]:
    """Root names bound or defaulted at any level: ``X = REPO_ROOT / "<root>"``
    assignments and ``def f(root: Path = REPO_ROOT)``-style ``/ "<root>"``
    defaults anywhere in the tree."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        root_name = None
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            root_name = node.right.value
        elif (
            isinstance(node.right, ast.BinOp)
            and isinstance(node.right.op, ast.Div)
            and isinstance(node.right.left, ast.Constant)
            and isinstance(node.right.left.value, str)
        ):
            root_name = node.right.left.value
        if root_name in SCAN_ROOTS:
            bound.add(root_name)
    return bound


def _derives_scope(source: str, tree: ast.Module) -> bool:
    """True when the gate's walk is rooted in the derived surface, or honestly rooted elsewhere: a
    single-root walk makes a single-root claim TRUE -- dishonesty is a claim the walk cannot honor.
    Derivation is an AST FACT (import or name reference), never a substring: the mention bypass."""
    derives = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "tests._walk_primitives"
        and any(alias.name == "SCAN_ROOTS" for alias in node.names)
        or (isinstance(node, ast.Name) and node.id == "SCAN_ROOTS" and isinstance(node.ctx, ast.Load))
        or (
            isinstance(node, ast.Attribute)
            and node.attr == "SCAN_ROOTS"
            and isinstance(node.value, ast.Name)
            and node.value.id in {"walk_primitives", "wp"}
        )
        for node in ast.walk(tree)
    )
    if derives:
        return True
    claimed: set[str] = set()
    for doc in _docstrings(tree):
        claimed |= _claimed_roots(doc)
    walked = {
        node.left.value
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
        and node.left.value in SCAN_ROOTS
    }
    return claimed <= walked | _root_bindings(tree)


def test_gate_docstrings_claim_no_root_their_walk_misses() -> None:
    """The audit that caught the docstring gate's lie becomes permanent: the gate claimed "crawlutron/,
    tests/ and scripts/" while scanning only crawlutron -- violations behind a green. A gate whose
    docstrings claim a tree scope must derive it from SCAN_ROOTS or walk the claimed roots."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "tests" / "invariants").glob("*.py")):
        if path.name in {"__pycache__", _DECLARATION}:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        derives = _derives_scope(source, tree)
        for doc in _docstrings(tree):
            claimed = _claimed_roots(doc)
            if claimed and not derives:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: claims {sorted(claimed)} but derives no SCAN_ROOTS scope"
                )
    assert not offenders, "\n".join(offenders)


def test_the_claim_detector_sees_the_historical_defect_shape() -> None:
    """The lie that started this: claim all roots, scan one. The detector must
    catch a synthetic gate claiming 'crawlutron/, tests/ and scripts/' with no
    SCAN_ROOTS derivation -- and stay silent when the derivation exists."""
    lying = '"""Sweeps crawlutron/, tests/ and scripts/ for violations."""\nx = 1\n'
    assert _claimed_roots(lying) == {"crawlutron", "tests", "scripts"}
    honest = '"""Sweeps the derived surface."""\nfrom tests._walk_primitives import SCAN_ROOTS\n'
    assert _claimed_roots(honest) == set()


def test_a_subset_claim_is_a_claim() -> None:
    """Claiming two roots while deriving none is dishonest even when a third
    tree exists -- the claim is pinned against the derivation, not the count."""
    subset = '"""Walks tests/ and scripts/ only."""\nx = 1\n'
    assert _claimed_roots(subset) == {"tests", "scripts"}


def test_path_references_are_not_scope_claims() -> None:
    """A docstring naming a file under a root ("tests/_clocks.py") or a subtree
    ("``crawlutron/models``") references the code, it does not claim a scan
    scope -- the detector must stay silent on both shapes."""
    reference = '"""The test suite reads ONE clock: tests/_clocks.py."""\nx = 1\n'
    subtree = '"""Nothing in ``crawlutron/models`` is a grab-bag."""\nx = 1\n'
    assert _claimed_roots(reference) == set()
    assert _claimed_roots(subtree) == set()


def test_a_prose_mention_of_the_constant_is_not_a_derivation() -> None:
    """The mention-shaped bypass (review Attack 1): a gate with the historical lie shape and a
    docstring that merely SAYS "SCAN_ROOTS" must stay an offender -- derivation is an AST binding
    (import or name reference), never a substring test."""
    bypass = (
        '"""Sweeps crawlutron/, tests/ and scripts/ for violations. The scope law (SCAN_ROOTS)'
        ' says every gate must derive its surface."""\nx = 1\n'
    )
    tree = ast.parse(bypass)
    assert _claimed_roots(bypass) == {"crawlutron", "tests", "scripts"}
    assert _derives_scope(bypass, tree) is False, (
        "a prose mention of SCAN_ROOTS counted as a derivation -- the lie walks"
    )
