"""Extraction and discovery reach the tree only through the content pipeline."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    SANCTIONED,
    app_trees,
    functions,
    name_reads,
    named_calls_to_any,
    offender,
    offender_at,
    resolved_expr_name,
)
from tests.rules import Bites, Registry, Rule

# --------------------------------------------------------------------------- The content pipeline: one
# dispatch, one discovery call, one projection The browser and HTTP strategies each carried the same

_CONTENT_PIPELINE_OWNER = frozenset({"content_pipeline.py"})  # owner: dispatches the raw extractors


# Import-resolved, so an alias cannot slip past the sweep.
_EXTRACTION_ENTRY_POINTS = frozenset(
    {
        "crawlutron.extraction.content.extract_document",
        "crawlutron.extraction.content.extract_html",
        "crawlutron.extraction.content.extract_content",
        "crawlutron.extraction.content.extract_post_fetch_discovery",
        "crawlutron.extraction.content.normalized_extraction",
    }
)


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


def _extraction_reach_past_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a use of a raw extractor outside the content pipeline.

    Keyed on name-in-load-position, not a call or import: both strategies
    dispatched an offloaded extractor as an argument.
    """
    offenders: list[str] = []
    for node in name_reads(tree):
        if not isinstance(node.ctx, ast.Load):
            continue
        name = resolved_expr_name(tree, node)
        if name in _EXTRACTION_ENTRY_POINTS:
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"{name} is dispatched outside "
                    f"strategies/content_pipeline.py; run the mode chain through _run_extraction",
                )
            )
    return offenders


_EXTRACTION_PROOF_PATH = APP_ROOT / "transport" / "_synthetic.py"


def _assert_owner_extraction_still_fires() -> None:
    """The exemption is not what makes the tree clean: the owner's own calls are
    the shape this detector matches, so the real pipeline must still fire when
    its tree is read at a non-sanctioned path."""
    owner = next((path, tree) for path, tree in app_trees() if path.name in _CONTENT_PIPELINE_OWNER)
    assert owner[0].name == "content_pipeline.py", owner[0]
    assert _extraction_reach_past_offenders(owner[1], _EXTRACTION_PROOF_PATH), (
        "the detector no longer matches the owner"
    )


EXTRACTION_IS_DISPATCHED_BY_THE_CONTENT_PIPELINE_ALONE = Rule(
    name="extraction-is-dispatched-by-the-content-pipeline-alone",
    find=_extraction_reach_past_offenders,
    summary="dispatch extraction through strategies/content_pipeline._run_extraction",
    excludes=SANCTIONED | _CONTENT_PIPELINE_OWNER,  # owner: its own dispatch is the matched shape
    premise=_assert_owner_extraction_still_fires,
    bite=lambda: (
        # The offloaded extractor AND the bare extractor call are two dispatch shapes, so a source carrying
        # both is worth exactly two findings.
        Bites(
            "from crawlutron.extraction.content import extract_html, normalized_extraction\n"
            "async def f(decoded, final_url):\n"
            "    extracted = await asyncio.to_thread(extract_html, decoded, final_url)\n"
            "    return normalized_extraction(extracted)\n",
            "_run_extraction",
            count=2,
        ),
        Bites(
            "from crawlutron.extraction.content import extract_content as _ec\n"
            "def f(body, content_type, url):\n"
            "    return _ec(body, content_type, url)\n",
            "_run_extraction",
        ),
    ),
    clean=lambda: (
        # Routing through the chokepoint is the sanctioned shape.
        "from crawlutron.transport.content_pipeline import _run_extraction\n"
        "async def f(body, final_url):\n"
        "    return await _run_extraction(extraction='full', content=body, decoded='')\n",
    ),
)


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


RULES = Registry(
    (
        EXTRACTION_IS_DISPATCHED_BY_THE_CONTENT_PIPELINE_ALONE,
        DISCOVERED_LINK_RESULT_FIELDS_COME_FROM_THE_PIPELINE,
    )
)


def test_no_rule_violations():
    """The three extraction modes are dispatched in one module, not per strategy."""
    RULES.check_all_clean()
