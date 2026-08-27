# browser-crawler

A policy-driven, self-hosted web crawler and content-extraction service built around
FastAPI. It exposes a single, machine-readable crawl contract that supports:

- **HTTP fetch strategies** — transparent declared-crawler HTTP and TLS-fingerprint
  impersonation (curl_cffi).
- **Browser rendering** — headless/headed Firefox via [Camoufox](https://github.com/daijro/camoufox)
  for JS-heavy, anti-bot, or challenge pages.
- **Document extraction** — PDF/DOCX/PPTX/XLSX/TXT via markitdown, plus main-text
  extraction via trafilatura.
- **Content classification** — 27 crawl classifications (success, blocked,
  network_error, robots_disallowed, challenge_detected, etc.) so callers can branch
  on outcome rather than guessing.
- **Discovery endpoints** — capabilities, agent hints, and a `.well-known` service
  descriptor so agents/bots can discover the contract without prior knowledge.
- **SSRF protection** — target URLs are validated to be public HTTP(S) only.
- **Webhooks, batching, recursion, rate limiting, robots.txt respect** — best-effort,
  caller-owned frontier semantics.

The service is one component of the `dev-stack` local web-services suite and is
intended to be deployed as a Docker container behind Caddy (`.local` hostnames).

---

## Build & Run

The service is profile-gated in Compose (`browser-crawler`). From the repository
root (`/Users/adambarber/Projects/dev-stack`):

```bash
# Build the image (multi-stage; the Camoufox browser binary is fetched once in an
# isolated stage so editing requirements.txt does not re-download ~1.3 GB).
docker compose -f docker-compose.yml -f docker-compose.municipal.yml \
  --profile browser-crawler build browser-crawler

# Run it (recreates only this service).
docker compose -f docker-compose.yml -f docker-compose.municipal.yml \
  --profile browser-crawler up -d browser-crawler
```

The container listens on `127.0.0.1:8765` (and via Caddy at
`http://browser-crawler.dev-stack.orb.local`). The served API docs (Swagger UI)
are available at `/docs` and the machine-readable schema at `/openapi.json`.

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

Every crawl result carries a `classification` from a fixed enum of **27 values**
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
  ~300&thinsp;MB browser process each), and when browser processes hold memory
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
| `BROWSER_CRAWLER_GLOBAL_CONCURRENCY` | _(internal default `4`; not operator-wired)_ | Concurrency floor for the recursive dispatcher. Real concurrency is self-managed by the resource guard; `/ready` no longer keys off this value. |
| `BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY` | `1` | Max concurrent tasks per host. |
| `BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS` | `1000` | Delay between requests to the same host. |
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

> The container's production command also reads `BROWSER_CRAWLER_WORKERS` (uvicorn worker count).

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
docker/browser-crawler/
├── Dockerfile            # Multi-stage build; isolated camoufox-binary stage
├── requirements.txt      # Runtime deps (camoufox, fastapi, trafilatura, markitdown, ...)
├── requirements-dev.txt  # Dev/test deps
├── app/
│   ├── main.py           # FastAPI app, lifespan, route wiring
│   ├── engine.py         # Crawl orchestration + fallback-chain scheduling
│   ├── scheduler.py      # Strategy worker scheduling/semaphores
│   ├── service.py        # Request → job mapping, webhook delivery, recursion
│   ├── classify.py       # Outcome classification + signatures
│   ├── constants.py      # Strategies, classification sets, signatures
│   ├── models.py         # Internal + API (Pydantic) models
│   ├── settings.py       # BROWSER_CRAWLER_* configuration
│   ├── api_models.py     # Request/response schemas (RenderRequest, etc.)
│   ├── validation.py     # URL/SSRF validation, request validation
│   ├── crawl/            # strategy implementations (http, browser, document, dom)
│   ├── health/           # /health, /ready
│   └── discovery/        # /v1/capabilities, /v1/agent-hints, /.well-known
├── tests/                # Behavior tests (no mocks; real HTTP + real Camoufox)
└── docs/references/      # Manual test plan + agent contract
```

---

## Testing

Behavior tests run against the real stack (a local `http.server`, a real Camoufox
browser, and real fixtures — no mocks). Use the dedicated venv:

```bash
/private/tmp/bc-venv/bin/python -m pytest docker/browser-crawler/tests -q
```

The manual test plan lives at `docs/references/browser-crawler-manual-test-plan.md`
and the agent integration contract at `docs/references/browser-crawler-agent-contract.md`.
