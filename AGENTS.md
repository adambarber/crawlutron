# AGENTS.md

This file provides guidance to agents working in this repository.

- The service is a stateless execution surface. Callers own frontier queueing, dedupe, persistence, and host-policy decisions; do not add caller state to the service to make a caller's problem easier.
- Agent/bot integrations should use the machine-readable discovery endpoints first (`/.well-known/crawler-service` and `/v1/capabilities`) and follow the durable contract in `docs/references/crawlutron-agent-contract.md`.
- It must not bypass Cloudflare or WAF controls, solve CAPTCHAs, rotate proxies or identities, inject credentials, or continue after an explicit denial. Terminal classifications are reported, not worked around.

## Agent dispatch rules (process)

- **Pre-development review before any feature or fix.** Read the ticket, the modules it touches, and the gates that govern them; state how the change fits the current architecture before writing code. A ticket whose body cites stale paths must be re-grounded from the live tree first.
- **Architectural review gate on tickets.** A GitHub ticket must carry an architectural review (its reach, its design choice, its walk-past plan) before an implementation agent picks it up. A quick `scripts/ticket_arch_review.sh <issue>` check assists the reviewer; it does not replace the review.
- **All development is TDD.** Red first for every behavior; record the red output; then green; then refactor. Budgets and ratchets move DOWN only, in the same commit as the work that earns them.
- **Gates seal shut at zero.** When a gate's offender count reaches zero, delete its budget/exemption table and leave an unconditional assertion — no configuration surface survives a sealed gate. Verify with a mutation check (inject the violation; the gate must name file and line).
- **Document process improvements here.** Any change to dispatch rules, review requirements, or gate doctrine is written into this section in the same commit that lands it.
