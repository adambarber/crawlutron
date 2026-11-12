# Combined adversarial review — `admission` plan v1

Synthesis of five independent reviews of `docs/design/admission-library-plan.md`.
Each review was grounded in the live tree (main 1772d630) by its author. This
document is the reconcile agent's input: every finding that survived into the
hardened v2 specification must resolve it explicitly, with a ruling.

## Verdict (all five)

The doctrine is right — one decision point for pacing, the floor internalized so
no downstream `batch_size` can cancel it, closed-sum `Granted | Idle`, Clock/
Sleeper injection killing the `asyncio.sleep` monkeypatch, `CallerQuota` and the
webhook semaphore kept as separate non-kernel objects. **Block the migration
until the runtime state machine — not the type surface — is specified.** Type
tightening is real but does not prevent over-admission, deadlock, or lost
progress; the unstated runtime behavior (ramp, release, wakeup, floor
precedence) is the risk.

## P0 — unspecified runtime laws (the kernel is these; pin before migration)

1. **Ramp ownership and growth rule.** The gate must own the ramp privately.
   Caller-owned ramp (or any public reset) recreates the caller-overwrite bug the
   design exists to kill. Spec: initial ramp; growth trigger/amount/cadence
   (`ramp*1.10` when `scale>=1.0` today); decay formula with explicitly separated
   upper and lower bounds — `max(global_concurrency, ramp*scale)` conflates a
   maximum with a floor and needs a named clamp; updates per admission/release/
   sensing; behavior after stalls and idle periods. Expose a read-only snapshot
   and a documented quiescent reset only. **A call site exists today
   (`CrawlState.ramp`, recursive.py) — the spec must name its fate.**

2. **Floor vs hard capacity precedence.** "Never zero for a wanted request" can
   conflict with a hard memory ceiling. Which wins? If the floor wins, a single
   in-flight item is accepted as an OOM risk and must be said so. If capacity
   wins, the invariant needs an escape hatch. When even one unit cannot fit and
   nothing is in flight, surface a **terminal oversize/configuration condition**,
   not a retry-forever.

3. **Lease and release accounting.** Every `Granted` must correspond to a lease
   that records amount and owning gate, releases exactly once idempotently
   (scoped usage; release on cancellation/exception), updates in-flight
   accounting, and signals potential admission progress. Reject releases to the
   wrong gate. **No TTL/expiry in v1** unless fencing is defined (expiring a
   lease whose work continues causes double accounting).

4. **Concurrency contract.** `admit` is synchronous, nonblocking, and atomic
   with respect to other admissions and releases; safe for concurrent callers
   with no external code or I/O under the state lock. Waiting (retry) is defined
   separately: after a release, a relevant sensing update, or a bounded
   monotonic timeout, with a versioned signal so an event between decision and
   wait is not lost. Document `Idle` reasons — retryable pressure vs terminal
   incapacity — so callers can distinguish them. (Today `WeightedAdmission` is
   stateless; the new gate is not — pin the model.)

5. **The scale law.** "Min over every resource" and "ride CPU while headroom >
   threshold" are labels, not laws. Define the Ratio→Scale function, define
   `headroom` (`1-usage` vs `(hard-usage)/(hard-target)` — two different
   formulas exist today), pin monotonicity invariants (scale non-increasing in
   usage; allocation non-decreasing in scale) as gate-able properties.

6. **Per-lane split.** A total-only floor with cost asymmetries
   (GENERAL 48MiB vs HEAVY 700MiB) can systematically starve the HEAVY lane;
   today `_dispatch_paced` explicitly takes `browser_jobs[:1]`. Either make the
   floor per-lane or add per-lane minimums, or the design reintroduces the
   starvation the old code guarded against. Also state the floor-vs-`want` clamp:
   floor is `min(want.total, floor)`, stated as an invariant with correct
   arithmetic.

7. **Capacity equation and cost units.** Nothing states
   `capacity_lane = min(scale_slots, ceiling, want_lane, free/cost)` or whether
   `Budget.free` recomputes per admit. `Cost.units: int` is unitless while the
   example is bytes — type it as `Cost(memory_bytes: Bytes)` or state the unit.

8. **Yield ownership.** Does `admit` sleep internally (gate-side, via Sleeper)
   before returning the floor grant, or return `paced=True` for the caller to
   sleep? Pick gate-side; move `paced` into Progress/telemetry rather than a
   door bool. (One wait site.)

9. **Gauge failure semantics.** Repo doctrine is fail-closed. A raising gauge:
   treat that resource as hard-saturated (floor grant) or propagate? Pin it.

10. **Cold-gauge posture.** Today is fail-open for scale (`http_scale_factor`
    returns full scale when cold) — and the review marked this "too confidently"
    absent a coupling to the initial ramp: fail-open at startup admits at full
    scale before the first memory sample, which is only safe if the initial ramp
    is small. Define explicit gauge states (healthy / unhealthy / cold / stale);
    cold permits only a bounded probing allowance and prevents ramp growth;
    prolonged memory staleness stops new admission. State which resources may
    have different cold policies.

## P1 — the spec contradicts its own six invariants

1. `floor_fraction: int` is a raw number crossing the door; `0` → ZeroDivision;
   negatives silently floor to 1. Make it a validated value type.
2. `ramp_multiplier: Count` is dimensionally wrong (a multiplier is not a work
   count); `Count × Count` is nonsense. Retype as a validated `Multiplier(≥1)`
   or delete it; state whether batch ≤ `ramp_max` or ≤ `ramp_max × multiplier`.
3. "Construction enforces exactly the enum's keys" is true only for `Want`.
   `Sensing` (gauges/limits) and `Allocation` claim completeness with no
   `__post_init__`; `Budget.reserved` completeness is unstated. Add the checks
   or the invariant is prose.
4. `Limits` has no `target <= hard` validation — a swapped pair constructs.
5. `LaneSpec.ride_threshold` "only when RIDE_CPU_WHEN_FREE" is a comment, not a
   check; a MIN_ALL lane can carry a stray threshold, a RIDE lane can carry
   None. Use a sum type (`MinAll | RideCPUWhenFree(threshold)`) or refuse the
   inconsistent combination.
6. `Granted` with zero total is still constructible — the floor lives in
   `admit`'s body, a tested invariant, not a type one. Honest fix:
   `Granted.__post_init__` requires `allocation.total >= 1` (plus `Allocation`
   completeness); the floor formula is a property test under mutation coverage.
7. `StallGuard(clock=None)` recreates the two-clock bug it kills — today's
   guard defaults to its own `time.monotonic`. Drop the param; read the gate's
   clock.
8. "Duration is the only time unit crossing the door" vs `Clock.now() -> float`
   and `Progress.last_advance: float`. Add an `Instant`/`Stamp` value type;
   monotonic by contract, never wall-clock for stall detection.
9. `ResourceGauge.is_warm()` is declared and never consumed. Spec cold-gauge
   behavior (P0-10) or delete it.

## Contested rulings the reconcile agent must make

- **Ratio/Scale collapse.** Review 1 says collapse (both are [0,1] fractions;
   a swapped value is still a valid fraction). Reviews 2/4 push back: Ratio is a
   threshold and Scale is an output; swapping them is a semantic bug even in the
   same range — collapse only if no operation or invariant differs, and state
   that frozen dataclasses enforce nothing at the call site without a static
   checker (decide: mypy/pyright contract vs runtime contract).
- **Budget scope.** All agree the mapping-sum-of-bytes across non-byte
   resources is a category error; all agree it goes unused in the example.
   Drop from v1, or narrow to a memory-only single dimension.
- **`Want` constructor.** `Want.at_least` (review 1) is a misleading name —
   it zero-fills, not lower-bounds (reviews 2/4). Use `Want.of`; require all
   lanes; reject empty `Want` (do not accept `Want({})`).
- **`Granted.scale` contract.** All agree it is informational for observability
   only; the caller does not apply it. Release uses lease identity/accounting,
   never a caller-supplied scale.
- **Monotonic time.** All agree `Progress.last_advance` and retry deadlines are
   monotonic; wall-clock timestamps may remain informational.

## Migration gaps the plan must close

1. **`tests/_admission_doubles.py` migration is its own unit**, with an
   explicit call-site inventory (grep the tree, name the count in the red:
   every caller of `pinned_resource_gate` migrates in the same commit;
   `PinnedGate` becomes a `FakeResourceGauge`; the doubles exercise the same
   lease and wakeup contract as the real gate). Today `PinnedGate` subclasses
   the production `ResourceGate` and skips `__init__` (verified
   tests/_admission_doubles.py:21-52); under Protocols the doubles become pure
   fakes.
2. **`wait_while_saturated` has no named fate.** Presumably absorbed by
   admit's yield+floor — say so, or hard-ceiling park semantics change
   silently.
3. **The derivation layer** (`build_admission_policy`, `max_http_concurrency_for`,
   `TaskRamBudget`) needs a home — presumably seam-side, producing
   `LaneSpec.ceiling`/`Cost`.
4. **Loop-lag semantics change silently**: today lag is multiplicative
   (`batch_scale_factor = min(mem, cpu) × lag.factor()`); the closed
   `{MEMORY, CPU, LOOP_LAG}` with per-resource Limits implies min-all over
   three ratios — a dispatch-curve delta. Record the behavior delta or give
   LOOP_LAG a multiplicative role.
5. **Line-number drift**: review 5 (tree-grounded) found the plan's cited line
   numbers drifted ~20 lines vs the 11-11 snapshot. Re-pin the drift table from
   the live tree before the deletion commits — string-drift doctrine.
6. **Import-contract and ratchet work**: new top-level package means extending
   the file-size ratchet `_BUDGETS` with an `admission/` root; seal "zero
   crawlutron imports" as an import-contract gate, not a comment; add a
   source-scan gate for `time.monotonic`/`asyncio.sleep` in `admission/`; add a
   mutation gate on the floor law; add the seam-parity test (old knob vs new
   kernel, same decision) before deleting `pressure_sizing` sites.
7. **Release sequencing**: cut 0.4.0rc0 first (tree is ready; CHANGELOG empty),
   then the library, then the seam migration, then the transport-pooling spec.

## Acceptance-test program (from report 4, kept whole)

1. Ramp initialization, growth, decay, bounds, caller isolation.
2. Saturation where the floor and hard capacity conflict.
3. Permanent incapacity with zero work in flight.
4. Release on success, exception, and cancellation; duplicate release harmless.
5. Concurrent admissions and releases without exceeding the applicable limit.
6. Cold and stale sensing, including no ramp growth on missing evidence.
7. Wakeup on release and sensing changes, including an event arriving
   immediately before waiting.
8. Stall detection across large wall-clock adjustments.
9. Every production caller and test double identified by the migration
   inventory.

Implementation order: state machine and precedence → remove external ramp
mutation and add lease accounting → bounded cold-start and monotonic timing →
tighten the type surface → migrate call sites and test doubles → deterministic
acceptance suite before enabling the new gate.

## Pending spec items (report 3's additions, all accepted)

- Specify the `Idle` vs `Granted` decision rule explicitly (the invariant
  implies `Idle` *can* return for `want.total>0`; under what condition?).
- PEP 695 `type Admission = Granted | Idle` is annotation-only at runtime:
  `isinstance(x, Admission)` and `case Admission():` raise TypeError; match on
  `Granted()` / `Idle()` directly.
- Acknowledge that `__post_init__` guards make illegal states *fail at
  construction* (runtime), not *unrepresentable* (type-level); sum-type fixes
  are genuinely structural; runtime validation is the Python-appropriate
  contract and must be stated as such, with what the type system catches vs
  what tests must cover.