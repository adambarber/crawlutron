# Adversarial performance review — second pass

> **Historical document.** This review is dated 2026-09-15 and describes the tree as it stood then — including paths (e.g. `app/`) that no longer exist after later restructuring. It is retained as a record of what was reviewed, not as a description of the current layout.

All findings below are **new**; nothing from the first review is repeated. Items are prioritized by throughput impact.

---

## 1. Critical performance issues

### 1.1 `CrawlerSettings.from_mapping()` is called on every HTTP fetch

**Where:** `app/crawl/strategies/http.py` → `_effective_max_content_length` → `_default_global_concurrency`:

```python
def _default_global_concurrency() -> int:
    from app.settings import CrawlerSettings

    try:
        return max(1, int(CrawlerSettings.from_mapping().global_concurrency))
    except Exception:
        return 4
```

`_effective_max_content_length` is invoked once per strategy attempt in `_http_strategy_result`. Because `_payload_policy` normalizes `max_content_length_bytes` to `0` (the "unset" sentinel), and `0 > 0` is false, the fallback branch fires **on every HTTP/CFFI fetch**. `CrawlerSettings.from_mapping()` walks ~40 `CRAWLUTRON_*` env vars, each through `get_mapping_int`/`get_mapping_float`/`parse_bool_value` with try/except and logging.

**Impact:** ~100–500 µs per fetch of pure environment parsing. For a crawl of 10 000 URLs (two HTTP strategies each), that's 2–10 seconds of pure waste, plus a hidden coupling where a settings typo can silently change the effective per-page cap per request.

**Fix:** Hoist the concurrency into a module-level constant read once at import (or pass the *actual* batch concurrency — see 1.5 of the first review). At minimum, cache the parsed settings on the engine and pass it through.

---

### 1.2 `normalize_public_http_url` re-reads `os.environ` per link

**Where:** `app/validation.py`:

```python
def normalize_public_http_url(url: str) -> str:
    ...
    allow_private = parse_bool_env("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", default=False)
```

This runs for **every URL and every discovered link**. `_extract_discovered_links` calls it per anchor; `_payload_urls` calls it per submitted URL; scope filtering calls it per link; the frontier calls it per expansion. On a page with 2 000 links, that's 2 000 `os.environ.get` + `str.strip().lower()` + set-membership checks, plus the `ipaddress.ip_address` try/except and `_resolver_numeric_ipv4` string walk.

**Impact:** ~10–50 µs per link. A 1 000-page crawl with 500 links/page = 500 000 calls = 5–25 seconds of pure normalization overhead, on top of the actual fetch time. This is a per-link cost that scales linearly with discovery depth.

**Fix:** Hoist `allow_private` into a module-level constant evaluated once at import (`_ALLOW_PRIVATE_TARGETS = parse_bool_env(...)`), and add a fast path that skips IP-address classification when the hostname contains a letter beyond `[0-9a-fx.]` (i.e., it can't be a numeric literal). For repeated hostnames, cache `(host → is_private)` in a bounded dict.

---

### 1.3 `_ResultSpill._gc_stale_spills` scans the entire temp directory per crawl

**Where:** `app/crawl/engine.py` → `_ResultSpill.__init__`:

```python
try:
    self._gc_stale_spills(gc_age_seconds)
except Exception:
    pass
```

`_gc_stale_spills` does `os.listdir(tempfile.gettempdir())` then `os.path.getmtime` for every file whose name starts with `bc-spill-`. On a shared host with a busy `/tmp` (thousands of unrelated files), this is a full directory scan on **every crawl request**, including single-page crawls that don't need a spill at all.

**Impact:** O(temp_dir_size) per request. On a busy box this can be tens of milliseconds per request, and it grows without bound as other processes write to `/tmp`.

**Fix:** Rate-limit GC to once per N seconds (or once per process), track the last GC time in a module global, and skip the scan if less than, say, 60 s has elapsed. Also consider only running GC when the spill file itself exceeds a size threshold.

---

### 1.4 `HttpCache._evict_if_needed` materializes the full key list

**Where:** `app/crawl/cache.py`:

```python
def _evict_if_needed(self) -> None:
    if len(self._data) > self._max_entries:
        over = len(self._data) - self._max_entries
        for url in list(self._data)[:over]:
            self._data.pop(url, None)
```

`list(self._data)` allocates a list of **every key** in the cache (up to 100 000) just to pop the first `over` entries. Since `dict` preserves insertion order, `itertools.islice` would suffice.

**Impact:** ~800 KB allocation + 100 000-element iteration per eviction. Evictions happen on every `store()` once the cap is reached, so under sustained load this is a per-write cost that dwarfs the actual cache work.

**Fix:**
```python
import itertools

for url in list(itertools.islice(self._data, over)):
    self._data.pop(url, None)
```

---

### 1.5 `_BROWSER_COOKIES_BY_DOMAIN` has no lock

**Where:** `app/crawl/strategies/browser.py`:

```python
_BROWSER_COOKIES_BY_DOMAIN: OrderedDict[str, str] = OrderedDict()


def _store_browser_cookies(domain: str, header: str) -> None:
    _BROWSER_COOKIES_BY_DOMAIN[domain] = header
    while len(_BROWSER_COOKIES_BY_DOMAIN) > _BROWSER_COOKIES_MAX:
        _BROWSER_COOKIES_BY_DOMAIN.popitem(last=False)


def get_browser_cookies_for(domain: str) -> str | None:
    val = _BROWSER_COOKIES_BY_DOMAIN.get(domain)
    if val is not None:
        _BROWSER_COOKIES_BY_DOMAIN.move_to_end(domain)
    return val
```

`OrderedDict` is **not thread-safe for concurrent mutation**. `popitem(last=False)` during a concurrent `move_to_end` can raise `KeyError`, and concurrent `move_to_end` calls can corrupt the internal linked list. The GIL makes individual C-level ops atomic but not the composite operations here.

**Impact:** Under concurrent browser renders (the pool exists precisely for this), cookies can be lost, `KeyError` can propagate into the render path, or the LRU order can be corrupted. Failures here are silent (cookies simply not replayed on the CFFI retry), so they degrade capture without obvious symptoms.

**Fix:** Wrap both functions in a `threading.Lock`, or use the `BoundedTTLCache` chokepoint that already exists in the codebase with the right contract.

---

### 1.6 `DomainScheduler._purge_expired` runs on every cooldown set

**Where:** `app/scheduler.py`:

```python
def set_domain_cooldown(self, domain: str, seconds: float) -> None:
    if seconds > 0:
        self._domain_cooldowns[domain] = time.time() + seconds
    self._purge_expired()
```

`_purge_expired` rebuilds both `_domain_cooldowns` and `_circuit_open_until` by iterating all entries. It's called on every 429 cooldown set.

**Impact:** For a crawl hitting many rate-limited domains, this is O(domains) per 429. On a 10 000-domain crawl with 1 % rate-limited, that's 100 × 10 000 dict rebuilds = 1 M operations, plus GC pressure from the new dicts.

**Fix:** Call `_purge_expired` at most once per batch (in `run`), or gate it behind a counter (e.g., every 100 cooldown sets). The memory leak it guards against is bounded by domain count anyway.

---

### 1.7 `ContentCache._enforce_limits` unlinks files while holding the lock

**Where:** `app/crawl/content_cache.py`:

```python
def _enforce_limits(self) -> None:
    with self._lock:
        while self._index and self._over_limits():
            oldest_key = next(iter(self._index))
            self._drop_locked(oldest_key)  # calls old_path.unlink()
```

`_drop_locked` calls `old_path.unlink()`, which is a filesystem syscall, **inside the cache lock**. Under concurrent `set`/`aget` traffic, every eviction stalls all readers and writers.

**Impact:** Filesystem latency (often 100 µs–10 ms on network-backed or container-overlay filesystems) is added to the cache's critical section. With a large cache under steady write pressure, this can serialize the entire cache.

**Fix:** Collect the paths to unlink inside the lock, release the lock, then unlink. The index accounting must still happen under the lock; only the syscall moves out.

---

## 2. Concurrency / correctness issues affecting throughput

### 2.1 Double `checked_payload_urls` call per request

**Where:** `app/api/crawl_endpoint.py` calls `checked_payload_urls(payload_dict, settings.max_batch_size)` for validation, then `app/crawl/engine.py` → `crawl_request` calls it again. For a 1 000-URL batch, that's 2 000 `normalize_public_http_url` calls (each with the per-call env read from 1.2).

**Impact:** ~2× the URL-normalization cost for large batches. Small compared to fetch time, but pure waste.

**Fix:** Have `crawl_request` accept an optional pre-validated URL list, or move the batch-size gate entirely into the engine and have the endpoint call `checked_payload_urls` only to fail fast (in which case the engine should skip the check when the list is already provided).

---

### 2.2 `_seed_is_fresh` does a disk read per sitemap URL

**Where:** `app/crawl/engine.py` → `_seed_is_fresh` calls `content_cache.cached_at_of(key)` for **every strategy** in the policy, per sitemap URL:

```python
for strategy in policy.get("strategies", DEFAULT_STRATEGIES):
    key = _content_cache_key(...)
    cached_at = content_cache.cached_at_of(key)
```

`cached_at_of` → `_read_envelope` → `path.read_text()` + `json.loads`. For a sitemap with 10 000 URLs and a 4-strategy chain, that's 40 000 disk reads during seeding, before any fetching begins.

**Impact:** Seconds to minutes of disk I/O per large sitemap. The seed phase becomes the bottleneck for sitemap-driven crawls.

**Fix:** Either (a) batch the freshness check into one pass over the cache index (read the index once, compare in memory), or (b) restrict the check to the first strategy in the chain (the one that will actually be attempted first) rather than every strategy.

---

### 2.3 `ContentCache._read_envelope` blocks the event loop when called from async

**Where:** `_read_envelope` is a sync method that does `path.read_text()` + `json.loads()`. It's called from:
- `get()` (sync — fine)
- `cached_at_of()` (sync — but called from `_seed_is_fresh` inside the async `_sitemap_seed_urls`)

`_seed_is_fresh` is invoked from `_sitemap_seed_urls`, which runs inside the event loop (`asyncio.gather`). Each call performs a synchronous disk read on the loop.

**Impact:** For a sitemap with many URLs, this serializes the seed phase and blocks all concurrent crawls for the duration.

**Fix:** Either move `_seed_is_fresh` behind `asyncio.to_thread`, or make the freshness check use an async cache read (`aget` / a batched async path).

---

### 2.4 `scope_allows` wraps every link match in a timeout

**Where:** `app/crawl/scope_match.py` → `match_bounded(compiled, url)` uses `regex.Pattern.search(..., timeout=...)`. The `regex` engine's timeout adds per-call setup/teardown overhead (timer registration, signal handling on some builds).

**Impact:** For a scope-filtered crawl of 10 000 links, 10 000 timeout-wrapped matches. If the pattern is simple (the common case), the timeout overhead can dominate the actual match cost. The timeout is only needed for pathological patterns.

**Fix:** Compile a "fast" and a "bounded" variant: if the pattern is short and contains no nested quantifiers (`*+{` sequences), use stdlib `re` with no timeout; otherwise use the bounded engine. Or, since `compile_untrusted` already rejects over-long patterns, use a generous timeout (5 s) and rely on the length bound — the timeout is only a backstop for patterns that pass the length check but still backtrack.

---

### 2.5 `_SHARED_CONTENT_CACHE` and `_async_crawl_semaphore` are lazily initialized without locks

**Where:** `app/crawl/engine.py`:
```python
_SHARED_CONTENT_CACHE: ContentCache | None = None


def _get_shared_content_cache() -> ContentCache:
    global _SHARED_CONTENT_CACHE
    if _SHARED_CONTENT_CACHE is None:
        ...
```

And `app/api/async_crawl_runner.py`:
```python
_async_crawl_semaphore: asyncio.Semaphore | None = None


def _get_async_crawl_semaphore() -> asyncio.Semaphore:
    global _async_crawl_semaphore
    if _async_crawl_semaphore is None:
        ...
```

Two threads can both observe `None` and both construct. The loser's object is discarded (leaking the `ContentCache`'s SQLite/file handles or the semaphore's event-loop binding). For the semaphore, the discarded one may have been bound to a different loop.

**Impact:** Rare, but under a cold-start burst it can leak a `ContentCache` (with open directory handles) or produce a semaphore bound to the wrong loop, causing intermittent `RuntimeError: ... is bound to a different event loop`.

**Fix:** Use `threading.Lock` around the check-and-set, or accept the caller's lock-free contract and document the risk.

---

### 2.6 `_CGROUP_CACHE` is mutated without a lock

**Where:** `app/cgroup_sense.py`:

```python
_CGROUP_CACHE: dict[str, tuple[float, object]] = {}


def _read_text(path: str) -> str | None:
    ...
    _CGROUP_CACHE[path] = (now, value)
    return value
```

`_read_text` is called from `read_memory_stats` and `read_cpu_quota`, which are sync and may be invoked from `asyncio.to_thread` (via `_resource_gate` probes and admission checks). Concurrent dict writes from multiple threads can corrupt the dict (CPython makes single `__setitem__` atomic under the GIL, but the read-then-write pattern and `len` checks are not).

**Impact:** Low (CPython GIL makes individual dict ops atomic), but the cache can return stale or torn values, and a future PyPy/no-GIL build would break. The `_invalidate_cgroup_cache()` call from tests is not synchronized with in-flight reads.

**Fix:** Wrap `_read_text`/`_read_int` in a `threading.Lock`, or use the `BoundedTTLCache` chokepoint.

---

## 3. Resource-awareness gaps

### 3.1 `_HTTP_TASK_RAM_BYTES = 12 MiB` is still too low (new angle)

The first review noted this, but there's a second-order effect worth flagging: because the value is so low, the admission allows ~90 concurrent HTTP tasks at 3 GiB. Each task also holds:
- a `httpx` stream context (small)
- a decoded body string (up to `max_content_length` = 10 MiB)
- the lxml tree for the same body (~5–10× the HTML size in RAM during parse)
- trafilatura's internal buffers

So the *effective* per-task RAM is closer to 60–120 MiB for HTML pages, not 12 MiB. The admission will over-admit by 5–10×, and the resource gate will only intervene after the RSS has already spiked — too late to prevent OOM.

**Fix:** Raise to 48–64 MiB, or better, make the estimate content-type-aware: HTML tasks cost more than JSON/plain-text tasks.

---

### 3.2 `WeightedAdmission` double-scales the browser tier

The browser pool is sized to use ≤ 50 % of RAM (`_BROWSER_RAM_FRACTION = 0.5`). Then `WeightedAdmission.browser_capacity()` multiplies that pool size by `headroom_fraction()`:

```python
def browser_capacity(self) -> int:
    return max(0, int(self._p.max_browser_concurrency * self.headroom_fraction()))
```

If memory is at 60 %, headroom is 0.4, so the browser tier shrinks to 40 % of a pool that was already sized for RAM. The double discount means browser renders are throttled well below what RAM can support, starving the highest-value strategy on memory-heavy hosts.

**Fix:** Only apply the headroom scale when `memory_ratio() > _BROWSER_RAM_FRACTION` (i.e., when the pool's own reserve has been exceeded). Below that, the pool sizing already accounts for RAM; the admission should not shrink further.

---

### 3.3 `CpuGate.is_warm` short-circuits on unbounded cgroups

```python
def is_warm(self) -> bool:
    if self._quota_cpus() is None:
        return True
    return self._last_usage is not None and self._last_wall is not None
```

On a host with no cgroup CPU quota (bare-metal or `cpu.max = max`), the gate reports "warm" immediately, so the first recursive batch uses the full `_concurrency_mult = 2` ramp. That's fine for CPU, but the *memory* gate may not be warm either, and the two gates are combined via `min()`. The first batch can therefore overshoot on memory before the memory gate has a sample.

**Impact:** First-batch memory spike on unbounded-CPU hosts. Particularly relevant for the "first crawl after startup" scenario.

**Fix:** Have `ResourceGate` track warmth per sub-gate and use `concurrency_mult = 1` if *either* sub-gate is cold. The current `_cpu.is_warm()` check only guards the CPU side.

---

### 3.4 `_MAX_DERIVED_POOL_SIZE = 4` caps browser concurrency on large hosts

On a 64 GiB host, the derived browser pool is capped at 4 (by `_MAX_DERIVED_POOL_SIZE`), even though RAM would allow 45+ browsers. For maximum capture of challenge-heavy sites, this is a hard ceiling independent of resources.

**Impact:** Browser-strategy throughput plateaus at 4 × `max_concurrent_pages` = 12 concurrent renders on any host, regardless of RAM. On a 64 GiB box with 32 CPU cores, this leaves the machine idle.

**Fix:** Make the cap a function of RAM (e.g., `min(limit // _BROWSER_RAM_BYTES, cpu_count() * 2)`), or remove it and rely on `_BROWSER_RAM_FRACTION` plus the resource gate to prevent overshoot. The current cap exists to protect the 3 GiB default but is applied unconditionally.

---

### 3.5 `CRAWLUTRON_MAX_WEBHOOK_CONCURRENCY` defaults to 4

For "hundreds of pages per minute" via the async/webhook path, 4 concurrent background crawls is the bottleneck, not the crawler itself. Each crawl can process a batch, so the effective concurrency is `4 × batch_size`. For batch size 1 (single-URL requests), that's 4 URLs in flight.

**Impact:** The 202 path is limited to ~4 × (URLs per batch) × (batches/min) URLs/min. For single-URL webhook requests, that's 4 × 60 = 240 URLs/min at best, with no headroom.

**Fix:** Raise the default to 16 (still bounded), or derive it from `global_concurrency`.

---

## 4. Maximum capture: missing discovery primitives

These are pure capture gaps — URLs or content that the current pipeline will miss.

### 4.1 No `<base href>` handling

`_extract_discovered_links` resolves against `source_url`:

```python
normalized_url = normalize_public_http_url(urljoin(source_url, href))
```

If a page has `<base href="https://cdn.example.gov/assets/">`, links like `href="page.html"` resolve to the **page URL**, not the base. This is a correctness bug for many CMS-generated pages and will silently miss or mis-attribute links.

**Fix:** Before link extraction, scan the parsed tree for `<base href>` and use it as the resolution base (falling back to `source_url`).

### 4.2 No `Link` header parsing

HTTP `Link` headers (`Link: <https://example.gov/next>; rel="next"`) are the standard way to expose pagination, canonical URLs, and preload hints. Currently only HTML `<link rel="next">` and `<a rel="next">` are detected.

**Fix:** In `_http_strategy_result`, parse the `link` header (RFC 8288) and feed `rel="next"`, `rel="alternate"`, `rel="canonical"`, `rel="preconnect"`, `rel="dns-prefetch"` into the discovery diagnostics.

### 4.3 No `<meta http-equiv="refresh">` handling

Municipal sites frequently use meta refresh for redirects (`<meta http-equiv="refresh" content="0; url=/new-page">`). These are invisible to the crawler.

**Fix:** Extract the URL from the `content` attribute and add it to `discovered_links` with a `rel=["refresh"]` marker (or a dedicated `meta_refresh_url` field).

### 4.4 No `data-*` URL attribute extraction

Modern frameworks (Vue, React apps, HTMX) put URLs in `data-href`, `data-url`, `data-src`, `data-action`, `data-endpoint`. SPA-routed municipal portals are increasingly built this way.

**Fix:** Add a permissive scan of `data-*` attributes on `a`, `button`, `div`, `form`, and `[data-href]` selectors. Reuse `public_url_or_none` for egress filtering.

### 4.5 No `<area href>` support

Image maps on municipal GIS pages use `<area href>`. Currently only `<a>` tags are scanned.

**Fix:** Extend `_extract_discovered_links` to `tree.iter("a", "area")`.

### 4.6 No `srcset` / `sizes` extraction

Image URLs from `srcset` are not captured. For content extraction this is irrelevant, but for asset discovery (e.g., historical map scans) it matters.

**Fix:** Add a `discovered_images` diagnostic with `src`, `srcset`, `sizes` from `<img>`, `<source>`, `<picture>`.

### 4.7 No CSS `url()` / `@import` extraction

Stylesheet URLs and `background-image: url(...)` values are not discovered. For a "maximum capture" crawler, these reveal asset trees.

**Fix:** Parse `<style>` blocks and inline `style=""` attributes for `url(...)`; resolve against the page URL and add to diagnostics.

### 4.8 No `<object>` / `<embed>` / `<iframe>` following

`_extract_frames` reports iframe URLs but the recursive crawler does not enqueue them (documented as out of scope). For capture, iframe content often contains the real page (e.g., legacy frameset sites, Google Docs embeds).

**Fix:** Add a policy flag `follow_frames: true` that enqueues same-origin iframe URLs at depth+1. Cross-origin frames should still be recorded but not fetched by default.

### 4.9 No JSON-LD URL extraction

`_extract_llm_metadata` captures JSON-LD into `metadata.json_ld` but does not extract URLs from `@id`, `url`, `sameAs`, `contentUrl`, `embedUrl`, `image`, `logo`, `mainEntityOfPage`. These frequently point to canonical pages, structured data, or asset trees.

**Fix:** Walk the parsed JSON-LD for these keys and add discovered URLs to diagnostics (with a `json_ld` source marker). Pipe through `public_url_or_none`.

### 4.10 No `<link rel="alternate" hreflang>` handling

International municipal sites advertise language alternates via `<link rel="alternate" hreflang="fr" href="/fr/page">`. These are separate content pages that the current crawler will miss unless linked from the visible navigation.

**Fix:** Extract `hreflang` alternates into `discovered_links` with `rel=["alternate", "hreflang"]`.

### 4.11 No `<link rel="amphtml">` handling

AMP versions are often the only cacheable version of a page and sometimes the only one not blocked.

**Fix:** Extract `amphtml` links into a dedicated `diagnostics.amp_url` field; optionally enqueue as a fallback strategy.

### 4.12 No `X-Robots-Tag` header handling

A page with `X-Robots-Tag: noindex, nofollow` is currently fetched and parsed normally. The service honors robots.txt but not the per-response directive.

**Fix:** After fetching, check `x-robots-tag` and (a) classify the response as `robots_disallowed` if `noindex`, (b) skip link extraction if `nofollow`. This is both a capture and a politeness improvement.

### 4.13 No `Content-Location` header handling

The `Content-Location` header indicates the canonical URI of the returned representation. Useful for deduplicating across redirect chains.

**Fix:** Surface it in diagnostics; optionally use it as the canonical key for content-cache dedup.

---

## 5. Maximum capture: missing classification / HTTP handling

### 5.1 Missing HTTP status classifications

`_http_strategy_result` maps 403→`access_denied`, 404→`not_found`, 410→`gone`, 429→`rate_limited`, ≥500→`site_error`. Missing:

- **451 Unavailable For Legal Reasons** — common on municipal sites for records removed by court order. Classify as `legal_block` (new classification) or `access_denied` with an `http_status=451` signal.
- **507 Insufficient Storage** — server-side capacity, not client error. Currently falls into `site_error` and is terminal.
- **511 Network Authentication Required** — captive portal, common in hotel/guest networks. Should be classified as `login_required` with a distinct signal.
- **300 Multiple Choices** — could indicate alternative representations.
- **203 Non-Authoritative Information** — proxy-modified content.

### 5.2 `Retry-After` only honored on 429

A 503 with `Retry-After: 60` is common during maintenance. Currently classified as `site_error` (terminal) with no cooldown. The origin's explicit recovery window is ignored.

**Fix:** Parse `Retry-After` on 503 (and 502/504) and set a domain cooldown, same as 429. The `_retry_after_seconds` helper already exists; just extend the branch.

### 5.3 `robots.txt` directives not honored

The parser handles `User-agent`, `Allow`, `Disallow`, `Crawl-delay`, `Sitemap`. Missing:

- **`Request-rate: 1/5s`** — the older directive some municipal sites still use for politeness. Parse and translate to a per-domain delay.
- **`Noindex: /path`** — non-standard but present on some older stacks.
- **`Host:`** — Yandex-specific canonical host directive.

### 5.4 `site_error` is terminal but sites recover

A 500 today may be a 200 tomorrow. The current classification marks `site_error` as terminal in whole-site jobs (no re-queue). For maximum capture, transient 5xx should be re-queued like `network_error`.

**Fix:** Add `site_error` with `http_status >= 500` to `TRANSIENT_CLASSIFICATIONS` in `job_runner`, or at least to the whole-site retry path with a lower cap.

### 5.5 No captive-portal / walled-garden detection

A 200 response whose body is a login or captive-portal page is currently classified by prose signature, which works for common cases but misses branded portals. The `login_required` classification fires only on specific phrases.

**Fix:** Add structural signals: a form with exactly one password field and no other content, or a redirect to an auth domain. Enrich `STRONG_CLASSIFICATION_MARKERS` with these.

### 5.6 No SPA/hash-route handling

`#/page/2` fragments are stripped by `normalize_public_http_url`, so SPA routes that use hash-based navigation are collapsed to the root URL. For sites that use hash routing (increasingly rare but still present in older municipal systems), this misses the entire content tree.

**Fix:** Add a policy flag `preserve_fragments: true` that skips fragment-stripping during discovery (not during fetch — fragments never reach the server). Or detect the hash-routing pattern (fragment matches `/^#!?\//`) and enqueue the fragment as a separate discovery entry.

---

## 6. Additional optimizations

### 6.1 `_extract_llm_metadata` runs again in `render_result_to_unified`

`render_result_to_unified` sets `"markdown": None` unconditionally:

```python
"markdown": None,
```

But `/v1/render` results pass through `build_unified_crawl_response` → `build_unified_result` → `format_crawl_item`, which sees `markdown=None`, then falls back to `text`:

```python
def format_crawl_item(item):
    markdown = item.get("markdown")
    if markdown:
        ...
    text = item.get("text")
    if not text:
        return None, {}
    return text, {}
```

So render results always return plain text as markdown, **never** trafilatura-processed. This is both a capture gap (render output isn't cleaned) and a cost (the HTML is re-parsed by `format_crawl_item`? No — `format_crawl_item` only parses when `html` and `url` are set and `markdown` is truthy, which it isn't here). So no wasted parse, but the render path bypasses the unified extraction entirely.

**Fix:** Have `render_result_to_unified` run the same extraction pipeline as the HTTP/browser strategies (trafilatura for HTML, markitdown for documents) rather than returning `None` for markdown.

### 6.2 `MetricsStore.as_prometheus` rebuilds the entire string on every scrape

For a service with `strategy_counts` and `classification_counts` populated across ~30 classifications, the output is ~100 lines rebuilt from scratch on every Prometheus scrape (typically every 15 s). Not a hot path, but the string concatenation uses `"".join(lines)` at the end, which is fine; the cost is the f-string formatting per line. At 100 lines × 15 s = negligible.

Skip unless the scrape interval drops below 1 s.

### 6.3 `_purge_expired` in `DomainScheduler` uses `time.time()` while circuit breakers use `time.monotonic()`

Mixing wall-clock and monotonic clocks is correct here (cooldowns are wall-clock-relative to `Retry-After`, circuit breakers are monotonic), but it means the `_domain_cooldowns` dict can accumulate entries if the system clock moves backward. The `now = time.time()` purge will not evict future-dated entries.

**Impact:** Minor memory leak on clock adjustments. Not a throughput issue.

### 6.4 `_HTTP_CLIENT_INIT_LOCK` and `_PENDING_CLIENT_CLOSE_LOCK` are never released on process exit

Both are module-level `threading.Lock`s. If a request thread crashes while holding one, subsequent requests will deadlock. This is a general pattern in the codebase, but these two are on the hot path of every HTTP fetch (`get_async_http_client` acquires `_HTTP_CLIENT_INIT_LOCK`).

**Impact:** Low probability, high blast radius. The lock scope is small (client construction and pending-close list mutation), so a crash inside is unlikely but possible.

**Fix:** Use `threading.RLock` and/or `with` blocks (already used), and ensure no exceptions can escape while the lock is held. The construction path already catches exceptions; verify the pending-close path does too.

### 6.5 `_decode_body` tries `body.decode(charset)` first, then `charset_normalizer`

For a body with a wrong declared charset, the strict decode fails and the code falls back to `charset_normalizer`, which scans the whole body. For a 10 MiB body with declared `utf-8` but actually `windows-1252`, this is a full scan per fetch.

**Impact:** ~10–50 ms per affected fetch. Common on older municipal sites with mismatched headers.

**Fix:** Add a heuristic pre-check: if the body contains only ASCII bytes, skip charset detection entirely (fast path). Or use `charset_normalizer`'s streaming API with a sampled prefix.

### 6.6 `_html_diagnostics` and `_extract_discovered_links` share a tree, but `extract_js_endpoints` does not

`_http_strategy_result` passes `tree` to `_html_diagnostics` and `_extract_discovered_links`, but `extract_js_endpoints(html, normalized_url)` takes the raw HTML and runs regex. For large pages, this is an extra O(html) scan after the tree is already parsed. If the tree were used (iterating `<script>` tags), the scan would be O(scripts) instead of O(html).

**Impact:** One extra full-HTML regex scan per page during discovery. ~1–5 ms per page on large pages.

**Fix:** Extend `extract_js_endpoints` to accept an optional parsed tree and iterate `<script>` text nodes.

### 6.7 `_extract_discovered_links` builds a `DiscoveredLink` per anchor

The dataclass is frozen and has 7 fields. For 2 000 anchors, that's 2 000 allocations plus `urljoin`, `urlparse`, `normalize_public_http_url`, `re.sub` for whitespace, and `unescape` on the text. Per-link cost is ~30–100 µs.

**Impact:** 60–200 ms per page for link-heavy pages. On a crawl of 1 000 pages with 500 links each, that's 30–100 seconds of pure link extraction.

**Fix:** Batch the normalization: collect all hrefs first, normalize in one pass with a shared host cache, then construct dataclasses. Also consider lazy evaluation — only normalize and construct links if the caller will consume them (i.e., if `max_discovered_links > 0` and discovery is enabled).

### 6.8 `_content_cache_key` serializes headers per fetch

```python
json.dumps(sorted(base_headers.items()), separators=(",", ":"))
```

For fingerprint rotation profiles with many headers, this is a per-fetch JSON serialization. For a stable request profile, the key is identical across fetches and could be cached per `(url, strategy, extraction, selectors)` tuple.

**Impact:** ~10–50 µs per fetch. Small but pure waste.

**Fix:** Cache the header-key component in a bounded dict keyed by `(strategy, frozenset(base_headers.items()))`.

### 6.9 `_ResultSpill.write` writes synchronously under a lock

```python
async def write(self, index: int, result: EngineCrawlResult) -> None:
    line = await asyncio.to_thread(json.dumps, {"i": index, "r": to_dict(result)})
    async with self._write_lock:
        self._fh.write(line + "\n")
```

The `to_dict` and `json.dumps` are offloaded, but the `_fh.write` is a synchronous file write on the event loop under a lock. For large results, this blocks.

**Fix:** Offload the write too (batch multiple lines and write via `to_thread`), or use an OS-level `os.write` on a raw fd with `O_APPEND` to avoid Python's buffered I/O overhead.

### 6.10 `BoundedTTLCache.purge_expired` is O(n) with a lock

```python
def purge_expired(self, now: float | None = None) -> int:
    cutoff = self._clock() if now is None else now
    with self._lock:
        stale = [key for key, (expires_at, _v) in self._entries.items() if expires_at <= cutoff]
        ...
```

For a 100 000-entry cache (HttpCache uses a different class, but `robots_cache` and `_SITEMAP_CACHE` use BoundedTTLCache), this iterates all entries under the lock. `_purge_robots_cache` is called only from tests, but `put()` internally relies on the LRU eviction which is O(1) — so the O(n) purge is not on the hot path. Fine.

Skip unless the cache size grows.

---

## 7. Summary table

| Priority | Finding | Category | Impact |
|----------|---------|----------|--------|
| P0 | `CrawlerSettings.from_mapping()` per HTTP fetch (1.1) | Perf | Env parse per fetch |
| P0 | `normalize_public_http_url` reads env per link (1.2) | Perf | O(links) env ops |
| P0 | `_gc_stale_spills` scans tempdir per crawl (1.3) | Perf | O(tempdir) per request |
| P0 | `HttpCache._evict_if_needed` full-list materialization (1.4) | Perf | O(cache size) per eviction |
| P0 | `_BROWSER_COOKIES_BY_DOMAIN` unlocked OrderedDict (1.5) | Concurrency | Silent cookie loss / corruption |
| P0 | `_purge_expired` per cooldown set (1.6) | Perf | O(domains) per 429 |
| P0 | `_enforce_limits` unlinks under lock (1.7) | Perf | Filesystem latency in critical section |
| P1 | Double `checked_payload_urls` (2.1) | Perf | 2× URL normalization |
| P1 | `_seed_is_fresh` disk read per sitemap URL (2.2) | Perf | O(sitemap × strategies) disk reads |
| P1 | `_read_envelope` blocks loop from async (2.3) | Perf | Event loop stall during seeding |
| P1 | `scope_allows` timeout overhead per link (2.4) | Perf | O(links) timer setup |
| P1 | Lazy singletons without locks (2.5) | Concurrency | Leaks / wrong-loop binding |
| P1 | `_CGROUP_CACHE` unlocked (2.6) | Concurrency | Stale reads, future-proofing |
| P1 | `_HTTP_TASK_RAM_BYTES` too low (3.1) | Resource | Over-admission → OOM risk |
| P1 | Browser tier double-scaling (3.2) | Resource | Starves browser renders |
| P1 | `_MAX_DERIVED_POOL_SIZE = 4` (3.4) | Resource | Caps browser throughput on large hosts |
| P1 | Webhook concurrency default 4 (3.5) | Resource | Caps 202-path throughput |
| P2 | `<base href>` not handled (4.1) | Capture | Wrong link resolution |
| P2 | `Link` header not parsed (4.2) | Capture | Misses pagination/canonical |
| P2 | `meta refresh` not handled (4.3) | Capture | Misses redirect targets |
| P2 | `data-*` attributes not scanned (4.4) | Capture | Misses SPA routes |
| P2 | `<area>` not scanned (4.5) | Capture | Misses image maps |
| P2 | `srcset`/`@import`/`object`/`embed` (4.6–4.8) | Capture | Misses asset trees |
| P2 | JSON-LD URL extraction (4.9) | Capture | Misses structured-data URLs |
| P2 | `hreflang`/`amphtml` (4.10–4.11) | Capture | Misses alternates |
| P2 | `X-Robots-Tag` / `Content-Location` (4.12–4.13) | Capture | Ignored directives |
| P2 | 451/507/511 classifications (5.1) | Capture | Misclassified outcomes |
| P2 | `Retry-After` on 5xx (5.2) | Capture | Ignores recovery window |
| P2 | `Request-rate` robots directive (5.3) | Capture | Ignores politeness |
| P2 | `site_error` terminal (5.4) | Capture | No re-queue on recovery |
| P2 | Captive portal detection (5.5) | Capture | Misses portal pages |
| P2 | SPA hash routes (5.6) | Capture | Collapses to root |
| P3 | Render bypasses extraction (6.1) | Capture | Raw text returned |
| P3 | `extract_js_endpoints` not tree-aware (6.6) | Perf | Extra O(html) scan |
| P3 | Link dataclass allocation per anchor (6.7) | Perf | O(links) allocations |
| P3 | `_content_cache_key` header serialization (6.8) | Perf | Per-fetch JSON |
| P3 | `_ResultSpill.write` sync write under lock (6.9) | Perf | Loop stall on large spills |
| P3 | Charset detection fallback (6.5) | Perf | Full-body scan on mismatch |

---

## 8. Recommended sequencing

**Week 1:** Hoist settings/env reads out of hot paths (1.1, 1.2), rate-limit spill GC (1.3), fix `HttpCache` eviction (1.4), add lock to `_BROWSER_COOKIES_BY_DOMAIN` (1.5), gate `_purge_expired` (1.6), move unlink out of lock (1.7). These are all small, localized changes with measurable impact.

**Week 2:** Single `checked_payload_urls` (2.1), batch sitemap freshness (2.2), offload `_seed_is_fresh` (2.3), fast/bounded scope regex split (2.4), lock lazy singletons (2.5), raise `_HTTP_TASK_RAM_BYTES` (3.1), fix double browser scaling (3.2), make `_MAX_DERIVED_POOL_SIZE` RAM-derived (3.4), raise webhook concurrency (3.5).

**Week 3 (P2 capture):** `<base href>`, `Link` header, `meta refresh`, `data-*`, `<area>`, `srcset`, JSON-LD URLs, `hreflang`, `amphtml`, `X-Robots-Tag`, 451/507/511, `Retry-After` on 5xx, `Request-rate`, `site_error` re-queue, SPA hash routes. These directly expand capture rate.

**Week 4 (P3 polish):** Render extraction, tree-aware JS endpoint extraction, batched link construction, cache key memoization, async spill writes, ASCII fast path for decode.

The first two weeks alone should lift the sustained throughput into the hundreds-per-minute range on a 3 GiB container; the capture work is what turns that into *useful* pages rather than just *fetched* ones.
