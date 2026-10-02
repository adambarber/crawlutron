"""Whatever leaves the process is built in exactly one shape.

Response envelopes, error envelopes, job rows, render options, wire models
and advertised payload keys each have a chokepoint that constructs them.
A second construction site is a shape that will drift from the model it
mirrors, so these checks make the second one red."""

from __future__ import annotations

import ast
import pathlib
import re
from enum import StrEnum
from typing import get_args

import pytest

from app.crawl.migrations.manifest import LEGACY_COLUMNS, StoreSchema
from app.models.unified_crawl_response import UnifiedCrawlResponse
from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    app_trees,
    call_keyword_values,
    call_nodes,
    calls_named,
    calls_named_leaf,
    dict_displays,
    file_tree,
    functions,
    name_reads,
    named_calls_to_any,
    offender,
    offender_at,
    relative_posix,
    resolved_call_name,
    resolved_expr_name,
    string_literals,
)
from tests.rules import Bites, Rule, Silent, Trips


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
        # A dict that merely mentions one envelope key is not an envelope, and
        # flagging it would make the guard something people route around.
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


# ---------------------------------------------------------------------------
# Crawl-response shaping chokepoint
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The same chokepoint, two more edges
#
# ``abuild_unified_crawl_response`` is the response builder, but the drift that
# actually shipped was never in the builder: it was in the ADAPTER -- the two
# endpoints each read a raw producer envelope and passed four fields by name,
# and a field added to one and forgotten on the other was dropped by the model
# without a word. The rules below close both ends of that: the envelope is
# adapted in one module, and its scalar fields are passed in one function.
# ---------------------------------------------------------------------------

_ENVELOPE_OWNER_MODULE = "app/api/response_shaping.py"


_ENVELOPE_OWNER_FUNCTION = "_response_envelope"


_ENVELOPE_BUILDERS = ("abuild_unified_crawl_response", "build_unified_crawl_response")


# The only callers permitted to skip the owner's envelope read, each named with
# the reason it is not the same shape as a wire endpoint's:
#   app/api/async_crawl_runner.py -- the detached background runner. It answers a
#     webhook instead of a caller and reads the envelope LENIENTLY
#     (``result.get("submitted_count")``), because a producer that stopped
#     stamping a key must fail a job, not raise out of a background task that
#     has no caller to answer.
#   app/mcp/tools.py -- the MCP crawl tool, which is the same adaptation as
#     /v1/crawl and is still spelled out at its own call site. PENDING: it is
#     outside app/api, so it is named here rather than migrated, and this entry
#     is what a reviewer should delete once it goes through the owner.
_ENVELOPE_ADAPTER_EXEMPT = frozenset({"app/api/async_crawl_runner.py", "app/mcp/tools.py"})


# Derived from the model, not transcribed: the rule is "every field the wire
# model requires is passed by one function", so the field set has to come from
# the model or this guard would check a list that can fall behind it.
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
        # The sanctioned adaptation, run at the owner's own module: the owner is
        # exempt by path, and that exemption is the whole content of the rule.
        Silent(
            "async def f(result):\n"
            "    return await aunified_response_from_result(result, adapt=render_result_to_unified)\n",
            at=REPO_ROOT / _ENVELOPE_OWNER_MODULE,
        ),
        # The detached background runner reads the envelope LENIENTLY
        # (``result.get("submitted_count")``) and is a named exempt producer.
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
        # A ``**stored`` deserialization splat carries no explicit scalar fields,
        # so it is not a second spelling of the envelope's field list.
        Silent(
            "def get_crawl_result(stored):\n    return UnifiedCrawlResponse(**stored)\n",
            at=_HAND_ROLLED_ENVELOPE_PROOF_PATH,
        ),
        # The owner's own build is the ONE sanctioned site; the fields are passed
        # only there, so the identical source is silent at the owner module.
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


# ---------------------------------------------------------------------------
# Job-row accumulator chokepoint (write amplification)
# ---------------------------------------------------------------------------


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
    # Word boundaries, because the dropped names are substrings of the live
    # ones: `results_count` and `visited_size` are output ALIASES of the
    # accumulator subqueries, and `crawl_job_results` is the table itself.
    # Underscore is a word character, so none of those can match -- only a
    # column named exactly the banned word does.
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
        # The live shape: subqueries over the accumulator tables aliased back to
        # names that CONTAIN the dropped columns. A substring match would reject
        # this, which is why the detector matches whole words.
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


# ---------------------------------------------------------------------------
# Render-options construction chokepoint
# ---------------------------------------------------------------------------


def _render_options_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``RenderOptions(...)`` construction outside the resolver.

    The effective render options were once hand-assembled at every entry
    point and the copies drifted -- the browser strategy hardcoded the
    capture knobs while policy reads respected the caller elsewhere, and
    the /v1/render builder silently dropped browser_profile. Only
    app/crawl/render_options.py may decide the shape; callers map their own
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
        "Use app.crawl.render_options.resolve_render_options so request-side "
        "options and server defaults merge in exactly one place"
    ),
    excludes=SANCTIONED,
    bite=lambda: Bites(
        "def f(policy):\n    return RenderOptions(timeout_seconds=policy['t'], include_html=True)\n",
        "built outside resolve_render_options",
    ),
    clean=lambda: (
        # The resolver's own construction is the ONE sanctioned build site, and the
        # exemption is a PATH exemption -- so it has to be proved at the real path. At
        # a synthetic path the same source fires, which is the whole point: the
        # exemption is narrow enough to not generalize, and this asserts that.
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


# ---------------------------------------------------------------------------
# Error-envelope shape
# ---------------------------------------------------------------------------

# app/api/exceptions.py owns the envelope; it is the sanctioned definition
# (exclude matches by file *name*, matching app_trees' own semantics).
ENVELOPE_SANCTIONED = SANCTIONED | {"exceptions.py"}


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _envelope_dict_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag hand-rolled error envelopes; the factory is the only definition.

    Three shapes: ANY ``HTTPException(...)`` construction (the wire shape
    may only be built by :func:`http_error`/``browser_pool_unavailable`` -- a
    bare ``HTTPException(detail="str")`` reaches the handler's fallback and
    used to be laundered into a fabricated ``http_<status>`` code); a
    ``HTTPException(detail={...code, message...})`` literal (the pre-factory
    form); and any dict carrying all three envelope keys anywhere (catches
    JSONResponse/return-value variants).
    """
    offenders = [
        offender_at(
            path,
            call,
            "HTTPException detail= envelope literal"
            if any(
                kw.arg == "detail"
                and isinstance(kw.value, ast.Dict)
                and ({"code", "message"} <= _dict_keys(kw.value))
                for kw in call.keywords
            )
            else "bare HTTPException construction",
        )
        for call in calls_named_leaf(tree, {"HTTPException"})
    ]
    offenders.extend(
        offender_at(path, node, "error envelope dict literal")
        for node in dict_displays(tree)
        if {"code", "message", "details"} <= _dict_keys(node)
    )
    return offenders


_HAND_ROLLED_ERROR_ENVELOPES_FORBIDDEN = Rule(
    name="no-hand-rolled-error-envelopes",
    find=_envelope_dict_offenders,
    summary="Use app.api.exceptions.http_error/browser_pool_unavailable",
    excludes=ENVELOPE_SANCTIONED,
    bite=lambda: (
        # Four offenders from one source: two bare HTTPException builds, one
        # detail= envelope literal, one three-key dict. Counted exactly, because a
        # detector that reported one here would be hiding which shapes it still sees.
        Trips(
            "raise HTTPException(status_code=404, detail={'code': 'not_found', 'message': 'gone'})\n"
            "response = {'code': 'pending', 'message': 'm', 'details': {}}\n"
            "raise HTTPException(status_code=404, detail='plain string')\n"
            "raise fastapi.HTTPException(status_code=500)\n",
            count=4,
        ),
    ),
    clean=lambda: (
        "from app.api.exceptions import http_error\n"
        "raise http_error(404, 'not_found', 'gone')\n"
        "response = build_thing({'code': 1, 'message': 'x'})\n",
    ),
)


def test_no_hand_rolled_error_envelopes():
    """Every HTTP error body flows through error_envelope/http_error."""
    _HAND_ROLLED_ERROR_ENVELOPES_FORBIDDEN.scan()


def test_envelope_detector_bites():
    """All three drift shapes fire; factory-shaped code stays clean."""
    _HAND_ROLLED_ERROR_ENVELOPES_FORBIDDEN.check_bites()


# ---------------------------------------------------------------------------
# Raw exception text never reaches the wire (information disclosure)
#
# render.py built the worker-failure result with ``error=str(error)`` from a
# caught exception, and ``to_dict`` is pure serialization -- so the raw text
# (DNS names, filesystem paths, secrets) reached the /v1/render body verbatim.
# ``terminal_crawl_result`` is the sanctioned construction site and takes an
# already-classified error; this keeps raw text from drifting back into any
# error= field of a wire-visible result.
# ---------------------------------------------------------------------------

_TERMINAL_RESULT_SANCTIONED = frozenset({"result_helpers.py"})


_RESULT_ERROR_SINKS = frozenset({"CrawlResult", "terminal_crawl_result"})


_EXCEPTION_NAMES = frozenset({"error", "exc", "exception", "err"})


def _is_raw_exception_text(value: ast.expr) -> bool:
    """True for ``str(exc)``/``repr(exc)`` or an f-string interpolating one."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("str", "repr"):
        return True
    if isinstance(value, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id in _EXCEPTION_NAMES
            for part in value.values
        )
    return False


def _raw_exception_error_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a wire-visible result built with the raw text of an exception."""
    offenders: list[str] = []
    for node, callee in named_calls_to_any(tree, _RESULT_ERROR_SINKS):
        for kw in node.keywords:
            if kw.arg == "error" and _is_raw_exception_text(kw.value):
                offenders.append(offender_at(path, node, f"{callee}(error=<raw exception text>)"))
    return offenders


NO_RAW_EXCEPTION_TEXT_IN_RESULT_ERROR_FIELD = Rule(
    name="no-raw-exception-text-in-result-error-field",
    find=_raw_exception_error_offenders,
    summary="classify via classify_error_code, never raw exception text",
    excludes=SANCTIONED | _TERMINAL_RESULT_SANCTIONED,
    bite=lambda: (
        # ``str(exc)`` on a direct result construction, and the same through the
        # sanctioned primitive -- both sinks carry the raw text verbatim.
        Bites("def f(exc):\n    return CrawlResult(error=str(exc))\n", "raw exception text"),
        Bites("def f(exc):\n    return terminal_crawl_result(error=str(exc))\n", "raw exception text"),
        # An f-string interpolating the exception is the leak in disguise.
        Bites("def f(error):\n    return CrawlResult(error=f'failed: {error}')\n", "raw exception text"),
        Bites("def f(e):\n    return CrawlResult(error=repr(e))\n", "raw exception text"),
    ),
    clean=lambda: (
        # The classified shape: an already-classified error reaches the wire.
        "def f(exc):\n    return terminal_crawl_result(error=classify_error_code(exc))\n",
    ),
)


def test_no_raw_exception_text_in_result_error_field():
    """The ``error`` field is a stable classification, never ``str(exc)``."""
    NO_RAW_EXCEPTION_TEXT_IN_RESULT_ERROR_FIELD.scan()


def test_raw_exception_error_detector_bites():
    """The leak shape fires on both sinks; the classified shape stays clean."""
    NO_RAW_EXCEPTION_TEXT_IN_RESULT_ERROR_FIELD.check_bites()


# --- A wire model may not silently drop a domain model's field ---------------
#
# app/models/taxonomy.py holds the internal dataclasses and the pydantic response models
# are the ones under app/models/ (app/api_models.py is the caller-supplied
# side). `DiscoveredLink` was declared in both -- the wire twin is now
# field names, and *nothing* connects the two: response_shaping converts
# generically by attribute, and a strict pydantic response model DROPS any key
# it does not declare. So adding a field to the dataclass and forgetting the
# BaseModel is not a type error and not a test failure -- the field stops
# existing on the wire, and everything downstream reads a default. That is the
# CapabilitiesResponse failure mode this project already shipped once, which
# is why the pair gets a gate rather than a comment asking people to keep two
# lists in step.
#
# Written as a rule over whatever pair exists rather than a hardcoded name, so
# the next twin is covered without a second edit here.

_DOMAIN_MODEL_MODULE = "app/models/taxonomy.py"


# The pydantic RESPONSE models. They moved out of app/api_models.py and then out
# of app/api_responses.py, and now live one per file under app/models/, so the
# wire side is a derived set rather than a path: the rule below is written over
# whatever pair exists, and deriving the set means a record added tomorrow is
# covered without a second edit here.
_WIRE_MODEL_DIR = "app/models"


def _wire_tree() -> ast.Module:
    """Every response model in the package, merged into one comparable tree.

    The rule pairs a domain record with a wire model by class NAME, so merging
    the declarations loses nothing the rule reads; only the module each came from
    does, which is what the failure text below names instead.
    """
    merged = ast.Module(body=[], type_ignores=[])
    for path in sorted(REPO_ROOT.glob(f"{_WIRE_MODEL_DIR}/*.py")):
        merged.body.extend(ast.parse(path.read_text(encoding="utf-8")).body)
    return merged


# The domain side is not one module: a record is a declared key set wherever it
# is declared. PageMetadata's producer lives with the extractor, not with the
# internal dataclasses, and reading only app/models/taxonomy.py made the pair invisible --
# which is how three wire fields survived with no producer. The rule pairs by
# class NAME, so the producer record deliberately carries the same name as the
# wire model it must agree with: the name is how the rule finds the pair, and a
# reader seeing one name in two modules reads the contract rather than two
# unrelated things.
_PRODUCER_RECORD_MODULE = "app/crawl/llm_format.py"


def _is_dataclass(node: ast.ClassDef) -> bool:
    """True for a ``@dataclass`` class, bare or parameterised."""
    return any(
        (isinstance(d, ast.Name) and d.id == "dataclass")
        or (isinstance(d, ast.Call) and getattr(d.func, "id", None) == "dataclass")
        for d in node.decorator_list
    )


def _is_record(node: ast.ClassDef) -> bool:
    """True for a class that DECLARES its key set: a dataclass or a TypedDict.

    Both spell the same contract -- a closed set of named fields -- so the mirror
    rule reads both. The TypedDict half is what let an extractor's output be
    checked against its response model at all; before it, the extractor returned
    ``dict[str, Any]``, which declares nothing, and the pair simply did not
    exist to be compared.
    """
    return _is_dataclass(node) or any(
        isinstance(base, ast.Name)
        and base.id in ("TypedDict", "NotRequired")
        or isinstance(base, ast.Subscript)
        and isinstance(base.value, ast.Name)
        and base.value.id in ("TypedDict", "Required", "NotRequired")
        for base in node.bases
    )


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    assert found, f"{name} is not declared in this module"
    return found[0]


def _declaring_classes(tree: ast.Module) -> set[str]:
    return {n.name for n in tree.body if isinstance(n, ast.ClassDef) and _is_record(n)}


def _paired_names(domain_tree: ast.Module, wire_tree: ast.Module) -> set[str]:
    """Class names this rule would actually compare, one per side.

    Exists so the no-op check below asks the rule's own question rather than a
    restatement of it. It once compared "is a record here" against "is a record
    there", which on the wire side is a pydantic model and never a record -- so
    the guard could never pass and would have needed to be deleted to be fixed.
    """
    domain = _declaring_classes(domain_tree)
    wire = {n.name for n in wire_tree.body if isinstance(n, ast.ClassDef) and _is_pydantic_model(n)}
    return domain & wire


def _is_pydantic_model(node: ast.ClassDef) -> bool:
    """True for a class deriving from ``BaseModel``, directly or subscripted."""
    return any(
        isinstance(target := (base.value if isinstance(base, ast.Subscript) else base), ast.Name)
        and target.id == "BaseModel"
        for base in node.bases
    )


def _declared_fields(node: ast.ClassDef) -> list[str]:
    return [
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


def _wire_mirror_drift(domain_tree: ast.Module, wire_tree: ast.Module) -> list[str]:
    """Field names that differ between a domain dataclass and its wire model."""
    domain = {n.name: n for n in domain_tree.body if isinstance(n, ast.ClassDef) and _is_record(n)}
    wire = {n.name: n for n in wire_tree.body if isinstance(n, ast.ClassDef) and _is_pydantic_model(n)}
    drifted: list[str] = []
    for name in sorted(domain.keys() & wire.keys()):
        internal = set(_declared_fields(domain[name]))
        external = set(_declared_fields(wire[name]))
        drifted.extend(
            f"{name}.{field_name} exists internally and is dropped on the wire"
            for field_name in sorted(internal - external)
        )
        drifted.extend(
            f"{name}.{field_name} is declared on the wire and nowhere internally"
            for field_name in sorted(external - internal)
        )
    return drifted


def test_wire_models_mirror_every_domain_field():
    """A domain field cannot disappear between the dataclass and the response model."""
    domain_path, domain_tree = file_tree(_DOMAIN_MODEL_MODULE)
    drifted = _wire_mirror_drift(domain_tree, _wire_tree())
    assert not drifted, (
        f"A pydantic response model DROPS every key it does not declare, so a field added to "
        f"a domain dataclass in {domain_path.name} without a twin under {_WIRE_MODEL_DIR}/ does "
        f"not fail loudly -- it quietly stops existing on the wire. Keep the pair in step, or "
        f"give them separate names and a deliberate converter: " + "; ".join(drifted)
    )


def test_wire_mirror_detector_bites():
    """A dropped field and a wire-only field both fire; an exact pair stays clean."""
    domain = ast.parse("@dataclass(frozen=True)\nclass Link:\n    url: str\n    host: str\n    depth: int\n")
    matching = ast.parse(
        "from pydantic import BaseModel\nclass Link(BaseModel):\n"
        "    url: str\n    host: str\n    depth: int\n"
    )
    assert not _wire_mirror_drift(domain, matching)

    drifted = _wire_mirror_drift(domain, ast.parse("class Link(BaseModel):\n    url: str\n    host: str\n"))
    assert len(drifted) == 1, drifted
    assert "Link.depth" in drifted[0] and "dropped on the wire" in drifted[0], drifted

    drifted = _wire_mirror_drift(
        domain,
        ast.parse("class Link(BaseModel):\n    url: str\n    host: str\n    depth: int\n    score: float\n"),
    )
    assert len(drifted) == 1, drifted
    assert "Link.score" in drifted[0] and "nowhere internally" in drifted[0], drifted

    # A bare @dataclass and a subscripted BaseModel[str] base are the same two
    # decorators spelled the other way round; both must still be matched.
    assert _wire_mirror_drift(
        ast.parse("@dataclass\nclass Link:\n    url: str\n    host: str\n    depth: int\n"),
        ast.parse("class Link(BaseModel[str]):\n    url: str\n    host: str\n"),
    )
    # An unrelated name on both sides is not a pair -- a wire-only model and a
    # domain-only dataclass must not be reported against each other.
    assert not _wire_mirror_drift(
        ast.parse("@dataclass\nclass Link:\n    url: str\n"),
        ast.parse("class Other(BaseModel):\n    url: str\n    host: str\n"),
    )


def test_page_metadata_fields_are_produced_by_the_extraction_record():
    """Every ``PageMetadata`` field must be one the extractor can actually fill.

    The sibling mirror rule above pairs a domain *dataclass* with a wire model.
    PageMetadata's producer was a plain ``dict[str, Any]``, so it was in neither
    half of that pair and the whole record was unguarded: three fields were
    declared on the wire that no extractor had ever produced, and every consumer
    reading one got a default forever. That is a promise the model cannot keep,
    and nothing in the tree said so.

    Declaring the extractor's output as a record puts it in the pair, so this is
    the same rule reading a second kind of declaration -- ``total=False``
    TypedDict rather than a dataclass -- and the fix is to delete the unfillable
    fields, which is what a consumer would need anyway.
    """
    domain_path, domain_tree = file_tree(_PRODUCER_RECORD_MODULE)
    wire_tree = _wire_tree()
    assert _paired_names(domain_tree, wire_tree), (
        f"No declared record in {domain_path.name} shares a name with a response model under "
        f"{_WIRE_MODEL_DIR}/, so this rule compares nothing and passes. That is the failure "
        f"this rule exists to catch: PageMetadata's producer was an undeclared dict, which left "
        f"the pair absent rather than equal. Declare the extractor's output as a record that "
        f"keeps the wire model's name."
    )
    drifted = _wire_mirror_drift(domain_tree, wire_tree)
    assert not drifted, (
        f"The extractor's record and the response models under {_WIRE_MODEL_DIR}/ disagree about "
        f"A field declared on the wire and filled by nothing is always None on the wire -- a "
        f"promise the model cannot keep -- so delete it or teach {domain_path.name} to fill it. A "
        f"field the extractor fills and the model does not declare is dropped at the splat. "
        f"Each defect names itself: " + "; ".join(drifted)
    )


def test_producer_record_detector_bites():
    """The mirror rule reads a TypedDict exactly as it reads a dataclass."""
    record = ast.parse("class PageMetadata(TypedDict, total=False):\n    title: str\n    favicon: str\n")
    assert not _wire_mirror_drift(
        record, ast.parse("class PageMetadata(BaseModel):\n    title: str\n    favicon: str\n")
    )

    # A field the extractor fills and the model does not declare is dropped at the splat.
    drifted = _wire_mirror_drift(record, ast.parse("class PageMetadata(BaseModel):\n    title: str\n"))
    assert drifted == ["PageMetadata.favicon exists internally and is dropped on the wire"], drifted

    # ...and the reverse is a wire field that is always None, which is the defect
    # this record was declared to find.
    drifted = _wire_mirror_drift(
        ast.parse("class PageMetadata(TypedDict):\n    title: str\n"),
        ast.parse("class PageMetadata(BaseModel):\n    title: str\n    favicon: str\n"),
    )
    assert drifted == ["PageMetadata.favicon is declared on the wire and nowhere internally"], drifted


# --- An advertised payload key may not be dropped by its response model -------
#
# The two discovery endpoints build a dict by hand and hand it to FastAPI with a
# strict `response_model=`. Pydantic DROPS every key the model does not declare,
# so advertising a new block is not a type error and not a test failure -- the key
# stops existing on the wire while every test that calls the payload function
# directly stays green, because those tests never go through the model. This
# project has shipped that once: `crawl_timing` was advertised and absent from
# the endpoint, and was caught only by curling the live service.
#
# The gate above covers the other twin, a domain dataclass and its response
# model. This covers the shape that actually shipped the bug: a hand-written dict
# literal beside a model that is maintained by hand. Both have to be edited for
# the payload to be correct, and nothing checked that they agreed.
#
# The pair is declared as data so adding a third advertised endpoint is one line
# here, and so the real tree and the bites proof below exercise the same code.

_ADVERTISED_PAYLOADS = (
    ("app/discovery/capabilities.py", "capabilities_payload", "CapabilitiesResponse"),
    ("app/discovery/agent_hints.py", "agent_hints_payload", "AgentHintsResponse"),
)


def _advertised_keys(tree: ast.Module, function_name: str) -> set[str]:
    """Top-level string keys of a payload function's single returned dict.

    Asserts rather than degrading when the shape is not statically readable. A
    payload assembled across several statements defeats extraction, and quietly
    reporting "nothing dropped" for exactly the shape this gate cannot see would
    recreate the defect it exists to catch -- a detector that goes blind without
    saying so is the gap, not the fix.
    """
    function = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function_name),
        None,
    )
    assert function is not None, f"{function_name} not found; update _ADVERTISED_PAYLOADS"
    returns = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
    assert len(returns) == 1, (
        f"{function_name} must be a single `return {{...}}` so its advertised keys can be "
        f"read; found {len(returns)} return statements"
    )
    payload = returns[0].value
    assert isinstance(payload, ast.Dict), (
        f"{function_name} must return a dict literal so its keys can be compared against the "
        f"response model; got {type(payload).__name__}"
    )
    return {k.value for k in payload.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _undeclared_advertised_keys(
    payload_tree: ast.Module, wire_tree: ast.Module, function_name: str, model_name: str
) -> list[str]:
    """Advertised payload keys the response model does not declare."""
    model = next((n for n in wire_tree.body if isinstance(n, ast.ClassDef) and n.name == model_name), None)
    assert model is not None, f"{model_name} not found under {_WIRE_MODEL_DIR}/"
    declared = set(_declared_fields(model))
    return [
        f"advertised key {key!r} is dropped by {model_name}"
        for key in sorted(_advertised_keys(payload_tree, function_name) - declared)
    ]


@pytest.mark.parametrize(("payload_module", "function_name", "model_name"), _ADVERTISED_PAYLOADS)
def test_every_advertised_payload_key_is_declared_on_its_response_model(
    payload_module: str, function_name: str, model_name: str
):
    """An advertised key that the response model does not declare is dropped on the wire."""
    _, payload_tree = file_tree(payload_module)
    dropped = _undeclared_advertised_keys(payload_tree, _wire_tree(), function_name, model_name)
    assert not dropped, (
        f"{function_name} advertises keys that {model_name} does not declare, and "
        f"response_model={model_name} silently filters every undeclared key -- so they reach "
        f"the dict-returning tests and never the wire. Add the field to {model_name}: " + "; ".join(dropped)
    )


def test_advertised_payload_detector_bites():
    """An undeclared advertised key fires; an exact pair stays clean; a blind spot raises."""
    wire = ast.parse("class Capabilities(BaseModel):\n    service: str\n    crawl_timing: Timing\n")
    exact = ast.parse("def payload():\n    return {'service': 'x', 'crawl_timing': {}}\n")
    assert not _undeclared_advertised_keys(exact, wire, "payload", "Capabilities")

    drifted = ast.parse(
        "def payload():\n    return {'service': 'x', 'crawl_timing': {}, 'limits': {}, 'extra': 1}\n"
    )
    reported = _undeclared_advertised_keys(drifted, wire, "payload", "Capabilities")
    assert len(reported) == 2, reported
    assert "'extra'" in reported[0] and "dropped by Capabilities" in reported[0], reported
    assert "'limits'" in reported[1], reported

    # A declared field the payload never sends is not drift in this direction: the
    # model is the stricter of the two and a required field is caught by FastAPI.
    assert not _undeclared_advertised_keys(
        ast.parse("def payload():\n    return {'service': 'x'}\n"), wire, "payload", "Capabilities"
    )

    # A payload assembled statement by statement defeats static extraction, and
    # going quiet on that shape would be the very defect this gate exists to stop.
    assembled = ast.parse("def payload():\n    d = {}\n    d['service'] = 'x'\n    return d\n")
    with pytest.raises(AssertionError, match="dict literal"):
        _undeclared_advertised_keys(assembled, wire, "payload", "Capabilities")


# --- A result may not spell its own (status, classification) pair -------------
#
# app/crawl/result_helpers.py is the chokepoint for building results, and both
# of its terminal primitives call _enforce_status_classification first. That
# check is the whole point: it makes a result that asserts a status the taxonomy
# would re-derive differently for the same label unrepresentable, because the
# two axes are free parameters and nothing else holds them together.
#
# A caller that builds a result itself can sidestep all of it by naming both
# axes as literals. The pair it writes is legal on the day it is written, which
# is exactly why nothing reports it, and the day the taxonomy moves a default
# the result is quietly wrong with no failure anywhere. Deriving one axis from
# status_for_classification, or copying both off an existing result, keeps the
# property -- which is what the browser strategy does. Naming both is how the
# property gets lost.

_RESULT_CONSTRUCTORS = frozenset({"CrawlStrategyAttempt", "EngineCrawlResult"})


_RESULT_CHOKEPOINT_MODULE = "app/crawl/result_helpers.py"


def _literal_status_classification_pairs(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a result built from a hand-written (status, classification) pair."""
    if relative_posix(path) == _RESULT_CHOKEPOINT_MODULE:
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
        f"Build results through {_RESULT_CHOKEPOINT_MODULE}, or derive one axis with "
        "constants.status_for_classification, so _enforce_status_classification can see "
        "the pair"
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
        # And the chokepoint exemption is keyed on the module, not on a name: a
        # spelled pair anywhere else still fires even under the same function name.
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
            at=REPO_ROOT / _RESULT_CHOKEPOINT_MODULE,
        ),
    ),
)


def test_results_derive_their_status_from_the_taxonomy():
    """No record outside the chokepoint spells both taxonomy axes as literals."""
    RESULTS_DERIVE_THEIR_STATUS_FROM_THE_TAXONOMY.scan()


def test_result_status_pair_detector_bites():
    """A spelled pair fires; deriving either axis stays clean."""
    RESULTS_DERIVE_THEIR_STATUS_FROM_THE_TAXONOMY.check_bites()


# ---------------------------------------------------------------------------
# The content pipeline: one dispatch, one discovery call, one projection
#
# The browser and HTTP strategies each carried the same three-way extraction
# dispatch, the same offloaded post-fetch discovery call, and the same block of
# discovered-links result fields. All three now live in
# app/crawl/strategies/content_pipeline.py, and `_discovery_result_fields` is
# the one place a discovery envelope becomes result fields.
#
# The projection was a correctness risk rather than cosmetics: a field threaded
# into the HTTP result but not the browser one is a divergence between the two
# answers to the same page, and nothing in the tree can see it -- both results
# are individually well-formed. So the two sweeps below are tree-wide rather
# than file-scoped: a guard that only looked at the two modules it was written
# for would keep passing while a third strategy grew its own copy, which is the
# same empty-sweep failure the other chokepoint guards in this file were written
# against.
# ---------------------------------------------------------------------------

_CONTENT_PIPELINE_OWNER = frozenset({"content_pipeline.py"})


# Import-resolved, so an alias cannot slip past the sweep.
_EXTRACTION_ENTRY_POINTS = frozenset(
    {
        "app.crawl.extraction.extract_document",
        "app.crawl.extraction.extract_html",
        "app.crawl.extraction.extract_content",
        "app.crawl.extraction.extract_post_fetch_discovery",
        "app.crawl.extraction.normalized_extraction",
    }
)


# The terminal-result builders, not the model: result_helpers constructing
# EngineCrawlResult is where the discovered-links fields are DEFINED, which is
# a different thing from a call site spelling them.
_RESULT_BUILDERS = frozenset(
    {
        "binary_no_text_result",
        "engine_result_from_exception",
        "_failed_engine_result",
        "build_engine_result",
        "_skipped_engine_result",
        "_terminal_engine_result",
    }
)


_DISCOVERY_RESULT_FIELDS = frozenset(
    {"discovered_link_count", "discovered_links", "discovered_links_truncated"}
)


_DISCOVERY_PROJECTION = "_discovery_result_fields"


def _extraction_reach_past_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a use of a raw extractor outside the content pipeline.

    Keyed on the *name in load position* rather than on a call: the shape both
    strategies used was an offloaded extractor, ``asyncio.to_thread(extract_html,
    ...)``, where the extractor is an argument rather than a callee, and a
    call-keyed detector misses exactly that. Keyed on the use rather than the
    import because binding a name is not the duplication, dispatching on it is.

    ``self.extract_html(page)`` in the browser worker is a method on the
    renderer, not this function, so it resolves to a different name and is not
    an offender. A reference *within* extraction.py resolves to the bare name
    rather than the dotted one, which is how the owner of these functions is
    left out of its own sweep without being named in it.
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


_EXTRACTION_PROOF_PATH = REPO_ROOT / "app" / "crawl" / "strategies" / "_synthetic.py"


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
    excludes=SANCTIONED | _CONTENT_PIPELINE_OWNER,
    premise=_assert_owner_extraction_still_fires,
    bite=lambda: (
        # The offloaded extractor AND the bare extractor call are two dispatch
        # shapes, so a source carrying both is worth exactly two findings.
        Bites(
            "from app.crawl.extraction import extract_html, normalized_extraction\n"
            "async def f(decoded, final_url):\n"
            "    extracted = await asyncio.to_thread(extract_html, decoded, final_url)\n"
            "    return normalized_extraction(extracted)\n",
            "_run_extraction",
            count=2,
        ),
        Bites(
            "from app.crawl.extraction import extract_content as _ec\n"
            "def f(body, content_type, url):\n"
            "    return _ec(body, content_type, url)\n",
            "_run_extraction",
        ),
    ),
    clean=lambda: (
        # Routing through the chokepoint is the sanctioned shape.
        "from app.crawl.strategies.content_pipeline import _run_extraction\n"
        "async def f(body, final_url):\n"
        "    return await _run_extraction(extraction='full', content=body, decoded='')\n",
    ),
)


def test_extraction_is_dispatched_by_the_content_pipeline_alone():
    """The three extraction modes are dispatched in one module, not per strategy."""
    EXTRACTION_IS_DISPATCHED_BY_THE_CONTENT_PIPELINE_ALONE.scan()


def test_extraction_reach_past_detector_bites():
    """A re-spelled dispatch fires, offloaded and aliased; routing through it stays clean."""
    EXTRACTION_IS_DISPATCHED_BY_THE_CONTENT_PIPELINE_ALONE.check_bites()


def _respelled_discovery_result_fields_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag discovered-links result fields spelled at a result-builder call.

    ``**`` spread is the sanctioned shape and is not a keyword, so a strategy
    that routes the envelope through ``_discovery_result_fields`` reports
    nothing; a strategy that re-spells one field reports that field.
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
                    f"spread content_pipeline._discovery_result_fields instead",
                )
            )
    return offenders


DISCOVERED_LINK_RESULT_FIELDS_COME_FROM_THE_PIPELINE = Rule(
    name="discovered-link-result-fields-come-from-the-pipeline",
    find=_respelled_discovery_result_fields_offenders,
    summary=("project the discovery envelope through content_pipeline._discovery_result_fields"),
    excludes=SANCTIONED | _CONTENT_PIPELINE_OWNER,
    bite=lambda: (
        # The historical shape: the block spelled at the result call.
        Bites(
            "def build(post_fetch, error):\n"
            "    return build_engine_result(\n"
            "        discovered_link_count=post_fetch['discovered_link_count'],\n"
            "        discovered_links=post_fetch['discovered_links'],\n"
            "        error=error,\n"
            "    )\n",
            "_discovery_result_fields",
        ),
    ),
    clean=lambda: (
        # The sanctioned spread is not a spelling of individual fields.
        "def build(post_fetch, error):\n"
        "    return build_engine_result(\n"
        "        **_discovery_result_fields(post_fetch, classification_evidence=[], error=error),\n"
        "        next_page_url=post_fetch['next_page_url'],\n"
        "    )\n",
        # A discovered-links field that is not a result field is somebody else's.
        "def count(post_fetch):\n"
        "    return stats(discovered_link_count=post_fetch['discovered_link_count'])\n",
    ),
)


def test_discovered_link_result_fields_come_from_the_pipeline():
    """A discovery envelope reaches a result through one projection."""
    DISCOVERED_LINK_RESULT_FIELDS_COME_FROM_THE_PIPELINE.scan()


def test_discovery_result_field_detector_bites():
    """A re-spelled projection fires; the spread, and a non-result sink, stay clean."""
    DISCOVERED_LINK_RESULT_FIELDS_COME_FROM_THE_PIPELINE.check_bites()


def test_discovery_result_projection_has_exactly_one_definition():
    """The projection is one function, so it cannot be forked per strategy.

    Tree-wide for the same reason the sweeps above are: a second definition
    would satisfy every structural guard above while reintroducing the exact
    duplication they exist to prevent.
    """
    definitions = [
        offender(path, node.lineno)
        for path, tree in app_trees(exclude=frozenset())
        for node in functions(tree)
        if node.name == _DISCOVERY_PROJECTION
    ]
    assert len(definitions) == 1, f"the discovery projection was defined again: {definitions}"
    assert "app/crawl/strategies/content_pipeline.py" in definitions[0], definitions


# ---------------------------------------------------------------------------
# One declaration of what a result is
# ---------------------------------------------------------------------------


def _taxonomy_members(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


def literal_members(alias: object) -> set[str]:
    return set(get_args(alias))


def test_the_internal_and_wire_taxonomies_name_the_same_members():
    """The engine's result records and the wire enums agree, set for set.

    This is what lets the adapters convert instead of coercing. Reading an
    internal ``Literal`` into a wire ``StrEnum`` is a total function exactly when
    the two name the same members -- and that is not something a type checker can
    verify, because a ``Literal`` and a ``StrEnum`` are unrelated types and the
    enum constructor accepts any ``str``.

    It matters because the alternatives were both worse. The adapters used to
    wrap each conversion in ``try/except`` and substitute a fallback: an
    unmapped ``status`` became ``failed`` and an unmapped ``strategy`` became
    ``None``, both silently. That fallback is the shape of the bug this whole
    change set out to remove -- a producer that emitted something new and a
    response that answered anyway -- only one layer down from the result
    record. The conversions are now total, with no fallback to reach, and this
    is what keeps them total.

    All three axes are checked: status, classification, and strategy.
    """
    from app.constants import CrawlClassification, ResultStatus
    from app.models.crawl_status import CrawlStatus
    from app.models.crawl_strategy import CrawlStrategy
    from app.models.taxonomy import Classification, CrawlStrategyName

    for axis, internal, wire in (
        ("status", literal_members(ResultStatus), _taxonomy_members(CrawlStatus)),
        ("classification", literal_members(Classification), _taxonomy_members(CrawlClassification)),
        ("strategy", literal_members(CrawlStrategyName), _taxonomy_members(CrawlStrategy)),
    ):
        assert internal == wire, (
            f"the {axis} taxonomies disagree: "
            f"internal-only={sorted(internal - wire)} wire-only={sorted(wire - internal)}. "
            f"app/api/response_shaping.py converts this axis without a fallback branch, "
            f"which is only true while the sets are equal -- so add the member to both sides "
            f"in the same change."
        )
