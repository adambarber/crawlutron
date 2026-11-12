# Adversarial review round 5 — `admission` v2.4 spec (implementation-readiness)

**Verdict: defects-found — not yet an implementation brief.** Reviewed by
ses_e3153629affe6GCrdXsMHIxLes against `docs/design/admission-library-v2.md`
(v2.4, ~930 lines) with empirical asynccontextmanager probes (3.14:
`finally` runs on body-raise; `sys.exc_info()` discriminates normal vs raised
exit) and live-tree grounding (`saturated_dispatch_floor = max(1, ramp//4)`
at `pressure_sizing.py:249`, `NO_PROGRESS_STALL_SECONDS = 60.0` at
`admission_stall.py:25`, loop-lag at `pressure_gates.py:199`). Every round-4
finding is RESOLVED; the rounds-1–3 regression sweep is INTACT (B1 FloorDivisor
through contextvar, 13-count, :186-194 cite all verified). Two fresh MAJORs
and two MINORs in the v2.4 surface.

## MAJOR — R5-B1: exceptioned-body exit leaves `work_completed` unspecified,
reopening the round-4 blind hole via the exception door (§4.2 × §4.4 × §0 #4)

The spec states only the NORMAL-exit value ("`__aexit__` passes
`work_completed=True` on normal body exit") and never the EXCEPTIONED-body
value — while §4.2 itself distinguishes "body exit, exception, or
cancellation". A naive implementer writes `finally: await
self._lease.release()` (default True). Empirically, a body that raises
immediately produces exactly that: the `finally` runs with True after the
exception and after `__aexit__` was skipped — an immediately-raising body
advances `last_advance`. A crash-looping engine (admit → raise instantly →
release-True → re-admit) never trips the pinned 60s `admission_stalled` stop —
the precise blind hole round-4 B-1 fixed, reopened through the exception door.
Mechanically avoidable: inside the generator `finally`, `sys.exc_info()[0]` is
None on normal exit and the exception type on body-raise — the design can
distinguish; the spec never tells the writer to.

**Minimal fix (mandated):** §4.2 — on EXCEPTIONED body exit (including an
exception before any work), the generator `finally` passes
`work_completed=False` (a raised body has not completed its grant); body
cancellation likewise passes `work_completed=False`; only a normal body exit
OR an explicit caller `release(work_completed=True)` advances progress. Name
`sys.exc_info()` as the mechanism. Add `test_exceptioned_body_does_not_advance_progress`
(mutant "release passes True unconditionally" reds) to §10 row 4.

## MAJOR — R5-B2: the lock-park flow places enter-lease AFTER the lock
release, breaking the atomic-commit guarantee (§4.3 flow × §4.3 atomicity ×
§4.2 in-flight commit)

The flow reads "re-acquire lock → re-decide → release lock → enter lease →
yield to body", but §4.2 defines enter-lease as "(commits the in-flight
reservation)" — a mutation of gate state — and §4.3 claims correctness is
"trace-invariant under any interleaving". Interleaving that breaks it: parked
admits A and B both re-awaken. A re-decides (grants +n), releases the lock. B
re-acquires (in-flight still I — A has not committed), grants +m, commits
first (in-flight = I+m). A commits (in-flight = I+m+n) — overshoot if
I+m+n > capacity. Exactly what `test_concurrent_admit_release_never_exceeds_capacity`
is designed to catch.

**Minimal fix (mandated):** reorder to "re-acquire lock → re-decide → **enter
lease (commit in-flight under the lock)** → release lock → yield to body".
The release-may-interleave-before-reacquire note stays; the enter-lease commit
must be atomic with the decision. State commit-under-lock explicitly.

## MINOR — R5-B3: `work_completed` honesty contract asserted but never scoped
(§4.2/§4.4)

`work_completed=True` is the default and the gate cannot distinguish completed
from uncompleted work; "or relies on the default only when it actually
completed" is unenforceable prose (the round-4 write-up itself conceded
"release() is one opaque call"). Acceptable engineering — `work_completed` is
the caller's self-report; the stall detector detects honest-engine stalls — but
that position is not stated, foreclosing a future "tighten the flag"
reviewer revolt. One clause in §4.4: "`work_completed` is a caller self-report:
the gate can neither verify the body's work nor distinguish an unworked
`work_completed=True`. The progress law assumes good-faith callers and detects
honest-engine stalls; a caller that lies True can suppress
`admission_stalled` (out-of-scope, mirroring v1's dishonest `record_progress`)."

## MINOR — R5-B4: "remaining stale_bound" provenance (rule 1)

"Remaining" is only non-ambiguous if measured from staleness onset
(`stale_bound − (now − staleness_onset)`), shrinking to zero so a repeatedly
re-staled park terminates. If re-measured fresh per re-park, a sensor that
flakes stale→healthy→stale could park the admit indefinitely. The text uses
"remaining" consistently (safe reading) but never states the provenance.
One parenthetical in rule 1: measured from when the resource entered stale,
**never reset on a re-park** — repeated re-STALE parks converge to zero and
terminate.

## Cosmetic

§10 row 8 lists `test_early_return_release_without_work_does_not_advance_progress`
twice in the gate column (also echoed in §4.4 prose) — collapse to one named
probe plus the new exceptioned-body probe so collected-ID reconciliation
cannot double-count.

## Gate-readiness

All four are unstated runtime/contract laws (the two MAJORs), not type
answers; each has a one-clause minimal fix and each is caught by an
already-named acceptance probe if the writer implements the naive reading. The
reviewer's position: as written the spec is not ready, but it is four clauses
+ one probe from ready. The next revision (v2.5) plus one verification round
should reach implementation-brief status. Rows 4/5/8 will carry the B-1/B-2
probes (exceptioned-body no-progress, parked-admit-wakes, concurrent
never-exceeds).