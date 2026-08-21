# Adversarial Security & Robustness Review — browser-crawler

**Date:** 2026-08-21 (updated to incorporate an additional external review)
**Scope:** `docker/browser-crawler` (app/, tests/, configs). Read-only analysis; no code modified.
**Methodology note:** Three focused subagents were first dispatched (network / resources / API). Two external model
providers returned `402` (unconfigured keys) and did zero work; one `hy3-free` child performed genuine analysis but
exhausted its turn budget before writing its file. This report was completed directly in the parent agent, incorporating
the real findings recovered from that child's transcript and verifying every claim against the current source. An
additional adversarial review (user-provided) was then received and its two findings were verified against the code and
folded in as **F2 (expanded)** and **F9 (new)** below.

---

## 1. Prior-fix verification (the 7 earlier findings)

All seven previously-fixed issues are **present and effective** in the current code:

| Prior finding | Verification | Status |
|---|---|---|
| CRITICAL ReDoS in recursive scope regex | `_compile_scope_regex_safe` used in `engine.py` loop | OK |
| HIGH raw exception text in API diagnostics | `error_detail` moved to `logger.warning(..., extra={"error_detail": ...})` (http.py:1073); not in returned `diagnostics` | OK |
| HIGH local-mode SSRF misroute to 127.0.0.1 | `_resolve_and_pin_host` returns real hostname when `ALLOW_PRIVATE_TARGETS=1` (http.py:337) | OK |
| MEDIUM robots regex recompile | `_robots_pattern_to_regex` wrapped in `functools.lru_cache` (engine.py) | OK |
| MEDIUM httpx client leak on loop change | `_schedule_client_close`/`_safe_aclose` (http.py) | OK |
| MEDIUM JobStore concurrency | `threading.threading.Lock` + `PRAGMA busy_timeout=5000` (job_store.py) | OK |
| LOW XPath/CSS selector DoS | `_eval_selector` worker-thread timeout (extraction.py) | OK |

**Caveat:** the SSRF fixes cover the *initial* URL (resolve-once pinning + private-IP rejection). They do **not**
cover (a) **cross-host HTTP redirects** (F1), (b) the **browser render path** including DNS-rebinding and the
post-redirect document fetch (F2), or (c) the **unresolved-hostname → 127.0.0.1 fallback** (F9). These three are the
remaining SSRF exposure.

---

## 2. Summary of findings

| ID | Severity | Area | Location |
|---|---|---|---|
| F1 | HIGH | SSRF via HTTP redirect bypass | http.py:404,427 |
| F2 | HIGH | Browser SSRF: DNS-rebinding + redirect + unprotected `urlopen` | browser.py:165,184,206,209; document.py:71,80 |
| F3 | HIGH | Unauthenticated crawler API (enables F1/F2/F9 + abuse) | main.py; README.md:4,154 |
| F4 | MEDIUM | `max_pages=0` => unbounded recursive crawl | payload_helpers.py:57-58 |
| F5 | MEDIUM | Document extraction unbounded (XML entity expansion, no wall-clock cap) | extraction.py:245-247 |
| F6 | MEDIUM/LOW | `ContentCache` unbounded on disk when enabled | content_cache.py:42,69-71 |
| F7 | LOW | SSRF check misses CGNAT / multicast / NAT64 / unspecified | http.py:325 |
| F8 | LOW | Browser reaches internal hosts via redirects/subresources (unpinned) | browser.py:184 |
| **F9** | **HIGH** | **Unresolved-hostname → 127.0.0.1 SSRF bypass (fetch + webhook)** | **http.py:350,379; main.py:356,358-367** |

> Severity note: the external review rated F2's DNS-rebinding vector MEDIUM; it is assessed **HIGH** here because its
> impact is SSRF-to-internal (same class as F1/F9). F9 is rated HIGH by both.

---

## 3. Detailed findings

### F1 — SSRF via HTTP redirect bypass  (HIGH)
**Location:** `app/crawl/strategies/http.py`
- `_build_pinned_request` calls `_check_ssrf_protection(url)` for the *initial* URL only (line 404).
- The fetch uses `client.stream("GET", pinned_url, ..., follow_redirects=True)` (line 427).

**Attack:** The crawler fetches a user-supplied (or discovered) URL whose host is attacker-controlled
(e.g. `http://evil.example/`). `evil.example` returns `302 -> http://169.254.169.254/latest/meta-data`
(or `http://10.0.0.1/`, `http://localhost:...`). httpx automatically follows the redirect and connects
to the **new host**, which is **never re-checked** by `_check_ssrf_protection`. The IP pinning applied to
the initial host does not carry across a cross-host redirect. Result: full SSRF to cloud metadata / internal
services, defeating the entire SSRF control.

**Fix:** Disable automatic redirect following (`follow_redirects=False`) and implement manual redirect
handling that, for **every hop**, re-runs `_check_ssrf_protection` and re-pins the IP via
`_resolve_and_pin_host` before connecting. Treat redirect loops and excessive hop counts as errors.

---

### F2 — Browser SSRF: DNS-rebinding + redirect + unprotected document fetch  (HIGH; reviewer rated DNS-rebinding MEDIUM)
**Location:** `app/crawl/strategies/browser.py`, `app/crawl/strategies/document.py`
- `_render_with_browser` calls `_check_ssrf_protection(url)` on the **initial** URL only (browser.py:165).
  A code comment claims "Camoufox also validates" — this is incorrect; Camoufox (a modified Firefox) has **no
  built-in SSRF protection**, and the browser re-resolves and follows redirects/subresources to whatever hosts
  they point at.
- `page.goto(url)` (browser.py:184) passes the **original** `url`, so the browser's network stack resolves the
  hostname **again at navigation time** → **DNS-rebinding (TOCTOU) SSRF**: an attacker's DNS returns a public IP
  for the first resolution (passes the check) and a private IP (e.g. `127.0.0.1`, `10.0.0.0/8`) for the second
  resolution when the browser connects.
- `final_url = page.url` (browser.py:206) is the **post-redirect** URL; for PDFs,
  `text = self._pdf_text_extractor(final_url)` (browser.py:209) calls the default extractor
  `_default_document_text_extractor`, which does `urllib.request.urlopen(url, timeout=30)` (document.py:71,80) —
  **no SSRF protection at all** (the `# noqa: S310 - public crawled URL` justification is invalid because
  `final_url` can be an internal post-redirect address).

**Attack (DNS-rebinding):** Crawl a page on an attacker-controlled domain whose DNS is configured to flip from a
public IP (passes `_check_ssrf_protection`) to a private IP at browser-connect time. The browser navigates to the
private address and returns its rendered content/screenshot → SSRF-to-internal read. **Attack (redirect/PDF):** a
page or PDF link that `302`s to `http://169.254.169.254/...` makes `final_url` internal, and `urlopen` fetches it
with no guard.

**Fix:**
- Pin the browser navigation to the validated IP: compute `(hostname, pinned_ip) = _resolve_and_pin_host(url)`,
  build a `pinned_url` (`http://<pinned_ip>:<port>/...`), and `page.goto(pinned_url, extra_http_headers={"Host": hostname})`
  (or use `page.route` to rewrite the host) so the browser connects to the exact validated IP and cannot be rebound.
- Re-run `_check_ssrf_protection(final_url)` (and on any subresource fetch) before using it; route document
  fetching through the pinned httpx fetcher (which has SSRF protection) instead of raw `urllib.request.urlopen`.

---

### F3 — Unauthenticated crawler API  (HIGH)
**Location:** `app/main.py` (no auth dependency anywhere in `app/`); README.md:4,154 (FastAPI, listen port
`BROWSER_CRAWLER_HOST_PORT` default 8765).

A repo-wide search for `Depends`/`Bearer`/`api_key`/`Authorization`/`security` finds **no authentication or
authorization enforcement** (the only `Depends` is DI in `health/readiness.py`; `api_models.py:118` merely
*describes* "authorized to crawl" in a docstring — not enforced).

**Impact:** Anyone who can reach the port can:
- Trigger F1/F2/F9 SSRF against internal infrastructure / cloud metadata.
- Drive recursive crawls (`max_pages=0`, see F4) to abuse third parties (bandwidth amplification /
  crawler-as-DDoS) at the operator's egress.
- Force resource exhaustion (large fetches + unbounded extraction, F5/F6).

**Fix:** Require an API key / token on mutation endpoints (at minimum the crawl-orchestration routes); bind to
`127.0.0.1` by default or place behind an authenticated reverse proxy; add per-client rate limiting and a
global concurrency cap.

---

### F4 — `max_pages=0` => unbounded recursive crawl  (MEDIUM)
**Location:** `app/crawl/payload_helpers.py:57-58`
```python
max_depth = max(0, min(20, int(limits.get("max_depth", 0))))
max_pages = max(0, min(2000, int(limits.get("max_pages", 0))))
```
`min(2000, 0) == 0`, so a caller sending `max_pages: 0` gets **no page cap**; only `max_depth` (<=20) bounds
the crawl. With depth 20 and no page limit, a recursive BFS over a link-rich site can fetch thousands of pages
(each up to 50 MB + extraction), a serious resource/abuse vector — especially unauthenticated (F3).

**Fix:** Treat `0` as a sane default (e.g. 200) or reject it; lower the ceiling for unauthenticated callers;
add a global in-flight crawl/page budget.

---

### F5 — Document extraction unbounded (XML entity expansion / no wall-clock cap)  (MEDIUM)
**Location:** `app/crawl/extraction.py:245-247` (`_extract_document` -> `markitdown`), vs the CSS/XPath cap
`_eval_selector` (extraction.py:46-60) added for the earlier LOW finding.

The decompression-bomb guard caps the **HTTP body** at 50 MB, but a 50 MB malicious `XML`/`XLSX`/Office
document with internal entity expansion (billion-laughs) is handed to `markitdown`/`lxml` with **no
size/memory/wall-clock bound**. Unlike CSS/XPath selectors (which now run under `_eval_selector`'s timeout),
document extraction has no timeout, so a crafted document can balloon memory or hang the worker.

**Fix:** Wrap document extraction in a wall-clock-bounded worker (mirror `_eval_selector`); cap document size
before parsing; pass `huge_tree=False` / disable entity resolution where possible; consider streaming/limit
on markitdown input.

---

### F6 — `ContentCache` unbounded on disk when enabled  (MEDIUM/LOW)
**Location:** `app/crawl/content_cache.py:42,69-71`
```python
def __init__(self, ..., max_entries: int | None = None, ...):
    self.max_entries = max_entries
...
def _enforce_max_entries(self) -> None:
    if self.max_entries is None or self.max_entries <= 0:   # no-op when None
        return
```
When `content_cache_enabled=True` (opt-in), there is **no LRU eviction** (default `max_entries=None`) and **no
per-entry size cap**. The cached envelope includes `body_b64` (base64 of the <=50 MB decompressed body, ~67 MB
per entry). Many distinct URLs => unbounded disk growth (DoS).

**Fix:** Enforce a non-`None` default `max_entries` and a per-entry size cap when caching is enabled; surface
cache size limits in config.

---

### F7 — SSRF runtime check misses CGNAT / multicast / NAT64 / unspecified  (LOW)
**Location:** `app/crawl/strategies/http.py:325`
```python
def _is_private_or_loopback_ip(ip: str) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_link_local
```
Missing `is_reserved`, `is_multicast`, `is_unspecified`. In this Python, `100.64.0.0/10` (CGNAT) is **not**
`is_private`, and multicast (`224.0.0.1`) / IPv6 NAT64 (`64:ff9b::/96`) / `0.0.0.0` are not rejected. Edge
cases (not the dominant SSRF path, which is F1/F2/F9), but trivially hardenable.

**Fix:** Also reject `is_reserved/is_multicast/is_unspecified`; add CGNAT (`100.64.0.0/10`) and IPv6
unique-local (`fc00::/7`) ranges to the blocklist.

---

### F8 — Browser reaches internal hosts via redirects/subresources (unpinned)  (LOW)
**Location:** `app/crawl/strategies/browser.py:184` (`page.goto` follows redirects; subresources loaded freely).
Even outside the PDF/`urlopen` path (F2), the headless browser navigates to and loads resources from
redirect/internal targets with no pinning or per-resource check. The "Camoufox also validates" comment
overstates protection.

**Fix:** Document the egress risk; for sensitive deployments, run the browser in a restricted network
namespace / through the authenticated, SSRF-checked fetch path.

---

### F9 — Unresolved-hostname → 127.0.0.1 SSRF bypass (fetch + webhook)  (HIGH)
**Location:** `app/crawl/strategies/http.py:350,379`; `app/main.py:356,358-367`

When a hostname does **not** resolve, the SSRF helpers fail open in a way that connects to loopback:

- `_check_ssrf_protection` (http.py:350) does `except socket.gaierror: return` — it **silently passes** an
  unresolvable hostname instead of blocking it.
- `_resolve_and_pin_host` (http.py:378-379) does `except socket.gaierror: return hostname, "127.0.0.1"` —
  it falls back to pinning **`127.0.0.1`**.

Because `_check_ssrf_protection` passes for an unresolvable host, the request is not blocked, and the
`127.0.0.1` pin makes the HTTP client connect to **loopback** (port 80/443 from `_build_pinned_request`).
An attacker submitting a URL with a non-resolving hostname (e.g. `http://nonexistent.invalid/`) forces the
crawler to fetch `http://127.0.0.1:80/...` and return the response as the crawl result — reaching any local
service on that port. This *bypasses* the normal private-IP block (a direct `http://127.0.0.1/` **would** be
blocked, but the unresolvable-hostname trick is not).

**Blast radius — webhook path too:** `_deliver_webhook` (main.py:323) calls
`_resolve_and_pin_host(webhook_url)` (main.py:356) and POSTs the crawl payload to the resulting `pinned_url`
(main.py:358-367). `_validate_webhook_url` (main.py:371-395) checks `localhost`/`host.docker.internal` and calls
`_check_ssrf_protection`, but as noted that call **silently passes on gaierror**. So an unresolvable webhook URL
passes validation and is delivered to `127.0.0.1` — an SSRF that delivers the crawl result/payload to an internal
service. Both the crawl-fetch and webhook-outbound paths share this root cause, so a single fix in
`_resolve_and_pin_host` closes both.

**Fix:** On `socket.gaierror`, do **not** fall back to loopback. Recommended:
```python
    except socket.gaierror:
        return hostname, hostname   # client re-resolves and fails with a connection error (no loopback)
```
(or fail closed with `raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}")`). Also make
`_check_ssrf_protection` fail closed on `gaierror` (treat an unresolvable hostname as not-a-valid-public-target
rather than silently returning). Either way, the request must never be routed to `127.0.0.1` for an
attacker-influenced hostname.

---

## 4. Recommended priority order
1. **F9** + **F1** + **F2** — close the three SSRF gaps (unresolved-hostname loopback, HTTP redirect, browser
   DNS-rebinding/redirect/urlopen). F9 is a one-line root-cause fix that also covers the webhook path.
2. **F3** — authenticate the API and rate-limit (turns F1/F2/F9/abuse into authenticated-only).
3. **F4** / **F5** — bound recursive crawl size and document-extraction time/memory.
4. **F6** / **F7** / **F8** — hardening and defense-in-depth.

## 5. Verification notes
- All line citations are against the current working tree at commit `9751250`.
- No code was modified during this review.
- The 7 prior fixes were independently confirmed present and effective (Section 1).
- The two findings from the additional external review were verified against the source and incorporated as
  **F2 (expanded: DNS-rebinding + redirect + urlopen)** and **F9 (new: unresolved-hostname → 127.0.0.1 SSRF,
  fetch + webhook)**.
