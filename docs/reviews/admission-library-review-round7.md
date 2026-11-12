# Adversarial review round 7 — `admission` v2.6 spec (final verification)

**Verdict: defects-found — one BLOCKER in the Option-B delegation; not yet the
implementation brief.** Reviewed by ses_e3145c2ecffeIk9KlD5hZP4tvN against
`docs/design/admission-library-v2.md` (v2.6, ~1025 lines) with live-tree
verification of the delegation's cited signal. Everything round-6 required is
genuinely resolved; the full rounds-1–5 regression sweep is intact; rows 4/8
coherence verified (both key on the single gate-liveness predicate: grant OR
completed release). One BLOCKER: the delegation that seals the round-6 ruling
over-promises.

## BLOCKER — R7-B1: the delegated crawl-side signal, as cited, is itself blind
to the crash-loop (§4.2:411-412 + changelog:94, × `admission_stall.py:129`)

The doc delegates crash-loop detection to "the crawl-side output-progress
signal (`page_count` growth)". But the live signal at the cite the doc names as
the absorb site (`admission_stall.py:129`, cited at §9 line 672) is:

    if allowed_jobs or state.page_count > page_count_before:
        stall_guard.record_progress()

where `allowed_jobs = await stages.start_runner(...)` was set one line up
(:118). A re-granting crash-loop produces a truthy `allowed_jobs` on EVERY
pass (each admit grants ≥1 under the kernel's floor law), so
`record_progress()` fires before any output and the stall never trips. The
delegated catcher, as-cited, inherits exactly the grant-advance blindness the
doc just proved on the kernel side — R6-B1's false closure re-admits on the
crawl side.

Compounding: §4.2:407-411 and changelog:88 reproduce the blind formula
(`allowed_jobs or page_count > page_count_before`) and label it "the same
doctrine as production … counts admits as progress" — the reader is offered
the BLIND variant as the reference. And NO §9 migration unit operationalizes
the delegation (no `page_count`/`allowed_jobs`/`crawl_batches`/output
derivation anywhere in §9); it is asserted in prose only, with its one
grounded referent being the blind formula.

**Minimal fix (two clauses, mandated):** in §4.2's Option-B block and mirrored
once in §9 (migration unit 2 or a new unit): *"The delegated crawl-side
detector is `page_count` growth ONLY — the `allowed_jobs` term of the legacy
`crawl_batches` record (`admission_stall.py:129`) must NOT advance it, or it
inherits the kernel's grant-blindness (each re-grant makes `allowed_jobs`
truthy before any output). Tighten to `page_count > page_count_before` on the
delegated record."*

## Cleared

- R6-B1 kernel-side boundary: RESOLVED, no residual kernel-detects-caller-work
  phrasing (16 grep hits all legitimate — historical changelog or correct
  negative framing).
- R6-B3 asymmetry, R6-B4 re-scope, R6-M1 sync-release, changelog-verbatim:
  all RESOLVED. Rows 4/8 coherent under one predicate (q1 trace passes).
- Full rounds-1–5 regression sweep: ALL INTACT. Live inventory verified
  (4 pinned_resource_gate + 13 PinnedGate + ContentionFreeGate).
- v2.4 "`ramp // 4`" vs live `max_http_concurrency // 4`: pre-existing,
  matches integer-denominator semantics (in-code `#618` comment), non-blocking.

## Gate-readiness

The two-clause tightening (reachable in §4.2 + §9, plus a matching changelog
supersession note) is the ONLY thing between this document and
implementation-brief status. The reviewer's explicit position: "Apply it and
this document is the implementation brief."