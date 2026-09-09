# Browser Crawler Code Review & Refactoring Plan

> **Status 2026-09-03: ARCHIVED — main.py thin-out complete.** The `app/service.py` / `app/browser.py` / `app/robots.py`
> monolith referenced below no longer exists. Current topology is `app/main.py` (~218 lines, app factory only) +
> `app/api/*` routers + `app/crawl/*` engine. 744 tests, `ruff check app` green for `app/api` + `app/main.py`.
> Bugs 1–4 below are resolved. This document is retained as historical context; see `app/main.py`
> and `app/api/` for the current contract.

## Architecture Overview (HISTORICAL — see `app/` for current)

| File | Lines | Purpose |
|------|-------|---------|
| `app/main.py` | ~500 | FastAPI app, routes, readiness, exception handlers — **now 218 lines, routers extracted to `app/api/*`** |
| `app/service.py` | ~1300 | Crawl orchestration, strategies, link extraction, LLM-format output — **deleted; now `app/crawl/engine.py` (1142) + `app/crawl/strategies/http.py` (1703) + `app/crawl/strategies/browser.py` (902)** |
| `app/browser.py` | ~200 | Camoufox browser wrapper — **now `app/crawl/strategies/browser_pool.py` (150) + `app/crawl/strategies/browser.py`** |
| `app/models.py` | ~180 | Dataclasses |
| `app/api_models.py` | 224 | Pydantic models, enums |
| `app/settings.py` | ~60 | Env config |
| `app/classify.py` | ~300 | Classification logic |
| `app/validation.py` | ~80 | URL validation |
| `app/scheduler.py` | ~120 | DomainScheduler |
| `app/robots.py` | ~50 | robots.txt parsing — **now `app/crawl/fetch_helpers.py` + `app/crawl/engine.py` robots cache** |

Tests: 67 total (historical) → **744 passed (fast suite, 2026-09-03), `app/api/*` + `app/crawl/*` covered**.

## Smoke Test Results

All 7 endpoints reachable:
- GET /health -> 200
- GET /ready -> 200  
- GET /v1/capabilities -> 200 (7 strategies, 26 classifications, 20 output fields)
- GET /v1/agent-hints -> 200
- GET /.well-known/crawler-service -> 200
- POST /v1/crawl -> 200 (all 7 strategies)
- POST /v1/render -> 200
- GET /openapi.json -> 200

## Bugs Found

### BUG 1: `/v1/crawl` response_model mismatch
Actual response: `{"success":true,"data":[{"url","markdown","metadata","warning","error"}]}`
`response_model=CrawlResponse` expects: `original_url, normalized_url, classification, selected_strategy, quality_score...`
FastAPI validates against CrawlResponse but the actual response never matches.

### BUG 2: Invalid URL returns 200 instead of 422
`POST /v1/crawl {"url":"not-a-url"}` -> 200 with null data
OpenAPI promises 400/422 with CrawlerError.

### BUG 3: Gzip decompression broken
3 of 6 decomp tests FAIL with AssertionError.

### BUG 4: `main.py` is monolithic
ReadinessState class, configure_app(), 7 route handlers, exception handlers, startup/shutdown all in one file.

## Code Duplication

| Duplication | Locations |
|-------------|-----------|
| Classification constants | api_models.py (enum) vs service.py (hardcoded sets) |
| DEFAULT_CRAWL_STRATEGIES | service.py + main.py + test assertions |
| URL validation | validation.py + route handlers |
| Error wrapping (CrawlerError) | Every route handler |
| sys.path manipulation | Every test file |
| Mock worker setup | Multiple test files |

## Test Coverage Gaps

| Area | Missing |
|------|---------|
| main.py | ReadinessState class, exception handlers, error paths |
| scheduler.py | Global/per-domain limits, async wait |
| robots.py | Zero tests |
| classify.py | Most classification values |
| validation.py | Internationalized domains, IPv6, edge cases |
| settings.py | Zero tests |
| browser.py | Integration coverage |

## Refactoring Plan

### Current module structure (post-refactor, 2026-09-03):
```
app/
  main.py               # THIN (~218) — FastAPI factory + lifespan + app.state wiring only
  api/
    crawl_endpoint.py / crawl_router.py   # POST /v1/crawl + GET /v1/crawl/{id}
    render.py / render_endpoint.py / render_router.py  # POST /v1/render
    map_endpoint.py / map_router.py       # POST /v1/map
    jobs_router.py                        # POST/GET /v1/jobs*
    metrics_router.py                     # GET /metrics
    crawl_executor.py                     # shared crawl path (browser-pool guard)
    async_crawl_runner.py                 # bounded background webhook crawl
    response_shaping.py                   # unified formatters
    middleware.py                         # 16 MiB body-size guard (C1/C3 fixes)
    exceptions.py                         # CrawlerError normalization
    dependencies.py                       # app.state → Depends providers
    policy_defaults.py / main_helpers.py  # shared constants/helpers
    webhooks.py                           # SSRF-safe webhook delivery
  health/readiness.py   # ReadinessState + /health + /ready (now singleton READINESS_STATE)
  discovery/capabilities.py, agent_hints.py, well_known.py
  crawl/
    engine.py (1142) • strategies/http.py (1703) • strategies/browser.py (902)
    strategies/browser_pool.py • extraction.py • decompression.py • ... etc
```

### Historical planned structure (pre-refactor, retained for traceability):
```
app/
  health/
    readiness.py        # ReadinessState + /health + /ready
  discovery/
    capabilities.py     # /v1/capabilities
    agent_hints.py      # /v1/agent-hints
    well_known.py       # /.well-known/crawler-service
  crawl/
    engine.py           # crawl_request orchestration
    decompression.py    # _decompress_body
    llm_format.py # _extract_llm_metadata
    strategies/
      http.py           # HTTP strategies
      browser.py        # Browser strategies
      extraction.py     # Document extraction
  main.py               # THIN - only FastAPI app creation + route wiring
```

### Tests:
```
tests/
  conftest.py           # Central sys.path + fixtures
  api/
    test_health.py
    test_discovery.py
    test_crawl.py
    test_render.py
  crawl/
    test_engine.py
    test_decompression.py
    test_llm_format.py
  test_classify.py
  test_validation.py
  test_scheduler.py
  test_robots.py
  test_settings.py
```

## Implementation Steps (red/green/refactor)

1. Create `app/health/readiness.py`, write test, move class
2. Create `app/crawl/decompression.py`, write test, move function, fix gzip bug
3. Create `app/crawl/llm_format.py`, move `_extract_llm_metadata`
4. Fix `/v1/crawl` response_model mismatch
5. Fix invalid URL error handling
6. Create `tests/conftest.py`, eliminate per-file sys.path
7. Write tests for: robots.py, settings.py, ReadinessState, error paths
8. Run `pytest --cov=app --cov-fail-under=100`
