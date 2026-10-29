# Contributing

Thanks for your interest in crawlutron. This document covers the
mechanics of submitting a change.

## Development Setup

```bash
# Clone + enter the repository
git clone https://github.com/adambarber/crawlutron.git
cd crawlutron

# Install dependencies in a virtualenv (Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Logging is configured by ``app/runtime.logging_config.configure_service_logging``
and lives entirely in this repository; no external shared-tree package is
required.

## Running the Test Suite

```bash
# Fast hermetic suite (default; excludes browser/slow markers).
./scripts/run-tests.sh

# Single test
./scripts/run-tests.sh -k test_cookie_namespace

# Full suite including browser-marked tests (requires Camoufox).
./scripts/run-tests.sh -o addopts=
```

## Live End-to-End Verification

Beyond the in-process suite, `scripts/e2e_verify.py` drives a *running*
service (default `http://127.0.0.1:8765`) and asserts on live responses:
health/discovery, anonymous and keyed crawls, `/.well-known`, 401/429
refusals, and cross-key read isolation.

```bash
python scripts/e2e_verify.py            # against the default service
python scripts/e2e_verify.py http://host:port https://example.org
```

This harness is a manual live-smoke command, not a CI gate: operators run it
against a deployed instance to confirm the live surface end to end.

Run `scripts/probe_session.py` to probe an arbitrary site through the real
browser + curl_cffi fetch ladder. Both scripts are import-safe: importing
them has no side effects, and the keyed checks only run when
`CRAWLUTRON_KEY_ADMIN_TOKEN` is set.

## Coding Style

- Python 3.11+ syntax; ruff enforces import order, pyupgrade, and the
  baseline PEP 8 checks (see `pyproject.toml`).
- 110-char line limit.
- Public functions take keyword arguments where the call site benefits.
- Every public module has a module-level docstring; every public function
  has a one-line summary plus a more detailed paragraph.

## Conduct

Review the change, not the contributor, and bring a reason rather than a
preference. `CODE_OF_CONDUCT.md` states the expectations in full.

## Submitting a Change

1. Branch off `main`.
2. Add tests for any behavioral change. New code without tests will not
   be merged.
3. Run the full fast suite locally and ensure it passes.
4. Open a PR with:
   - A description of the change and the motivation.
   - Links to any related issues.
   - A note on whether the change touches security-sensitive code
     (SSRF, cookies, persistence, resource limits).

## Security-Sensitive Areas

These require explicit review from a maintainer before merge:

- `app/models/normalized_url.py` and `app/models/host_policy.py` — the URL
  admission chokepoint: the `NormalizedUrl` constructor is the single place
  raw URL text is parsed, and the public/SSRF egress verdict plus the
  private-target opt-in are decided here.
- `app/transport/pinned_transport.py` — transport SSRF guard
  (`check_ssrf_protection`, `resolve_and_pin_host`), request pinning,
  redirect-target admission, `Host:` header construction.
- `app/transport/cookie_jar.py` — per-tenant cookie namespace
  (`COOKIE_NAMESPACE` ContextVar) and cookie isolation.
- `app/transport/browser.py` and `browser_driver.py` — browser SSRF,
  storage_state path validation, opt-in cookie-consent auto-acceptance.
- `app/transport/robots.py` and `app/transport/robots_parse.py` — robots policy and
  rule cache; `parse_robots_rules` / `parse_robots_crawl_delay` must use
  `USER_AGENT_TOKEN`.
- `app/safety/validation.py` — egress filter for scraped JS-endpoint URLs
  (`filter_js_endpoints`), resolved-address public check
  (`is_public_ip_value`).
- `app/runtime/pressure_sizing.py` and `app/transport/browser_pool.py` —
  browser-pool sizing from cgroup memory limits; `BROWSER_RAM_FRACTION`.
- `app/main.py` — middleware order, body-size limits, error envelopes.
- `app/api/webhooks.py` — webhook delivery (`deliver_pinned_webhook`).
- `app/config/env.py` — `parse_bool_env` and the operator environment reads,
  including the private-target opt-in.
- `app/extraction/output_formats.py` — CSV serialization (``_to_csv``),
  response serialization.
- `app/config/settings.py` — defaults (paths, security knobs); changing a
  default is a security decision.

If your PR modifies any of these, please call it out in the description
and link the relevant section of `docs/reviews/ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`.
