"""Extraction and discovery reach the tree only through the content pipeline."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    SANCTIONED,
    app_trees,
    functions,
    named_calls_to_any,
    offender,
    offender_at,
)
from tests.rules import Bites, Registry, Rule

# --------------------------------------------------------------------------- The content pipeline: one
# dispatch, one discovery call, one projection The browser and HTTP strategies each carried the same

_CONTENT_PIPELINE_OWNER = frozenset({"content_pipeline.py"})  # owner: dispatches the raw extractors


# The terminal-result builders, not the model: result_helpers constructing EngineCrawlResult is where the
# discovered-links fields are DEFINED, which is a different thing from a call site spelling them.
_RESULT_BUILDERS = frozenset(
    {
        "binary_no_text_result",
        "engine_result_from_exception",
        "failed_engine_result",
        "build_engine_result",
        "skipped_engine_result",
        "terminal_engine_result",
    }
)


_DISCOVERY_RESULT_FIELDS = frozenset(
    {"discovered_link_count", "discovered_links", "discovered_links_truncated"}
)


_DISCOVERY_PROJECTION = "discovery_result_fields"


# The discovery projection's one-definition census sits with the rules it guards:
# a second definition would satisfy every structural guard while reintroducing the duplication.


def _respelled_discovery_result_fields_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag discovered-links result fields spelled at a result-builder call.

    ``**`` spread is the sanctioned shape; a strategy routing through
    ``discovery_result_fields`` reports nothing, a re-spelling reports fields.
    """
    offenders: list[str] = []
    for node, callee in named_calls_to_any(tree, _RESULT_BUILDERS):
        spelled = sorted({kw.arg for kw in node.keywords if kw.arg in _DISCOVERY_RESULT_FIELDS})
        if spelled:
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"{callee} spells {', '.join(spelled)}; "
                    f"spread content_pipeline.discovery_result_fields instead",
                )
            )
    return offenders


DISCOVERED_LINK_RESULT_FIELDS_COME_FROM_THE_PIPELINE = Rule(
    name="discovered-link-result-fields-come-from-the-pipeline",
    find=_respelled_discovery_result_fields_offenders,
    summary=("project the discovery envelope through content_pipeline.discovery_result_fields"),
    excludes=SANCTIONED | _CONTENT_PIPELINE_OWNER,  # owner: projects the fields it defines
    bite=lambda: (
        # The historical shape: the block spelled at the result call.
        Bites(
            "def build(post_fetch, error):\n"
            "    return build_engine_result(\n"
            "        discovered_link_count=post_fetch['discovered_link_count'],\n"
            "        discovered_links=post_fetch['discovered_links'],\n"
            "        error=error,\n"
            "    )\n",
            "discovery_result_fields",
        ),
    ),
    clean=lambda: (
        # The sanctioned spread is not a spelling of individual fields.
        "def build(post_fetch, error):\n"
        "    return build_engine_result(\n"
        "        **discovery_result_fields(post_fetch, classification_evidence=[], error=error),\n"
        "        next_page_url=post_fetch['next_page_url'],\n"
        "    )\n",
        # A discovered-links field that is not a result field is somebody else's.
        "def count(post_fetch):\n"
        "    return stats(discovered_link_count=post_fetch['discovered_link_count'])\n",
    ),
)


def test_discovery_result_projection_has_exactly_one_definition():
    """The projection is one function, so it cannot be forked per strategy.

    Tree-wide for the same reason as the sweeps above: a second definition would
    satisfy every structural guard while reintroducing the exact duplication.
    """
    definitions = [
        offender(path, node.lineno)
        for path, tree in app_trees(exclude=frozenset())
        for node in functions(tree)
        if node.name == _DISCOVERY_PROJECTION
    ]
    assert len(definitions) == 1, f"the discovery projection was defined again: {definitions}"
    assert "packages/core/src/crawlutron/transport/content_pipeline.py" in definitions[0], definitions


RULES = Registry((DISCOVERED_LINK_RESULT_FIELDS_COME_FROM_THE_PIPELINE,))


def test_no_rule_violations():
    """The three extraction modes are dispatched in one module, not per strategy."""
    RULES.check_all_clean()
