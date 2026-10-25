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
- **Commit verification trap (2026-10-25, three occurrences):** never pipe a commit through `tail`/`grep` (the pipe swallows pre-commit's exit code — exit 0 with nothing landed); never compound `--no-verify` into a multi-statement command; after every commit attempt verify `git log -1` shows the expected message BEFORE proceeding.
- **Hardstand adoption mandate (user directive 2026-10-24):** all implementation agents run inside their hardstand workspace only (config: hardstand.toml, workspaces under `.hardstand/` INSIDE `~/Projects/crawlutron` on `work/*` branches, venv via `ln -sfn ../../.venv .venv` symlink, zero downloads). Agents that hit blockers (tests not working, etc.) must message the parent and WAIT for guidance, never self-resolve by leaving the workspace or editing main. The parent is the sole responsible agent for enforcing adoption/compliance — fix hardstand if broken, do not abandon it. All 8 pre-commit hooks must work correctly INSIDE each workspace and BLOCK commits until green — verify by planting a violating commit (e.g. overlong docstring) inside a workspace and proving pre-commit refuses it, then a clean commit lands.
