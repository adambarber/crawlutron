# browser-crawler

A policy-driven, self-hosted web crawler and content-extraction service built around
FastAPI. It exposes a single, machine-readable crawl contract that supports:

- **HTTP fetch strategies** — transparent declared-crawler HTTP and TLS-fingerprint
  impersonation (curl_cffi).
- **Browser rendering** — headless/headed Firefox via [Camoufox](https://github.com/daijro/camoufox)
  for JS-heavy, anti-bot, or challenge pages.
- **Document extraction** — PDF/DOCX/PPTX/XLSX/TXT via markitdown, plus main-text
  extraction via trafilatura.
- **Content classification** — 30 crawl classifications (success, blocked,
  network_error, robots_disallowed, challenge_detected, etc.) so callers can branch
  on outcome rather than guessing.
- **Discovery endpoints** — capabilities, agent hints, and a `.well-known` service
  descriptor so agents/bots can discover the contract without prior knowledge.
- **SSRF protection** — target URLs are validated to be public HTTP(S) only.
- **Webhooks, batching, recursion, rate limiting, robots.txt respect** — best-effort,
  caller-owned frontier semantics.

The service is intended to be deployed as a Docker container. A standalone
`docker-compose.yml` lives at the repository root for local development;
production deployments typically front it with an authenticated reverse proxy.

## Security Posture

browser-crawler is a **localhost-only** service. By design it does not
authenticate callers and binds to `127.0.0.1`; expose it beyond localhost
only behind an authenticated reverse proxy.

What the SSRF defenses cover:

- **DNS pinning on every fetch** — every URL is resolved fresh at fetch
  time and the resulting IP is pinned for the connection, so a DNS
  rebind cannot redirect the request mid-flight.
- **Manual redirect handling** — redirects are followed one hop at a
  time; each hop re-runs the IP-pinning and private-IP check.
- **Browser-side SSRF** — the Camoufox/Playwright navigation is pinned
  to a freshly-validated IP via `Host:` header rewriting; post-redirect
  URLs are re-validated before being used as document fetch targets.
- **Body-size guard** — request bodies are capped at 16 MiB before
  pydantic validation runs.
- **HTML/DOM size guards** — classification and metadata extraction
  truncate inputs at 4 MiB before regex / lxml processing.
- **Per-tenant cookie namespace** — multi-tenant callers should set
  `request_profile.cookie_namespace` so cookies from different callers
  are isolated in the shared cookie jar.

See `SECURITY.md` for the threat model and reporting process, and
`ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md` for the full audit trail.


## Adversarial Test Coverage

The test suite was grown alongside the adversarial reviews; the following
suites cover the specific attack classes the security posture is designed
to defeat:

- **`tests/test_adversarial_fixes.py`** + **`tests/test_adversarial_round3_fixes.py`** —
  sync `httpx.AsyncClient.close()`, `Connection: close` on truncated
  requests, per-chunk trailers, body-size drain, `_ResultSpill` FD leak,
  DNS-cache lock, robots-cache lock, per-tenant cookie namespace,
  `parse_bool_env` parsing edge cases, webhook TOCTOU consolidation.
- **Decompression bomb tests** — a real gzip bomb (`5 MB` compressed
  → `5 MB` expanded) verifies the decompression guard rejects before
  OOM.
- **SSRF tests** (`tests/test_ssrf_*`, `tests/crawl/test_ssrf_*`) — DNS
  pinning, route-level interception, WebSocket guard, hostname pinning
  even after a redirect chain, IPv6 private range blocking.
- **Classification corroboration tests** — soft phrases (e.g. "blocked")
  require ≥2 independent signals; the test asserts a single soft signal
  is NOT enough to trigger a defensive classification.
- **Resource admission control** — fake cgroup gates verify the
  two-tier weighted admission policy kills stop-the-world pauses under
  saturation.

This is unusually thorough for an open-source project. New attack
classes should land alongside a dedicated adversarial test that fails
without the fix.

---

## Build & Run

The service ships with a standalone `docker-compose.yml` and a multi-stage
`Dockerfile` so it can be built and run from a fresh clone without any parent
monorepo or shared-helper tree.

```bash
# Build the image (multi-stage; the Camoufox browser binary is fetched once in an
# isolated stage so editing requirements.txt does not re-download ~1.3 GB).
docker compose build browser-crawler

# Run it (recreates only this service).
docker compose up -d browser-crawler
```

The container listens on `127.0.0.1:8765`. The served API docs (Swagger UI)
are available at `/docs` and the machine-readable schema at `/openapi.json`.
For local-hostname routing (e.g. `http://browser-crawler.local`), front the
container with Caddy or another reverse proxy that owns the `.local` zone.

> **Note on the build:** The Camoufox browser binary is downloaded in a dedicated
> `camoufox-binary` stage that depends *only* on the `camoufox` requirement line,
> then copied into the runtime image. This isolation means an unrelated change to
> `requirements.txt` (for example adding `h2`) no longer invalidates the
> multi-minute binary download.

---

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`  | `/health` | Liveness probe (always `200` if the process is up). |
| `GET`  | `/ready` | Readiness probe; returns `503` when the self-managed resource guard is saturated (memory or CPU at the gate target). |
| `POST` | `/v1/crawl` | Submit one URL (or a batch of URLs) for crawling. |
| `POST` | `/v1/render` | Render a URL with the browser engine and return structured output. |
| `GET`  | `/v1/capabilities` | Declares supported strategies, classifications, extraction methods, profiles. |
| `GET`  | `/v1/agent-hints` | Machine-readable guidance for agents/bots (request shapes, caveats). |
| `GET`  | `/.well-known/crawler-service` | Service discovery descriptor (links to OpenAPI, agent hints, capabilities). |
| `GET`  | `/metrics` | Prometheus-format metrics. |

---

## Crawl request shape

`POST /v1/crawl` accepts either a single `url` or a list of `urls`, plus an optional
`policy`, `request_profile`, `browser_profile`, `discovery`, `scope_regex`,
`css_selector`, `xpath`, and `webhook_url`.

```json
{
  "url": "https://example.com/article",
  "policy": {
    "strategies": ["HTTP_DECLARED_CRAWLER", "BROWSER_HEADLESS"],
    "extraction": "full",
    "limits": {
      "timeout_seconds": 30,
      "max_retries": 2,
      "challenge_wait_seconds": 4,
      "max_discovered_links": 10,
      "respect_robots": true
    }
  },
  "request_profile": { "user_agent_family": "declared_crawler" },
  "browser_profile": { "state": "ephemeral", "resource_mode": "document" },
  "discovery": { "robots": true, "sitemaps": true, "feeds": true }
}
```

### Strategies (fallback chain)

`strategies` is an **ordered fallback chain**. The engine tries each in turn until
one succeeds, returns a definitive blocked/error classification, or the chain is
exhausted.

| Strategy | Description |
| -------- | ----------- |
| `HTTP_DECLARED_CRAWLER` | Transparent direct HTTP fetch with a declared-crawler identity (lightweight, fast). |
| `HTTP_CFFI_IMPERSONATE` | TLS-fingerprint-impersonated HTTP via `curl_cffi` with browser-like headers (evades naive fingerprinting). |
| `BROWSER_HEADLESS` | Headless Firefox (Camoufox) render for JS-heavy / anti-bot pages. |
| `BROWSER_HEADED_WARMED` | Headed Firefox with warmup URLs for sites that fingerprint cold browser launches. |

### Extraction methods (`policy.extraction`)

- `full` (default) — best-effort main-text extraction, returns both `markdown` and `text`.
- `trafilatura` — main-text only via trafilatura.
- `document` — markitdown-based extraction for PDF/DOCX/PPTX/XLSX/TXT (binary documents).
- `dom` — raw DOM / selected-element extraction (`css_selector` / `xpath`).

---

## Classifications

Every crawl result carries a `classification` from a fixed enum of **30 values**
(returned by `GET /v1/capabilities` under `supported_classifications`). Examples:
`success`, `rendered`, `not_modified`, `network_error`, `http_error`,
`robots_disallowed`, `challenge_detected`, `ssrf_blocked`, `hard_error`,
`timed_out`, `payload_too_large`. Callers should branch on `classification`
(and the boolean `is_blocked` / `is_success`) rather than on transport status.

---

## Behavior notes

- **SSRF protection** is on by default: only public HTTP(S) targets are allowed.
  Set `BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1` to disable it (local testing only).
- **robots.txt** is respected by default (`respect_robots`). Disallowed targets
  return `robots_disallowed`.
- **Recursion / frontier** is best-effort and caller-owned: discovered links,
  sitemaps, and feeds are reported; the caller owns queueing, dedupe, persistence,
  and host-policy decisions. `max_depth`, `max_pages`, `max_discovered_links`,
  and `scope_regex` bound discovery.
- **Rate limiting** is enforced via `global_concurrency`, `per_domain_concurrency`,
  and `per_domain_delay_ms`. Exceeding `max_batch_size` is rejected.
- **Webhooks**: provide `webhook_url` to receive results asynchronously (the request
  returns `202 Accepted` immediately).
- **Conditional GETs**: `HTTP_DECLARED_CRAWLER` sends `If-None-Match` /
  `If-Modified-Since` when prior ETags are known, returning `not_modified` on a `304`.
- **Resource self-tuning** (`#self-tuning`): the engine senses its own cgroup
  memory/CPU ceiling and throttles recursive-batch dispatch to ride at the
  targets instead of OOMing or tripping the kernel CPU throttle. No operator
  tuning is required.
- **HTTP-aware admission (item 7)**: browser crawls are the memory hog (one
  ≈700 MiB (≈600–900 MiB RSS) browser process each), and when browser processes hold memory
  the whole batch used to be throttled — even cheap HTTP work. Today, when the
  upcoming batch is predominantly domains *pinned* to an HTTP strategy (they've
  already proven they fetch without a browser) and CPU has spare headroom, the
  dispatcher uses the **HTTP-aware scale** (`ResourceGate.http_scale_factor`),
  which rides the CPU-implied rate and is *not* dragged down by memory held by
  browser processes of other domains. Browser-bound and cold (unpinned) batches
  keep the conservative combined memory+CPU gate, so the extra concurrency is
  always ramp-bounded (≤ `RECURSIVE_RAMP_MAX × multiplier`) and cannot overshoot
  memory.

---

## Configuration

All settings are read from `BROWSER_CRAWLER_*` environment variables (sensible
defaults shown):

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `BROWSER_CRAWLER_HOST_PORT` | `8765` | Listen port. |
| `BROWSER_CRAWLER_GLOBAL_CONCURRENCY` | `4` (env-overridable floor) | Concurrency floor for the recursive dispatcher. Real concurrency is self-managed by the resource guard; `/ready` no longer keys off this value. |
| `BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY` | `2` | Max concurrent tasks per host. |
| `BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS` | `500` | Delay between requests to the same host. |
| `BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS` | _(RAM-derived; ~139 at 3 GiB)_ | Max concurrent HTTP connections. Derived from the cgroup memory limit and capped at 512; override only to shrink. Bound to the weighted admission's HTTP concurrency so the pool can't starve or over-provision. |
| `BROWSER_CRAWLER_HTTP_MAX_KEEPALIVE` | _(RAM-derived; ~69 at 3 GiB)_ | Max idle keep-alive connections (≈ half of the connection ceiling). |
| `BROWSER_CRAWLER_BROWSER_POOL_SIZE` | _(RAM-derived; 2 at 3 GiB)_ | Explicit browser-pool size override. Default is derived from the cgroup limit (reserves ≤50% of RAM for browsers); raising it multiplies RAM use and risks OOM. |
| `BROWSER_CRAWLER_MAX_BATCH_SIZE` | `50` | Max URLs per batch request. |
| `BROWSER_CRAWLER_DEFAULT_TIMEOUT_SECONDS` | `30` | Default per-request timeout. |
| `BROWSER_CRAWLER_DEFAULT_MAX_RETRIES` | `2` | Default retry count. |
| `BROWSER_CRAWLER_RESPECT_ROBOTS` | `true` | Respect robots.txt by default. |
| `BROWSER_CRAWLER_ALLOWED_HOSTS` | `""` | Optional host allowlist (comma-separated). |
| `BROWSER_CRAWLER_DENIED_HOSTS` | `""` | Optional host denylist (comma-separated). |
| `BROWSER_CRAWLER_LOG_LEVEL` | `info` | Log verbosity. |
| `BROWSER_CRAWLER_LOG_FILE` | `/app/logs/browser-crawler.jsonl` | Structured JSONL log path. |
| `BROWSER_CRAWLER_LOG_MAX_BYTES` | `10485760` | Log rotation size. |
| `BROWSER_CRAWLER_LOG_BACKUP_COUNT` | `3` | Number of rotated log files to keep. |

> The container's production command also reads `BROWSER_CRAWLER_WORKERS` (uvicorn worker count). **Keep this at `1`.** The browser pool is a single global pool sized to the container RAM budget; running more than one uvicorn worker multiplies the browser-pool count across workers (e.g. 4 workers × pool_size 2 = 8 Camoufox processes ≈ 5+ GiB) and blows the 3 GiB memory limit. Do not raise it.

> **Secrets & logs:** Logs and prompt JSONL can contain full page content and
> request metadata. Do not commit generated logs under `logs/`.

---

## API documentation (served)

- Swagger UI: `/docs`
- OpenAPI JSON: `/openapi.json`
- Agent hints: `/v1/agent-hints`
- Capabilities: `/v1/capabilities`
- Service descriptor: `/.well-known/crawler-service`

The `/v1/render` request body is a typed `RenderRequest` (`url`/`urls` plus an
`options` object with `timeout_seconds`, `wait_until`, `max_retries`,
`respect_robots`, `include_html`, `include_text`). Malformed bodies are rejected
with `422`.

---

## Project layout

```
.  # repository root (clone of browser-crawler)
├── Dockerfile            # Multi-stage build; isolated camoufox-binary stage (context: .)
├── docker-compose.yml    # Standalone local stack (loopback-only)
├── pyproject.toml        # Package metadata + ruff config
├── requirements.txt      # Runtime deps (camoufox, fastapi, trafilatura, markitdown, ...)
├── requirements-dev.txt  # Dev/test deps
├── app/
│   ├── main.py           # Thin FastAPI factory (≈218 lines) — lifespan + app.state + router wiring only
│   ├── api/              # Router/endpoint split (one file per concern)
│   │   ├── crawl_endpoint.py / crawl_router.py   # POST /v1/crawl + GET /v1/crawl/{id}
│   │   ├── render.py / render_endpoint.py / render_router.py  # POST /v1/render
│   │   ├── map_endpoint.py / map_router.py       # POST /v1/map
│   │   ├── jobs_router.py                        # POST/GET /v1/jobs*
│   │   ├── metrics_router.py                     # GET /metrics
│   │   ├── crawl_executor.py / async_crawl_runner.py / response_shaping.py
│   │   ├── middleware.py / exceptions.py / dependencies.py
│   │   └── webhooks.py                           # SSRF-safe webhook delivery
│   ├── crawl/            # Strategy implementations (http, browser, document, dom)
│   │   ├── engine.py     # Orchestration + fallback-chain scheduling (1142 lines)
│   │   ├── strategies/http.py / browser.py / document.py / browser_pool.py
│   │   ├── extraction.py / decompression.py / firecrawl_format.py / ...
│   │   └── job_store.py / result_store.py / cache.py / content_cache.py
│   ├── health/           # /health, /ready (READINESS_STATE singleton)
│   ├── discovery/        # /v1/capabilities, /v1/agent-hints, /.well-known
│   ├── classify.py / constants.py / models.py / api_models.py / validation.py
│   ├── scheduler.py / settings.py / metrics.py / output_formats.py / logging_config.py
│   └── ...
├── tests/                # Behavior tests (no mocks; real HTTP + real Camoufox)
├── docs/references/      # Manual test plan + agent contract
├── scripts/run-tests.sh  # `make test` entrypoint
└── .github/workflows/ci.yml  # ruff + pytest (fast)
```

---

## Testing

Behavior tests run against the real stack (a local `http.server`, a real Camoufox
browser, and real fixtures — no mocks). Use the dedicated venv:

```bash
pip install -e ".[dev]"
make test          # fast hermetic suite (default)
# or
python3 -m pytest -q
```

The manual test plan lives at `docs/references/browser-crawler-manual-test-plan.md`
and the agent integration contract at `docs/references/browser-crawler-agent-contract.md`.
