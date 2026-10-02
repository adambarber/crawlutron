# crawlutron

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
- **Web search** — `POST /v1/search` discovers public URLs from a query through a
  keyed provider (Tavily / Brave / Serper) with a keyless DuckDuckGo fallback.
- **MCP** — `POST /mcp` exposes search/crawl/map/render as tools over the
  Model Context Protocol (2026-07-28, stateless streamable HTTP).
- **SSRF protection** — target URLs are validated to be public HTTP(S) only.
- **Webhooks, batching, recursion, rate limiting, robots.txt respect** — best-effort,
  caller-owned frontier semantics.

The service is intended to be deployed as a Docker container. A standalone
`docker-compose.yml` lives at the repository root for local development;
production deployments typically front it with an authenticated reverse proxy.

## Security Posture

crawlutron is a **localhost-only** service. By design it does not
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
`ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md` for the dated adversarial review of
the crawler.


## Adversarial Test Coverage

The test suite was grown alongside the adversarial reviews; the following
suites cover the specific attack classes the security posture is designed
to defeat:

- **Truncation and drain** (`tests/test_body_cap_state_machine.py`) — chunked
  handling, `Connection: close` on a truncated request, per-chunk trailers and
  headers, body-size drain after truncation, the rule that no further bytes are
  admitted once a chunk trips the cap, and the 413 the shipped app returns on a
  body over the cap.
- **File-descriptor ownership** (`tests/test_spill_stream.py`,
  `tests/invariants/test_ownership.py`) — `_ResultSpill` deletes the file it
  owns and leaves the one it borrowed.
- **Cache locking** (`tests/crawl/test_dns_cache.py`,
  `tests/crawl/test_robots_policy_consolidation.py`) — concurrent access to
  the DNS and robots caches, including that robots reads go through the one
  chokepoint.
- **Cookie namespacing** (`tests/crawl/test_cookie_namespace.py`) — per-tenant
  cookies cannot cross a tenant boundary.
- **Env parsing** (`tests/test_env_parsing.py`) — `parse_bool_env` edge cases,
  including that a typo cannot silently open a policy.
- **Webhook TOCTOU** (`tests/api/test_webhook_delivery.py`) — one resolved
  target per delivery, consolidated at the chokepoint.
- **Decompression bomb tests** — a real gzip bomb (`~1 KB` compressed →
  `1 MB` expanded), plus deflate and brotli bomb cases, verify the decompression
  guard rejects over-cap output before OOM.
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
docker compose build crawlutron

# Run it (recreates only this service).
docker compose up -d crawlutron
```

The container listens on `127.0.0.1:8765`. The served API docs (Swagger UI)
are available at `/docs` and the machine-readable schema at `/openapi.json`.
For local-hostname routing (e.g. `http://crawlutron.local`), front the
container with Caddy or another reverse proxy that owns the `.local` zone.

> **Note on the build:** The Camoufox browser binary is downloaded in a dedicated
> `camoufox-binary` stage that depends *only* on the `camoufox` requirement line,
> then copied into the runtime image. This isolation means an unrelated change to
> `requirements.txt` (for example adding `h2`) no longer invalidates the
> multi-minute binary download.

---

## SQLite schema migrations

Migrations live in `app/crawl/migrations/`. Every store schema — jobs, results, metrics, coordination — lives in the single `cache/crawler.db`; startup applies that database's pending migrations under yoyo's lock, and an exact pre-ledger table is stamped instead of recreated. The CLI names the schema explicitly because they share one file.

```bash
# Apply pending migrations (normally automatic when the store opens).
.venv/bin/python -m app.crawl.migrations apply cache/crawler.db crawl_jobs
.venv/bin/python -m app.crawl.migrations apply cache/crawler.db results

# Add a migration: add a versioned schema to manifest.py, point CURRENT_SCHEMAS at it,
# then add a yoyo step with apply and rollback SQL under the migration directory.
$EDITOR app/crawl/migrations/manifest.py
$EDITOR app/crawl/migrations/0002_crawl_jobs_descriptive_name.py

# Stamp an existing database after checking it against the declared schema.
.venv/bin/python -m app.crawl.migrations stamp cache/crawler.db crawl_jobs
.venv/bin/python -m app.crawl.migrations stamp cache/crawler.db results

# Roll back one schema's applied migrations in reverse order; do not open the
# store afterward. Rolling one schema back leaves the others applied.
.venv/bin/python -m app.crawl.migrations rollback cache/crawler.db crawl_jobs
.venv/bin/python -m app.crawl.migrations rollback cache/crawler.db results
```

---

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`  | `/health` | Liveness probe (always `200` if the process is up). |
| `GET`  | `/ready` | Readiness probe; returns `503` when the self-managed resource guard is saturated (memory or CPU at the gate target). |
| `POST` | `/v1/crawl` | Submit one URL (or a batch of URLs) for crawling. |
| `POST` | `/v1/render` | Render a URL with the browser engine and return structured output. |
| `POST` | `/v1/search` | Discover public URLs from a query via the configured search provider. |
| `POST` | `/v1/map` | Crawl a site's link graph and return the discovered URL set (no content extraction). |
| `GET` | `/v1/crawl/{request_id}` | Fetch the result of an async crawl submission. |
| `GET`/`POST` | `/v1/jobs` | Whole-site resumable jobs: create (with frontier), list, inspect, pause/resume/cancel. |
| `POST` | `/mcp` | MCP 2026-07-28 endpoint exposing search/crawl/map/render as tools. |
| `GET`  | `/v1/capabilities` | Declares supported strategies, classifications, extraction methods, profiles. |
| `GET`  | `/v1/agent-hints` | Machine-readable guidance for agents/bots (request shapes, caveats). |
| `GET`  | `/.well-known/crawler-service` | Service discovery descriptor (links to OpenAPI, agent hints, capabilities). |
| `GET`  | `/metrics` | Prometheus-format metrics. |

---

## Web search

`POST /v1/search` discovers public URLs from a query and returns SERP-style
results (`{success, query, provider, limit, data: [{url, title, description}]}`).
It does **not** crawl result URLs — pipe them into `POST /v1/crawl`.

The backend is operator-configured, not caller-selected:

- `CRAWLUTRON_SEARCH_PROVIDER` (`auto` | `tavily` | `brave` | `serper` | `duckduckgo`).
- `auto` (default) activates the first configured keyed provider in priority
  order (Tavily to Brave to Serper) and falls back to the keyless DuckDuckGo
  HTML endpoint when no API key is set. The keyless endpoint may be
  captcha-blocked from datacenter IPs, so configure a key for production use.
- A selected provider without its key returns `503 search_provider_unavailable`
  naming the missing `CRAWLUTRON_*_API_KEY` variable.

Result URLs pass the same public-URL validation as crawl targets (loopback and
private targets are dropped). `GET /v1/capabilities` reports the active
provider and the configured provider list.

## MCP

`POST /mcp` exposes `search`, `crawl`, `map`, and `render` as tools over the
**Model Context Protocol (2026-07-28)** — the stateless revision: no
`initialize` handshake, no sessions. Every request carries its protocol
version and client capabilities in `_meta`, and the transport validates the
`MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` headers against the body.

- `server/discover` — supported protocol versions, capabilities, server identity.
- `tools/list` — the four tools with JSON-Schema inputs.
- `tools/call` — invokes a tool; returns text `content` plus
  `structuredContent`, with `isError` set for execution failures.
- The tools mirror the REST endpoints and delegate to the same handlers, so
  **all four strategies and every extraction method are available through MCP**.
- Webhook/async delivery is not available over MCP.

The endpoint is loopback-only and rejects non-loopback `Origin` headers (DNS
rebinding defense).

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
| `HTTP_CFFI_IMPERSONATE` | TLS-fingerprint-impersonated HTTP via `curl_cffi` with browser-shaped headers, for sites that reject non-browser clients. |
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
  Set `CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1` to disable it (local testing only).
  Enabling it logs a WARNING naming the disabled guard, and lets caller-supplied
  URLs reach private, loopback, and link-local addresses including cloud
  metadata endpoints.
- **robots.txt** is respected by default (`respect_robots`). Disallowed targets
  return `robots_disallowed`.
- **Recursion / frontier:** `POST /v1/crawl` recursively expands one in-memory
  frontier for the duration of a request and reports discovered links, sitemaps,
  and feeds; cross-run queue ownership remains with the caller. `POST /v1/jobs`
  instead owns a durable whole-site frontier. Each persisted `frontier` and
  `in_flight` entry is `{"url": "...", "depth": N}`; `url` is the canonical page
  identity and depth 0 is the stored canonical root. Legacy string entries load
  at depth 0 when they are the root and at the effective `max_depth` otherwise,
  preventing untracked additional recursion during upgrade.
- **Whole-site admission:** the engine dispatcher and durable job runner share
  canonical-identity, scope, analytics, document, robots, crawl-depth, and page-cap
  admission. When `discovery.sitemaps` is enabled, root sitemap pages enter the
  durable frontier at depth 0 through that same pipeline. `max_depth=0` is
  single-page. A positive job `max_pages` counts unique canonical page identities
  across the root, pending frontier, in-flight recovery,
  visited pages, and retries. Missing or zero `max_pages` is unbounded for jobs;
  `/v1/crawl` retains its bounded service default. A positive job cap sets
  `cap_truncated=true` in status and `stats.cap_truncated=1` only when it refuses
  or truncates eligible queued or discovered work; normal `BATCH_SIZE` chunking
  does not set it. Already-admitted in-flight work is never dropped: recovery
  preserves at-least-once delivery and records `stats.cap_overcommitted=1`.
- **Crawl duration & async**: a synchronous `POST /v1/crawl` has **no server-side
  time ceiling** — it returns only when the whole bounded recursion finishes, so
  duration scales with `max_depth`, `max_pages`, per-domain politeness delays, and
  the per-URL budget (`limits.total_url_budget_seconds`, default 180s, which fails a
  single tarpit URL, not the crawl). For large or recursive crawls send
  `async_mode=true` (or `webhook_url`) to get an immediate `202` + `status_url`, then
  poll `GET /v1/crawl/{request_id}`; or use `POST /v1/jobs` for durable, resumable,
  pause/cancel-able whole-site jobs. Read timeouts on the sync path must cover the
  chosen policy and batch size.
- **Rate limiting** is enforced via `global_concurrency`, `per_domain_concurrency`,
  and `per_domain_delay_ms`. Exceeding `max_batch_size` is rejected.
- **Webhooks / async**: provide `webhook_url` to receive results asynchronously, or
  `async_mode=true` to get a `202 Accepted` with a `status_url` to poll
  `GET /v1/crawl/{request_id}`. Both return immediately instead of blocking on the
  crawl.
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

All settings are read from `CRAWLUTRON_*` environment variables (sensible
defaults shown):

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `CRAWLUTRON_GLOBAL_CONCURRENCY` | `4` (env-overridable floor) | Concurrency floor for the recursive dispatcher. Real concurrency is self-managed by the resource guard; `/ready` no longer keys off this value. |
| `CRAWLUTRON_PER_DOMAIN_CONCURRENCY` | `2` | Max concurrent tasks per host. |
| `CRAWLUTRON_PER_DOMAIN_DELAY_MS` | `500` | Delay between requests to the same host. |
| `CRAWLUTRON_HTTP_MAX_CONNECTIONS` | _(RAM-derived; ~139 at 3 GiB)_ | Max concurrent HTTP connections. Derived from the cgroup memory limit and capped at 512; override only to shrink. Bound to the weighted admission's HTTP concurrency so the pool can't starve or over-provision. |
| `CRAWLUTRON_HTTP_MAX_KEEPALIVE` | _(RAM-derived; ~69 at 3 GiB)_ | Max idle keep-alive connections (≈ half of the connection ceiling). |
| `CRAWLUTRON_BROWSER_POOL_SIZE` | _(RAM-derived; 2 at 3 GiB)_ | Explicit browser-pool size override. Default is derived from the cgroup limit (reserves ≤50% of RAM for browsers); raising it multiplies RAM use and risks OOM. |
| `CRAWLUTRON_SEARCH_PROVIDER` | `auto` | Search backend: `auto` (first configured keyed provider) or explicit `tavily` / `brave` / `serper` / `duckduckgo`. |
| `CRAWLUTRON_TAVILY_API_KEY` | `""` | Tavily Search API key (activates the `tavily` provider). |
| `CRAWLUTRON_BRAVE_API_KEY` | `""` | Brave Search API key (activates the `brave` provider). |
| `CRAWLUTRON_SERPER_API_KEY` | `""` | Serper.dev API key (activates the `serper` provider). |
| `CRAWLUTRON_SEARCH_LIMIT_DEFAULT` | `5` | Default result count per search. |
| `CRAWLUTRON_SEARCH_LIMIT_MAX` | `20` | Hard cap on results per search. |
| `CRAWLUTRON_SEARCH_TIMEOUT_SECONDS` | `15` | Provider call timeout. |
| `CRAWLUTRON_MAX_BATCH_SIZE` | `50` | Max URLs per batch request. |
| `CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS` | `30` | Default per-request timeout. |
| `CRAWLUTRON_DEFAULT_MAX_RETRIES` | `2` | Default retry count. |
| `CRAWLUTRON_RESPECT_ROBOTS` | `true` | Respect robots.txt by default. |
| `CRAWLUTRON_ALLOWED_HOSTS` | `""` | Optional host allowlist (comma-separated suffix match; case, IDNA, and port normalized). |
| `CRAWLUTRON_DENIED_HOSTS` | `""` | Optional host denylist (comma-separated suffix match; deny wins over allow). |
| `CRAWLUTRON_LOG_LEVEL` | `info` | Log verbosity. |
| `CRAWLUTRON_LOG_FILE` | `/app/logs/crawlutron.jsonl` | Structured JSONL log path. |
| `CRAWLUTRON_LOG_MAX_BYTES` | `10485760` | Log rotation size. |
| `CRAWLUTRON_LOG_BACKUP_COUNT` | `3` | Number of rotated log files to keep. |

Host policy is applied at the shared public-URL chokepoint before the SSRF/private-target check. Entries are normalized as case-insensitive IDNA hostnames and may include a port; a denied host wins over an allowed host, while an empty allowlist permits every otherwise-valid public host.

> The container's production command also reads `CRAWLUTRON_WORKERS` (uvicorn worker count). **Keep this at `1`.** The browser pool is a single global pool sized to the container RAM budget; running more than one uvicorn worker multiplies the browser-pool count across workers (e.g. 4 workers × pool_size 2 = 8 Camoufox processes ≈ 5+ GiB) and blows the 3 GiB memory limit. Do not raise it.

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
.  # repository root (clone of crawlutron)
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
│   │   ├── extraction.py / decompression.py / llm_format.py / ...
│   │   └── job_store.py / result_store.py / cache.py / content_cache.py
│   ├── health/           # /health, /ready (READINESS_STATE singleton)
│   ├── discovery/        # /v1/capabilities, /v1/agent-hints, /.well-known
│   ├── search/           # /v1/search provider abstraction + endpoint
│   │   ├── providers.py  # Tavily / Brave / Serper / DuckDuckGo + selection
│   │   └── endpoint.py / router.py
│   ├── mcp/              # MCP 2026-07-28 endpoint (POST /mcp)
│   │   ├── protocol.py   # JSON-RPC helpers + request-metadata validation
│   │   ├── tools.py      # search/crawl/map/render tool registry
│   │   └── server.py / router.py
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

The manual test plan lives at `docs/references/crawlutron-manual-test-plan.md`
and the agent integration contract at `docs/references/crawlutron-agent-contract.md`.
