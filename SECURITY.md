# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.2.x   | Yes                |
| 0.1.x   | Critical fixes only|
| < 0.1   | No                 |

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
  rebinding (finding F9 in `ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`).
- Validates every redirect hop manually so a `302 -> 169.254.169.254/`
  cannot be followed.
- Pins the browser navigation IP and re-validates post-redirect URLs.
- Caps request bodies (16 MiB), decompressed responses (50 MiB), CSS/XPath
  selector runtime (1 s), and HTML-size-to-lxml input (4 MiB) so a malicious
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

## Operational Guidance

- Never expose this service to the public internet without authentication,
  rate-limiting, and a concurrency cap on a per-client basis.
- Keep `CRAWLUTRON_ALLOW_PRIVATE_TARGETS=0` in production; loopback
  exemption is local-dev only. Setting it logs a `SSRF private-target guard
  DISABLED` WARNING at first use; treat that line in your logs as a finding.
- Keep `CRAWLUTRON_COOKIE_PERSISTENCE_ENABLED=false` unless you
  understand the cross-tenant cookie-leak risk; multi-tenant deployments
  should additionally set `request_profile.cookie_namespace`.
- Run behind a reverse proxy that strips `Server` headers and adds
  request-id logging.

## Audit Trail

- `ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md` tracks the findings and
  remediation of past adversarial reviews (last updated 2026-08-21).
