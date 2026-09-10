# Changelog

All notable changes to this project are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
