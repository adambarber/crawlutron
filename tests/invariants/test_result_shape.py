"""Whatever leaves the process is built in exactly one shape.

Envelopes, job rows and render options each have one constructing chokepoint.
"""

from __future__ import annotations

import ast
import pathlib
import re

from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.storage.migrations.manifest import LEGACY_COLUMNS, StoreSchema
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
from tests.rules import Bites, Rule, Silent


def _cache_envelope_literals(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag any dict literal shaped like a content-cache envelope.

    Keyed on the *stored shape* -- the key set every cache entry carries --
    rather than on the two write paths that happened to spell it, so a third
    writer is caught too. A dict that merely mentions ``expires`` is not an
    envelope; it has to carry the whole contract.
    """
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
    excludes=SANCTIONED | {"content_cache.py"},
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


def test_cache_envelope_is_built_by_one_owner():
    """The content-cache entry envelope has one builder.

    ``set`` and ``aset`` each built the on-disk envelope themselves, which is
    the cache's *stored contract*: a field added for one write path was absent
    from the other, so an entry written through it was missing something the
    reader expects. ``ContentCache._entry_to_store`` is the owner; a new write
    path has to go through it.
    """
    CACHE_ENVELOPE_IS_BUILT_BY_ONE_OWNER.scan()


def test_cache_envelope_detector_bites():
    """The envelope sweep must fire on a hand-rolled envelope elsewhere.

    Without this, an exhausted or renamed sink pattern would report zero
    offenders and the guard above would pass forever while covering nothing.
    """
    CACHE_ENVELOPE_IS_BUILT_BY_ONE_OWNER.check_bites()


# --------------------------------------------------------------------------- Crawl-response shaping
# chokepoint ---------------------------------------------------------------------------


def _crawl_response_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag explicit ``UnifiedCrawlResponse(..., results=[...])`` construction
    outside response_shaping — callers must use build_unified_crawl_response so
    persisted/webhook/REST payloads cannot drift (endpoint-wide).
    ``**stored`` deserialization splats have no explicit results= keyword."""
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
    summary="Use app.api.response_shaping.build_unified_crawl_response",
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "def f(results):\n    return UnifiedCrawlResponse(request_id='r', results=results)\n",
        "explicit UnifiedCrawlResponse",
    ),
    clean=lambda: ("def f(stored):\n    return UnifiedCrawlResponse(**stored)\n",),
)


def test_crawl_response_built_only_by_shaping_chokepoint():
    """Every UnifiedCrawlResponse-with-results flows through the shaper."""
    CRAWL_RESPONSE_BUILT_ONLY_BY_THE_SHAPING_CHOKEPOINT.scan()


def test_crawl_response_chokepoint_detector_bites():
    """Synthetic bypass fires; **stored deserialization splat stays clean."""
    CRAWL_RESPONSE_BUILT_ONLY_BY_THE_SHAPING_CHOKEPOINT.check_bites()


# The rules below close both ends of that: the envelope is adapted in one module, and its scalar fields are
# passed in one function. ---------------------------------------------------------------------------

_ENVELOPE_OWNER_MODULE = "app/api/response_shaping.py"


_ENVELOPE_OWNER_FUNCTION = "_response_envelope"


_ENVELOPE_BUILDERS = ("abuild_unified_crawl_response", "build_unified_crawl_response")


# The only callers permitted to skip the owner's envelope read, each named with the reason it is not the same
# shape as a wire endpoint's: app/api/async_crawl_runner.py -- the detached background runner.
_ENVELOPE_ADAPTER_EXEMPT = frozenset({"app/api/async_crawl_runner.py", "app/mcp/tools.py"})


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


_ENVELOPE_ADAPTER_PROOF_PATH = REPO_ROOT / "app" / "api" / "crawl_endpoint.py"


CRAWL_RESPONSE_ENVELOPE_IS_ADAPTED_BY_ONE_OWNER = Rule(
    name="crawl-response-envelope-is-adapted-by-one-owner",
    find=_unsanctioned_envelope_adapter_offenders,
    summary=(
        "adapt a producer envelope with app.api.response_shaping's envelope owner, "
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
            at=REPO_ROOT / "app" / "api" / "async_crawl_runner.py",
        ),
    ),
)


def test_crawl_response_envelope_is_adapted_by_one_owner():
    """Endpoints read a producer envelope through the shaping owner, not by hand."""
    CRAWL_RESPONSE_ENVELOPE_IS_ADAPTED_BY_ONE_OWNER.scan()


def test_crawl_response_envelope_adapter_detector_bites():
    """A hand-rolled adapter fires; the owner module and the exempt producers stay clean."""
    CRAWL_RESPONSE_ENVELOPE_IS_ADAPTED_BY_ONE_OWNER.check_bites()


def _hand_rolled_envelope_fields_offenders(tree, path):
    """Flag the unified envelope's scalar fields being passed outside their owner.

    Every one of those fields is REQUIRED by :class:`UnifiedCrawlResponse`, so a
    second spelling of the list is a second place to forget a field -- and a
    forgotten field is dropped silently, which is how one shipped missing from
    one endpoint and was caught only by curling the live endpoint. The
    ``submitted_count`` fallback that decides what the wire calls a batch's size
    lives in the same function for the same reason: it used to be written in both
    builders, and two copies of a fallback is how two paths start disagreeing
    about a count.
    """
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


_HAND_ROLLED_ENVELOPE_PROOF_PATH = REPO_ROOT / "app" / "api" / "crawl_endpoint.py"


def _hand_rolled_envelope_source(fn_name: str) -> str:
    """The proof's hand-rolled ``UnifiedCrawlResponse`` build, named ``fn_name``.

    Written out twice it was the same drift-prone block of fixture; the owner
    half is the identical source with the function renamed to the owner's, so
    the one spelling backs both the firing and the sanctioned-owner proof.
    """
    return (
        f"def {fn_name}(results, request_id):\n"
        "    return UnifiedCrawlResponse(results=results, request_id=request_id, success=True, "
        "submitted_count=len(results), policy_schema_version='v')\n"
    )


CRAWL_RESPONSE_ENVELOPE_FIELDS_ARE_PASSED_BY_ONE_OWNER = Rule(
    name="crawl-response-envelope-fields-are-passed-by-one-owner",
    find=_hand_rolled_envelope_fields_offenders,
    summary=(
        "pass the unified envelope's fields in app.api.response_shaping._response_envelope; "
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


def test_crawl_response_envelope_fields_are_passed_by_one_owner():
    CRAWL_RESPONSE_ENVELOPE_FIELDS_ARE_PASSED_BY_ONE_OWNER.scan()


def test_crawl_response_envelope_fields_detector_bites():
    """A second field spelling fires, anywhere; the owner's own build and a
    ``**stored`` deserialization stay clean."""
    CRAWL_RESPONSE_ENVELOPE_FIELDS_ARE_PASSED_BY_ONE_OWNER.check_bites()


# --------------------------------------------------------------------------- Job-row accumulator chokepoint
# (write amplification) ---------------------------------------------------------------------------


def _job_row_accumulator_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag SQL that reads or writes a dropped ``crawl_jobs`` column.

    ``crawl_jobs`` used to carry ``results`` and ``visited`` as JSON columns, so
    every batch persist re-serialized the job's entire page history and total
    write volume grew as the square of the page count (migration 0003). Both are
    append-only accumulators and now live in their own tables.

    The banned set is read from the manifest rather than written out here, so
    the detector cannot drift from the schema: the columns in LEGACY_COLUMNS are
    by definition the ones a current store no longer has. Migrations are exempt
    because dropping a column is the one operation that must name it.
    """
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
    summary=("Append-only accumulators belong in crawl_job_results / crawl_job_visited, not on the job row"),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        'def f(conn, job):\n    return conn.execute("UPDATE crawl_jobs SET visited = ?", (job.visited,))\n',
        "visited",
    ),
    clean=lambda: (
        # The live shape: subqueries over the accumulator tables aliased back to names that CONTAIN the
        # dropped columns. A substring match would reject this, which is why the detector matches whole words.
        "def f(conn, job):\n"
        '    conn.execute("UPDATE crawl_jobs SET frontier = ?", (job.frontier,))\n'
        "    conn.execute('INSERT INTO \"crawl_job_results\" (job_id, seq, payload) VALUES (?, ?, ?)')\n"
        "    conn.execute('INSERT INTO \"crawl_job_visited\" (job_id, url) VALUES (?, ?)')\n"
        '    conn.execute("SELECT (SELECT count(*) FROM crawl_job_results r WHERE r.job_id = job.job_id) '
        'as results_count FROM crawl_jobs job")\n',
    ),
)


def test_crawl_jobs_accumulators_are_never_row_columns():
    """No SQL outside the migrations may name a column the row no longer has."""
    CRAWL_JOBS_ACCUMULATORS_ARE_NEVER_ROW_COLUMNS.scan()


def test_job_row_accumulator_detector_bites():
    """A synthetic wide-row write fires; the accumulator tables and the queue stay clean."""
    CRAWL_JOBS_ACCUMULATORS_ARE_NEVER_ROW_COLUMNS.check_bites()


# --------------------------------------------------------------------------- Render-options construction
# chokepoint ---------------------------------------------------------------------------


def _render_options_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``RenderOptions(...)`` construction outside the resolver.

    The effective render options were once hand-assembled at every entry
    point and the copies drifted -- the browser strategy hardcoded the
    capture knobs while policy reads respected the caller elsewhere, and
    the /v1/render builder silently dropped browser_profile. Only
    app/admission/render_options.py may decide the shape; callers map their own
    source into the resolver's mappings."""
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
        "Use app.admission.render_options.resolve_render_options so request-side "
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
            at=REPO_ROOT / "app" / "crawl" / "render_options.py",
        ),
    ),
)


def test_render_options_built_only_by_resolver():
    """Every effective RenderOptions flows through resolve_render_options."""
    RENDER_OPTIONS_BUILT_ONLY_BY_THE_RESOLVER.scan()


def test_render_options_chokepoint_detector_bites():
    """Synthetic bypass fires; the resolver's own construction stays clean."""
    RENDER_OPTIONS_BUILT_ONLY_BY_THE_RESOLVER.check_bites()
