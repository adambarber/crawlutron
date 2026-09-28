# PioneerResearch Crawler Adapter Plan

**Status:** active
**Created:** 2026-06-23
**Started:** 2026-06-23
**Completed:** -
**Branch:** main
**Related:** PioneerResearch issue #121; dev-stack commits `13d7fbf`, `b047d01`

## Context

This service is a stateless execution service. PioneerResearch should own crawl frontier state, dedupe, persistence, host policy review, and application-specific retry decisions.

This plan documents the integration boundary for PioneerResearch issue #121 without changing the PioneerResearch app in this repository. The current crawler API contract is machine-readable through OpenAPI and agent discovery endpoints; this work note is adapter guidance, not the source of truth.

## Candidate Source Triage

The next likely PioneerResearch sources have different integration risk profiles:

- `https://malegislature.gov/` is a good next public-source target for the crawler smoke matrix and PioneerResearch adapter scripts. Start with direct/site-compatible HTTP, then browser fallback only when rendering or compatibility diagnostics show it is needed.
- `https://infoweb-newsbank-com.ezwx.ez.cwmars.org:58043/apps/news/browse-multi?p=NewsBank&t=favorite%3AWTGWWWTL%21Worcester%2520Telegram%2520%2526%2520Gazette%2520Collection&action=browse` is credentialed/library-proxy content. Treat it as an explicit opt-in, local-only workflow owned by the authorized user session. Do not store passwords, automate login bypass, rotate identity, or add it to default smoke matrices. If it is pursued, use a caller-provided browser state/cookie workflow after confirming license/terms allow collection, and keep captured cookies/logs out of git.

## Current Source of Truth

Agents and bot integrations should discover the live contract from the local service rather than hard-coding this plan:

- `GET /health`
- `GET /ready`
- `GET /.well-known/crawler-service`
- `GET /v1/capabilities`
- `GET /v1/agent-hints`
- `GET /openapi.json`
- Swagger UI at `/docs`

The current service contract is defined in `app/api_models.py` and exposed by `app/main.py`.

## Service Contract to Consume

Use `POST /v1/crawl` for new crawl execution. Keep `POST /v1/render` only for legacy browser-render callers.

Use `GET /health` as the lightweight liveness check and `GET /ready` as the crawl-capacity readiness check. A `/ready` HTTP 503 means active crawler work is saturated/backpressured, not necessarily crashed; PioneerResearch callers should queue, back off, or retry later rather than treating readiness saturation as service death. Blocking direct HTTP, document extraction, Trafilatura, and synchronous browser work are offloaded from the FastAPI event loop, so `/health` should remain responsive during long crawls even when individual crawl requests still need generous read timeouts.

Important response fields for the adapter:

- `status`
- `classification`
- `classification_evidence`
- `diagnostics`
- `selected_strategy`
- `attempted_strategies`
- `quality_score`
- `html`
- `text`
- `extraction_method`
- `discovered_links`
- `discovered_link_count`
- `discovered_links_truncated`
- `error`

Important policy fields for the adapter:

- `schema_version` currently `2026-06-23`
- `strategies`
- `limits`
- `headers`
- `request_profile`
- `browser_profile`
- `discovery`

## Adapter Responsibilities

PioneerResearch should:

1. Build an explicit crawl policy per host or job.
2. Send the policy and URL batch to `POST /v1/crawl`.
3. Persist every result with selected strategy, attempt history, classification evidence, diagnostics, and final URL.
4. Treat `classification_evidence` and `diagnostics` as review evidence, not automatic host-policy mutation.
5. Enqueue `discovered_links` only after app-level normalization, scope checks, dedupe, and persistence.
6. Use `discovered_link_count` and `discovered_links_truncated` to decide whether a broader extraction pass is needed.
7. Use `diagnostics.discovery.sitemap_urls` and `diagnostics.discovery.feeds` as candidate inputs for caller-owned frontier expansion.
8. Use `diagnostics.cooldown.retry_after_seconds` and retry diagnostics to avoid hammering fragile hosts.
9. Keep Chonkie/chunking downstream of the chosen `text`/`html` extraction output.

## Policy Recommendations

Default municipal page policy:

```json
{
  "schema_version": "2026-06-23",
  "strategies": ["HTTP_DECLARED_CRAWLER", "HTTP_SITE_COMPAT", "BROWSER_HEADLESS"],
  "limits": {
    "timeout_seconds": 30,
    "max_retries": 2,
    "challenge_wait_seconds": 15,
    "max_discovered_links": 250
  },
  "request_profile": {
    "user_agent_family": "desktop_chrome",
    "accept_language": "en-US,en;q=0.9",
    "cookie_jar": "none"
  },
  "browser_profile": {
    "state": "fresh",
    "viewport": "desktop",
    "resource_mode": "full",
    "cookie_jar": "none"
  },
  "discovery": {
    "robots": true,
    "sitemaps": true,
    "feeds": true
  }
}
```

Challenge-prone host policy:

```json
{
  "schema_version": "2026-06-23",
  "strategies": ["HTTP_SITE_COMPAT", "BROWSER_HEADED_WARMED"],
  "limits": {
    "timeout_seconds": 45,
    "max_retries": 1,
    "challenge_wait_seconds": 20,
    "max_discovered_links": 250
  },
  "request_profile": {
    "user_agent_family": "desktop_chrome",
    "accept_language": "en-US,en;q=0.9",
    "cookie_jar": "per_host"
  },
  "browser_profile": {
    "state": "per_host_warmed",
    "viewport": "desktop",
    "resource_mode": "full",
    "cookie_jar": "per_host"
  },
  "discovery": {
    "robots": true,
    "sitemaps": true,
    "feeds": true
  }
}
```

Document-first policy for known PDFs:

```json
{
  "schema_version": "2026-06-23",
  "strategies": ["DOCUMENT_EXTRACT", "HTTP_DECLARED_CRAWLER"],
  "limits": {
    "timeout_seconds": 30,
    "max_retries": 1,
    "max_discovered_links": 0
  },
  "discovery": {
    "robots": true,
    "sitemaps": false,
    "feeds": false
  }
}
```

## Classification and Diagnostics Handling

PioneerResearch should treat these classifications as terminal or reviewable outcomes instead of trying to bypass them:

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

Useful diagnostics currently include request profile, retry attempts, rate-limit cooldown, runtime/final URL, robots metadata, sitemap/feed candidates, browser profile, and content completeness metrics.

## Regression Fixtures

The regression suite includes real Mass.gov HTML fixtures under `tests/fixtures/browser_crawler/` with a manifest at `tests/fixtures/browser_crawler/manifest.json`:

- `massgov_city_town_websites.browser.html`
- `massgov_municipal_databank.browser.html`
- `massgov_403.http.html`

These fixtures cover the known Mass.gov behavior where direct HTTP can return a 403 but browser fallback can produce valid rendered content. PioneerResearch integration tests should mirror that scenario by asserting fallback behavior and avoiding automatic `rate_limited` classification from script/style contents.

## Manual Smoke Workflow

After changing crawler code locally:

```bash
docker compose build browser-crawler
docker compose up -d browser-crawler
curl -fsS http://127.0.0.1:8765/ready
python3 scripts/probe_session.py
```

Use `classification`, `classification_evidence`, `diagnostics`, `selected_strategy`, `quality_score`, and `discovered_link_count` to decide whether the result is usable for PioneerResearch ingestion.
