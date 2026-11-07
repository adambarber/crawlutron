# Gate-exemption inventory (2026-10-15) — every exclusion from a mechanical gate

Goal: remove the exemption wiring itself; end state is a gate that accepts the
whole tree without a hand-maintained exception list, table, or ledger row.

## 1. Budget tables — SEALED AT ZERO (verified against the live gates, 2026-11-07)

Every budget table below has reached its terminus: each budget died in the same
commit as the work that earned the drop, per the repo's own doctrine, leaving
unconditional zero-assertions (or an honest current value where a metric still
measures). The values below are what the live gates enforce NOW.

| Gate | File | Current unconditional state |
|---|---|---|
| docstring length (3-line) | tests/invariants/test_comment_length.py | zero over-limit docstrings across all three trees; no budget, no exemption (`test_no_overlong_docstring_anywhere`) |
| comment runs (2-line) | same file | zero long runs across all three trees; no budget, no exemption (`test_zero_long_comment_runs_everywhere`) |
| file size (500) | tests/invariants/test_file_size.py | 0 / 0 / 0 |
| function length | tests/test_metrics_budget.py | sealed, no budget row; 60-line ceiling is the metric definition (`_METRIC_LIMITS`); app, tests and scripts zero tests assert no over-long functions |
| abc size | same file `_BUDGETS` | app 0, scripts sealed at zero, tests 0 |
| largest class | same file `_BUDGETS` | app 345, scripts 0, tests 291 (the honest worst-class measure, still ratcheted) |
| block length | same file | sealed at zero (app, scripts, tests), no budget row |
| typing: undeclared records / bare Any | tests/test_typing_budget.py | zero everywhere: app tree has NO budget table (unconditional terminus), tests sealed at `{undeclared record: 0, bare Any: 0}`, scripts sealed at zero |

## 2. Private-import ledger — ELIMINATED (2026-10-20, #190)

A uniform private-import wait-list once held every sanctioned reach (row tables
in tests/ feeding a `Kind`/`Declaration` shape with a both-ways correspondence
gate). Every row was resolved by design -- ownership moves and genuine-API
promotions, never test-accommodation -- so the ledger fell to zero. The row
tables, the `Kind` union, the `Declaration` type and the two correspondence tests
were deleted with the last row. What remains is enforcement only: the
app-to-app cross-module weld detector, the suite/scripts private-name detector,
and the conftest door detector all still bite, now with zero exemptions.

## 3. Rule-level skip/excludes lists — ELIMINATED or REASONED (2026-10-28, wave 1c)

Every list below was probed red-first (dropped locally; the rule fired or the
file did not exist). Two were eliminated outright; the rest are live owner
exemptions, each now carrying its one-line reason at the site:

| File | Exemption | Verdict |
|---|---|---|
| test_dedup_construction.py | `_FAKE_BUILDER_SANCTIONED`, `_ROBOTS_SCANNER_SANCTIONED` | reason written (sanctioned builders / scanner itself) |
| test_dedup_filters.py | `_JS_ENDPOINTS_FILTER_SANCTIONED` | ELIMINATED (dead name; rule excludes validation.py inline) |
| test_dynamic_regex.py | `SANCTIONED` = {safe_regex.py} | reason lives at the definition (tests/_walk_trees.py) |
| test_bounds.py | discovery/bounded_io/decompression/chokepoint skips | reasons written (each names its owner) |
| test_async_discipline.py | `_READINESS_GATE_EXEMPT` | reason written inline (owner gate); list deleted |
| test_handle_ownership.py | skip {ids.py} | reason written (the mint itself) |
| test_public_surface.py | `_MODELS_CONCERN_MODULES` | reason pre-existing + existence guard test |
| test_result_errors.py | `ENVELOPE_SANCTIONED`, `_TERMINAL_RESULT_SANCTIONED` | reasons written (envelope factory / terminal sinks) |
| test_result_pipeline.py | `_CONTENT_PIPELINE_OWNER` | reasons written (dispatch owner) |
| test_result_shape.py | content_cache.py, `_ENVELOPE_ADAPTER_EXEMPT` | reasons written (envelope owner / adapters) |
| test_taxonomy.py | `_TRANSIENCE_HOME_SANCTIONED` | ELIMINATED (constants.py no longer exists) |
| test_taxonomy.py | `_ERROR_CHOKEPOINT_SANCTIONED`, `_POOL_EXHAUSTION_CATCH_SANCTIONED` | reasons written (chokepoint / 503 + worker guard) |

## 4. Doc-reference exclusions

tests/invariants/test_doc_contracts.py `_DOC_REFERENCE_EXCLUSIONS` = 4 dated
records (CHANGELOG, reviews, archived plan). These are deliberate historical
records, not gate exemptions; keep as reasons, they are already the "named with
reason" shape.

## 5. Nested-context nets (not exemption lists, but same rot risk)

- test_doc_contracts.py `_SKIP_DIRECTORIES`
- test_async_discipline.py conftest-thread-safety net
- test_public_surface.py `sizes = {}` per-file holes

## Priority — CLOSED (2026-11-07)

Every priority item below has landed; the list is kept as history only.

1. **Metrics gate → zero** — DONE: budgets sealed; the remaining largest-class
   values (345/291) are the live ratchet, not exemptions.
2. **Typing budget 204 → 0** — DONE: zero budgets across all trees.
3. **Private-import ledger — done** (machinery + rows removed in #190).
4. **Docstring budget 1677 → 0** — DONE: zero across all trees, no budget.
5. **Rule skip/excludes listed above** — DONE: eliminated or reason-encoded (§3).
6. **safe_regex / validation.py / bounds owners** — resolved in wave 1c (§3).