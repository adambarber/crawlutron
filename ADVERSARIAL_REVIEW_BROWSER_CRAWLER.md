# Adversarial Review: Browser Crawler Python Codebase

**Repository**: `docker/browser-crawler/`  
**Review Date**: 2025 (current session)  
**Scope**: Complete codebase — app logic, strategies, API, tests, Docker, docs

---

## Executive Summary

The browser-crawler is a **well-architected, production-grade local crawling service** with strong separation of concerns, thoughtful async orchestration, and defense-in-depth security defaults (SSRF protection, no credential injection, no CAPTCHA solving). The modular refactoring (health, discovery, crawl/engine, strategies) is largely complete and improves maintainability significantly.

**Overall Risk Level**: **LOW–MEDIUM** — No critical vulnerabilities, but several correctness bugs, test flakiness risks, and architectural debt items that should be addressed before relying on this for production municipal workloads.

---

## 1. Critical Correctness Bugs

### 1.1 Gzip Decompression Failure in Conditional Cache Path
**File**: `app/crawl/strategies/http.py` (via `app/crawl/decompression.py`)  
**Severity**: **HIGH** — Silent data corruption for compressed responses

```python
# In _http_strategy_result(), around line 520:
resp_headers = {key.lower(): value for key, value in response.headers.items()}
body = _decompress_body(response.body, resp_headers.get("content-encoding", ""))

# BUG: response.body from httpx.stream().aread() is ALREADY decompressed by httpx
# when Content-Encoding: gzip is present. _decompress_body() then tries to 
# gzip-decompress an already-decompressed body, producing garbage or raising.
```

**Evidence**: 3 of 6 decompression tests FAIL per `review-and-refactoring-plan.md` (BUG 3).

**Fix**: Check if httpx already decompressed (it does by default). Only manually decompress when using `curl_cffi` or raw sockets.

### 1.2 Invalid URL Returns 200 Instead of 422
**File**: `app/main.py` → `crawl()` endpoint  
**Severity**: **HIGH** — API contract violation

```python
# Current: ValueError from validation bubbles up as 422 via exception handler
# BUT: The exception handler only catches RequestValidationError (pydantic),
# not raw ValueError from _payload_urls/_payload_policy.
```

**Evidence**: `review-and-refactoring-plan.md` BUG 2.

### 1.3 `/v1/crawl` Response Model Mismatch
**File**: `app/main.py`  
**Severity**: **MEDIUM** — FastAPI validation bypass

The endpoint declares `response_model=UnifiedCrawlResponse` but returns a manually constructed response. If the shape diverges, FastAPI won't validate it.

---

## 2. Security Issues

### 2.1 SSRF Protection Bypass via Environment Variable
**File**: `app/validation.py`, `app/crawl/strategies/http.py`  
**Severity**: **MEDIUM** — Opt-out is too easy

```python
# validation.py:41
allow_private = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "") not in {"", "0", "false", "no"}
```

**Issue**: Any caller with env access can disable SSRF protection. In a multi-tenant or shared CI environment, this is a footgun.

**Recommendation**: Require explicit `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1` (not just non-empty) and log a warning when disabled.

### 2.2 Cookie Jar Persistence Path Traversal
**File**: `app/crawl/strategies/http.py` → `CookieJar.save_to_file()` / `load_from_file()`  
**Severity**: **LOW** — Controlled path, but no validation

```python
def save_to_file(self, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)  # No path traversal check
```

If `cookie_jar_path` is attacker-controlled (via settings), could write outside `/app/cache`. Currently hardcoded but future-proof with `Path(path).resolve().relative_to(cache_dir)`.

### 2.3 Browser `storage_state` Accepts Arbitrary Paths
**File**: `app/crawl/strategies/browser.py` → `_get_browser_kwargs()`

```python
storage_state = profile.get("storage_state")
if storage_state:
    kwargs["storage_state"] = storage_state  # Passed directly to Camoufox/Playwright
```

**Risk**: If `browser_profile.storage_state` comes from untrusted policy input, could read arbitrary files. The profile is caller-controlled via policy.

**Mitigation**: Validate `storage_state` is under an allowed directory or is a known key.

### 2.4 No Request Size Limit on Webhook Delivery
**File**: `app/main.py` → `_deliver_webhook()`

```python
async with httpx.AsyncClient(timeout=30.0) as client:
    await client.post(webhook_url, json=result, headers={"Content-Type": "application/json"})
```

**Risk**: Large crawl results (many URLs, full HTML) → OOM or DoS of webhook target. Add `content-length` limit or stream.

---

## 3. Correctness & Logic Bugs

### 3.1 Content Cache Key Collision Risk
**File**: `app/crawl/content_cache.py` → `make_key()`

```python
@staticmethod
def make_key(*parts: Any) -> str:
    return "|".join(json.dumps(p, sort_keys=True, separators=(",", ":")) if not isinstance(p, str) else p for p in parts)
```

**Bug**: String parts are NOT JSON-encoded. If a string contains `|`, it breaks key parsing.

```python
make_key("a|b", "c") == make_key("a", "b|c")  # Both produce "a|b|c" → COLLISION
```

**Fix**: JSON-encode all parts, or use a tuple-hash approach.

### 3.2 Domain Scheduler Cooldown Deadlock (Partially Fixed)
**File**: `app/scheduler.py` → `run()`

```python
# Line 165-171: Cooldown awaited BEFORE global semaphore — GOOD
cooldown_expiry = self._domain_cooldowns.get(job.domain)
if cooldown_expiry is not None:
    now = time.time()
    if now < cooldown_expiry:
        await asyncio.sleep(cooldown_expiry - now)
```

**But**: Token bucket rate limiting (`_max_rps`) is applied AFTER global semaphore acquire (line 185), so a rate-limited domain still holds a global slot while sleeping. Move token bucket before global gate.

### 3.3 Recursive Crawl `max_pages` Off-by-One
**File**: `app/crawl/engine.py` → `crawl_request()` recursive branch

```python
while frontier and (max_pages == 0 or page_count < max_pages):
    # ...
    page_count += 1
    if depth >= max_depth:
        continue
    for link in result.discovered_links:
        if max_pages and page_count + len(frontier) >= max_pages:  # CHECK
            break
        frontier.append(...)
```

**Bug**: The check uses `page_count + len(frontier)` but `page_count` is incremented BEFORE adding new links. If `max_pages=1`, the root page counts as 1, then `page_count + len(frontier) = 1 + 0 = 1 >= 1` → breaks correctly. But if root page has 0 discovered links, `page_count=1`, `max_pages=1`, loop continues to next iteration where `page_count < max_pages` is False. Actually works but logic is convoluted.

**Better**: Track `total_queued = page_count + len(frontier)` and check against `max_pages`.

### 3.4 Classification Evidence Source Mismatch
**File**: `app/classify.py` → `classify_page_with_evidence()`

```python
visible_haystack = f"{title or ''}
{text or ''}".lower()
html_haystack = _html_without_script_and_style(html).lower()
# Searches visible_haystack first, then html_haystack
```

**Issue**: For HTML pages, `visible_haystack` contains `text` (extracted via trafilatura/markdownify) which may have DIFFERENT wording than raw HTML. A signature present in raw HTML but stripped by trafilatura won't match in `visible_haystack`, but WILL match in `html_haystack` — but the source reported is `"visible_text"` for the first match. This misattributes evidence source.

**Fix**: Track which haystack actually matched.

### 3.5 Browser Challenge Wait Infinite Loop Risk
**File**: `app/crawl/strategies/browser.py` → `_extract_with_challenge_wait()`

```python
deadline = time.monotonic() + self._challenge_wait_seconds
while time.monotonic() < deadline:
    self._sleep(1)
    # ...
```

**Bug**: If `self._sleep` is patched to no-op (as in tests with `_mock_sleeps`), this becomes a **busy loop** consuming 100% CPU. The test fixture patches `time.sleep` and `asyncio.sleep` to no-op.

**Fix**: Use `asyncio.sleep` with a minimum delay, or check `if deadline - time.monotonic() < 0.1: break`.

### 3.6 Shadow DOM Flattening Mutation During Iteration
**File**: `app/crawl/strategies/browser.py` → `_flatten_shadow_dom()`

```javascript
const all = [];
collect(document, all);  // Collects ALL elements including shadow roots
for (const el of all) {
    const sr = el.shadowRoot;
    if (sr && sr.mode === 'open') {
        const kids = Array.from(sr.childNodes);
        for (const k of kids) {
            if (k.nodeType === 1) {
                el.appendChild(document.importNode(k, true));  // MUTATES DOM
            }
        }
    }
}
```

**Bug**: Moving nodes from shadow root to light DOM while iterating `all` (which includes those moved nodes' descendants) can cause:
- Duplicate processing
- Infinite loops if moved nodes have shadow roots
- `HierarchyRequestError` if moving a node into its own descendant

**Fix**: Collect shadow root children FIRST, then move in a separate pass.

### 3.7 Pagination Detection Only Checks `rel="next"` on `<a>` and `<link>`
**File**: `app/crawl/extraction.py` → `_detect_pagination()`

```python
_NEXT_REL_ANCHOR_RE = re.compile(r"<a[^>]*rel=["']([^"']+)["'][^>]*>", re.IGNORECASE)
_NEXT_REL_LINK_RE = re.compile(r"<link[^>]*rel=["']([^"']+)["'][^>]*>", re.IGNORECASE)
```

**Misses**: `<link rel="next" href="...">` with other attributes order, `<a rel="next prev">`, `<nav><a rel="next">`. Regex is fragile.

**Fix**: Use lxml parser (already available) instead of regex.

---

## 4. Performance & Resource Management

### 4.1 Browser Renderer Memory Leak — Browser Recycle Logic Flawed
**File**: `app/crawl/strategies/browser.py` → `BrowserRenderer._acquire_browser()`

```python
if (
    self._browser is None
    or getattr(self._browser, "is_closed", False)
    or self._render_count >= self._max_browser_renders
):
    # Recreates browser
```

**Issue**: `_render_count` increments in `_render_with_browser()` AFTER successful render. If a render fails (exception), `_render_count` is NOT incremented, but the browser may be in a bad state. The next render reuses the same broken browser.

**Fix**: Increment `_render_count` in `finally` block, or reset browser on ANY exception.

### 4.2 HTTP Client Connection Pool Not Tuned for Concurrency
**File**: `app/crawl/strategies/http.py` → `get_async_http_client()`

```python
limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60.0)
```

**Issue**: With `global_concurrency=4` (default), 100 connections is fine. But if scaled to 50 workers, pool exhaustion occurs. Make pool size configurable via settings.

### 4.3 Content Cache No Size Limit
**File**: `app/crawl/content_cache.py` → `ContentCache`

No max entries, no LRU eviction. Unbounded disk growth if `content_cache_enabled=true` and many unique URLs crawled.

**Fix**: Add `max_entries` parameter with LRU eviction (or at least log warning).

### 4.4 Job Store SQLite No WAL Mode
**File**: `app/crawl/job_store.py` → `JobStore.__init__()`

```python
self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
# Missing: self._conn.execute("PRAGMA journal_mode=WAL")
```

**Impact**: Concurrent readers (API) + writer (crawl job) → lock contention. WAL mode allows concurrent read/write.

### 4.5 Robots.txt Cache Never Expires Entries Properly
**File**: `app/crawl/engine.py` → `_fetch_robots_rules()`

```python
robots_cache: dict[str, tuple[float, list[tuple[bool, str]]]] = {}
robots_ttl = 600.0
# ...
cached = robots_cache.get(origin)
if cached and cached[0] > now:
    return cached[1]
```

**Issue**: Cache dict grows unbounded — no cleanup of expired entries. Add periodic purge or use `functools.lru_cache` with TTL.

---

## 5. Architecture & Maintainability

### 5.1 Circular Import Risk: `app.service` ↔ `app.crawl.engine`
**File**: `app/service.py` imports from `app.crawl.engine`; `app/crawl/engine.py` imports from `app.service` (via `app.validation`)

Currently works because imports are at module level, not function level, but fragile.

### 5.2 `app/crawl/strategies/http.py` is a God Module (~1400 lines)
Contains: HTTP fetchers, cookie jar, fingerprint rotation, header building, decompression, classification, quality scoring, discovery diagnostics, conditional cache, content-type detection, binary detection, retry logic.

**Recommendation**: Split into:
- `http/fetcher.py` — `_default_http_fetcher`, `_default_cffi_fetcher`, `_fetch_with_retries`
- `http/headers.py` — `_headers_for_strategy`, `_select_fingerprint`, `_cookie_header_for_url`
- `http/cookies.py` — `CookieJar`, `_GLOBAL_COOKIE_JAR`
- `http/classify.py` — classification helpers (or reuse `app.classify`)
- `http/discovery.py` — `_discovery_diagnostics`, robots/sitemap/feeds
- `http/response.py` — `_http_strategy_result`, `_quality_score`, `_content_metrics`

### 5.3 Strategy Pattern Not Fully Realized
`app/crawl/strategies/__init__.py` is empty. Strategies are just functions (`_http_strategy_result`, `_browser_strategy_result`, document extraction in `engine.py`).

**Better**: Define a `CrawlStrategy` protocol/ABC with `execute(job, policy) -> EngineCrawlResult` so new strategies are pluggable.

### 5.4 `CrawlerSettings` Frozen Dataclass But Mutable Dicts in Policy
**File**: `app/settings.py` → `CrawlerSettings` is `frozen=True`  
**File**: `app/crawl/payload_helpers.py` → returns mutable `dict` for policy

Policy dict is mutated in `engine.py` (e.g., `policy["limits"]["max_depth"] = ...`). If same settings object reused, mutations leak.

**Fix**: Deep-copy policy in `crawl_request` or use immutable policy object.

### 5.5 Dual Classification Constant Definitions
**File**: `app/constants.py` → `HTTP_BLOCKED_CLASSIFICATIONS`, `BROWSER_BLOCKED_CLASSIFICATIONS`  
**File**: `app/api_models.py` → `CrawlClassification` enum  
**File**: `app/crawl/strategies/browser.py` → redefines `BROWSER_BLOCKED_CLASSIFICATIONS`

**Issue**: `app/crawl/strategies/browser.py` lines 15-21 redefine constants that exist in `app.constants`. If they drift, behavior diverges.

**Fix**: Import from `app.constants` everywhere. Remove local redefinitions.

---

## 6. Test Quality & Coverage Gaps

### 6.1 Global Sleep Patching Causes Flaky Timing Tests
**File**: `tests/conftest.py` → `_mock_sleeps` fixture (autouse, session scope)

```python
async def _fast_asyncio_sleep(*args, **kwargs):
    return None
```

**Problem**: 
- `test_scheduler.py::TestScheduler::test_cooldown_delay_applied_to_domain` uses `real_sleeps` fixture to opt out
- But `_extract_with_challenge_wait` busy-loops when sleep is no-op (see 3.5)
- Tests that rely on real timing (adaptive throttling, cooldowns) are fragile

**Recommendation**: Don't patch sleeps globally. Use `pytest-asyncio` with `asyncio.run()` and let tests control time via dependency injection (pass `sleep` callable).

### 6.2 Zero Tests for Critical Modules
Per `review-and-refactoring-plan.md`:
- `robots.py` — **0 tests** (module doesn't even exist in current code — robots logic is inline in `engine.py`)
- `settings.py` — **0 tests**
- `ReadinessState` class — **partial coverage**
- Error paths in API endpoints — **minimal coverage**

### 6.3 Test Fixtures Use Real Network in Some Cases
**File**: `tests/helpers/server.py` → `spawn_server()` uses `ThreadingHTTPServer` on random port

Good for integration tests, but:
- Port conflicts in parallel CI
- No IPv6 support
- Slow startup/teardown

**Recommendation**: Use `httpx.ASGITransport` for unit tests (already available as `async_client` fixture), reserve real server for browser integration tests only.

### 6.4 Browser Tests Require Camoufox Binary (~1.3 GB)
**File**: `tests/crawl/test_browser.py` → `@pytest.fixture(autouse=True) async def setup_renderer`

This makes tests slow and heavy. The fake browser tests in `test_browser_features.py` are better for CI.

**Recommendation**: Mark real-browser tests as `@pytest.mark.integration` and run separately.

---

## 7. API Design Issues

### 7.1 `CrawlPolicy` Allows Mutually Exclusive Fields
**File**: `app/api_models.py`

```python
class CrawlPolicy(BaseModel):
    strategies: list[CrawlStrategy]  # fetch strategies
    extraction: CrawlExtractionMethod  # parsing pipeline
    # ...
    css_selector: str | None = None
    xpath: str | None = None
```

**Issue**: `css_selector` and `xpath` only apply when `extraction` is HTML-based (FULL, TRAFILATURA, DOM). They are ignored for DOCUMENT extraction but still accepted. No validation.

### 7.2 `discovery` Field Accepts Arbitrary Keys
```python
discovery: dict[str, bool] = Field(default_factory=dict)
```

Only `robots`, `sitemaps`, `feeds` are recognized. Extra keys silently ignored. Should use a typed model.

### 7.3 `max_depth` and `max_pages` Default to 0 (Disabled)
```python
max_depth: int = Field(0, ge=0, le=20)
max_pages: int = Field(0, ge=0, le=2000)
```

**Issue**: 0 disables recursion. But the recursive crawl logic in `engine.py` treats `max_depth > 0` as enabled. A caller sending `max_depth: 0` explicitly gets different behavior than omitting it (which also defaults to 0). This is fine but confusing. Use `None` for "unset" and default in payload normalization.

### 7.4 Webhook URL Validation Minimal
```python
if not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://")):
    raise HTTPException(...)
```

No SSRF check on webhook URL! Could post to `http://localhost:8765/shutdown` or internal services.

**Fix**: Apply same `normalize_public_http_url` + SSRF check to webhook URLs.

---

## 8. Documentation & Contract Drift

### 8.1 Agent Contract Lists Deprecated Strategies
**File**: `docs/references/browser-crawler-agent-contract.md`

```markdown
Supported strategy names:
- HTTP_DECLARED_CRAWLER
- HTTP_SITE_COMPAT        ← DOES NOT EXIST in code (only HTTP_CFFI_IMPERSONATE)
- BROWSER_HEADLESS
- BROWSER_HEADED_WARMED
- DOCUMENT_EXTRACT        ← This is an EXTRACTION method, not a STRATEGY
- TRAFILATURA_EXTRACT     ← This is an EXTRACTION method, not a STRATEGY
```

**Drift**: The contract document is stale. Strategies vs Extractions are confused.

### 8.2 Recommended Policy Uses Non-Existent Strategy
```json
"strategies": ["HTTP_DECLARED_CRAWLER", "HTTP_SITE_COMPAT", "BROWSER_HEADLESS"]
```

`HTTP_SITE_COMPAT` doesn't exist. Should be `HTTP_CFFI_IMPERSONATE`.

### 8.3 `review-and-refactoring-plan.md` References Deleted `robots.py`
The plan says "Create `app/crawl/robots.py`" but robots logic is now inline in `engine.py` (with `_parse_robots_rules`, `_fetch_robots_rules`, `_robots_allows`). The plan is outdated.

---

## 9. Dependency & Supply Chain Risks

### 9.1 Heavy Dependency Tree
| Package | Purpose | Risk |
|---------|---------|------|
| `camoufox[asyncio]` | Browser automation | Large binary (~1.3 GB), frequent updates, Playwright fork |
| `curl_cffi` | TLS impersonation | C extension, Rust dependency, supply chain surface |
| `trafilatura` | HTML extraction | Pure Python, low risk |
| `markitdown` | Document extraction | Microsoft, many transitive deps (pdfplumber, python-pptx, etc.) |
| `pdfplumber` | PDF table extraction | Depends on `pdfminer.six`, `pillow`, `lxml` |
| `charset-normalizer` | Encoding detection | Pure Python, low risk |
| `brotli` | Compression | C extension |
| `h2` | HTTP/2 | Pure Python |

**Risk**: `camoufox` and `curl_cffi` are the highest supply-chain risk (binary blobs, C/Rust extensions). Pin exact versions in `requirements.txt` (currently `camoufox[asyncio]>=0.3.0` is unpinned).

### 9.2 Docker Build Isolation Good
The multi-stage Dockerfile isolates Camoufox binary download (smart!), but `requirements.txt` in builder stage still uses unpinned `camoufox`.

---

## 10. Recommended Fixes Priority Order

### P0 — Critical (Do Before Production Use)
1. **Fix gzip decompression bug** (`http.py` + `decompression.py`)
2. **Fix invalid URL → 200 bug** (exception handling in `main.py`)
3. **Fix content cache key collision** (`content_cache.py`)
4. **Add SSRF check to webhook URLs** (`main.py`)
5. **Fix browser renderer recycle on failure** (`browser.py`)

### P1 — High (Correctness & Security)
6. **Fix shadow DOM flattening mutation bug** (`browser.py`)
7. **Move token bucket before global semaphore** (`scheduler.py`)
8. **Add WAL mode to SQLite** (`job_store.py`)
9. **Validate `storage_state` path** (`browser.py`)
10. **Fix pagination detection with lxml** (`extraction.py`)
11. **Remove duplicate classification constants** (`browser.py`, `constants.py`)

### P2 — Medium (Architecture & Maintainability)
12. **Split `http.py` into focused modules**
13. **Add `max_entries` + LRU to ContentCache**
14. **Add robots cache cleanup**
15. **Make HTTP connection pool configurable**
16. **Update agent contract doc to match code**
17. **Add typed `DiscoveryConfig` model for `discovery` field**

### P3 — Low (Test Quality & Polish)
18. **Remove global sleep patching; inject sleep callable**
19. **Add tests for `settings.py`, `validation.py` edge cases, error paths**
20. **Mark real-browser tests as integration-only**
21. **Pin `camoufox` version in requirements.txt**
22. **Add `PRAGMA journal_mode=WAL` to JobStore**

---

## 11. Positive Observations (What Works Well)

✅ **Modular refactoring** — Health, discovery, crawl engine, strategies cleanly separated  
✅ **Async orchestration** — DomainScheduler with adaptive throttling, circuit breaker, token bucket  
✅ **Security defaults** — SSRF protection, no CAPTCHA solving, no credential injection, no proxy rotation  
✅ **Unified extraction pipeline** — trafilatura + markitdown + DOM, consistent Markdown output  
✅ **Rich diagnostics** — frames, forms, JS endpoints, intercepted XHR, pagination detection  
✅ **Resumable jobs** — SQLite-backed JobStore with pause/resume/cancel  
✅ **Multiple output formats** — JSON, JSONL, Markdown, CSV via `output_formats.py`  
✅ **Machine-readable discovery** — `/v1/capabilities`, `/v1/agent-hints`, `/.well-known/crawler-service`  
✅ **Docker multi-stage build** — Camoufox binary isolated, non-root user, healthcheck  
✅ **Prometheus metrics** — `/metrics` endpoint with strategy/classification breakdown  
✅ **Comprehensive test fixtures** — Real HTTP server, PDF with tables, shadow DOM, lazy-load  

---

## 12. Suggested Immediate Action Plan

```bash
# 1. Run tests to establish baseline
cd docker/browser-crawler
python -m pytest tests/ -v --tb=short 2>&1 | head -100

# 2. Fix gzip decompression (P0)
# Edit app/crawl/strategies/http.py: check if httpx auto-decompressed
# Edit app/crawl/decompression.py: add "already_decompressed" detection

# 3. Fix invalid URL handling (P0)
# Edit app/main.py: catch ValueError in crawl/render/map endpoints explicitly

# 4. Fix content cache key collision (P0)
# Edit app/crawl/content_cache.py: JSON-encode all parts in make_key()

# 5. Fix browser recycle on failure (P0)
# Edit app/crawl/strategies/browser.py: increment _render_count in finally

# 6. Run tests again to verify fixes
python -m pytest tests/ -v --tb=short
```

---

## Appendix: File-by-File Risk Summary

| File | Lines | Risk | Notes |
|------|-------|------|-------|
| `app/main.py` | ~500 | MEDIUM | API contract bugs, exception handling gaps |
| `app/service.py` | ~130 | LOW | Thin re-export layer |
| `app/crawl/engine.py` | ~600 | MEDIUM | Recursive crawl logic, robots inline |
| `app/crawl/strategies/http.py` | ~1400 | HIGH | God module, gzip bug, SSRF, cookie jar |
| `app/crawl/strategies/browser.py` | ~600 | MEDIUM | Memory leak risk, shadow DOM bug, challenge wait busy-loop |
| `app/crawl/strategies/document.py` | ~100 | LOW | Clean, focused |
| `app/crawl/extraction.py` | ~400 | MEDIUM | Regex-based pagination, fragile |
| `app/crawl/job_store.py` | ~200 | LOW | Missing WAL mode |
| `app/crawl/content_cache.py` | ~150 | MEDIUM | Key collision, no size limit |
| `app/crawl/cache.py` | ~80 | LOW | Simple, works |
| `app/scheduler.py` | ~250 | MEDIUM | Cooldown deadlock fixed, token bucket ordering |
| `app/classify.py` | ~150 | LOW | Evidence source misattribution |
| `app/validation.py` | ~80 | MEDIUM | SSRF opt-out too permissive |
| `app/settings.py` | ~80 | LOW | Clean |
| `app/api_models.py` | ~500 | LOW | Good validation, minor drift in agent contract |

---

*End of Adversarial Review*
