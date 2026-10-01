"""The suite reaches an ``app`` module through a public name, or through the ledger.

43 of the 49 structural commits that moved ``app/`` also moved ``tests/``. That
ratio is what this file exists to shrink, and it is expensive exactly once per
refactor, which is the kind of cost that gets re-litigated instead of paid down.

The underlying fact is that a test importing an ``app`` module's private name is
not making a choice -- it is reporting that the module has no level above its
insides, so there is nowhere else to stand. ``app/crawl/extraction.py`` published
four entry points that were all *compositions*, while the twenty contracts
underneath them were private; a caller reading the public surface would conclude
it was covered, and the suite knew better.

So the gate is deliberately blunt: **a private ``app`` name reaches the suite only
through a declared ledger row with a reason.** It is blunt because a gate that
weighs the merit of an import is a convention, and conventions decay. What the
bluntness buys is the only thing worth enforcing here -- a new weld cannot land
without somebody writing down why it is acceptable.

The ledger is :mod:`tests.private_import_ledger`, and it is expected to shrink.
Rows leave it as modules gain real surfaces (issues #70 through #82); rows that
stay get their kind and reason filled in, which turns a line item into a
decision. A row the suite no longer imports is a lie about the code, so the drift
guard below holds the names to reality in both directions and the hand-maintained
part is left as the reasons alone.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    from_imports,
    nodes_of,
    relative_posix,
    resolved_expr_name,
    trees_under,
)
from tests.private_import_ledger import LEDGER
from tests.rules import Registry, Rule

_TESTS_DIR = REPO_ROOT / "tests"

# Conftest is where the harness reaches the process lifecycle it started, and it is
# the one file whose imports are plumbing rather than assertions. It is still gated
# -- an undeclared weld there is just as invisible -- but it is where the declared
# lifecycle rows legitimately live, so the name is worth naming in the failure text.
_LIFECYCLE_OWNER = "conftest"


def _private_app_names(tree: ast.Module) -> dict[str, int]:
    """``"module.name" -> lineno`` for every private ``app`` name the file reaches.

    Two forms reach the same private name and the first version of this gate read
    only one of them, which left 19 welds invisible: nineteen modules the suite
    reaches through a bound module object rather than a from-import, as in
    ``extraction_mod._quality_score``. A gate that can be walked around by
    spelling the import differently is not a gate, so both forms are resolved to
    the same ``"module.name"`` key and the ledger holds names rather than
    spellings.
    """
    found: dict[str, int] = {}

    def record(key: str, lineno: int) -> None:
        module, _, name = key.rpartition(".")
        if module.startswith("app") and name.startswith("_") and not name.startswith("__"):
            found.setdefault(key, lineno)

    for node in from_imports(tree):
        if not node.module or not node.module.startswith("app") or node.level:
            continue
        for alias in node.names:
            record(f"{node.module}.{alias.name}", node.lineno)
    for node in nodes_of(tree):
        if not isinstance(node, ast.Attribute) or node.attr.startswith("__"):
            continue
        record(resolved_expr_name(tree, node), node.lineno)
    return found


def _undeclared_private_import_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    where = " (harness owner)" if path.name == _LIFECYCLE_OWNER else ""
    return [
        f"{relative}{where}:{lineno}: {key} is a private app name with no ledger row"
        for key, lineno in sorted(_private_app_names(tree).items())
        if key not in LEDGER
    ]


PRIVATE_IMPORT_LEDGER = Rule(
    name="the-suite-imports-only-public-or-declared-app-names",
    find=_undeclared_private_import_offenders,
    summary=(
        "imports a private app name that the ledger does not declare; give the module a public "
        "surface, or record the name in tests/private_import_ledger.py with its kind and reason"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        # An undeclared weld: the gate is the only thing standing between this and
        # a test whose import is invisible to a reviewer of the module it reaches.
        # The name has to be one the ledger does not already hold, or the proof
        # fires on nothing -- which is the failure this rule is meant to prevent.
        "from app.crawl.strategies.browser import _undeclared_probe_name\n",
        # And the `import x.y` / `x.y._name` form, which reaches the same private
        # name through a module object instead of a from-import.
        "import app.crawl.engine\n\nassert app.crawl.engine._undeclared_probe\n",
    ),
    clean=lambda: (
        # A public name needs no ledger row -- that is the whole point of the fix.
        "from app.crawl.engine import crawl_request\n",
        # A declared lifecycle weld is allowed, and says why.
        "from app.crawl.extraction import _shutdown_doc_extraction_executor\n",
        # A name from outside app/ is not this rule's business.
        "from tests.private_import_ledger import LEDGER\n",
        # A public name through a bound module object is the same name publicly,
        # so it needs no row either.
        "import app.crawl.extraction as extraction\n\nvalue = extraction.extract_content\n",
    ),
)

RULES = Registry((PRIVATE_IMPORT_LEDGER,))


def test_no_rule_violations():
    RULES.check_all_clean()


def test_every_rule_bites():
    """A ledger gate that can only fire is indistinguishable from no gate.

    Both directions are proved: the bite is a private import nobody declared, and
    the clean set is a public name, a declared one, a non-``app`` module, and an
    attribute read. The last two matter because a rule keyed on the text
    ``_`` rather than on the import form would fire on all of them.
    """
    RULES.check_all_bite()


def test_ledger_rows_all_correspond_to_a_real_import():
    """A row the suite no longer imports is a lie about the code.

    This is what keeps the ledger from becoming a second hand-maintained mirror of
    the import graph: the names are verified in both directions on every run, so
    the only thing a maintainer can drift is a reason -- prose, which is theirs to
    hold. Without it, a row deleted from the suite would linger as a debt nobody
    is paying, and the ledger would grow a false floor.
    """
    actual: set[str] = set()
    for _, tree in trees_under(_TESTS_DIR):
        actual |= set(_private_app_names(tree))
    stale = sorted(set(LEDGER) - actual)
    assert not stale, (
        f"{len(stale)} ledger row(s) name a private import the suite no longer makes; "
        f"delete them: {', '.join(stale)}"
    )
