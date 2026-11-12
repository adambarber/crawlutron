# Adversarial review round 4 — `admission` v2.3 spec (implementation-readiness)

**Verdict: defects-found — not yet an implementation brief.** Reviewed by
ses_e31593f5cffeJgED8xh4YzVqrC against `docs/design/admission-library-v2.md`
(v2.3, ~895 lines) with empirical Python probes (asynccontextmanager semantics
verified on 3.14) and live-tree grounding (`admission_stall.py:25`
`NO_PROGRESS_STALL_SECONDS = 60.0`, `:78-79` classification, `:49` two-clock
default, `crawl_batches` progress on `allowed_jobs or page_count >
page_count_before`). Every round-1/2/3 resolution is **INTACT or RESOLVED** —
the seven-round regression sweep is clean. Two fresh MAJORs and four MINORs on
the v2.3 surface.

## MAJOR — B-1: early-return `release()` × releases-count-as-progress opens a
stall-detector blind hole (§4.4 × §4.2 × §0 #4)

V2.3 made early-return `release()` a documented supported door (§4.2) AND made
every release advance `last_advance` unconditionally (§4.4 "Progress is defined
as 'a grant OR a release'"). A dishonest/faulty body that admits, calls
`granted.lease.release()` immediately, never does the work, exits, re-admits —
each cycle advances `last_advance`; `StallGuard` (keys on `last_advance` only)
never trips the pinned 60s `admission_stalled` stop while competitors starve.
`release()` is one opaque call — the gate cannot tell completed work from
no-work. The round-3 fix *widened* the pre-existing hole (v2.2 grants-only had
it only for boxes that re-grant; v2.3 extends the mask to releases that do not
win a re-grant). Live doctrine notes admits-count-as-progress pre-exists
(`allowed_jobs or page_count > page_count_before`), so B-1 is a widening, not
de-novo.

**Minimal fix (chosen):** add a work-completion signal to release —
`release(*, work_completed: bool = True)`. Only a *completed* release (or a
grant) advances `last_advance`; an uncompleted early-return leaves
`last_advance` frozen so the 60s stop still fires. Add acceptance probe
`test_early_return_release_without_work_does_not_advance_progress`. (If
no-work-release suppression is instead accepted, it must be stated in §4.4 —
it currently is not.)

## MAJOR — B-2: the `threading.Lock` lifetime across the internal park is
unspecified — deadlock hazard (§4.3 × §4.2)

The only lock statement is "released before the `with` body yields" — *after*
the park. Model-A parks inside `__aenter__` via `_wait_for_progress()` (an
`await`). A naive implementer holds the `threading.Lock` across the park; a
concurrent **release** (which must take the lock to mutate in-flight + set the
versioned wakeup) blocks on acquire; the wakeup never reaches the parked admit;
RETRY_PRESSURE (event-bounded, "until a release/sensing wakeup") deadlocks
indefinitely. "No external code or I/O under the lock" *implies* the await is
outside, but the lock-vs-park lifetime and re-acquisition after wake are never
stated.

**Minimal fix:** one clause in §4.3 — "the `threading.Lock` is **released
before `_wait_for_progress()` parks** (the gate never holds the lock across an
await); the gate re-acquires it after wake to re-decide." Add a concurrency
probe: a parked admit awaiting while another thread releases must wake, not
deadlock.

## MINORs

1. **B-3 (STALE-park caller horizon):** §4.3 rule 1 makes the caller's
   `async with gate.admit(want)` block for up to the remaining `stale_bound`
   before `AdmissionRefused(STALE)` surfaces. Intended escape (MINOR-1 fix),
   but §4.3's "synchronous, non-blocking" claim should not apply to the STALE
   door; document the caller's bounded blocking horizon for a STALE resource.
2. **B-4 (asynccontextmanager finally):** empirically (3.14), when `__aenter__`
   raises/cancels mid-park, `__aexit__` is NOT called, but the generator's own
   cleanup (`finally`) DOES run. §4.2's safety rests on the concrete guard
   "release is a no-op on `self._lease is None`" — correct and sufficient — but
   the "by protocol" justification is imprecise. Sharpen: the abnormal-aenter
   path runs the generator `finally`; release there must no-op on
   `lease is None`.
3. **B-5 (`Lease` absent from the complete type surface):** `Granted(…,
   lease: Lease)` references `Lease`; §4.2/§9.1 describe its door — but §3's
   "complete type surface" table has no `Lease` row. An implementer reading §3
   cannot type `granted.lease`. Add a `Lease` row (owner-gate identity,
   `release() -> bool`, idempotent, wrong-gate rejection).
4. **B-6 (ghost Model-B echo):** §4.2 line ~301 "no separable 'admit then
   `with granted.lease()`'" describes a residual in the abolished vocabulary —
   under Model A there IS no `with granted.lease()` form. Rewrite to "no
   separable lease entry distinct from the `async with` admit."

## Gate-readiness

All four are unstated runtime/contract laws (the two MAJORs), not type
answers — exactly the class this review series pins before implementation.
Rulings table sweep clean (P0-2/P0-3/P0-4 all Model-A). All acceptance rows
reconciled except the new probes named above. The next revision (v2.4) plus a
verification round should reach implementation-brief status.