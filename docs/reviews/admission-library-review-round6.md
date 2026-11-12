# Adversarial review round 6 — `admission` v2.5 spec (final verification)

**Verdict: defects-found — not yet an implementation brief.** Reviewed by
ses_e314de0abffeCA1ssYcgx0bG9s against `docs/design/admission-library-v2.md`
(v2.5, 993 lines read in full) with real 3.14 `asynccontextmanager`/cancellation
probes in the temp dir. Every round-1–5 resolution holds; the regression sweep
is clean. One BLOCKER-class internal contradiction in the crash-loop closure
claim, one MINOR asymmetry, one precision correction to R5-B3.

## Mechanics verified CORRECT (probes, not claims)

- **`sys.exc_info()` survives the real double-yield shape.** In the genuine
  `@asynccontextmanager` machinery, `__aexit__` runs first (returns False →
  re-propagate), and the generator's `finally` executes with `sys.exc_info()`
  **still live**: ValueError on body-raise, `CancelledError` on body
  cancellation, None on normal exit. R5-B1's mechanism works on both paths
  (the round-5 probe was a raw generator; round 6 re-probed the real shape).
- **Release-in-finally under cancellation is safe**: `release()` is
  synchronous (`-> bool`, `threading.Lock` bookkeeping) — no await point to
  interrupt; a single delivered `CancelledError` runs a bounded finally to
  completion then re-propagates.
- R5-B2 commit-under-lock, R5-B4 stale_bound provenance, cosmetic collapse:
  all RESOLVED.

## BLOCKER — R6-B1: the crash-loop closure claim (§4.2:381-383) is false under
the grant-advance law (§4.4:506-508)

§4.2 asserts a crash-looping engine (admit → raise instantly → release-False →
re-admit) "still trips the 60s stop." Trace under Model A + grant-advance:
(1) every re-admit GRANTS — a live `Granted` is the only way to reach the
body; (2) each grant executes `last_advance = max(last_advance, now)`; (3) the
`release(work_completed=False)` then does not advance — irrelevant, the grant
already did; (4) cycle repeats, `now − last_advance` never approaches 60s, the
StallGuard (keyed on `last_advance` only) **never fires**. R5-B1 closed only
the RELEASE surface; the grant surface is open. One `last_advance` field
cannot simultaneously satisfy the ordering law (grant must advance it —
round-2 MAJOR-1: "Progress fails on the very first grant") and detect a
crash-loop (grant must not advance it). Reachability: an ordinary
`async with gate.admit(...)` retry loop that raises each iteration.

Two resolutions were offered:
- **(A) Deferred completion credit** — a grant updates `last_decision` but not
  `last_advance` until its body exits `work_completed=True`. Closes the
  crash-loop but breaks the long-grant case (a 70s body with no other activity
  false-stalls at 60s — the exact MAJOR-1 failure mode) and must be retraced
  against row-8's draining case.
- **(B) Delegate engine-level crash-loop detection** — the kernel's StallGuard
  detects admission-gate liveness (grants, completions, transactions);
  a re-granting crash-loop is engine-level dysfunction the kernel cannot
  distinguish from legitimate rapid admission cycles. The 60s
  `admission_stalled` stop for *engine output* is delegated to the crawl-side
  output-progress signal (the legacy `crawl_batches`
  `allowed_jobs or page_count > page_count_before` record — admits-count-as-
  progress is ALREADY production doctrine, verified live); rewrite
  §4.2:381-383 to remove the false kernel claim.

**PARENT RULING: Option B.** Grant-advance mirrors production doctrine (the
round-4 reviewer verified `allowed_jobs or page_count > page_count_before`
counts admits as progress today). The kernel guarantees: the gate never
returns (0,0) when work is wanted, parks are bounded, capacity is never
overshot, and gate-level liveness is StallGuarded. Detecting a caller's
work-failure loop is not the kernel's contract — a generic admission library
cannot know what "work" means; that signal belongs where output is measurable
(crawl side). The `work_completed` flag remains as honest-caller telemetry
(prevents release-masking, feeds the crawl-side output derivation) but the
kernel must claim exactly what it can guarantee.

## MINOR — R6-B3: completed-then-raised asymmetry unstated (§4.4)

Grant at t₀ → body does 10s real work → raises at t₁₀ → finally
`release(work_completed=False)` → `last_advance` stays t₀ → a want at t₀+65
trips the stop though 10s of work happened. The unqualified exceptioned=False
rule is the honest fail-closed position (the gate cannot audit a raised body's
partial work) but it is only implied, never stated.

**Fix:** one clause in §4.4 — "an exceptioned exit is treated as
non-progress unconditionally, even when the body performed work before
raising; a grant is the only mid-grant progress marker. A body that works
then raises freezes progress at its grant-time; the false-stall this can
cause for a work-then-fail pattern is the accepted fail-closed cost."

## MINOR — R6-B4: good-faith scoping under-inclusive (§4.4:533-543)

R5-B3's clause scopes suppression to "a caller that lies True". But the normal
body-exit door is NOT a self-report: `__aexit__` synthesizes
`work_completed=True` on ANY normal exit. A buggy-but-honest body that admits,
does nothing, and exits normally is credited True — suppress the stop without
ever lying. Same mathematical surface as a liar; not closed by R5-B1.

**Fix:** re-scope — "the gate treats EVERY normal body exit as completed
work; it cannot distinguish an idle-but-normal body from a worked one, nor
police a lying `True`. Both are accepted out-of-scope."

## MINOR — R6-M1 (reviewer): state `release()` is synchronous

The spec never labels `release()` as sync (`-> bool`, `threading.Lock`
bookkeeping, no await) — an implementer could read the finally's release as an
await point. One clause: "`release()` is synchronous; it performs no I/O and
cannot be interrupted mid-bookkeeping."

## Gate-readiness

All three revisions are one-clause textual changes plus the Option-B rewrite
of §4.2:381-383. The row-4 probes stay (they pin the release surface); the
option-B landing should note that kernel-side crash-loop detection is NOT
claimed. The next revision (v2.6) plus one verification round reaches the
implementation brief.