"""Whatever leaves the process is built in exactly one shape.

Envelopes, job rows and render options each have one constructing chokepoint.
"""

from __future__ import annotations

import ast
import pathlib
import re

from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.storage.migrations.manifest import LEGACY_COLUMNS, StoreSchema
from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    call_keyword_values,
    calls_named,
    dict_displays,
    functions,
    named_calls_to_any,
    offender_at,
    relative_posix,
    string_literals,
)
from tests.rules import Bites, Registry, Rule, Silent


def _cache_envelope_literals(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag any dict literal shaped like a content-cache envelope.

    Keyed on the stored key set so a third write path is caught too; it must
    carry the whole contract, not just ``expires``."""
    required = {"expires", "value", "cached_at"}
    offenders: list[str] = []
    for node in dict_displays(tree):
        keys = {
            key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if required <= keys:
            offenders.append(offender_at(path, node, "cache-envelope"))
    return offenders


CACHE_ENVELOPE_IS_BUILT_BY_ONE_OWNER = Rule(
    name="cache-envelope-is-built-by-one-owner",
    find=_cache_envelope_literals,
    summary="the content-cache envelope is built outside its owner",
    excludes=SANCTIONED | {"content_cache.py"},  # owner: builds the envelope it stores
    bite=lambda: Bites(
        "def write(self, key, value, expires):\n"
        '    return {"expires": expires, "value": value, "cached_at": expires}\n',
        "cache-envelope",
    ),
    clean=lambda: (
        # A dict that merely mentions one envelope key is not an envelope, and flagging it would make the
        # guard something people route around.
        'def f(expires, value):\n    return {"expires": expires, "value": value}\n',
    ),
)


# --------------------------------------------------------------------------- Crawl-response shaping
# chokepoint ---------------------------------------------------------------------------


def _crawl_response_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag explicit ``UnifiedCrawlResponse(..., results=[...])`` outside response_shaping.

    Callers must use ``build_unified_crawl_response`` so persisted/webhook/REST
    payloads cannot drift; ``**stored`` splats have no explicit ``results=``."""
    if path.name == "response_shaping.py":
        return []
    offenders: list[str] = []
    for node, _ in call_keyword_values(
        tree,
        function_names="UnifiedCrawlResponse",
        keyword_names="results",
    ):
        offenders.append(offender_at(path, node, "explicit UnifiedCrawlResponse(results=...) build"))
    return offenders


CRAWL_RESPONSE_BUILT_ONLY_BY_THE_SHAPING_CHOKEPOINT = Rule(
    name="crawl-response-built-only-by-the-shaping-chokepoint",
    find=_crawl_response_construction_offenders,
    summary="Use crawlutron.service.response_shaping.build_unified_crawl_response",
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "def f(results):\n    return UnifiedCrawlResponse(request_id='r', results=results)\n",
        "explicit UnifiedCrawlResponse",
    ),
    clean=lambda: ("def f(stored):\n    return UnifiedCrawlResponse(**stored)\n",),
)


# The rules below close both ends of that: the envelope is adapted in one module, and its scalar fields are
# passed in one function. ---------------------------------------------------------------------------

_ENVELOPE_OWNER_MODULE = "crawlutron/service/response_shaping.py"


_ENVELOPE_OWNER_FUNCTION = "_response_envelope"


_ENVELOPE_BUILDERS = ("abuild_unified_crawl_response", "build_unified_crawl_response")


_ENVELOPE_ADAPTER_EXEMPT = frozenset(
    {"crawlutron/api/async_crawl_runner.py", "crawlutron/mcp/handlers.py"}  # adapters, not wire endpoints
)


# Derived from the model, not transcribed: the rule is "every field the wire model requires is passed by one
# function", so the field set has to come from the model or this guard would check a list that can fall
_ENVELOPE_SCALAR_FIELDS = frozenset(UnifiedCrawlResponse.model_fields) - {"results"}


def _unsanctioned_envelope_adapter_offenders(tree, path):
    """Flag a response builder called from outside the shaping owner module."""
    relative = relative_posix(path)
    if relative == _ENVELOPE_OWNER_MODULE or relative in _ENVELOPE_ADAPTER_EXEMPT:
        return []
    return [
        offender_at(path, call, f"{callee}() outside {_ENVELOPE_OWNER_MODULE}")
        for call, callee in named_calls_to_any(tree, _ENVELOPE_BUILDERS)
    ]


_ENVELOPE_ADAPTER_PROOF_PATH = REPO_ROOT / "crawlutron" / "api" / "crawl_endpoint.py"


CRAWL_RESPONSE_ENVELOPE_IS_ADAPTED_BY_ONE_OWNER = Rule(
    name="crawl-response-envelope-is-adapted-by-one-owner",
    find=_unsanctioned_envelope_adapter_offenders,
    summary=(
        "adapt a producer envelope with crawlutron.service.response_shaping's envelope owner, "
        "so an envelope field cannot be passed on one path and forgotten on another"
    ),
    excludes=SANCTIONED,
    bite=lambda: (
        Bites(
            "async def f(result):\n"
            "    return await abuild_unified_crawl_response(result['results'], "
            "options=EnvelopeOptions(request_id=result['request_id']))\n",
            "abuild_unified_crawl_response",
            at=_ENVELOPE_ADAPTER_PROOF_PATH,
        ),
        Bites(
            "def f(results):\n    return build_unified_crawl_response(results)\n",
            "build_unified_crawl_response",
            at=_ENVELOPE_ADAPTER_PROOF_PATH,
        ),
    ),
    clean=lambda: (
        # The sanctioned adaptation, run at the owner's own module: the owner is exempt by path, and that
        # exemption is the whole content of the rule.
        Silent(
            "async def f(result):\n"
            "    return await aunified_response_from_result(result, adapt=render_result_to_unified)\n",
            at=REPO_ROOT / _ENVELOPE_OWNER_MODULE,
        ),
        # The detached background runner reads the envelope LENIENTLY (``result.get("submitted_count")``) and
        # is a named exempt producer.
        Silent(
            "async def f(request_id):\n"
            "    return await abuild_unified_crawl_response([], "
            "options=EnvelopeOptions(request_id=request_id, success=False))\n",
            at=REPO_ROOT / "crawlutron" / "api" / "async_crawl_runner.py",
        ),
    ),
)


def _hand_rolled_envelope_fields_offenders(tree, path):
    """Flag the unified envelope's scalar fields being passed outside their owner.

    Every field is REQUIRED, so a second spelling is a place to forget one --
    dropped silently, as one shipped and was caught only by curling live."""
    relative = relative_posix(path)
    offenders: list[str] = []
    for call in calls_named(tree, "UnifiedCrawlResponse"):
        if not any(keyword.arg in _ENVELOPE_SCALAR_FIELDS for keyword in call.keywords):
            continue
        enclosing = next(
            (f for f in functions(tree) if f.lineno <= call.lineno <= (f.end_lineno or f.lineno)),
            None,
        )
        if (
            relative == _ENVELOPE_OWNER_MODULE
            and getattr(enclosing, "name", None) == _ENVELOPE_OWNER_FUNCTION
        ):
            continue
        offenders.append(
            offender_at(path, call, f"envelope fields are passed by {_ENVELOPE_OWNER_FUNCTION} only")
        )
    return offenders


_HAND_ROLLED_ENVELOPE_PROOF_PATH = REPO_ROOT / "crawlutron" / "api" / "crawl_endpoint.py"


def _hand_rolled_envelope_source(fn_name: str) -> str:
    """The proof's hand-rolled ``UnifiedCrawlResponse`` build, named ``fn_name``.

    The owner half is the identical source renamed, so one spelling backs both
    the firing and the sanctioned-owner proofs."""
    return (
        f"def {fn_name}(results, request_id):\n"
        "    return UnifiedCrawlResponse(results=results, request_id=request_id, success=True, "
        "submitted_count=len(results), policy_schema_version='v')\n"
    )


CRAWL_RESPONSE_ENVELOPE_FIELDS_ARE_PASSED_BY_ONE_OWNER = Rule(
    name="crawl-response-envelope-fields-are-passed-by-one-owner",
    find=_hand_rolled_envelope_fields_offenders,
    summary=(
        "pass the unified envelope's fields in crawlutron.service.response_shaping._response_envelope; "
        "a field the model requires is dropped, not rejected, when a caller forgets it"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        _hand_rolled_envelope_source("build"),
        _ENVELOPE_OWNER_FUNCTION,
        at=_HAND_ROLLED_ENVELOPE_PROOF_PATH,
    ),
    clean=lambda: (
        # A ``**stored`` deserialization splat carries no explicit scalar fields, so it is not a second
        # spelling of the envelope's field list.
        Silent(
            "def get_crawl_result(stored):\n    return UnifiedCrawlResponse(**stored)\n",
            at=_HAND_ROLLED_ENVELOPE_PROOF_PATH,
        ),
        # The owner's own build is the ONE sanctioned site; the fields are passed only there, so the identical
        # source is silent at the owner module.
        Silent(
            _hand_rolled_envelope_source(_ENVELOPE_OWNER_FUNCTION),
            at=REPO_ROOT / _ENVELOPE_OWNER_MODULE,
        ),
    ),
)


# --------------------------------------------------------------------------- Job-row accumulator chokepoint
# (write amplification) ---------------------------------------------------------------------------


def _job_row_accumulator_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag SQL that reads or writes a dropped ``crawl_jobs`` column.

    ``results``/``visited``/``frontier`` were JSON columns (quadratic batch
    rewrites), now tables; the banned set is read from the migration manifest."""
    if "migrations" in path.parts:
        return []
    banned = {column.name for column in LEGACY_COLUMNS[StoreSchema.CRAWL_JOBS]}
    # Word boundaries, because the dropped names are substrings of the live ones: `results_count` and
    # `visited_size` are output ALIASES of the accumulator subqueries, and `crawl_job_results` is the table
    pattern = re.compile(r"\b(" + "|".join(sorted(banned)) + r")\b")
    offenders: list[str] = []
    for node in string_literals(tree):
        if not isinstance(node.value, str) or "crawl_jobs" not in node.value:
            continue
        named = sorted({match.group(0) for match in pattern.finditer(node.value.lower())})
        if named:
            offenders.append(offender_at(path, node, f"SQL names crawl_jobs accumulator column(s) {named}"))
    return offenders


CRAWL_JOBS_ACCUMULATORS_ARE_NEVER_ROW_COLUMNS = Rule(
    name="crawl-jobs-accumulators-are-never-row-columns",
    find=_job_row_accumulator_offenders,
    summary=(
        "Append-only accumulators belong in crawl_job_results / crawl_job_visited /"
        " crawl_job_frontier, not on the job row"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        'def f(conn, job):\n    return conn.execute("UPDATE crawl_jobs SET frontier = ?", (job.frontier,))\n',
        "frontier",
    ),
    clean=lambda: (
        # The live shape: subqueries over the accumulator tables aliased back to names that CONTAIN the
        # dropped columns. A substring match would reject this, which is why the detector matches whole words.
        "def f(conn, job):\n"
        "    conn.execute('INSERT INTO \"crawl_job_frontier\" (job_id, url) VALUES (?, ?)')\n"
        "    conn.execute('INSERT INTO \"crawl_job_results\" (job_id, seq, payload) VALUES (?, ?, ?)')\n"
        "    conn.execute('INSERT INTO \"crawl_job_visited\" (job_id, url) VALUES (?, ?)')\n"
        '    conn.execute("SELECT (SELECT count(*) FROM crawl_job_results r WHERE r.job_id = job.job_id) '
        'as results_count FROM crawl_jobs job")\n',
    ),
)


# --------------------------------------------------------------------------- Render-options construction
# chokepoint ---------------------------------------------------------------------------


def _render_options_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``RenderOptions(...)`` construction outside the resolver.

    Hand-assembled per entry point, the options drifted (hardcoded capture
    knobs, dropped ``browser_profile``); only the resolver may decide the shape."""
    if path.name == "render_options.py":
        return []
    offenders: list[str] = []
    for node in calls_named(tree, "RenderOptions"):
        offenders.append(offender_at(path, node, "RenderOptions(...) built outside resolve_render_options"))
    return offenders


RENDER_OPTIONS_BUILT_ONLY_BY_THE_RESOLVER = Rule(
    name="render-options-built-only-by-the-resolver",
    find=_render_options_construction_offenders,
    summary=(
        "Use crawlutron.admission.render_options.resolve_render_options so request-side "
        "options and server defaults merge in exactly one place"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "def f(policy):\n    return RenderOptions(timeout_seconds=policy['t'], include_html=True)\n",
        "built outside resolve_render_options",
    ),
    clean=lambda: (
        # The resolver's own construction is the ONE sanctioned build site, and the exemption is a PATH
        # exemption -- so it has to be proved at the real path.
        Silent(
            "def f(request, defaults):\n    return RenderOptions(**kwargs)\n",
            at=REPO_ROOT / "crawlutron" / "crawl" / "render_options.py",
        ),
    ),
)


RULES = Registry(
    (
        CACHE_ENVELOPE_IS_BUILT_BY_ONE_OWNER,
        CRAWL_RESPONSE_BUILT_ONLY_BY_THE_SHAPING_CHOKEPOINT,
        CRAWL_RESPONSE_ENVELOPE_IS_ADAPTED_BY_ONE_OWNER,
        CRAWL_RESPONSE_ENVELOPE_FIELDS_ARE_PASSED_BY_ONE_OWNER,
        CRAWL_JOBS_ACCUMULATORS_ARE_NEVER_ROW_COLUMNS,
        RENDER_OPTIONS_BUILT_ONLY_BY_THE_RESOLVER,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()
