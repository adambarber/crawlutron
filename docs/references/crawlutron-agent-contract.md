# Crawlutron Agent Contract

The Crawlutron service is a local, policy-driven execution service for
public web crawling. Agents should treat its machine-readable discovery
surfaces as the source of truth and use this reference as integration
guidance.

## Discovery Surfaces

When the service is running at its default address `http://127.0.0.1:8765`,
discover the contract from:

- `GET /health`
- `GET /ready`
- `GET /.well-known/crawler-service`
- `GET /v1/capabilities`
- `GET /v1/agent-hints`
- `GET /openapi.json`
- Swagger UI at `/docs`
- `POST /mcp` — MCP 2026-07-28 endpoint (see [MCP](#mcp-mcp-2026-07-28))

The static implementation source of truth is `crawlutron/models/`; the
discovery payloads and OpenAPI app metadata are in `crawlutron/main.py`.

## Endpoint Selection

Use `POST /v1/crawl` for policy-driven crawling. Use `POST /v1/render` only
for legacy browser-render callers that need raw rendered HTML/text without the
universal strategy chain. Use `POST /v1/search` to discover public URLs from a
query without crawling them. Use `POST /mcp` to drive search/crawl/map/render
as MCP tools from an agent runtime.

Use `POST /v1/jobs` for durable, resumable whole-site jobs. Control one with
`POST /v1/jobs/{job_id}/control`: `pause` stops the runner at the next batch
boundary (frontier retained), `resume` respawns it from the persisted frontier,
and `cancel` is terminal with the frontier kept for audit. Read job state with
`GET /v1/jobs/{job_id}`; permanently remove a job with `DELETE /v1/jobs/{job_id}`,
which cancels a running job first.

Use `GET /health` for process liveness and Docker healthcheck interpretation.
Use `GET /ready` for crawl-capacity readiness before submitting large batches
or retrying after a timeout.

## Caller Authentication (API Keys)

Keys are minted by the operator: `POST /v1/keys` (`{"label": ...}`, optional
`max_pages` / `max_concurrency` quotas) accepts only the operator token
(`CRAWLUTRON_KEY_ADMIN_TOKEN`) presented as `X-API-Key` -- a caller key at
the keys routes is a 401, and an unconfigured token seals key management
(#436). Callers present the issued key as `X-API-Key` (or an
`Authorization: Bearer` token). Jobs and results created under a key are
readable only under that key: `GET /v1/crawl/{request_id}`, its `/trace`, and
the `/v1/jobs` reads answer 404 to any other caller. Requests with no key are
anonymous and keep the legacy behavior (anonymous rows stay anonymous-readable).
A wrong or revoked key is 401; a crawl past the key's page or concurrency
quota is 429 `quota_exceeded`. A keyed crawl's cookies are always namespaced to
the key id; anonymous crawls share one bounded cookie jar and a declaration of
`request_profile.cookie_namespace` on any door is refused with 422
`anonymous_cookie_namespace` (#559). Per-caller request counts
are exposed in `/metrics` as `crawler_crawl_requests_total{caller="<key_id>"}`.

Operator token (`CRAWLUTRON_KEY_ADMIN_TOKEN`) rotation is a redeploy: set a new
value and restart the service. Comparison is constant-time (`secrets.compare_digest`);
there is no in-place hot rotation, so the previous value stops working only when
the new process starts.

## Liveness and Readiness

`GET /health` is intentionally lightweight: it confirms the FastAPI process can
respond, and it does not start a browser, inspect crawl saturation, or perform
upstream network work. Docker healthchecks target `/health` so a saturated
crawler is not marked dead only because long-running crawl work is active.

`GET /ready` reports `status`, `active_requests`, `max_active_requests`, and
`saturated`. HTTP 503 from `/ready` means crawl work is at configured capacity
and callers should treat it as backpressure; it is not proof that the process
crashed. Confirm process liveness with `/health` and back off, queue, or retry
later when `/ready` reports `ready`.

Blocking direct HTTP fetches, document extraction, Trafilatura extraction, and
synchronous browser work are offloaded from the FastAPI event loop. This
preserves `/health` responsiveness while crawls run, but it does not make
individual crawl requests instantaneous; caller read timeouts still need to
cover the chosen policy and batch size.

## Current Policy Schema

Current policy schema version: `2026-06-23`.

Supported strategy names (fetch strategies):

- `HTTP_DECLARED_CRAWLER` — Transparent direct HTTP with declared crawler User-Agent
- `HTTP_CFFI_IMPERSONATE` — TLS-fingerprint-impersonated HTTP via curl_cffi (browser-shaped headers, for sites that reject non-browser clients)
- `BROWSER_HEADLESS` — Headless Firefox rendering via Camoufox
- `BROWSER_HEADED_WARMED` — Headed Firefox with warmup URLs

Supported extraction methods (parsing pipelines, orthogonal to fetch strategy):

- `full` — Unified pipeline (trafilatura for HTML, markitdown for documents, plain-text passthrough)
- `trafilatura` — Forces HTML main-text extraction only
- `document` — Forces markitdown document extraction for PDFs and non-HTML bodies
- `dom` — DOM text extraction (browser-rendered pages)

Policy fields agents may send:

- `schema_version`
- `strategies`
- `limits.timeout_seconds`
- `limits.max_retries`
- `limits.challenge_wait_seconds`
- `limits.max_discovered_links`
- `limits.max_depth`
- `limits.max_pages`
- `limits.total_url_budget_seconds`
- `headers`
- `request_profile`
- `browser_profile`
- `discovery`

## Recommended Browser Fallback Policy

```json
{
  "schema_version": "2026-06-23",
  "strategies": ["HTTP_DECLARED_CRAWLER", "HTTP_CFFI_IMPERSONATE", "BROWSER_HEADLESS"],
  "extraction": "full",
  "limits": {
    "timeout_seconds": 30,
    "max_retries": 2,
    "challenge_wait_seconds": 15,
    "max_discovered_links": 250
  },
  "request_profile": {
    "user_agent_family": "desktop_chrome",
    "accept_languages": ["en-US", "en;q=0.9"]
  },
  "browser_profile": {
    "viewport": "desktop",
    "resource_mode": "full",
    "auto_accept_cookie_consent": false
  },
  "discovery": {
    "robots": true,
    "sitemaps": true,
    "feeds": true
  }
}
```

## Response Fields Agents Should Preserve

Persist these fields downstream for auditability and policy review:

- `status`
- `classification`
- `classification_evidence`
- `diagnostics`
- `selected_strategy`
- `attempted_strategies`
- `quality_score`
- `final_url`
- `http_status`
- `content_type`
- `text`
- `html`, when intentionally requested and storage is safe
- `extraction_method`
- `discovered_links`
- `discovered_link_count`
- `discovered_links_truncated`
- `error`
- `validators` (origin `etag` / `last_modified`; feed back via
  `CrawlRequest.validators` to revalidate cheaply — a `304` yields a
  `not_modified` result with no content instead of a full fetch)

## Result Retrieval

Completed `/v1/crawl` responses are stored under their `request_id` (default
retention: 1 hour / 1000 entries) and retrievable at `GET /v1/crawl/{request_id}` —
the same URL advertised as `status_url` in `async_mode`/webhook-accepted 202
responses. A synchronous `POST /v1/crawl` has **no server-side time ceiling**: it
returns only when the whole bounded recursion finishes, so its duration scales with
`max_depth`, `max_pages`, per-domain politeness delays, and the per-URL budget
(`limits.total_url_budget_seconds`, which fails a single tarpit URL, not the crawl).
Per-domain concurrency, delays, and circuit breakers are enforced **within one
request** (each request runs its own scheduler); concurrent requests each enforce
them independently, so serializing or pacing across requests is the caller's job
— the service is stateless and does not aggregate per-domain politeness across
callers, requests, or worker processes.
For large or recursive crawls, submit with `async_mode=true` (or `webhook_url`) to
get an immediate 202 + `status_url` and poll it, or use `POST /v1/jobs` for durable,
resumable whole-site jobs. Use `GET /v1/crawl/{request_id}` both to poll an async
result and to recover a result if your HTTP connection drops mid-response.

## Diagnostics Interpretation

`diagnostics` is structured metadata for operators and caller-owned crawl
policy decisions. Current diagnostics may include:

- `request_profile`: direct HTTP profile used.
- `retry`: attempt count and transient error evidence.
- `cooldown`: `Retry-After`-derived per-domain cooldown hints.
- `runtime`: elapsed time and final URL.
- `robots`: robots URL, crawl delay, and sitemap hints.
- `discovery`: sitemap URLs and feed links discovered from robots, common sitemap locations, or HTML alternate links.
- `browser_profile`: browser state, viewport, resource mode, cookie jar hints, and the `auto_accept_cookie_consent` opt-in (default false).
- `content_metrics`: visible text length, HTML length, link count, frame count, form count, frameset presence, and text-to-HTML ratio.
- `frames`: legacy `<frame>` / `<iframe>` URLs with normalized URLs, frame names, titles, source URL, host, and same-host flags.
- `forms`: form method/action/target/name/id metadata and control summaries. Form actions are resolved to normalized URLs, but forms are not submitted automatically.

Agents should treat diagnostics as evidence for reviewable policy suggestions,
not automatic host-policy mutation. A sparse frameset can return HTTP 200 and
`rendered` while still receiving a low `quality_score`; inspect
`diagnostics.frames` and crawl selected frame URLs explicitly when appropriate.

## Classification Handling

Do not attempt to bypass these terminal or reviewable classifications:

- `captcha_detected`
- `bot_challenge_detected`
- `access_denied`
- `login_required`
- `paywall_detected`
- `rate_limited`
- `cookie_consent_required`
- `javascript_required`
- `unsupported_browser`
- `geolocation_blocked`
- `waf_challenge_detected`
- `robots_disallowed`
- `binary_no_text`

Treat `rendered` plus a useful `quality_score` and content metrics as content
success. Do not treat HTTP 200 alone as success.

## Link and Discovery Boundaries

The service reports bounded `discovered_links`, frame URLs, form metadata,
sitemap candidates, and feed candidates. It guarantees "never the same page twice"
*within a request* — submitted URLs are deduped by canonical page identity (host
case, default port, fragment, trailing slash, UTM/tracking params, and query-param
order are normalized), and successful fetches are reused across requests within the
content-cache TTL. It does not own *cross-run* recursive frontier expansion, frontier
dedupe/persistence, form submission, pagination state, or application-specific scope
rules. Caller applications own those responsibilities.

Use `discovered_link_count` and `discovered_links_truncated` to decide whether
to rerun with a higher `limits.max_discovered_links` value. Treat discovered
pagination URLs as evidence only when form/search state matters; callers must
preserve the original search criteria before enqueueing paginated result URLs.

## Web Search (`POST /v1/search`)

`POST /v1/search` discovers public URLs from a query. It returns SERP-style
results and **does not crawl** them — pipe result URLs into `POST /v1/crawl`.

- Response shape: `{success, query, provider, limit, data: [{url, title, description}]}`.
- The active provider is operator-configured, not caller-selected:
  `CRAWLUTRON_SEARCH_PROVIDER` (`auto` | `tavily` | `brave` | `serper` |
  `duckduckgo`). `auto` activates the first configured keyed provider
  (Tavily → Brave → Serper) and falls back to the keyless DuckDuckGo HTML
  endpoint when no key is set. `GET /v1/capabilities` reports
  `search.active_provider` and `search.configured_providers`.
- Result URLs pass the same public-URL validation as crawl targets:
  loopback/private targets are dropped, never returned.
- Provider failures surface as `502 search_provider_error`; a selected but
  unconfigured provider surfaces as `503 search_provider_unavailable` naming
  the missing `*_API_KEY` variable.
- Result titles and snippets are provider-supplied and unverified; treat them
  as hints until the URL itself is crawled.

## MCP (MCP 2026-07-28)

`POST /mcp` exposes the crawler's four primitives as MCP tools over the
**2026-07-28** protocol (stateless streamable HTTP). This revision has **no
`initialize` handshake and no sessions**: every request carries its protocol
version and client capabilities in `_meta`, and the transport requires
`MCP-Protocol-Version`, `Mcp-Method`, and (for `tools/call`) `Mcp-Name`
headers that MUST match the body.

- `server/discover` (required by the spec) — lists supported protocol
  versions, capabilities, and server identity.
- `tools/list` — exposes `search`, `crawl`, `map`, `render` with JSON-Schema
  inputs. The tools mirror the REST endpoints exactly and delegate to the same
  handlers, so **all four strategies and every extraction method are available
  through MCP**; the crawl/map tools accept the full default strategy chain.
- `tools/call` — returns `resultType: "complete"` with text `content` and
  `structuredContent`; tool execution failures return `isError: true` with an
  actionable message.
- Webhook/async delivery (`async_mode` / `webhook_url`) is intentionally not
  exposed over MCP; those arguments are rejected with a tool error naming
  `POST /v1/crawl`.
- Validation: missing/mismatched headers → `400` with `-32020`/`-32022`;
  missing `_meta` → `400 -32602`; unknown RPC method → `404 -32601`; unknown
  tool → `-32602` "Unknown tool"; non-loopback `Origin` → `403`.

## Safe Operation

The service is designed for local public-page crawling only. Agents must not
add Cloudflare bypass, WAF bypass, CAPTCHA solving, authentication bypass,
paywall bypass, proxy rotation, identity rotation, credential injection,
public network binding, or broad unqualified `docker compose up -d`
workflows. Passive challenge waiting may observe whether a public page
resolves during the configured timeout, but it must not click, solve,
outsource, evade, or continue after explicit denial. Cookie-consent
auto-accept (`browser_profile.auto_accept_cookie_consent`) is disabled by
default; when explicitly opted in, the browser may click consent banners
on the caller's behalf.

Use the targeted helper for local operation (from the repository root):

```bash
make test                      # fast hermetic test suite
python3 -m pytest -q           # same, via pytest directly
./scripts/run-tests.sh         # zsh entrypoint mirroring make test
docker compose build crawlutron
# Run the container on 127.0.0.1:8765 (recreates only this service):
docker compose up -d crawlutron
```

## Fixture and Regression Corpus

Real fixture coverage lives under `tests/fixtures/crawlutron/`, with a
manifest at `tests/fixtures/crawlutron/manifest.json`. The Mass.gov
fixtures cover direct HTTP `access_denied` with successful browser fallback
and guard against false `rate_limited` classifications from script/style
contents.