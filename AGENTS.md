# AGENTS.md

This file provides guidance to agents working in this repository.

- The service is a stateless execution surface. Callers own frontier queueing, dedupe, persistence, and host-policy decisions; do not add caller state to the service to make a caller's problem easier.
- Agent/bot integrations should use the machine-readable discovery endpoints first (`/.well-known/crawler-service` and `/v1/capabilities`) and follow the durable contract in `docs/references/browser-crawler-agent-contract.md`.
- It must not bypass Cloudflare or WAF controls, solve CAPTCHAs, rotate proxies or identities, inject credentials, or continue after an explicit denial. Terminal classifications are reported, not worked around.
