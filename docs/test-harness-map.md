# Test harness module map

One line per `tests/_*.py` support module (docstrings are the source; this page
is a directory, not a tutorial — see `docs/testing.md` for the design "why").
79 modules as of this writing; add a row when adding a module.

## tests/ (top level)

| Module | Purpose |
| --- | --- |
| `_admission_doubles` | Admission test doubles shared by the capacity, scaling and policy gates. |
| `_admission_runs` | Recursive-crawl admission/task doubles shared across orchestration tests. |
| `_api_harnesses` | API test harnesses shared across the `api/` test modules. |
| `_app_factory` | The one way a test obtains the service's ASGI app (narrowing `main.app`). |
| `_asgi_request` | The one ASGI Request builder the body-cap suites share. |
| `_assertions` | DNS-resolver assertion helpers for the resolution tests. |
| `_async_harnesses` | Async harnesses: the `crawl_api` fixture, `serve`, key issuing, quota assertions. |
| `_browser_doubles` | One browser object replacing the ten once-rebound private methods. |
| `_browser_page_doubles` | Browser-page doubles for the render phases. |
| `_browser_pool_doubles` | Resource lifecycle doubles: renderers that record or gate their closes. |
| `_cache_fixtures` | Shared content-cache fixtures: scoped caches and watch doubles. |
| `_cffi_doubles` | cffi doubles: a recording session and a stream-shaped response. |
| `_chain_policies` | The one strategy-chain policy the crawl-side chain scenarios hand the chain. |
| `_ci_contract` | The shared CI-contract surface: paths, constants and config parsing. |
| `_classification_sets` | Classification test data shared by the taxonomy/classification modules. |
| `_clone_funnel_detectors` | AST detectors behind the clone-funnel sweep. |
| `_clone_funnel_table` | The clone-funnel funnel table (FUNNELS rows). |
| `_concurrency_probe` | Shared in-process concurrency-bound probing for the sync-crawl and search doors. |
| `_cookie_helpers` | Helpers for tests exercising the cookie jar's on-disk persistence. |
| `_cookie_retry_chain` | The one cookie-retry chain scenario the cffi-retry tests share. |
| `_crawl_runners` | Crawl runners: payloads, pages, chains and strategy entry. |
| `_dependency_overrides` | Shared FastAPI dependency-override installs (`bind_base_overrides`) for harness apps. |
| `_doc_pool_fixtures` | Document-extraction pool fixtures (pool + workers over scratch stores). |
| `_extraction_harnesses` | Extraction test harnesses shared across the extraction test modules. |
| `_extraction_helpers` | Test helpers for the extraction pipeline. |
| `_fetch_doubles` | The one import door for every fetch double (#314). |
| `_fetch_outcome` | What one fetch produced, typed across the queue (reason travels as a type). |
| `_fetcher_factories` | Sanctioned fetch fakes; definitions live in the sibling helper modules. |
| `_host_policy_entry_table` | The host-policy entry-point table, declared beside the tests that check it. |
| `_host_policy_probes` | Shared refusal probes: denied settings/policy, recorders, refused-result check. |
| `_http_fetch_doubles` | Shared HTTP fetch doubles for the transport edge tests. |
| `_http_test_helpers` | Small served-app helpers (tracker/settings wiring) for HTTP-door tests. |
| `_job_drivers` | Job-driver doubles: frontier/typed-state fakes for runner tests. |
| `_job_harnesses` | Job-runner harness: stores, fake engine seams, edge-triggered job-state waits. |
| `_job_store_doubles` | Job-store doubles whose subscription precedes the first read. |
| `_jscpd_gate_helpers` | The duplication gate wrapper, failing closed on thin scans. |
| `_lag_scheduling` | Loop-lag probe scripting (#190): scripted clock/sleep, `lagged_gate`. |
| `_lifespan_doubles` | Neutralise heavy lifespan teardown so a test can run the real lifespan. |
| `_lock_doubles` | Delegating lock doubles: observe one operation, inherit the rest. |
| `_lock_scope_walks` | Lock-scope detectors: no slow work lexically inside `with self._lock:`. |
| `_loop_liveness` | A liveness probe shared by the event-loop discipline tests. |
| `_metrics_budget_ledger` | Hermetic budget ledger for the metrics gates (#253). |
| `_parse_budget` | The lxml parse budget, bracketing `extraction.parse_count()`. |
| `_policy_factories` | Crawl-policy defaults, and the one way a test admits discovered links. |
| `_policy_harnesses` | An app over the crawl and policy routers with stores the test owns. |
| `_pool_harnesses` | Adapt a zero-argument fake factory to the `build(settings)` builder contract. |
| `_pool_ws_doubles` | Pool and websocket doubles shared across the `transport/` test modules. |
| `_process_doubles` | `multiprocessing.Process` stand-ins for the document-extraction pool. |
| `_profile_doubles` | A request-profile double whose value set flips after its first resolution. |
| `_quota_harness` | One keyed-service test client: per-request register, key admin, stores. |
| `_ratchet` | The ratchet funnel: budgets + collector in, deterministic assertions out. |
| `_recursive_harnesses` | Harnesses for driving `run_recursive_crawl` from a test. |
| `_render_producer` | Call the `/v1/render` producer the way an endpoint test needs to. |
| `_result_builders` | TypedDict/Unpack result builders for engine-result shapes. |
| `_scheduler_doubles` | Scheduler doubles, and the one way a test builds or drives a scheduler. |
| `_thread_fanout` | The N-way thread fan-out a concurrency test needs, written once. |
| `_timing_harnesses` | Zero-wall-clock instruments: no test sleeps to prove a timing claim. |
| `_transport_doubles` | Scripted httpx transport, and the genuine curl_cffi fetcher in a spawned interpreter. |
| `_validation_helpers` | Admit-or-None validation helpers for policy/model tests. |
| `_walk_assertions` | Detector assertions: bite once, fire once, stay clean. |
| `_walk_membership` | Membership checks: literals, models and vocabularies. |
| `_walk_primitives` | Walk primitives: scopes, parents, line numbers and offenders. |
| `_walk_readers` | Call, assignment and function readers over one tree. |
| `_walk_resolution` | Callee and import resolution: names behind calls. |
| `_walk_trees` | File-tree walks with exemptions and offender sweeps. |
| `_watch_harnesses` | The watches API over a test's own store (real router, overridden stores). |
| `_webhook_doubles` | Webhook delivery staged as a real httpx exchange (fake second-httpx). |
| `_worker_doubles` | Worker dispatch: the two shapes `call_worker` accepts, and the lie it names. |

## tests/invariants/

| Module | Purpose |
| --- | --- |
| `_armed_detectors` | The hand-rolled detectors the arming audit must also sweep. |
| `_encoding_sets` | The encoding-name sets the decodable-header invariant reads. |
| `_env_knobs` | The environment-knob walk the documentation gates share. |
| `_proof_paths` | Shared proof-site paths and tree scopes for the ownership invariant modules. |
| `_registry_machinery` | The property-registry machinery the registry detector modules share. |
| `_shared` | The helpers more than one themed invariant module needs (`assert_trees_clean`). |
| `_store_receivers` | Receiver resolution vocabulary for the on-the-loop store detector. |
| `_surface_ratchet` | The public-surface ratchets the public-surface gates read. |

## tests/api/ and tests/mcp/

| Module | Purpose |
| --- | --- |
| `api/_asgi_base` | The ASGI endpoint base: client + base_url fixtures shared across API tests. |
| `api/_offload_doubles` | Loop-guard store doubles and the off-loop call helper. |
| `mcp/_tool_calls` | Shared keyed tools/call harness (`KeyedToolCaller`, `tool_call`, `META`). |
