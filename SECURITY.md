# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.3.x   | Yes                |
| < 0.3   | Critical fixes only|

## Reporting a Vulnerability

Please **do not file public issues** for suspected security vulnerabilities.

Open a private security advisory on GitHub with:

- A description of the vulnerability and the affected component.
- Reproduction steps (a minimal request payload is ideal).
- The expected vs. observed behavior.

We aim to acknowledge reports within 2 business days and to ship a fix or
mitigation within 30 days for confirmed issues. Reporters will be credited
in the release notes unless they request anonymity.

## Threat Model

The crawlutron is designed for **localhost or trusted-network** operation.
It is a powerful SSRF-enabled service that:

- Resolves and **pins** the IP of every URL it fetches to defeat DNS
  rebinding (finding F9 in `docs/reviews/ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`).
- Validates every redirect hop manually so a `302 -> 169.254.169.254/`
  cannot be followed.
- Pins the browser navigation IP and re-validates post-redirect URLs.
- Caps request bodies (16 MiB), decompressed responses (10 MiB default
  content cap, caller-tunable up to the 1 GiB hard ceiling), CSS/XPath
  selector runtime (2 s default, `CRAWLUTRON_EXTRACTION_SELECTOR_TIMEOUT`),
  and HTML-size-to-lxml input (4 MiB) so a malicious
  input cannot exhaust memory or pin a worker.
- Default-binds to `127.0.0.1`; callers authenticate with an API key
  (`X-API-Key` / `Authorization: Bearer`), keyless requests crawl as a shared
  bounded anonymous identity, and key issuance is operator-gated via `/v1/keys`
  (`CRAWLUTRON_KEY_ADMIN_TOKEN`). Any exposure beyond localhost must still add
  a transport authentication layer
  (recommendation: Caddy basic-auth or an upstream reverse proxy).

### What it deliberately does **not** do

- Solve CAPTCHAs or bypass anti-bot challenges.
- Inject credentials, rotate proxies, or rotate identities.
- Bypass paywalls, logins, or any access control on the target.

### Known residual weaknesses

- **S1 — HTTPS DNS rebind window.** HTTP targets are IP-pinned end to end.
  HTTPS browser navigation does two fresh DNS resolves but lets the browser
  connect by hostname, so a hostile DNS server can rebind between check and
  connect.
- **S4 — credential origins ride the egress guard.** Credential origins pass
  the same SSRF egress check as crawl targets: a private/loopback credential
  origin requires `CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1`.
- **S5 — caller keys hash with plain SHA-256.** Unsalted SHA-256 is acceptable
  only because key secrets are `secrets.token_urlsafe(32)` (256-bit entropy);
  a digest table adds nothing to attack against an unguessable secret.

### Dangerous environment escape hatches

The README's "Dangerous environment escape hatches" table is the single
source (derived from the live settings/env code) for every `CRAWLUTRON_*`
variable that weakens a safety default. Keep `CRAWLUTRON_RESPECT_ROBOTS` and
`CRAWLUTRON_ALLOW_PRIVATE_TARGETS` unset in production. Webhook delivery is
https-only (#651): both webhook doors refuse `http://` callback targets.

## Operational Guidance

- Never expose this service to the public internet without authentication,
  rate-limiting, and a concurrency cap on a per-client basis.
- Keep `CRAWLUTRON_ALLOW_PRIVATE_TARGETS=0` in production; loopback
  exemption is local-dev only. Setting it logs a `SSRF private-target guard
  DISABLED` WARNING at first use; treat that line in your logs as a finding.
- Keep `CRAWLUTRON_COOKIE_PERSISTENCE_ENABLED=false` unless you
  understand the cross-tenant cookie-leak risk. Keyed crawls are always
  namespaced to their key id; anonymous crawls share one bounded jar and may
  not declare `request_profile.cookie_namespace` (the declaration is a 422).
- Run behind a reverse proxy that strips `Server` headers and adds
  request-id logging.

## Audit Trail

- `docs/reviews/ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md` tracks the findings and
  remediation of past adversarial reviews (last updated 2026-08-21).
