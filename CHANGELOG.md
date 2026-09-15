# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Performance
- **Concurrent robots screens (throughput #1)** (app/crawl/recursive.py,
  app/crawl/engine.py): the recursive batch screen, discovered-link screen,
  and the non-recursive batch screen now `asyncio.gather` their
  `robots_allows`/`robots_crawl_delay` checks instead of awaiting them one
  URL at a time. Same closures, same `robots_cache` chokepoint and SSRF
  semantics; in-batch duplicates keep visit-once behavior.
- **LLM metadata off the event loop (throughput #2)**
  (app/api/response_shaping.py): new additive `aformat_crawl_item` /
  `abuild_unified_result` / `abuild_unified_crawl_response` offload the
  per-item lxml metadata parse with `asyncio.to_thread` (items gathered
  concurrently) and are now used by the async runner, render, webhook, MCP,
  and job-runner paths. Sync shapers are unchanged. `crawl_endpoint.py` is
  owned by another stream and keeps the sync call until it adopts the seam.

### Added
- **Web search endpoint** (`POST /v1/search`): discover public URLs from a
  query via an operator-configured provider. Provider abstraction ships
  Tavily, Brave, and Serper (keyed) plus a keyless DuckDuckGo fallback;
  `BROWSER_CRAWLER_SEARCH_PROVIDER=auto` activates the first provider whose
  `*_API_KEY` is set. Result URLs pass the same public-URL/SSRF validation as
  crawl targets; provider failures surface as `502` and a selected-but-
  unconfigured provider as `503` naming the missing variable. Search metrics
  are exposed on `/metrics` (`crawler_search_*`).
- **MCP 2026-07-28 endpoint** (`POST /mcp`): stateless streamable-HTTP server
  exposing `search`, `crawl`, `map`, and `render` as MCP tools. Implements the
  stateless protocol (no `initialize` handshake): `server/discover`,
  `tools/list`, `tools/call`, per-request `_meta` validation, and
  header/body validation for `MCP-Protocol-Version` / `Mcp-Method` /
  `Mcp-Name`, with Origin (DNS-rebinding) rejection. Tools delegate to the
  same handlers as the REST endpoints, so all four strategies and every
  extraction method are available over MCP.
- **Discovery payloads** now advertise search + MCP: `/v1/capabilities` gains
  typed `search` and `mcp` capability blocks plus `web_search`/`mcp` feature
  flags; `/v1/agent-hints` and `/.well-known/crawler-service` link the new
  endpoints. Discovery routers now read settings via `Depends(get_settings)`
  instead of closing over import-time values.
- **Removed compatibility code**: `app/main.py` backward-compat exports
  (`_format_crawl_item`, `_render_result_to_unified`, `build_unified_crawl_response`)
  and the accepted-but-ignored `HttpCache(db_path=...)` parameter are gone;
  `/v1/render` now uses the injected settings like every other endpoint.

### Security
- **Thread-safety on `_DNS_CACHE`** (app/crawl/strategies/http.py): the
  module-level DNS resolution cache is now guarded by a `threading.Lock`,
  fixing a real concurrency bug where `_resolve_ips` (called from both
  the event loop and worker threads via `asyncio.to_thread`) could
  corrupt state or drop entries.
- **Thread-safety on `robots_cache`** (app/crawl/engine.py): the
  module-level robots.txt cache is now guarded by `_ROBOTS_CACHE_LOCK`.
  Concurrent reads, the `_purge_robots_cache` sweep, and the
  `_cap_robots_cache` eviction are all atomic.
- **Per-request cookie namespace** (app/crawl/strategies/http.py,
  app/crawl/engine.py): multi-tenant callers can now set
  `request_profile.cookie_namespace` on each crawl. Cookies are keyed
  by `(namespace, host)` so concurrent calls from different tenants
  cannot leak cookies through the shared `_GLOBAL_COOKIE_JAR`.
- **Body-size guard** (app/main.py): request bodies are capped at 16 MiB
  via an ASGI middleware that wraps `receive()`. A malicious caller
  can no longer exhaust memory with an oversized JSON before pydantic
  validation runs.
- **CSV injection guard** (app/output_formats.py): cells in attacker-
  influenced columns (`error`, `classification`, `status`) whose first
  character is `=`, `+`, `-`, `@`, TAB, or CR are prefixed with a
  single quote so spreadsheet apps do not execute them as formulas.
- **storage_state path allowlist tightened** (app/crawl/strategies/
  browser.py): only the operator-controlled `settings.cache_dir` is
  accepted. The previous `/tmp` fallback (world-readable on most hosts)
  is removed.
- **Size guards on HTML processing** (app/classify.py,
  app/crawl/firecrawl_format.py): HTML passed to the noise-stripping
  regex and the lxml-based metadata extractor is truncated to 4 MiB
  before processing, capping the worst-case RAM exposure of the
  classification and metadata paths.

### Hardening
- **Bounded `_PENDING_CLIENT_CLOSE`** (app/crawl/strategies/http.py):
  the async-pending client close list is now bounded at 32 entries with
  a `threading.Lock`, preventing unbounded growth across a stream of
  rapid event-loop changes.
- **`_ResultSpill` FD-leak fixed** (app/crawl/engine.py): the constructor
  now closes the raw `mkstemp` fd if `fdopen` fails; `close()` deletes
  the auto-created spill file; `_gc_stale_spills` failures are non-fatal.
- **`lifespan(None)` smell removed** (app/main.py): the lifespan handler
  is now an `@asynccontextmanager` directly, removing the misleading
  `lifespan=_lifespan(None)` indirection.

### Round 3 hardening (from REVIEW_ROUND3.md)
- **Body-size middleware: keep-alive poisoning fixed** (app/main.py):
  truncated requests now get ``Connection: close`` on the response and
  the receive() wrapper continues draining `original_receive()` until
  `more_body=False`, so a single oversized request cannot poison the
  underlying keep-alive socket for subsequent legitimate requests
  (REVIEW_ROUND3.md C1/C4).
- **Body-size middleware: per-chunk trailers preserved** (app/main.py):
  the capped receive() now merges `{**message, ...}` rather than
  `dict(message)` so ASGI per-chunk `headers` survive truncation
  (REVIEW_ROUND3.md C3).
- **`_PENDING_CLIENT_CLOSE` cap releases sockets synchronously**
  (app/crawl/strategies/http.py): the dropped client now has its
  synchronous `httpx.AsyncClient.close()` invoked before being
  released, so the httpcore connection pool is reclaimed even when
  no event loop is running. Dedupe check and cap check are now
  atomic under the same lock acquisition
  (REVIEW_ROUND3.md B1/B2/B3).
- **`_BROWSER_RAM_FRACTION` import fixed** (app/crawl/strategies/
  browser_pool.py): the symbol was referenced but not imported,
  which only surfaced when the lifespan ran (TestClient,
  uvicorn startup). The import is now correct.
- **`coverage_helpers.py` import fixed** (tests/coverage_helpers.py):
  `BROWSER_HEADLESS`, `EXTRACTION_DOCUMENT`, and `EXTRACTION_TRAFILATURA`
  were imported from `app.crawl.engine` but actually live in
  `app.constants`. The wrong import caused 27 gap-coverage test
  modules to fail collection. The constants now come from
  `app.constants` directly.

### Open-source extraction (adversarial review)
- **`local_service_logging` shim promoted to module** (app/logging_config.py):
  the parent-monorepo dependency on `local_service_logging` (imported
  via `try/except ImportError`) is replaced with a standalone
  `app.logging_config.configure_service_logging` helper. The new
  helper emits JSON-line records with rotating file output and is
  importable from any clone without the parent monorepo tree.
- **`shared/python` path references removed** (tests/conftest.py,
  scripts/probe_session.py): the `SHARED = ROOT.parent / "shared" /
  "python"` injection that tried to add the parent monorepo's shared
  helpers to `sys.path` is gone. The suite and the probe now rely on
  the package's own `app.*` modules.
- **`_ResultSpill.close()` double-unlink fixed** (app/crawl/engine.py):
  the auto-cleanup branch was unlinking the owned temp file twice;
  the duplicate `os.unlink` is removed (one happy-path unlink is
  enough).
- **SSRF env var parsing centralized** (app/validation.py,
  app/crawl/strategies/http.py, app/main.py): every
  `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS` check now goes through
  `app.validation.parse_bool_env`, which treats `1`/`true`/`yes`/`on`
  as truthy (case-insensitive). The previous `== "1"` literal in
  three files agreed on the value but rejected `"true"`/`"yes"`/`"on"`.
- **Browser-pool `None` guard with URL validation** (app/main.py):
  `_execute_crawl` now raises a clean 503 when the pool is None AND
  the request needs a browser step, after first validating the URL
  so an invalid URL still surfaces as a 422. Requests that only use
  HTTP strategies are unaffected by the pool state.
- **Hardcoded `/app/*` paths portable** (app/settings.py): the
  `log_file_path`, `cache_dir`, and `cookie_jar_path` defaults are
  now relative (`./logs/...`, `./cache/...`) so a fresh clone works
  without an `/app/` layout. Parent directories are created on
  startup.
- **Identity strings normalized** (app/crawl/strategies/http.py,
  app/crawl/engine.py, app/main.py, app/constants.py): the
  `dev-stack-browser-crawler/0.2 (+https://local.dev-stack/crawler)`
  User-Agent and the `dev-stack-browser-crawler` token are replaced
  with `DEFAULT_USER_AGENT` and `USER_AGENT_TOKEN` constants sourced
  from `app.constants`.
- **`worcesterma.gov` defaults replaced** (scripts/probe_session.py,
  scripts/e2e_verify.py): the `BROWSER_CRAWLER_PROBE_TARGET` default
  is now `https://example.com/`.
- **Webhook delivery paths consolidated** (app/main.py): the
  IP-pinned delivery logic now lives in `_deliver_pinned_webhook`
  and is shared by both `_post_webhook` and `_run_and_deliver_webhook`,
  closing the TOCTOU window between endpoint validation and
  delivery.
- **`_PENDING_CLIENT_CLOSE` lock released before close** (app/crawl/
  strategies/http.py): the synchronous `client.close()` call on the
  dropped oldest client now runs outside `_PENDING_CLIENT_CLOSE_LOCK`
  so the close path cannot block other callers.
- **Cookie-namespace default documented** (app/crawl/strategies/
  http.py): the docstring on `_COOKIE_NAMESPACE` now states the
  multi-tenant opt-in requirement explicitly so silent cross-tenant
  cookie leakage is harder to cargo-cult.

### Round 7 hardening (adversarial review + jscpd duplication audit)
- **Frontier carries only validated URLs (H1)** (app/api/job_runner.py,
  app/api/response_shaping.py): whole-site job expansion enqueued the raw
  discovered `url` href while the SafeUrl-validated `normalized_url` sat
  unused one field away, so the frontier and the visited dedupe keyed on
  different strings for the same page. New `frontier_link_url()` chokepoint
  returns only the validated field; an AST drift detector bans raw-href
  frontier sinks tree-wide.
- **Pinned-URL unification (H4/H5)** (SafeUrl.connect()): every
  pinned-IP composition (HTTP fetcher, document extractor, webhook
  delivery) routes through one chokepoint with IPv6 bracketing and
  per-hop re-validation; `final_url`/cookies report the logical URL, not
  the pinned IP. Drift invariant requires every `SafeUrl.parse` site to
  commit to a `pin` decision.
- **Env-core consolidation (H1-OSS)**: all `BROWSER_CRAWLER_*` numeric
  reads (including the 25 in `CrawlerSettings.from_mapping`) flow through
  `get_env_int/float`/`get_mapping_int/float` — bad strings can never
  reach arithmetic; the bool parser fails closed.
- **Capped webhook response drain (M3/L4)**: webhook delivery drains via
  `read_capped_async` (10 MiB) instead of unbuffered `client.post` bodies.
- **Single scope matcher (M10)**: engine/recursive's duplicated
  `_scope_allows` deduped into `app/crawl/scope_match.py`; one definition
  enforced by drift test.
- **Result shaping cannot drift (jscpd #1)**: every
  `UnifiedCrawlResponse`-with-results path (async runner, webhooks, both
  crawl_endpoint branches, render, MCP `tools/call`) now flows through
  `build_unified_crawl_response`; REST, job, webhook, and MCP payloads
  cannot diverge (#1715). The webhooks DI seam shrank 7 to 5 globals —
  shaper hooks existed only to be bypassed.
- **ContentCache mirror twins collapsed (jscpd #2)**: one locked probe and
  one locked store replace four hand-copied blocks. The twins had been
  hiding three real bugs: `aget` cold reads skipped the RAM-mirror entry
  cap (unbounded growth), `aset` mirrored before the disk write so a
  failed write left a stale mirror serving an unpersisted value, and the
  sync `get` never warmed the mirror the async path populated.
- **Error envelope factory (jscpd #3)**: 18 hand-rolled
  `{code, message, details}` dicts across 8 endpoint files replaced by
  `error_envelope`/`http_error`. Wire-visible effect: three error bodies
  that silently omitted `details` now always carry it (contract already
  documented it); the triplicated browser-pool 503 message is one
  constant. Envelope literals outside the sanctioned module fail CI.
- **One search-result normalization contract (jscpd #4)**: the
  Tavily/Brave/Serper parse loops — verbatim copies differing only in key
  names — collapsed into `_collect_results`. Behavior byte-identical; the
  risk retired is one provider's SSRF-drop or dedupe rule silently
  drifting from its siblings. Tree-wide detector keeps the loop in one
  place.
- **One robots.txt line-scanner (jscpd #5)**: crawl-delay, rules, and
  sitemaps each re-rolled the BOM/comment/`partition(":")` walk plus the
  RFC-9309 group accumulator, and had already drifted on the empty-value
  gate. `app/crawl/robots_parse.py` is the single tokenizer + selector;
  consumers supply only their "which directives do I accept" predicate.
  Shipped selection semantics (matched-group-is-final, invalid-delay falls
  back to star, agents accumulate across non-consumed lines) pinned by
  characterization tests. Detector bans a second `lstrip(BOM)+splitlines()`
  scan.
- **One sanctioned fetch fake (jscpd test-side)**: the 25 hand-built
  `HttpFetchResult(...)` fixtures across 14 test files now come from
  `_testkit.make_fetch`, which grew `headers`/`decompression_failed`
  passthroughs. No assertion changed; the win is that the next contract
  field lands in one builder instead of rippling through every fixture.
  Detector fails CI on a test that re-inlines the dataclass.
- **Decompression cap is mandatory (H2 closure)** (app/crawl/
  decompression.py, app/crawl/strategies/http.py): `max_output_bytes` was
  an optional keyword and three production paths called it without one —
  the uncapped `gzip.decompress`/`zlib.decompress`/`brotli.decompress`
  branches let a compressed bomb expand unbounded. The cap is now a
  required keyword and the uncapped branches are deleted; the defensive
  re-decompress in the HTTP strategy and the robots discovery read pass
  the policy cap and `DISCOVERY_BODY_MAX_BYTES` respectively. `_decode_body`
  lost its headers-triggered internal decompression entirely — decoding is
  downstream of the chokepoint, not a second decompression seam. AST
  detector bans any production decompress call that omits the bound.
- **One handle-minting primitive (H4 residue)** (app/crawl/ids.py):
  `new_request_id()`/`new_job_id()` replace the four re-inlined
  `uuid4().hex[:12]` mints (engine, crawl_endpoint, render) and job_store's
  raw hex. The 12-hex handles are the *only* lookup credential for
  `GET /v1/crawl/{request_id}`, so HIGH #9's "avoid brute-forceable short
  IDs" policy now governs them: 48 → 128 bits of entropy on the wire.
  The `crawl_` prefix and shape are unchanged, so no test assertions moved.
  AST detector bans any `uuid4()` call or `.hex` slice outside `ids.py`.
- **Batch-level failure accounting (job_runner)**: the per-item `failed`
  counter is cumulative across a batch, but its stats merge ran inside the
  loop — every item re-added the running total, inflating `total_failed`
  quadratically (2 failures reported as 3). The merge moved outside the
  loop; a two-failure execution test pins the count.
- **Await-in-lock detector stops being name-shy** (tests/
  test_safety_invariants.py): the M3 sweep matched only names containing
  "lock", so `self._mutex = threading.RLock()` — and the uppercase
  `_HTTP_CLIENT_INIT_LOCK`/`_PENDING_CLIENT_CLOSE_LOCK` in the HTTP
  strategy — escaped it. Matching is now case-insensitive stems
  (lock/mutex/guard) plus any name bound to a threading lock-family
  constructor in the module; bite test covers renamed, bound, and
  false-positive directions.
- **Env alias escape closed (H1 family)** (app/resource_pressure.py,
  tests/test_env_regression.py): `derive_browser_pool_size` read
  `BROWSER_CRAWLER_BROWSER_POOL_SIZE` raw and wrapped `int()` in a
  try/except two statements after the `.get()` — invisible to the
  coercion-shape detector. The value now comes through `get_env_int`, so
  a non-positive or blank override falls through to derivation instead of
  clamping to 1 (pinned by parametrized behavior tests; garbage now also
  logs). The detector gains shape 3: any `int`/`float` of a name assigned
  directly from `os.environ.get`/`getenv`/subscript, bite-proven.

### Round 8 hardening (codebase-review fix stream)
- **Sync `get()` honors the mirror lock contract (M1)**
  (app/crawl/content_cache.py): `get()` warmed the RAM mirror outside
  `self._lock` while `aget()` held it, so the multi-step mirror/index store
  raced concurrent writers. The store now runs under the lock on both paths;
  a regression test pins the contract.
- **One failed item counts once (M3)** (app/api/job_runner.py): an item that
  failed `build_unified_result` AND carried `status=="failed"` incremented
  the batch `failed` counter twice. The status-failed count now applies only
  to built items; a dropped item counts exactly once either way.
- **Dead code in `BoundedTTLCache.get_stale` (H3)** (app/bounded_ttl.py):
  a stale `peek` docstring + `return` sat unreachable after the method's real
  returns (the file always parsed — severity was hygiene, not HIGH). Deleted.
- **Bounded cookie jar (R1)** (app/crawl/strategies/http.py): the
  module-global jar never evicted per-`(namespace, host)` slots. Storage is
  now an `OrderedDict` with an LRU cap (`_COOKIE_JAR_MAX_HOSTS = 1024`,
  refreshed on write and read); regression tests pin the bound and the
  read-refresh recency.
- **Batch URLs dedupe exact duplicates (wire-visible)**
  (app/crawl/payload_helpers.py): `_payload_urls` returned the list as-is,
  so identical URLs in one request each ran the full engine. Exact-duplicate
  strings now collapse to their first occurrence (order-preserving) at the
  chokepoint both `crawl_request` and `render_request` share, and the
  batch-size check counts unique URLs. Wire effect: a duplicated URL yields
  one result instead of N. The two render tests asserting the old
  no-dedup counts were updated to the new contract.
- **O(1) frontier membership (M2)** (app/api/job_runner.py): dedup scanned
  the persisted frontier list per link (quadratic on wide crawls). A
  transient membership set mirrors the list — rebuilt on first use after
  every store load, synced by `_pop_frontier`/`_frontier_add` — while the
  list stays the crash-safe source of truth. A 25-link fan-out test pins the
  multi-batch contract.
- **Map reads links through `frontier_link_url` (L1)** (app/map.py,
  app/api/response_shaping.py): the site-map had its own link-URL reader
  with a raw-href fallback. It now uses the validated-`normalized_url`
  chokepoint (widened to attribute-held `DiscoveredLink` dataclasses — same
  validated field, raw href never consulted). Existing map/job/MCP tests
  pin the preserved shapes.
- **Shared test drive mechanics (jscpd test-side)** (tests/_testkit.py,
  tests/api/): the `/v1/crawl` payload builder and the drive-a-job-to-
  settled prologue were hand-copied per file. New `_testkit.make_crawl_payload`
  and a local `_run_job_to_completed` prologue own them; call sites pass
  only their intentional variants. Zero assertions changed.
- **Remaining test-side duplicates consolidated (jscpd)** (tests/):
  per-file setup factories folded into one home each — `_testkit.pinned_renderer`
  (Host-override navigation), `_testkit.provider_results` (keyed-provider
  response bodies), `_testkit.extract_function_body` (source-fingerprint
  assertions); local helpers dedupe the webhook delivery DNS-pin prologue and
  the MCP `_tool_call` invocation; LRU/recursion/scope/admission/extraction
  test prologues share one builder. One clone pair is kept by design:
  the `render_router`/`search/router` “pure router” boilerplate is the
  idiomatic FastAPI file shape — a shared factory would obscure the trivial
  wiring, so the convention stays.

### Round 9 hardening (adversarial review)
- **Bound the robots Crawl-delay politeness floor (M1)**
  (app/crawl/robots_parse.py, app/scheduler.py): a hostile robots.txt could
  return `Crawl-delay: 1e999` (overflows to `inf`), which was stored
  uncapped and turned into `asyncio.sleep(inf)` **outside** the per-URL
  budget — hanging the whole scheduler gather. The parser now rejects
  non-finite values (a parse-level rule), and `set_domain_min_delay` clamps
  finite values to the existing politeness ceiling (`_MAX_COOLDOWN_WAIT`)
  at the write — the chokepoint where a delay becomes a scheduler wait. The
  ceiling lives only in the scheduler; the parser never clamps. A tree-wide AST
  invariant (`test_domain_politeness_floor_is_committed_only_via_the_setter`)
  fails CI if `_domain_min_delay` is ever written outside the clamped setter.
- **One keyed-provider constructor (jscpd)** (app/search/providers.py):
  Tavily/Brave/Serper each re-inlined `__init__` storing `api_key`. A shared
  `_ApiKeyProvider` base owns the credential slot; a contract test pins that
  each provider transmits the key it was built with.
- **One sanctioned test poller (jscpd test-side)** (tests/_testkit.py,
  tests/api/test_async_crawl.py, tests/test_safety_invariants.py):
  `poll_until` is the single predicate poller — the hand-rolled
  `while True` + `asyncio.wait_for` loop in `test_async_crawl.py` returned
  a bare bool on timeout instead of failing with the condition named, so
  every re-inlined copy silently diverges from the sanctioned
  timeout/fail contract. The copy is migrated to
  `poll_until(..., message=...)`; a tree-wide detector bans the shape in
  tests (only a real `pytest.fail` call node counts — a docstring claim
   does not), bite-proven in both directions.
- **Ceiling on explicit `max_content_length_bytes` (M2)**
  (app/constants.py, app/crawl/payload_helpers.py, app/api_models.py): an
  oversized per-page cap (e.g. `10**12`) was honored verbatim by
  `_effective_max_content_length`, disabling the RAM-budget scaling AND
  inflating the decompression bound — two chokepoints defeated by one caller
  field. `MAX_CONTENT_LENGTH_BYTES` (1 GiB) is now the hard ceiling: rejected
  at policy normalization (mirroring the existing `max_rps` bound, never
  silently clamped) and rejected at the pydantic boundary via `le=` (422).
- **Exact byte accounting on a failed `aset` write (L1)**
  (app/crawl/content_cache.py): the failure rollback mutated `_index_total_bytes`
  with a delta that had never been applied (bytes enter the total only at
  commit) — a new key's `total -= size` subtracted uncounted bytes (a `max(0,`
  `…)` clamp hid it only on an empty cache) and an existing key's
  `+= prior_size - size` drove the total to a negative. Since `_write_data` is
  `atomic_write_text` (prior file byte-identical on failure) and nothing
  mutates index/sizes/total/mirror before the write, the rollback is reduced to
  `raise`, and the commit now re-reads `old_size` under the lock — race-safe,
  matching sync `set()`. Two new tests pin `total == sum(sizes)` across a
  failed new-key and existing-key write.
- **Hostile `Content-Length` can't crash a fetch (L2)**
  (app/crawl/strategies/http.py): both fetchers pre-checked the declared body
  size with a bare `int(content_length)`, so a crafted `Content-Length: abc`
  raised out of the fetch and turned one response into that URL's failure. A
  single `_declared_length_exceeds` chokepoint now parses it, treating
  malformed/negative/absent as "unknown size" (skip the pre-flight; the
  streamed running cap still bounds the body). The scripted httpx mock
  transport moved to `_testkit` (dedup). A tree-wide AST invariant
  (`test_header_numbers_are_converted_under_guard`) fails CI on any unguarded
  `int()`/`float()` of a header-derived value, exempting conversion sites
  inside `try/except ValueError` — so the same class can't silently reappear
  at a new fetcher.
- **Async batch size rejected at the boundary (L4)**
  (app/api/crawl_endpoint.py, app/crawl/payload_helpers.py, app/crawl/engine.py,
  app/api/render.py): the async crawl path returned 202 before checking
  `max_batch_size`, so an oversized batch was silently accepted and only
  failed inside the detached background task — the caller could never see
  why. A `checked_payload_urls` chokepoint (dedup + cap in one call) now
  validates before the 202 in the endpoint and inside `execute_crawl`, so
   sync and async share one gate and cannot drift on batch admission.
- **Bounded retry backoff in `_fetch_with_retries` (L3)**
  (app/crawl/strategies/http.py): a failing fetch retried back-to-back with no
  wait, hammering an origin that is down or rate-limiting. Now `_retry_backoff_seconds`
  inserts a capped exponential gap (base 0.25s, doubling, ceiling 2.0s) between
   attempts, so the retry budget widens gaps instead of busy-looping, and a large
   `max_retries` can never park a URL past the per-retry ceiling.
 - **Two new same-day drift detectors** (tests/test_safety_invariants.py), closing
   enforcement gaps rather than just the bugs:
   - `test_locked_cache_helpers_are_called_under_the_lock` — any `_*_locked` cache
     helper must be invoked inside a `with …lock` block. The twin detector only
     caught raw `_memory_cache` access, so the M1 class (`get()` calling
     `_remember_memory_locked` off-lock while `aget()` held it) slipped past; this
     makes an unlocked `_*_locked` call a tree-wide failure. Non-vacuously sees all
     nine real call sites in `content_cache.py`, all correctly guarded.
   - `test_decompression_reaches_only_through_the_chokepoint` — no `app` module may
     import or call `gzip`/`zlib`/`brotli` directly; body decoding must route through
     the capped `decompress_body` chokepoint. The existing cap sweep guards the
     chokepoint's callers; this guards the bypass around it (a re-introduced
      header-trusting, uncapped decompress). Both ship `*_bites` proofs.
- **Page-identity dedup at the batch chokepoint ("never crawl the same page
  twice")** (app/crawl/payload_helpers.py): `_payload_urls` collapsed only *exact*
  duplicate strings, so a request carrying both `.../x` and `.../x/?utm_source=tw`
  — the same page under `normalize_public_http_url`, the service's declared single
  source of truth for page identity — still crawled twice. The chokepoint now dedups
  on the canonical key: fragment / trailing-slash / tracking-param / param-order
  variants collapse, first occurrence wins, order preserved — matching the identity
  already used by the visited set, the frontier, and the content-cache key. An
  un-canonicalizable value (non-http, or a private target under the SSRF guard)
  keeps its raw key so it stays a distinct entry and request-target validation
  remains the caller's job. A tree-wide AST invariant
  (`test_batch_dedup_keys_on_canonical_page_identity`) fails CI on any `dict.fromkeys`
   batch dedup that doesn't route through the canonicalizer; it ships a `*_bites`
   proof and is non-vacuous (`dict.fromkeys` appears nowhere else in `app/`).
 - **Self-describing contract: latency, async, and the dedup guarantee are now
   advertised** (app/discovery/{capabilities,agent_hints,well_known}.py,
   app/api/crawl_router.py): `/v1/capabilities` previously omitted the one real time
   ceiling (`total_url_budget_seconds`) and never mentioned `async_mode`, so a caller
   sizing an integration had no machine-readable way to learn that a synchronous
   `/v1/crawl` has **no server-side time ceiling** (duration scales with `max_pages`,
   depth, politeness delays, and the per-URL budget). Capabilities now advertise
   `default_limits.total_url_budget_seconds`, a `features.async_mode` flag, and a
   `crawl_timing` block (`per_url_budget_seconds`, `sync_has_server_time_ceiling`, and
   a `large_crawl_guidance` pointing to `async_mode` + `status_url` polling / `POST
   /v1/jobs` for long crawls). `/v1/agent-hints` gains async + page-identity guidance;
   `/.well-known/crawler-service` links `/v1/map`, `/v1/jobs`, and
   `GET /v1/crawl/{request_id}`. The `/v1/crawl` OpenAPI description was corrected the
   same way (its `202` said "webhook" only, now "`async_mode` or `webhook_url`").
   Doc fidelity: the README and `browser-crawler-agent-contract.md` still claimed
   "callers own dedupe" and "5 MB → 5 MB" for the gzip-bomb test — both corrected to
   the real behavior (canonical within-request dedup + within-TTL reuse; a ~1 KB →
   1 MB gzip bomb plus deflate/brotli cases). Contract tests assert the new
    advertised fields.
 - **Durable whole-site jobs no longer lose the in-flight batch on a mid-crawl
   crash** (app/api/job_runner.py, app/crawl/job_store.py): exercising restart
   behaviour exposed that `_run_job` write-aheads the frontier pop (so the
   reload-each-iteration loop can't refetch a batch) but records results/
   `visited` only *after* the ~engine call, so a process killed mid-batch left
   the batch neither visited nor queued — on recovery `_pop_frontier` returned
   empty and the job was marked `COMPLETED` with **0 pages**. The persisted
   `CrawlJobState` now carries an `in_flight` list (schema `+ migration`, set in
   the same write-ahead save that removes the batch and cleared when its results
   commit); `_run_job` re-queues any non-empty `in_flight` onto the frontier at
   entry, so a crash resumes with at-least-once delivery (the `visited` set and
   content cache keep the re-fetch cheap and non-duplicating). This closes the
   #1834 "loses only in-flight work" claim — previously it also *dropped* it and
   falsely completed. A `TestCrashRecovery` test encodes the exact persisted
    post-pop/pre-record state and asserts recovery re-crawls it (the detection
    gap: nothing previously asserted in-flight durability).
  - **Transient failures no longer drain a whole-site frontier** (app/api/job_runner.py,
    app/crawl/job_store.py, app/constants.py): the runner marked *every* popped URL
    visited unconditionally, so a page that failed for a *transient* reason
    (network/dns/tls/timeout/browser crash — the long-dead but now-live
    `TRANSIENT_CLASSIFICATIONS`) was counted done and never re-queued. A whole-site
    job whose links fail during a network outage therefore drained to a seed-only
    `COMPLETED` — the exact 185 one-page towns from the 351-municipality exercise.
    `_run_job` now re-queues a transiently-failed URL (and a popped URL with no
    returned item) to the back of the frontier instead of visiting it, bounded by
    `MAX_URL_TRANSIENT_ATTEMPTS` (3) via a persisted `transient_retries` map so a
    permanently-dead page can't spin forever — on exhaustion it is recorded terminal
    with `error="retries_exhausted"`. Permanent failures (`not_found`/`site_error`/
    `gone`) stay terminal-once: a known-missing page is an answer, not a blip. New
    tree-wide detector `test_whole_site_visited_commit_is_transient_gated` forbids any
    `job.visited.add()` not gated on `TRANSIENT_CLASSIFICATIONS` (the local per-request
    `visited` set is deliberately out of scope), with a paired `*_bites` test.

### Deployment posture
- **Zero-telemetry invariant** (Dockerfile runtime ENV + docker-compose.yml):
  `ORT_DISABLE_TELEMETRY=1` disables onnxruntime's native client telemetry (via
  magika) at lib load, and the Microsoft OneCollector / App-Insights host family
  is sinkholed to `0.0.0.0` via `extra_hosts` as the un-bypassable egress
  chokepoint for libraries with no opt-out flag (Playwright's driver ships none
  in this version; camoufox phone-homes only on explicit operator action).
  Posture and verification documented in
  docs/references/browser-crawler-telemetry.md.

### Repository scaffolding
- Added `pyproject.toml`, `LICENSE` (MIT), `.env.example`, `SECURITY.md`,
  `CONTRIBUTING.md`, `.github/workflows/ci.yml`, `Makefile`,
  `docker/browser-crawler/scripts/run-tests.sh`.
- `requirements.txt` now lists `lxml`, `beautifulsoup4`, `markdownify`
  (previously silent dependencies).
- `scripts/probe_session.py` no longer hardcodes `/opt/stack/...`
  paths; target and output are env-var configurable.

### Test infrastructure
- `./test.sh browser-crawler` (dev-stack root) now delegates to
  `docker/browser-crawler/scripts/run-tests.sh` instead of failing on
  missing `tests.test_browser_crawler_api` / `tests.test_browser_crawler_core`.
- New `tests/crawl/test_cookie_namespace.py` covers the per-request
  cookie namespace behavior (7 tests).
- Test suite: **738 passed, 54 browser-marked deselected** in ~32 s.
- Added `tests/test_adversarial_round3_fixes.py` (7 tests) covering
  round-3 reviewer findings (sync `close()` on the dropped client,
  `Connection: close` on truncated requests, per-chunk trailers
  preserved across truncation, etc.).

## [0.1.0] - 2026-08-21

Initial extraction from `docker/browser-crawler` (dev-stack monorepo).
Captured the state at the close of the first adversarial review
(`ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`). All 7 prior findings (ReDoS, raw exception
text, local-mode SSRF misroute, robots regex recompile, httpx client
leak, JobStore concurrency, selector DoS) were already fixed.


## [0.3.0] - Open-Source Extraction Hardening

### Changed
- **Removed monorepo entanglements**: ``local_service_logging`` shim
  promoted to a standalone ``app/logging_config.py``. The
  ``shared/python`` path injection in ``tests/conftest.py`` and
  ``scripts/probe_session.py`` is gone. ``app/main.py`` no longer
  references any path outside the package itself.
- **Portable defaults**: ``app/settings.py`` defaults changed from
  ``/app/logs/...``, ``/app/cache`` to ``./logs/...``, ``./cache`` so
  the package works on a fresh checkout without a specific mount
  layout. The runtime now creates those directories at lifespan
  startup if they are missing.
- **Identity strings**: ``DEFAULT_USER_AGENT`` and ``USER_AGENT_TOKEN``
  constants added to ``app/constants.py``. All references to the
  dev-stack monorepo / local hostname have been replaced with the
  project-neutral ``browser-crawler/0.2`` identity. FastAPI
  ``contact`` and ``license_info`` now reference the project, not the
  monorepo.
- **Probe target neutral**: ``scripts/probe_session.py`` and
  ``scripts/e2e_verify.py`` default to ``https://example.com/``
  instead of a specific municipal domain.

### Security
- **SSRF env var parsing centralized**: ``app.validation.parse_bool_env``
  now owns the ``BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS`` parsing with
  consistent ``1``/``true``/``yes``/``on`` semantics. The
  ``os.environ.get(...) == "1"`` check at three call sites has been
  replaced.
- **Webhook TOCTOU window closed**: ``_post_webhook`` and
  ``_run_and_deliver_webhook`` now both route through a single
  ``_deliver_pinned_webhook`` helper, so the SSRF validation and
  IP-pinning happens in one pass. A webhook that passes validation
  cannot have its DNS record flipped between the check and the
  delivery.
- **``_PENDING_CLIENT_CLOSE`` lock scoping**: the synchronous
  ``client.close()`` call now happens OUTSIDE the critical section
  (the lock is released first, then the blocking close runs). This
  prevents the close-from-no-event-loop path from stalling other
  threads waiting on the lock.

### Fixed
- **Classification input change on script-truncated bodies**: the linear HTML
  noise scanner neutralizes an unclosed ``<script>`` to end of input, so for
  bodies truncated mid-script by ``max_content_length`` the text after the
  cut no longer reaches classification (the old regex left truncated-script
  content visible). Deliberate fail-closed trade; classification-sensitive
  callers may see diffs on truncated pages (final review #8).
- **``_ResultSpill.close()`` double-unlink**: removed the duplicate
  ``if self._own:`` block that could ``os.unlink`` the same path
  twice (raising ``FileNotFoundError`` once the first unlink
  succeeded).
- **Browser pool ``None`` guard**: a request that does not need any
  browser strategy (``HTTP_DECLARED_CRAWLER`` /
  ``HTTP_CFFI_IMPERSONATE`` only) no longer 503s when the browser
  pool is not initialized. URL validation still happens up front so
  invalid URLs surface as 422, not 503.

### Removed
- ``app/browser.py``: re-export shim for ``BrowserRenderer`` and
  related symbols. Canonical location is
  ``app.crawl.strategies.browser``.
- ``app/service.py``: re-export shim + the browser-only
  ``render_request`` helper. ``render_request`` moved to
  ``app/api/render.py``; everything else to ``app.crawl.engine`` /
  ``app.crawl.strategies.http`` / ``app.crawl.document_text`` /
  ``app.crawl.extraction`` (its canonical source).
- ``REVIEW_ROUND2.md``: 98-byte empty header, replaced by the
  successful ``REVIEW_ROUND3.md`` review.

### Build
- ``Dockerfile``: the circular ``COPY --from=browser-crawler:latest``
  reference (which broke fresh-clone builds where the source image
  had not been tagged yet) replaced with ``COPY --from=camoufox-binary``.
  The ``shared/python/local_service_logging`` ``COPY`` is gone (the
  module is now in-tree).
- ``docker-compose.yml``: new standalone file at the repo root so the
  service can be run from a fresh clone without any parent
  ``docker-compose.yml`` / ``docker-compose.municipal.yml`` mounts.
- ``Makefile`` ``clean`` target: now uses ``-prune`` to walk leaf
  ``__pycache__`` directories first, avoiding the "directory vanished
  mid-rm" race that the previous flat ``-exec rm -rf`` could hit.

### Developer
- ``mypy.ini`` + ``pyrightconfig.json``: optional type-checker configs
  for users running stricter checks locally. Both default to "type
  checking basic" mode and ignore ``tests/`` / ``scripts/`` (test
  code uses ``from coverage_helpers import *`` patterns that confuse
  whole-file checks).
- ``ssrf_guard_required`` pytest fixture: tests that exercise
  ``_check_ssrf_protection`` or ``_resolve_and_pin_host`` can opt in
  via this fixture, which fails the test if the SSRF helper ran
  while ``BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS`` was still enabled
  (the silent false-positive that lets the guard short-circuit
  before real test logic runs).


## [0.3.1] - Reviewer-5 Follow-Up

### Fixed (from `REVIEW_ROUND5.md`)
- **Dockerfile comment cleanup**: the `# browser-crawler:latest` comment in
  the runtime stage now describes the historical reason for using
  `--from=camoufox-binary`, not an active broken reference.
- **`app/main.py`** has zero runtime `try/except ImportError` for the
  logging helper. The previous comment that referenced the deleted
  monorepo shim has been condensed.
- **Dockerfile** no longer references `shared/python/local_service_logging`
  by name in comments (still no `COPY` line for it).
- **`CONTRIBUTING.md`** no longer tells contributors to vendor
  `local_service_logging` from a parent repo that does not exist. The
  paragraph now points at `app/logging_config.configure_service_logging`
  as the in-tree equivalent.
- **`tests/conftest.py`**: the suite-wide default for
  `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS` is now gated behind a new
  `BROWSER_CRAWLER_TEST_ALLOW_PRIVATE_TARGETS` env var. Defaulting to
  `"1"` (loopback-crawl-friendly) preserves all 740 existing tests.
  CI can set the new var to `"0"` for an SSRF-strict mode where every
  loopback test must explicitly opt in.
- **Webhook test race fixed**: `tests/api/test_crawl.py::test_v1_crawl_webhook_returns_202_and_delivers`
  now polls `WEBHOOK_RECEIVED` for up to 10s instead of asserting
  synchronously after the `POST /v1/crawl`. Polling helper moved to
  `tests/_testkit.py::poll_until` for reuse; `tests/api/test_async_crawl.py`
  imports it from there.
- **Stale docstring references to `app/service.py` and `app/browser.py`**
  removed from `app/constants.py`, `app/crawl/payload_helpers.py`, and
  `app/crawl/extraction.py`. The two shim files themselves were already
  deleted; only docstring breadcrumbs remained.

### Deferred
- Splitting `app/main.py` into routers (~900 lines, acceptable for
  initial release; tracked as P2).
- Replacing `_reset_global_http_state` fixture with DI/registry (P3).
- Auditing all `except Exception` catches (P3).


## [0.3.2] - Reviewer-6 Follow-Up

### Fixed (from `REVIEW_ROUND6.md` reviewer feedback)
- **`_parse_robots_crawl_delay`** uses `USER_AGENT_TOKEN` (was already
  fixed in the prior round but the reviewer noted it; verified twice).
- **SSRF test-suite default inverted**: `tests/conftest.py` now sets
  `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS` to `"0"` (the secure
  production default). A session-scoped autouse fixture
  (`_enable_loopback_for_test_suite`) flips it to `"1"` for the
  default test run so existing loopback-crawl tests keep working
  unchanged. CI runs that want SSRF-strict mode set
  `BROWSER_CRAWLER_TEST_STRICT_SSRF=1` to disable the session-wide
  flip; every loopback test must then opt in explicitly via
  `monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")`.
- **`docker-compose.yml`** standalone file exists at the repo root
  (already created in round 4; verified).
- **`app/browser.py`** and **`app/service.py`** shims are deleted
  (already done; verified no callers remain and stale docstring
  references cleaned up).
- **`setuptools`** removed from runtime `requirements.txt` /
  `pyproject.toml` (already done; verified no references remain).
- **`mypy.ini`** + **`pyrightconfig.json`** present at the repo root
  (already added; verified).

### Changed (Reviewer-6 follow-up)
- **`CONTRIBUTING.md`** security-sensitive file list expanded to
  include `app/validation.py` (SSRF guard), `app/crawl/strategies/
  http.py` (DNS pinning / cookie isolation), `app/crawl/strategies/
  browser_pool.py` (browser-pool sizing), and `app/settings.py`
  (defaults are security decisions).
- **`README.md`** adds an "Adversarial Test Coverage" section
  highlighting the dedicated adversarial test suites (decompression
  bombs, DNS pinning, classification corroboration, etc.).

### Refactored (split out of `app/main.py`)
- **`app/api/webhooks.py`** owns all webhook delivery helpers
  (`validate_webhook_url`, `deliver_pinned_webhook`, `post_webhook`,
  `run_and_deliver_webhook`, `deliver_webhook`). Module-level state
  (semaphore, shutdown event, callbacks) is wired once at lifespan
  startup via `set_webhook_dependencies`; cleared on teardown.
  This removed ~110 lines from `app/main.py` and made the webhook
  helpers independently testable.

### Documentation
- **`_gc_stale_spills`** gained a docstring explaining the inode/
  fd lifecycle race window (Linux: unlink-then-close is safe; Windows:
  PermissionError is caught and the spill stays).
- **`_compile_scope_regex_safe`** quantifier lookbehind
  (`(?<!\\)` vs the reviewer's claimed `(?<!\\))`) now has an
  inline comment explaining why the form used is correct.
- **`_SITEMAP_CACHE`** has a thread-safety comment noting GIL
  protection under CPython and the migration path if the parsing
  moves to a thread.
- **`ContentCache.set()` → `_enforce_max_entries()`** lock-release
  window has an inline comment documenting the benign over-sized
  index window.

### Deferred
- **Split `app/main.py` into routers** — the `/v1/crawl` endpoint
  alone is ~200 lines and tightly coupled to the FastAPI app instance
  (background tasks, metrics, exception handlers, job store).
  Splitting cleanly requires a Router refactor with dependency
  injection for background tasks + metrics; tracked as P3.
- **Replace `_reset_global_http_state` fixture with DI/registry** —
  manually resets ~8 module-level singletons. Replace with a context
  manager that owns the lifetime of each singleton. Tracked as P3.
- **Audit all `except Exception` catches** — there are ~30 sites
  across `app/`. Most are best-effort teardown (browser, HTTP client,
  webhook) and correctly swallow; a few (sitemap parsing, classification)
  could log the swallowed exception to aid debugging. Tracked as P3.
