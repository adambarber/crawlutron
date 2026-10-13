"""Results derive their status from the taxonomy, never by hand."""

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    call_nodes,
    offender_at,
    relative_posix,
    resolved_call_name,
)
from tests.rules import Bites, Rule, Silent

# --- A result may not spell its own (status, classification) pair -------------

_RESULT_CONSTRUCTORS = frozenset({"CrawlStrategyAttempt", "EngineCrawlResult"})


_RESULT_CHOKEPOINT_MODULES = frozenset(
    {"app/orchestration/engine_result.py", "app/orchestration/render_result.py"}
)


def _literal_status_classification_pairs(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a result built from a hand-written (status, classification) pair."""
    if relative_posix(path) in _RESULT_CHOKEPOINT_MODULES:
        return []
    offenders: list[str] = []
    for node in call_nodes(tree):
        if resolved_call_name(tree, node).rsplit(".", 1)[-1] not in _RESULT_CONSTRUCTORS:
            continue
        spelled = {
            kw.arg: kw.value
            for kw in node.keywords
            if kw.arg in ("status", "classification") and isinstance(kw.value, ast.Constant)
        }
        if len(spelled) == 2:
            offenders.append(offender_at(path, node, "hand-written (status, classification) pair"))
    return offenders


RESULTS_DERIVE_THEIR_STATUS_FROM_THE_TAXONOMY = Rule(
    name="results-derive-their-status-from-the-taxonomy",
    find=_literal_status_classification_pairs,
    summary=(
        "Build results through one of the result primitives in "
        + ", ".join(sorted(_RESULT_CHOKEPOINT_MODULES))
        + ", or derive one axis with constants.status_for_classification, so "
        "_enforce_status_classification can see the pair"
    ),
    excludes=SANCTIONED,
    bite=lambda: (
        Bites(
            'CrawlStrategyAttempt(strategy="BROWSER_HEADLESS", status="success",'
            ' classification="rendered")\n',
            "hand-written",
        ),
        Bites(
            'EngineCrawlResult(url=u, status="failed", classification="dns_error")\n',
            "hand-written",
        ),
        # A qualified call is the same construction.
        Bites(
            'models.EngineCrawlResult(url=u, status="failed", classification="blocked")\n',
            "hand-written",
        ),
        # And the chokepoint exemption is keyed on the module, not on a name: a spelled pair anywhere else
        # still fires even under the same function name.
        Bites(
            "def make():\n    return EngineCrawlResult(url=u, status='failed', classification='blocked')\n",
            "hand-written",
        ),
    ),
    clean=lambda: (
        # Derived, not spelled -- what the browser strategy does.
        "CrawlStrategyAttempt(strategy=s, status=status, classification=classification)\n",
        "CrawlStrategyAttempt(strategy=s, status=status_for_classification(label), classification=label)\n",
        # One axis spelled and one derived still cannot disagree silently.
        'CrawlStrategyAttempt(strategy=s, status="failed", classification=label)\n',
        # The same spelled pair inside the chokepoint module is the definition.
        Silent(
            "CrawlStrategyAttempt(strategy=s, status='failed', classification=label)\n",
            at=REPO_ROOT / "app/orchestration/engine_result.py",
        ),
    ),
)


def test_results_derive_their_status_from_the_taxonomy():
    """No record outside the chokepoint spells both taxonomy axes as literals."""
    RESULTS_DERIVE_THEIR_STATUS_FROM_THE_TAXONOMY.scan()


def test_result_status_pair_detector_bites():
    """A spelled pair fires; deriving either axis stays clean."""
    RESULTS_DERIVE_THEIR_STATUS_FROM_THE_TAXONOMY.check_bites()
