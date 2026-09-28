# Completion Summary

Status: completed and ready for local testing.

Completed commits:

- `e0f3a82` Add policy-driven crawler API
- `79f4066` Complete crawler extraction strategies
- `4d0d327` Harden crawler extraction runtime

Implemented outcomes:

- Added policy-driven `POST /v1/crawl` while preserving `POST /v1/render`.
- Added typed Pydantic request/response models and rich OpenAPI metadata.
- Added `GET /v1/capabilities`, `GET /v1/agent-hints`, and `GET /.well-known/crawler-service`.
- Implemented strategy pipeline support for direct HTTP, site-compatible HTTP, browser fallback, headed warmed browser policy surface, document extraction, and Trafilatura extraction.
- Added per-URL provenance, attempt history, classifications, quality scores, extraction methods, and stable validation errors.
- Preserved localhost-only/profile-gated Docker behavior and conservative concurrency defaults.

Verification completed before archive:

- `python3 -m unittest tests.test_browser_crawler_api -v`
- `python3 -m unittest tests.test_browser_crawler_core -v`
- `python3 -m unittest tests.test_config_validation.ConfigValidationTest.test_browser_crawler_service_is_profile_gated_and_configurable -v`
- `python3 -m unittest discover -s tests -v`
- `docker compose --profile browser-crawler config --services`
- `docker compose --profile browser-crawler config`
- `docker compose --profile browser-crawler build browser-crawler`

Next step: run the service locally through the safe helper and exercise `GET /v1/capabilities`, `GET /v1/agent-hints`, and `POST /v1/crawl` against known public/local test URLs.

---

# Spec: Browser Crawler Service

## Objective

Build a local, containerized browser-crawling service for collecting public web pages that require JavaScript rendering. The service accepts one URL or a batch of URLs, renders each page in a real Chromium browser driven by Python and `undetected-chromedriver`, then returns both:

- raw rendered HTML (`document.documentElement.outerHTML`), and
- raw rendered visible text (`document.body.innerText`).

The primary user is a developer/researcher collecting public municipal data where pages are technically public but may use fragile JavaScript, bot-sensitive middleware, or inconsistent availability. The crawler should be resilient and respectful: it should reduce accidental Selenium fingerprints, classify block conditions, retry transient failures, obey configured rate limits, and avoid hostile behavior such as CAPTCHA solving, credentialed bypass, paywall bypass, or excessive request volume.

### User stories

- As a researcher, I can post a single public URL and receive the rendered HTML and visible text.
- As a researcher, I can post a batch of public URLs and receive a per-URL result without waiting for each domain sequentially.
- As a researcher, I can distinguish successful renders from retries, network failures, robots/rate-limit rejections, CAPTCHA/block pages, and timeout failures.
- As an operator, I can configure concurrency, per-domain rate limits, retry policy, browser timeouts, allowed hosts, and logging without editing source code.
- As an operator, I can run tests before enabling the service and can preview Docker Compose topology before starting it.

### Acceptance criteria

- A `POST /v1/render` API accepts a JSON payload with one URL or many URLs.
- A response includes one result object per submitted URL, preserving request order.
- Different registrable domains can be processed concurrently up to a global concurrency limit.
- Same-domain requests are constrained by per-domain concurrency and delay settings.
- Each result includes status, final URL, HTTP/browser outcome metadata, rendered HTML when available, rendered visible text when available, timing, retry count, and block classification when applicable.
- The service can identify common block outcomes without attempting to defeat them: CAPTCHA/interstitial, login wall, access denied, 403/429, robots disallow when robots enforcement is enabled, navigation timeout, browser crash, and network/DNS/TLS failures.
- The initial implementation is TDD-first: behavior tests are written before implementation for request validation, concurrency scheduling, retry classification, block classification, and API response shape.

## Safety and Compliance Boundaries

Anti-detection in this project means reducing accidental automation fingerprints during legitimate access to public pages. It does not mean bypassing explicit controls or increasing pressure on target systems.

### Always do

- Crawl only public URLs supplied by the caller.
- Keep default rate limits conservative.
- Enforce configurable allowlists/denylists before navigation.
- Validate URL scheme and reject non-HTTP(S) schemes.
- Respect per-domain concurrency and backoff, especially after 429/503/timeouts.
- Detect CAPTCHA, login walls, paywalls, explicit access-denied pages, and block pages as terminal or operator-actionable outcomes.
- Log crawl decisions, retry counts, target host, result class, timing, and sanitized errors for auditability.
- Redact request headers, cookies, proxy credentials, and API keys from logs.
- Return structured errors rather than throwing raw stack traces to API clients.
- Use TDD for implementation and keep tests deterministic with fake browser/session adapters where possible.

### Ask first

- Adding or enabling outbound proxies.
- Persisting full rendered HTML/text to disk or databases.
- Crawling credentialed pages or pages behind user accounts.
- Changing robots.txt handling defaults.
- Increasing default global or per-domain concurrency above conservative local-development values.
- Introducing third-party CAPTCHA-solving, browser-fingerprinting, or traffic-routing services.
- Adding a public network binding beyond `127.0.0.1`.

### Never do

- Do not solve or outsource CAPTCHA challenges.
- Bypass authentication, paywalls, account gates, or IP bans.
- Ignore explicit `robots.txt` disallow rules when robots enforcement is enabled.
- Rotate identities to continue after a site has clearly denied access.
- Commit secrets, cookies, captured credentials, rendered page dumps, or generated crawl logs.
- Run broad `docker compose up -d` for this stack when a targeted profile command is sufficient.

## Tech Stack

- Language: Python 3.12 or newer.
- API framework: FastAPI with Uvicorn, unless implementation discovery finds a lighter fit.
- Browser automation: Selenium 4 plus `undetected-chromedriver`.
- Browser runtime: Chromium/Chrome inside a custom Docker image.
- Data model: Pydantic request/response models.
- Concurrency: `asyncio` orchestration with bounded worker pools; browser work runs in isolated worker tasks/processes as needed because Selenium is blocking.
- Domain parsing: `tldextract` or equivalent Public Suffix List-aware parsing for registrable-domain grouping.
- HTML/text extraction: JavaScript execution in the rendered page context, with fallback to Selenium page source and body text.
- Testing: Python `unittest` first, matching this repository's existing test style, with fakes for browser behavior and targeted integration tests for API contract.
- Containerization: Docker Compose service gated behind a new profile, likely `browser-crawler`.

Dependencies should be pinned in a service-specific requirements file during implementation. If dependency resolution makes `undetected-chromedriver` incompatible with the selected Chromium version, the implementation plan must pin a known-good browser/driver combination and document the upgrade path.

## API Contract

### `POST /v1/render`

Request body:

```json
{
  "urls": ["https://example.gov/search?q=permit"],
  "options": {
    "timeout_seconds": 30,
    "wait_until": "network_idle_or_timeout",
    "max_retries": 2,
    "respect_robots": true,
    "include_html": true,
    "include_text": true
  }
}
```

Single URL convenience form may be supported if it does not complicate validation:

```json
{
  "url": "https://example.gov/search?q=permit"
}
```

Response body:

```json
{
  "request_id": "crawl_20260623_abc123",
  "submitted_count": 1,
  "results": [
    {
      "url": "https://example.gov/search?q=permit",
      "status": "success",
      "classification": "rendered",
      "final_url": "https://example.gov/search?q=permit",
      "html": "<html>...</html>",
      "text": "Visible rendered text...",
      "attempts": 1,
      "elapsed_ms": 1842,
      "domain": "example.gov",
      "metadata": {
        "title": "Permit Search",
        "http_status_hint": null,
        "content_length": 123456,
        "browser_reused": false
      },
      "error": null
    }
  ]
}
```

### Result statuses

- `success`: rendered HTML/text is available.
- `blocked`: navigation completed but page appears to be a CAPTCHA, access-denied page, bot-block interstitial, login wall, or paywall.
- `disallowed`: request was rejected before navigation by allowlist, denylist, URL validation, or robots policy.
- `retry_exhausted`: transient failures were retried and no successful render was obtained.
- `failed`: non-retryable browser, validation, DNS, TLS, or internal error.

### Block classifications

- `rendered`
- `captcha_detected`
- `access_denied`
- `login_required`
- `paywall_detected`
- `rate_limited`
- `robots_disallowed`
- `navigation_timeout`
- `browser_crash`
- `network_error`
- `tls_error`
- `dns_error`
- `invalid_url`
- `unsupported_scheme`
- `unknown_error`

## Smart Crawling Behavior

### URL validation

- Accept only `http://` and `https://` URLs.
- Normalize and parse hostnames before scheduling.
- Reject localhost, private IP ranges, link-local ranges, and internal Docker hostnames by default to reduce SSRF risk.
- Apply optional allowlist/denylist patterns before any browser navigation.
- Deduplicate exact URLs only if the caller requests deduplication; otherwise preserve one result per submitted URL.

### Domain-aware concurrency

- Group scheduled URLs by registrable domain.
- Run multiple domains in parallel up to `BROWSER_CRAWLER_GLOBAL_CONCURRENCY`.
- Run same-domain tasks up to `BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY`, defaulting to `1`.
- Apply `BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS` between same-domain navigations.
- Apply stronger backoff after 429, 503, navigation timeout, or suspected block.
- Preserve input order in the final response even when execution completes out of order.

### Retry policy

Default retryable conditions:

- browser startup failure,
- browser crash before navigation completes,
- navigation timeout,
- transient network reset,
- DNS/TLS errors when configured as retryable,
- HTTP status hints of 429, 500, 502, 503, or 504 when observable.

Default non-retryable conditions:

- invalid URL,
- unsupported scheme,
- robots disallow,
- allowlist/denylist rejection,
- CAPTCHA detection,
- login required,
- paywall detection,
- explicit access denied unless configured for one conservative retry.

Retry behavior:

- Use exponential backoff with jitter.
- Cap retries per URL.
- Include retry count and last error in the result.
- Stop retrying a domain temporarily when repeated rate-limit/block classifications occur.

### Browser behavior

The implementation should follow public Selenium hardening guidance for reducing obvious automation fingerprints while staying within the project boundaries:

- Use `undetected-chromedriver` as the Chrome driver bootstrapper.
- Run a real browser profile per worker or controlled profile pool.
- Set realistic viewport dimensions.
- Avoid unnecessary Selenium defaults that expose automation flags when `undetected-chromedriver` supports safer defaults.
- Prefer normal page navigation and DOM extraction over direct scraping shortcuts.
- Wait for document readiness and a bounded network/DOM stability heuristic before extraction.
- Avoid credential/cookie injection unless explicitly approved in a future spec update.
- Do not fake human interaction unless it is a simple, deterministic requirement to reveal public content, such as clicking a public "accept cookies" or "show results" button and that behavior has explicit tests.

### Render completion strategy

Initial completion heuristic:

1. Navigate to the URL with page load timeout.
2. Wait for `document.readyState` to be `interactive` or `complete`.
3. Wait for either a short DOM-stability window or a maximum render timeout.
4. Check for block-page signatures.
5. Extract `document.documentElement.outerHTML` and `document.body.innerText`.

Future implementation may add site-specific adapters, but the first version should avoid hidden site-specific behavior.

## Project Structure

Expected implementation layout:

```text
docker/browser-crawler/
  Dockerfile
  requirements.txt
  app/
    __init__.py
    main.py
    models.py
    scheduler.py
    browser.py
    classify.py
    robots.py
    settings.py
scripts/
  browser-crawler.sh
tests/
  test_browser_crawler_api.py
  test_browser_crawler_scheduler.py
  test_browser_crawler_classifier.py
  test_browser_crawler_settings.py
  test_scripts.py
docs/work/active/
  2026-06-23-browser-crawler-service.md
```

Potential Compose additions during implementation:

- `browser-crawler` service under a `browser-crawler` profile.
- explicit environment variables only; no broad `env_file`.
- pinned image default in `.env.example`, such as `BROWSER_CRAWLER_IMAGE=dev-stack/browser-crawler:local`.
- localhost-only published API port, such as `127.0.0.1:${BROWSER_CRAWLER_HOST_PORT:-8765}:8765`.
- Caddy route only if local dashboard/API UX warrants it, e.g. `https://crawler.local`.
- log rotation using existing Compose logging anchor.
- `mem_limit` and shared memory sizing appropriate for Chromium.

## Commands

Specification and planning phase:

```bash
python3 -m unittest discover -s tests -v
docker compose --profile browser-crawler config --services
docker compose --profile browser-crawler config browser-crawler
```

Expected implementation commands:

```bash
python3 -m unittest tests.test_browser_crawler_api -v
python3 -m unittest tests.test_browser_crawler_scheduler -v
python3 -m unittest tests.test_browser_crawler_classifier -v
python3 -m unittest discover -s tests -v
docker compose --profile browser-crawler build browser-crawler
docker compose --profile browser-crawler up -d browser-crawler
curl -sS http://127.0.0.1:${BROWSER_CRAWLER_HOST_PORT:-8765}/health
curl -sS http://127.0.0.1:${BROWSER_CRAWLER_HOST_PORT:-8765}/v1/render \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.org"],"options":{"include_html":true,"include_text":true}}'
```

Safe helper expectations:

```bash
./scripts/browser-crawler.sh up --print-command
./scripts/browser-crawler.sh up
./scripts/browser-crawler.sh check
./scripts/browser-crawler.sh render https://example.org
```

## Code Style

The implementation should keep orchestration logic testable without launching Chrome in most tests. Browser automation should sit behind a small adapter interface so scheduler/retry/classification behavior can be tested with fakes.

Example style target:

```python
@dataclass(frozen=True)
class CrawlJob:
    index: int
    url: str
    domain: str
    options: RenderOptions


class DomainScheduler:
    def __init__(self, *, global_limit: int, per_domain_limit: int, delay_seconds: float) -> None:
        self._global_limit = global_limit
        self._per_domain_limit = per_domain_limit
        self._delay_seconds = delay_seconds

    async def run(self, jobs: list[CrawlJob], worker: CrawlWorker) -> list[CrawlResult]:
        """Run jobs with bounded global and per-domain concurrency, preserving input order."""
```

Conventions:

- Prefer explicit small classes/functions over framework magic.
- Keep Selenium calls isolated in `browser.py`.
- Keep block-page signature logic isolated in `classify.py`.
- Keep settings parsing isolated in `settings.py`.
- Return typed result objects; convert to API JSON at the boundary.
- Avoid sleeping in tests; inject clocks/backoff strategies where possible.
- Avoid real Docker, real municipal sites, or real browsers in unit tests.

## Testing Strategy

Use TDD. Write failing tests before implementation for each behavior slice.

### Unit tests

- URL validation rejects unsafe schemes and private network targets.
- Domain extraction groups URLs by registrable domain.
- Scheduler starts different domains concurrently while preserving per-domain ordering/delay.
- Scheduler preserves result order after out-of-order completion.
- Retry policy retries transient classifications and stops on terminal classifications.
- Classifier detects representative CAPTCHA/access denied/login/paywall/rate-limit snippets.
- Settings load defaults and environment overrides.

### API tests

- `POST /v1/render` validates payload shape.
- Single URL and batch payloads produce consistent response shape.
- Mixed success/failure batches return per-URL statuses rather than failing the whole request.
- Request limits reject overly large batches.
- Errors are structured and sanitized.

### Compose/script tests

- Compose service is profile-gated.
- Compose service uses explicit environment variables and localhost-only port binding.
- Helper script `--print-command` prints targeted safe commands before execution behavior.
- `.env.example` contains pinned image/default knobs.

### Integration tests

- Optional and manually gated browser smoke test against a local static page.
- Optional browser smoke test verifies JavaScript-rendered text is present in output.
- Avoid live municipal sites in automated tests.

## Observability

- Add structured JSON logs for request ID, URL host, classification, status, elapsed time, retry count, and sanitized error class.
- Include a `GET /health` endpoint that does not start a browser.
- Include an optional `GET /ready` endpoint that can verify browser startup if explicitly requested by configuration.
- Expose simple counters internally or in logs: submitted URLs, successes, blocked pages, disallowed URLs, retries, timeouts, browser crashes.
- Do not log full HTML/text by default.

## Configuration

Expected environment variables:

```text
BROWSER_CRAWLER_IMAGE=dev-stack/browser-crawler:local
BROWSER_CRAWLER_HOST_PORT=8765
BROWSER_CRAWLER_GLOBAL_CONCURRENCY=4
BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY=1
BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS=1000
BROWSER_CRAWLER_MAX_BATCH_SIZE=50
BROWSER_CRAWLER_DEFAULT_TIMEOUT_SECONDS=30
BROWSER_CRAWLER_DEFAULT_MAX_RETRIES=2
BROWSER_CRAWLER_RESPECT_ROBOTS=true
BROWSER_CRAWLER_ALLOWED_HOSTS=
BROWSER_CRAWLER_DENIED_HOSTS=
BROWSER_CRAWLER_LOG_LEVEL=INFO
BROWSER_CRAWLER_MEM_LIMIT=2g
BROWSER_CRAWLER_SHM_SIZE=1g
```

## Success Criteria

The feature is ready to implement when this spec is accepted and converted into tasks. The implementation is ready to use when:

- Tests are written first for each behavior slice.
- `python3 -m unittest discover -s tests -v` passes.
- `docker compose --profile browser-crawler config browser-crawler` validates the service shape.
- The service renders a local JavaScript test page and returns both rendered HTML and rendered text.
- Batch requests process different domains concurrently under deterministic scheduler tests.
- Same-domain requests are rate-limited under deterministic scheduler tests.
- Block pages are classified without attempting bypass.
- The helper script supports `--print-command`, `up`, `check`, and a simple `render` command.
- Documentation explains safe operation, limitations, and boundaries.

## Open Questions

1. Should robots.txt enforcement default to hard-block (`true`) or advisory logging for municipal public-record workflows?
2. Should the first API return full HTML/text inline only, or also support writing large results to local files later?
3. What maximum response size should be enforced per page and per batch?
4. Should the service require a local API key, or is localhost-only binding sufficient for this dev-stack phase?
5. Is a Caddy `.local` route (`crawler.local`) valuable for this API, or should it stay at `127.0.0.1` only?
6. Should simple public-consent clicks, such as cookie banners, be part of v1 or deferred until site-specific adapters exist?
