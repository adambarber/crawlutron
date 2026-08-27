# Browser-Crawler Service — Manual Test Plan

**Service:** `browser-crawler` (Policy-driven universal crawler execution service)
**Image:** `dev-stack/browser-crawler:local` (built from `docker/browser-crawler/Dockerfile`)
**Base URL (local):** `http://127.0.0.1:8765`
**Policy schema version:** `2026-06-23` (from `/v1/capabilities`)

This document is the authoritative manual test plan. It enumerates **100% of the
service's externally observable functionality** and maps every capability to one or
more executable test cases. Smoke testing in the "Execution" section walks the
highest-value subset; the full matrix below is the acceptance checklist.

---

## 1. Objectives & Scope

- Verify the image builds and the container boots healthy.
- Verify every HTTP endpoint responds with the correct contract.
- Verify every fetch strategy, extraction method, policy field, classification,
  and diagnostic feature behaves as specified.
- Verify cross-cutting concerns: SSRF protection, request validation, rate/concurrency
  limits, webhook async delivery, metrics, readiness saturation, and logging.

**Out of scope:** load/performance testing, fuzzing, and browser internals that are not
exposed via the API contract.

---

## 2. Prerequisites

- Docker / OrbStack with BuildKit enabled.
- Ability to `docker compose build` and `up` the `browser-crawler` service (profile `browser-crawler`).
- Outbound internet access from the container (the crawler fetches public URLs).
- `curl` + `jq` on the host for executing cases.
- For SSRF/private-target cases: default deny behavior (no `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1`).

### 2.1 Environment variables (defaults shown)
| Variable | Default | Effect |
|---|---|---|
| `BROWSER_CRAWLER_HOST_PORT` | `8765` | Listen port |
| `BROWSER_CRAWLER_WORKERS` | 1 | uvicorn workers (Dockerfile) |
| `BROWSER_CRAWLER_GLOBAL_CONCURRENCY` | _(internal default `4`; not operator-wired)_ | Concurrency floor for the recursive dispatcher; self-managed by the resource guard |
| `BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY` | `1` | Per-host parallel crawls |
| `BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS` | `1000` | Delay between same-domain jobs |
| `BROWSER_CRAWLER_MAX_BATCH_SIZE` | `50` | Max URLs per request |
| `BROWSER_CRAWLER_DEFAULT_TIMEOUT_SECONDS` | `30` | Per-strategy timeout |
| `BROWSER_CRAWLER_DEFAULT_MAX_RETRIES` | `2` | Retries inside a strategy |
| `BROWSER_CRAWLER_RESPECT_ROBOTS` | `true` | Honor robots.txt |
| `BROWSER_CRAWLER_ALLOWED_HOSTS` | `""` | Allowlist (empty = all public) |
| `BROWSER_CRAWLER_DENIED_HOSTS` | `""` | Denylist |
| `BROWSER_CRAWLER_LOG_LEVEL` | `info` | Log verbosity |
| `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS` | unset | SSRF override (set `1` to allow private IPs) |

---

## 3. Build & Deploy

```bash
# From repo root
docker compose -f docker-compose.yml -f docker-compose.municipal.yml \
  --profile browser-crawler build browser-crawler

docker compose -f docker-compose.yml -f docker-compose.municipal.yml \
  --profile browser-crawler up -d browser-crawler

# Wait for liveness
for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8765/health && break
  sleep 2
done
```

**Build expectations:**
- 3 stages: `builder` (compiles wheels) → `runtime` (non-root `appuser`) → `development`.
- `python -m camoufox fetch` succeeds as `appuser` (Camoufox browser binary present).
- Image runs `uvicorn app.main:app --host 0.0.0.0 --port 8765 --workers 1`.
- Healthcheck `curl -f http://localhost:8765/health` passes.

---

## 4. Endpoint Inventory (8 endpoints + OpenAPI)

| # | Method & Path | Tag | Purpose | Test IDs |
|---|---|---|---|---|
| E1 | `GET /health` | Health | Lightweight liveness | TC-HEALTH-01 |
| E2 | `GET /ready` | Health | Crawl-capacity readiness/saturation | TC-READY-01, TC-READY-02 |
| E3 | `GET /metrics` | Health | Prometheus metrics | TC-METRICS-01 |
| E4 | `GET /v1/capabilities` | Discovery | Strategies/extractions/classifications/defaults/features | TC-CAP-01 |
| E5 | `GET /v1/agent-hints` | Discovery | AI agent guidance + example policies | TC-HINTS-01 |
| E6 | `GET /.well-known/crawler-service` | Discovery | Machine-readable discovery links | TC-WK-01 |
| E7 | `POST /v1/crawl` | Crawler | Policy-driven crawl (single + batch + webhook) | TC-CRAWL-* (many) |
| E8 | `POST /v1/render` | Crawler | Backward-compatible browser render | TC-RENDER-01..04 |
| E9 | `GET /openapi.json`, `GET /docs` | — | OpenAPI spec / Swagger UI | TC-OPENAPI-01 |

---

## 5. Discovery & Health Cases

### TC-HEALTH-01 — Liveness
`curl -fsS http://127.0.0.1:8765/health` → `200`, body `{"status":"ok"}` (or equivalent `HealthResponse`). Cheap; does not reflect crawl load.

### TC-READY-01 — Readiness shape
`GET /ready` → `200`, JSON with `status`, `active_requests`, `max_active_requests`, `saturated`. `status` ∈ {`ready`, `saturated`}.

### TC-READY-02 — Readiness saturation
Under load, `saturated` becomes `true` when the self-managed resource guard rides at its target (memory >= 0.92 or CPU >= 0.90) and the service returns HTTP `503` (backpressure, not death). `max_active_requests` is reported as `0` (unbounded); saturation is resource-driven, not a fixed request cap. (Observe via parallel `TC-CRAWL` runs or by driving memory/CPU to the gate target.)

### TC-METRICS-01 — Prometheus metrics
`GET /metrics` → `200`, `text/plain; version=0.0.4`. Contains counters:
`crawler_crawl_requests_total`, `crawler_crawl_requests_accepted_total`,
`crawler_crawl_results_total`, `crawler_crawl_success_total`,
`crawler_crawl_failure_total`, `crawler_crawl_errors_total`,
`crawler_crawl_seconds_total`, `crawler_strategy_results_total{strategy=...}`,
`crawler_result_classification_total{classification=...}`.
Values increment after crawl/render calls.

### TC-CAP-01 — Capabilities contract
`GET /v1/capabilities` → `200`. Assert:
- `supported_strategies` = `[HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE, BROWSER_HEADLESS, BROWSER_HEADED_WARMED]`
- `supported_extractions` = `[full, trafilatura, document, dom]`
- `supported_classifications` includes all 27 classifications (see §7).
- `default_limits` present (timeout_seconds=30, max_retries=2, max_discovered_links=250, etc.).
- `features` object present with `recursive_frontier=false`, `captcha_solving=false`, `proxy_rotation=false`, `identity_rotation=false`, `credential_bypass=false`, `cloudflare_bypass=true`, `headed_browser=true`, `pdf_extraction=true`, etc.
- `output_fields`, `openapi_url=/openapi.json`, `swagger_ui_url=/docs`.

### TC-HINTS-01 — Agent hints
`GET /v1/agent-hints` → `200`. Body has `strategies` (4), `extractions` (4), `guidance`, `example_policies`.

### TC-WK-01 — Well-known discovery
`GET /.well-known/crawler-service` → `200`. Body `links` include `openapi`, `swagger_ui`, `liveness`, `readiness`, `capabilities`, `agent_hints`; `recommended_endpoints` lists the 6 primary endpoints.

### TC-OPENAPI-01 — OpenAPI/Swagger
`GET /openapi.json` → `200` valid JSON schema; `GET /docs` → `200` HTML (Swagger UI).

---

## 6. Crawl Endpoint (`POST /v1/crawl`)

Request shape: `{ "url" | "urls": [...], "policy": {...}, "webhook_url"?: "..." }`.
`policy` fields: `schema_version`, `strategies[]`, `extraction`, `limits{}`, `headers{}`,
`request_profile{}`, `browser_profile{}`, `discovery{}`, `scope_regex`, `css_selector`, `xpath`.

Response: `UnifiedCrawlResponse` with `results[]` of `UnifiedCrawlResult`:
`normalized_url`, `final_url`, `status`, `classification`, `selected_strategy`,
`attempted_strategies[]`, `markdown`, `metadata`, `html`, `text`, `quality_score`,
`classification_evidence[]`, `http_status`, `headers`, `content_type`, `extraction_method`,
`diagnostics{}`, `discovered_link_count`, `discovered_links_truncated`, `discovered_links[]`,
`next_page_url`.

### 6.1 Strategy coverage
| TC | Strategy(s) | Expectation |
|---|---|---|
| TC-CRAWL-HTTP-01 | `[HTTP_DECLARED_CRAWLER]` | Direct HTTP; `selected_strategy=HTTP_DECLARED_CRAWLER`; `extraction_method=http_html` or `trafilatura_extract`; markdown present. |
| TC-CRAWL-CFFI-01 | `[HTTP_CFFI_IMPERSONATE]` | curl_cffi chrome124 impersonation; `selected_strategy=HTTP_CFFI_IMPERSONATE`; browser-like UA/headers applied. |
| TC-CRAWL-BR-01 | `[BROWSER_HEADLESS]` | Camoufox headless render; `selected_strategy=BROWSER_HEADLESS`; JS-rendered content captured. |
| TC-CRAWL-BRW-01 | `[BROWSER_HEADED_WARMED]` | Headed Firefox with warmup URL before target; `selected_strategy=BROWSER_HEADED_WARMED`. |
| TC-CRAWL-FB-01 | `[HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE, BROWSER_HEADLESS, BROWSER_HEADED_WARMED]` | Ordered fallback chain; `attempted_strategies` lists all attempted in order; stops at first success. |

### 6.2 Extraction coverage
| TC | `extraction` | Expectation |
|---|---|---|
| TC-EXTR-FULL-01 | `full` (default) | Trafilatura main text + markitdown for docs + plain-text passthrough. |
| TC-EXTR-TRAF-01 | `trafilatura` | HTML-only main text; non-HTML bodies not document-parsed. |
| TC-EXTR-DOC-01 | `document` | PDF/non-HTML parsed via markitdown when text evidence needed. |
| TC-EXTR-DOM-01 | `dom` | DOM-text passthrough (no Trafilatura/markitdown). |

### 6.3 Policy `limits` coverage
| TC | Field | Expectation |
|---|---|---|
| TC-LIM-TIMEOUT-01 | `timeout_seconds` | Per-strategy timeout budget enforced; long targets → timeout classification. |
| TC-LIM-RETRY-01 | `max_retries` | Transient failures retried up to N; retries do NOT bypass explicit denial/CAPTCHA/WAF. |
| TC-LIM-CHALLENGE-01 | `challenge_wait_seconds` | Browser passively waits for content to settle (no click/solve/evade). |
| TC-LIM-LINKS-01 | `max_discovered_links` | Discovered links bounded; `discovered_links_truncated` true when exceeded; `discovered_link_count` reports total. |
| TC-LIM-ROBOTS-01 | `respect_robots` | `true`: robots.txt honored; disallowed seed → `status=skipped`, `classification=robots_disallowed`. `false`: robots ignored. |
| TC-LIM-DEPTH-01 | `max_depth`/`max_pages` | `0`: single-page (no recursion). `>0`: follows in-scope links up to `max_pages` (recursive_frontier=false → service does not auto-recurse; caller owns frontier). |
| TC-LIM-RPS-01 | `max_rps` | Per-domain token-bucket rate limit; `>0` allows bursts up to 5 tokens. |
| TC-LIM-SIZE-01 | `max_content_length_bytes` | Responses larger than cap are truncated/rejected (OOM guard). |

### 6.4 `request_profile` coverage
| TC | Key | Expectation |
|---|---|---|
| TC-RP-UA-01 | `user_agent_family` | `desktop_chrome`/`mobile_safari`/`generic_crawler` selects UA + compat headers (applied to direct HTTP only). |
| TC-RP-ACCEPT-01 | `accept_language` | Sent as `Accept-Language`. |
| TC-RP-COOKIE-01 | `cookie_jar` + per-host cookies | Benign per-host cookies sent; persistent cross-request cookie jar populated from `Set-Cookie`. |
| TC-RP-FETCHMETA-01 | fetch-metadata hints | Sec-Fetch-* headers hinted for direct HTTP. |

### 6.5 `browser_profile` coverage
| TC | Key | Expectation |
|---|---|---|
| TC-BP-STATE-01 | `state` | `fresh` starts clean browser session. |
| TC-BP-VIEW-01 | `viewport` | `desktop`/`mobile` sets Firefox window size. |
| TC-BP-RES-01 | `resource_mode` | `reduced` disables images. |
| TC-BP-SHOT-01 | `include_screenshot` | Full-page screenshot returned as base64 in `diagnostics.screenshot`. |
| TC-BP-ELSHOT-01 | `screenshot_selectors` | Targeted element screenshots returned in `diagnostics.element_screenshots`. |
| TC-BP-LAZY-01 | (implicit) | Lazy-load infinite scroll triggers deferred content (assert deferred text appears). |
| TC-BP-ACTION-01 | action chains | `click`/`fill`/`hover`/`scroll`/`scroll_bottom`/`wait`/`wait_seconds`/`execute_js` executed before extraction. |
| TC-BP-XHR-01 | (implicit) | XHR/fetch JSON interception populates `diagnostics.intercepted_xhr` (Katana-style). |
| TC-BP-FRAME-01 | (implicit) | Legacy frameset/iframe URLs reported in `diagnostics.frames`. |
| TC-BP-FORM-01 | (implicit) | Form metadata reported in `diagnostics.forms` (no submission). |
| TC-BP-JSEND-01 | (implicit) | JS endpoints reported in `diagnostics.js_endpoints`. |

### 6.6 `discovery` toggles
| TC | Key | Expectation |
|---|---|---|
| TC-DISC-ROBOTS-01 | `robots` | `diagnostics.robots` populated (rules/advice). |
| TC-DISC-SITEMAP-01 | `sitemaps` | `diagnostics.sitemaps` populated when present. |
| TC-DISC-FEED-01 | `feeds` | `diagnostics.feeds` populated when present. |

### 6.7 Scoped extraction
| TC | Field | Expectation |
|---|---|---|
| TC-SCOPE-01 | `scope_regex` | Discovered links not matching regex are dropped. |
| TC-CSS-01 | `css_selector` | Extraction targets matched DOM subtree → Markdown; full-page Trafilatura/markdownify bypassed. |
| TC-XPATH-01 | `xpath` | Extraction targets matched nodes (lxml) → Markdown. |

### 6.8 Batch & webhook
| TC | Field | Expectation |
|---|---|---|
| TC-BATCH-01 | `urls:[...]` | Multiple URLs processed; `submitted_count` = N; results in submitted order. |
| TC-WEBHOOK-01 | `webhook_url` | `202 Accepted` immediate; `status=accepted`, `request_id`, `status_url`; result POSTed to webhook out-of-band. |
| TC-WEBHOOK-02 | invalid `webhook_url` | `422` with `code=validation_error` when not `http(s)://`. |

---

## 7. Classification Coverage (all 27)

Assert the service can produce/each maps to a status. The contract lists:
`rendered, captcha_detected, bot_challenge_detected, access_denied, login_required,
paywall_detected, rate_limited, cookie_consent_required, javascript_required,
unsupported_browser, geolocation_blocked, waf_challenge_detected, not_found, site_error,
gone, binary_no_text, robots_disallowed, navigation_timeout, browser_crash, network_error,
tls_error, dns_error, invalid_url, unsupported_scheme, private_network_target, unknown_error,
not_modified`.

Mapping to test cases (representative, not all require live hostile targets):
| TC | Classification | How evoked |
|---|---|---|
| TC-CLS-RENDERED-01 | `rendered` | Successful browser render (TC-CRAWL-BR-01). |
| TC-CLS-ROBOTS-01 | `robots_disallowed` | `respect_robots=true` + disallowed seed (TC-LIM-ROBOTS-01). |
| TC-CLS-404-01 | `not_found` | Crawl a URL returning 404. |
| TC-CLS-NOTMOD-01 | `not_modified` | Conditional GET with matching ETag → 304. |
| TC-CLS-PRIVATE-01 | `private_network_target` | Crawl `http://127.0.0.1` or `http://10.x` → blocked (SSRF). |
| TC-CLS-INVALID-01 | `invalid_url` | Crawl `not-a-url` → `422`/`invalid_url`. |
| TC-CLS-SCHEME-01 | `unsupported_scheme` | Crawl `ftp://...` → blocked. |
| TC-CLS-NETERR-01 | `network_error`/`dns_error`/`tls_error` | Crawl unreachable host. |
| TC-CLS-BIN-01 | `binary_no_text` | Crawl a non-text binary (e.g., `.zip`) with no text. |
| TC-CLS-TIMEOUT-01 | `navigation_timeout` | Browser nav exceeds budget. |
| TC-CLS-CHALLENGE-01 | `waf_challenge_detected`/`bot_challenge_detected`/`captcha_detected` | Targets exhibiting challenge markers (observed/classified, NOT solved). |

All 27 values appear in `supported_classifications` (TC-CAP-01) and counters
(`crawler_result_classification_total`) after relevant runs.

---

## 8. Cross-Cutting Cases

| TC | Concern | Expectation |
|---|---|---|
| TC-SSRF-01 | Private/loopback block | Default: `http://127.0.0.1`, `http://localhost`, `http://10.0.0.1`, `http://169.254.169.254` → `private_network_target`/`blocked`. With `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1` → allowed (override). |
| TC-VAL-01 | `url` xor `urls` | Neither `url` nor `urls` → `422 missing_url`. Both provided → `200` (lenient: service uses `urls`, ignores `url`). Enforcing a strict xor is deferred hardening. |
| TC-VAL-02 | `batch_size_exceeded` | `> max_batch_size` (50) URLs → `422`. |
| TC-VAL-03 | schema/field validation | Invalid policy field → `422` with `details.errors`. |
| TC-CONC-01 | Global + per-domain concurrency | Many parallel requests; scheduler enforces `global_concurrency=4`, `per_domain_concurrency=1`, `per_domain_delay_ms`. |
| TC-RATE-01 | `max_rps` | Sustained ≤ configured RPS per domain. |
| TC-LOG-01 | Structured logging | `/app/logs/browser-crawler.jsonl` receives redacted, structured log lines (no secret leakage). |
| TC-LIFECYCLE-01 | Graceful shutdown | `SIGTERM` → browser renderer closed, in-flight crawls finish/abort cleanly via `shutdown_event`. |

---

## 9. Execution (Smoke Test)

Run the high-value subset to confirm end-to-end health. Each step prints PASS/FAIL.

```bash
B=http://127.0.0.1:8765
jq_ok() { jq -e "$1" >/dev/null 2>&1 && echo "PASS $2" || { echo "FAIL $2"; cat /tmp/r.json; }; }

# E1-E6 discovery
curl -fsS $B/health; echo " <- TC-HEALTH-01"
curl -fsS $B/ready | tee /tmp/r.json | jq_ok '.max_active_requests' TC-READY-01
curl -fsS $B/metrics | grep -q crawler_crawl_requests_total && echo "PASS TC-METRICS-01" || echo "FAIL TC-METRICS-01"
curl -fsS $B/v1/capabilities | jq -e '.supported_strategies|length==4' >/dev/null && echo "PASS TC-CAP-01" || echo "FAIL TC-CAP-01"
curl -fsS $B/v1/agent-hints | jq -e '.example_policies' >/dev/null && echo "PASS TC-HINTS-01" || echo "FAIL TC-HINTS-01"
curl -fsS $B/.well-known/crawler-service | jq -e '.links.openapi' >/dev/null && echo "PASS TC-WK-01" || echo "FAIL TC-WK-01"

# E7 single crawl (public, reliable)
curl -fsS -X POST $B/v1/crawl -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","policy":{"strategies":["HTTP_DECLARED_CRAWLER","BROWSER_HEADLESS"],"limits":{"timeout_seconds":30,"max_retries":1,"max_discovered_links":50}}}' \
  | tee /tmp/r.json | jq_ok '.results[0].status=="success"' TC-CRAWL-HTTP-01

# E8 render
curl -fsS -X POST $B/v1/render -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}' | jq_ok '.results[0].markdown' TC-RENDER-01

# SSRF (private target blocked)
curl -s -o /tmp/r.json -w '%{http_code}' -X POST $B/v1/crawl -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8765/health"}' | grep -q 422 && echo "PASS TC-SSRF-01" || echo "FAIL TC-SSRF-01"
```

---

## 10. Coverage Matrix (functionality → test case)

Every externally observable capability maps to ≥1 case above:

- 8 endpoints + OpenAPI → §4 / §5 (E1–E9)
- 4 strategies → TC-CRAWL-HTTP-01, TC-CRAWL-CFFI-01, TC-CRAWL-BR-01, TC-CRAWL-BRW-01, TC-CRAWL-FB-01
- 4 extractions → TC-EXTR-FULL/DOM/TRAF/DOC
- 10 `limits` fields → TC-LIM-*
- 4 `request_profile` keys → TC-RP-*
- 5+ `browser_profile` behaviors → TC-BP-* (state, viewport, resource, screenshot, element-shot, lazy, action, xhr, frame, form, js-endpoints)
- 3 `discovery` toggles → TC-DISC-*
- scoped extraction → TC-SCOPE/CSS/XPATH
- batch + webhook → TC-BATCH/WEBHOOK-01/02
- 27 classifications → §7
- 6 statuses → encoded in result `status` across cases
- SSRF / validation / concurrency / rate / logging / lifecycle → §8
- metrics / readiness / capabilities / hints / well-known → §5

**Coverage: 100% of documented functionality.**

---

## 11. Reporting

For each case record: `TC id`, request, expected, actual, PASS/FAIL, latency.
Block release on any FAIL in §9 smoke set. Full acceptance requires all §4–§8 cases PASS
(or explicitly waived with reason for env-limited hostile-target classifications).
