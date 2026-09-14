# Browser Crawler Agent Contract

The Browser Crawler Service is a local, policy-driven execution service for
public web crawling. Agents should treat its machine-readable discovery
surfaces as the source of truth and use this reference as integration
guidance.

## Discovery Surfaces

When the service is running at `http://127.0.0.1:${BROWSER_CRAWLER_HOST_PORT:-8765}`,
discover the contract from:

- `GET /health`
- `GET /ready`
- `GET /.well-known/crawler-service`
- `GET /v1/capabilities`
- `GET /v1/agent-hints`
- `GET /openapi.json`
- Swagger UI at `/docs`
- `POST /mcp` — MCP 2026-07-28 endpoint (see [MCP](#mcp-mcp-2026-07-28))

The static implementation source of truth is `app/api_models.py`; the
discovery payloads and OpenAPI app metadata are in `app/main.py`.

## Endpoint Selection

Use `POST /v1/crawl` for policy-driven crawling. Use `POST /v1/render` only
for legacy browser-render callers that need raw rendered HTML/text without the
universal strategy chain. Use `POST /v1/search` to discover public URLs from a
query without crawling them. Use `POST /mcp` to drive search/crawl/map/render
as MCP tools from an agent runtime.

Use `GET /health` for process liveness and Docker healthcheck interpretation.
Use `GET /ready` for crawl-capacity readiness before submitting large batches
or retrying after a timeout.

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
- `HTTP_CFFI_IMPERSONATE` — TLS-fingerprint-impersonated HTTP via curl_cffi (browser-like headers)
- `BROWSER_HEADLESS` — Headless Firefox rendering via Camoufox
- `BROWSER_HEADED_WARMED` — Headed Firefox with warmup URLs

Supported extraction methods (parsing pipelines, orthogonal to fetch strategy):

- `FULL` — Unified pipeline (trafilatura for HTML, markitdown for documents, plain-text passthrough)
- `TRAFILATURA` — Forces HTML main-text extraction only
- `DOCUMENT` — Forces markitdown document extraction for PDFs and non-HTML bodies
- `DOM` — DOM text extraction (browser-rendered pages)

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
  "extraction": "FULL",
  "limits": {
    "timeout_seconds": 30,
    "max_retries": 2,
    "challenge_wait_seconds": 15,
    "max_discovered_links": 250
  },
  "request_profile": {
    "user_agent_family": "desktop_chrome",
    "accept_language": "en-US,en;q=0.9"
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
  `BROWSER_CRAWLER_SEARCH_PROVIDER` (`auto` | `tavily` | `brave` | `serper` |
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
docker compose build browser-crawler
# Run the container on 127.0.0.1:8765 (recreates only this service):
docker compose up -d browser-crawler
```

## Fixture and Regression Corpus

Real fixture coverage lives under `tests/fixtures/browser_crawler/`, with a
manifest at `tests/fixtures/browser_crawler/manifest.json`. The Mass.gov
fixtures cover direct HTTP `access_denied` with successful browser fallback
and guard against false `rate_limited` classifications from script/style
contents.