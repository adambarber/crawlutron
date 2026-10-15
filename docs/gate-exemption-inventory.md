# Gate-exemption inventory (2026-10-15) — every exclusion from a mechanical gate

Goal: remove the exemption wiring itself; end state is a gate that accepts the
whole tree without a hand-maintained exception list, table, or ledger row.

## 1. Budget tables (the biggest piles)

| Gate | File | Current value | End state |
|---|---|---|---|
| docstring length (3-line) | tests/invariants/test_comment_length.py `_DOC_BUDGETS` | app 546 / tests 1098 / scripts 33 (≈1,677) | 0 across trees |
| comment runs (2-line) | same `_BUDGETS` | 0 / 0 | done |
| file size (500) | tests/invariants/test_file_size.py `_BUDGETS` | 0 / 0 / 0 | done |
| function length | tests/test_metrics_budget.py `_BUDGETS` | 30 offenders | 0 |
| abc size | same | 5 | 0 |
| largest class | same | 369 (255+ line class still exists) | remove metric or split class |
| block length | same | 11 | 0 |
| typing: undeclared records | tests/test_typing_budget.py `_BUDGETS` | 204 | 0 |

## 2. Private-import ledger (15 rows, each a sanctioned weld)

tests/_private_import_rows_a.py (11) + _private_import_rows_b.py (4).

- SEMAPHORE (#191): `_build_async_crawl_semaphore`, `_async_crawl_semaphore`,
  `_async_crawl_semaphore_loop` — own the state in the runner.
- CHOKEPOINT_STATE (whole-suite state): `job_lifecycle._tasks`,
  `job_store._Expected`, `registrable_domain._PSL`.
- LIFECYCLE (app-side weld): `pooled_clients._drain_pending_client_closes`.
- SUBSTITUTED_COLLABORATOR (network/process seams): `fetchers._release_stream_noblock`.
- SUBSTITUTED_CAP (config caps): `sqlite_store._BUSY_TIMEOUT_MS`,
  `sqlite_store._WAL_RETRY_SECONDS`.
- UNREVIEWED: `pinned_transport._http_413` (app), `robots._RobotsPathMatcher`,
  `robots._robots_pattern_to_regex`, `_remember_memory_locked`,
  `BROWSER_COOKIES_BY_DOMAIN._lock`, `main._dependencies`.

## 3. Rule-level skip/excludes lists

| File | Exemption | Rows |
|---|---|---|
| tests/invariants/test_dedup_construction.py | `_FAKE_BUILDER_SANCTIONED` = {_fetcher_factories.py, _fetch_doubles.py}; `_ROBOTS_SCANNER_SANCTIONED` | 2 lists |
| tests/invariants/test_dedup_filters.py | `_JS_ENDPOINTS_FILTER_SANCTIONED` = {validation.py} | 1 |
| tests/invariants/test_dynamic_regex.py | `SANCTIONED` = {safe_regex.py} (tests/_walk_trees.py) | 1 |
| tests/invariants/test_bounds.py | `bounded_io.py`, `discovery.py`, `_DECOMPRESS_SANCTIONED` = {decompression.py}, `_DISCOVERY_CHOKEPOINT` skip | 4 |
| tests/invariants/test_async_discipline.py | `_READINESS_GATE_EXEMPT` = {main_helpers.py, endpoint.py (PENDING)} | 2 |
| tests/invariants/test_handle_ownership.py | skip {app/storage/ids.py} | 1 |
| tests/invariants/test_public_surface.py | `_MODELS_CONCERN_MODULES` = {taxonomy.py, serialization.py} | 2 |
| tests/invariants/test_result_errors.py | `ENVELOPE_SANCTIONED`(+exceptions.py), `_TERMINAL_RESULT_SANCTIONED` = 3 | 2 lists |
| tests/invariants/test_result_pipeline.py | `_CONTENT_PIPELINE_OWNER` | 1 |
| tests/invariants/test_result_shape.py | `content_cache.py` owner + `SANCTIONED` | 1 |
| tests/invariants/test_taxonomy.py | `_TRANSIENCE_HOME_SANCTIONED`, `_ERROR_OK`, `_POOL_EXHAUSTION_CATCH_SANCTIONED` = 3 | 3 |

## 4. Doc-reference exclusions

tests/invariants/test_doc_contracts.py `_DOC_REFERENCE_EXCLUSIONS` = 4 dated
records (CHANGELOG, reviews, archived plan). These are deliberate historical
records, not gate exemptions; keep as reasons, they are already the "named with
reason" shape.

## 5. Nested-context nets (not exemption lists, but same rot risk)

- test_doc_contracts.py `_SKIP_DIRECTORIES`
- test_async_discipline.py conftest-thread-safety net
- test_public_surface.py `sizes = {}` per-file holes

## Priority (reward × reachability)

1. **Metrics gate → zero** (function length 30, abc 5, block 11 are counts;
   largest class 369) — one agent does the whole file plus class split.
2. **Typing budget 204 → 0** — substantive type work, one agent.
3. **Private-import ledger 15 → 0** — promote/dehome per row (see #190/#191).
4. **Docstring budget 1677 → 0** — large mechanical wave, agent per tree.
5. **Rule skip/excludes listed above** — structural encoding per list.
6. **safe_regex / validation.py / bounds owners** — fold the sanctioned file
   into the rule shape or delete the file.