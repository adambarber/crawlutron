# Browser Crawler Code Review & Refactoring Plan

## Architecture Overview

| File | Lines | Purpose |
|------|-------|---------|
| `app/main.py` | ~500 | FastAPI app, routes, readiness, exception handlers |
| `app/service.py` | ~1300 | Crawl orchestration, strategies, link extraction, Firecrawl compat |
| `app/browser.py` | ~200 | Camoufox browser wrapper |
| `app/models.py` | ~180 | Dataclasses |
| `app/api_models.py` | 224 | Pydantic models, enums |
| `app/settings.py` | ~60 | Env config |
| `app/classify.py` | ~300 | Classification logic |
| `app/validation.py` | ~80 | URL validation |
| `app/scheduler.py` | ~120 | DomainScheduler |
| `app/robots.py` | ~50 | robots.txt parsing |

Tests: 67 total (14 API + 46 core + 4 decomp + 2 scheduler), 65% coverage.

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

### New module structure:
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
    firecrawl_format.py # extract_firecrawl_format
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
    test_firecrawl_format.py
  test_classify.py
  test_validation.py
  test_scheduler.py
  test_robots.py
  test_settings.py
```

## Implementation Steps (red/green/refactor)

1. Create `app/health/readiness.py`, write test, move class
2. Create `app/crawl/decompression.py`, write test, move function, fix gzip bug
3. Create `app/crawl/firecrawl_format.py`, move `extract_firecrawl_format`
4. Fix `/v1/crawl` response_model mismatch
5. Fix invalid URL error handling
6. Create `tests/conftest.py`, eliminate per-file sys.path
7. Write tests for: robots.py, settings.py, ReadinessState, error paths
8. Run `pytest --cov=app --cov-fail-under=100`
