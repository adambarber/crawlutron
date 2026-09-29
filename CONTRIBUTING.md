# Contributing

Thanks for your interest in browser-crawler. This document covers the
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

Logging is configured by ``app/logging_config.configure_service_logging``
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

## Coding Style

- Python 3.11+ syntax; ruff enforces import order, pyupgrade, and the
  baseline PEP 8 checks (see `pyproject.toml`).
- 110-char line limit.
- Public functions take keyword arguments where the call site benefits.
- Every public module has a module-level docstring; every public function
  has a one-line summary plus a more detailed paragraph.

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

- `app/validation.py` — SSRF guard (`_check_ssrf_protection`,
  `_resolve_and_pin_host`), URL normalization, `parse_bool_env` for
  private-target opt-in, webhook URL validation.
- `app/crawl/strategies/http.py` — DNS pinning / cookie isolation /
  redirect handling / `Host:` header rewriting / per-tenant cookie
  namespace (`_COOKIE_NAMESPACE` ContextVar).
- `app/crawl/strategies/browser.py` — browser SSRF / storage_state path
  validation / `_auto_accept_cookie_consent`.
- `app/crawl/engine.py` — robots cache, recursive crawl caps,
  `_parse_robots_rules` / `_parse_robots_crawl_delay` group matching
  (must use `USER_AGENT_TOKEN`).
- `app/crawl/strategies/browser_pool.py` — browser-pool sizing from
  cgroup memory limits; ensure `_BROWSER_RAM_FRACTION` is honoured.
- `app/main.py` — middleware order, body-size limits, error envelopes,
  webhook delivery (`_deliver_pinned_webhook`).
- `app/output_formats.py` — CSV injection (`_csv_neutralize`),
  response serialization.
- `app/settings.py` — defaults (paths, security knobs); changing a
  default is a security decision.

If your PR modifies any of these, please call it out in the description
and link the relevant section of `ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`.
