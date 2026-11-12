# Adversarial review round 1 — `admission` v2 spec

**Verdict: defects-found (do not implement as written).** Reviewed by
ses_e31753978ffevMvv4cGTQDGGHO against `docs/design/admission-library-v2.md`
and the live tree at main. Doctrinal core sound; floor law arithmetically
broken in three places; seam-parity requirement self-contradictory.

## BLOCKERS

### B1. `floor(want) = max(1, ramp // floor_fraction)` magnifies instead of
fractioning — the `Fraction` retype inverted the arithmetic
`Fraction` validates `0 < value <= 1` (§3 table, "retype of floor_fraction"),
but the floor law divides by it: `ramp // 0.25 ≈ 4 × ramp`. The legacy
`floor_fraction` was an int denominator (`saturated_dispatch_floor =
max(1, ramp // 4)`); the retype flipped the domain from *denominator* to
*multiplier* while the formula still divides.
Fix: state the intended meaning ONCE. Denominator (≥1, formula `ramp //
floor_fraction`) or fraction (∈(0,1], formula `ramp * floor_fraction`).
Reconcile the type table with the floor law.

### B2. `floor_lane = min(want_lane, floor_global // lane_spread)` can zero
every wanted lane
`lane_spread` is never defined. Cold start with `initial_ramp = 1`,
`floor_global = 1`, mixed want (spread=2): `1 // 2 = 0` per lane →
`granted_total = 0` on the very first admit of a cold, healthy box.
Fix: define `lane_spread` (number of wanted lanes, ≥1) and lower-bound the
per-lane floor: `floor_lane = max(1, min(want_lane, floor_global //
lane_spread))`, mirroring the global `max(1, …)`.

### B3. HEAVY-only wants return zero — OVERSIZE predicate scoped to EVERY lane,
not the WANTED lanes
OVERSIZE fires only when `free < cost` for **every** lane. HEAVY-only want with
`free = 600 MiB` (above GENERAL 48 MiB, below HEAVY 700 MiB): OVERSIZE is false
(GENERAL fits), yet `granted_heavy = min(…, 600//700 = 0) = 0` →
`granted_total = 0`, violating the headline invariant, in a permanent strand
that is neither OVERSIZE nor reachable-retry. Real reachable shape:
`_bucket_tasks_by_tier` produces `browser_jobs` with empty `http_jobs`.
Fix: OVERSIZE evaluates the **wanted** lanes only ("no wanted lane can fit and
nothing is in flight → Idle(OVERSIZE)"). State what a zero-want lane means for
the floor spread (binds B2's `lane_spread`).

## MAJOR

### M1. Seam-parity test over-constrained to self-contradiction
§9.6 demands the old knob and new kernel produce "the same decision" with
"byte-identical caller semantics" — but the central design change is old
`(0,0)` vs new floor ≥1 under saturation, and caller-park → gate-yield. The
decisions are *designed* to differ there.
Fix: scope seam-parity to the **unsaturated** band (usage ≤ target, full
capacity both sides) and to lane-fit determination; declare the
saturated-regime delta (`0 → floor ≥ 1`, caller-park → gate-yield) as the
*intended* behavioral change. State which outputs are byte-identical (granted
`Allocation` per lane) and which may differ.

### M2. Abandoned-`Granted` lease leak strands the gate (and blocks OVERSIZE)
In-flight increments at admit (§4.3); releases only inside `with
granted.lease():` (§4.2). A caller that accepts a `Granted` but never enters
the `with` (exception between admit and with, dropped object, suppressed path)
leaks that lane's in-flight forever. No TTL/fencing (explicitly rejected) →
"nothing in flight" can never become true → OVERSIZE terminal can never fire →
system strands at retryable Idle(RETRY_PRESSURE) for the wrong reason.
Fix options: increment at the lease's `with` entry (admit reserves, with
commits); OR make admission and `with granted.lease()` inseparable by
construction; OR define the abandoned-Granted disposition.

### M3. `Cost.weight` declared, never consumed — dead member under the doc's
own doctrine (contrast P1-9 `is_warm`)
Capacity equation (§5.2) consumes only `Cost.memory_bytes`; LOOP_LAG acts
multiplicatively on scale, not on a cost weight. Drop `weight` or name its one
consumer.

### M4. Single wait site conflates a duration sleeper with an event wakeup;
the time-seam source-scan can't coexist with an in-package Sleeper
§4.3 needs event wakeup (release/sensing version bump → re-decide early);
§8 names the seam a `Sleeper` (duration); a duration-sleeper cannot wake on a
release. And the source-scan gate forbids `asyncio.sleep`/`time.monotonic`
and aliases in `packages/core/admission/**` — if a concrete event/sleep primitive is
implemented *in* the package, the gate reds its own seam.
Fix: define the wait as a single event-with-optional-timeout primitive whose
concrete implementation lives OUTSIDE `packages/core/admission/` (injected from the seam);
state the source-scan gate is satisfiable only under that reading, keyed on
`packages/core/admission/**` only (crawlutron imports out of scope).

### M5. `Progress(last_advance >= last_decision)` misfires on decisions that do
not advance (Idle)
An Idle return is a decision; natural implementation sets
`last_decision = clock.now()` on every admit. Advance at t0, Idle at t1 > t0 →
`last_advance = t0 < last_decision = t1` violates the invariant.
Fix: pin whether `last_decision` updates on Idle (if `last_decision` means
"last granting decision", the ordering law holds — say so) or drop/invert the
ordering law. StallGuard keys on `last_advance` only.

## MINOR

- m1. `PinnedGate` constructions: **13, not 14** — the spec's own list has
  exactly 13 (`test_admission_policy.py:154,160,164,170,179,185,194,211,216,
  221,225` = 11 + `test_admission_scaling.py:193,216` = 2). The 4
  `pinned_resource_gate` sites are correct; `ContentionFreeGate` correct
  (`_recursive_harnesses.py:37-59`).
- m2. `pressure_sizing.py:192-194` ceiling cite imprecise: GENERAL ceiling is
  `:186-193`, HEAVY at `:194`. Widen to `:186-194`.
- m3. `lane_spread` undefined (drives B2) — must be defined (recommended:
  nonzero wanted lanes).
- m4. Versioned wakeup spill-over across lanes unspecified: single global
  version wakes every Idle waiter regardless of lane (spurious-but-correct).
  State global-version spurious wakes acceptable, or mandate per-lane wakeup.
- m5. Ramp cadence asymmetry biases toward floor: growth once per admission;
  decay once per sensing update AND per release. Sustained sub-1.0 parks the
  ramp at `ramp_floor`. State that this is intended.
- m6. `cold_probe` semantics ambiguous: as an in-flight cap it bounds cold
  concurrency (each release reopens the probe), not cumulative admits; global
  vs per-lane unspecified. State the bound is concurrent cold in-flight and
  pick global vs per-lane.

## Migration-claim verification

All claims verified correct against the live tree EXCEPT:
- `PinnedGate` constructions: 13 (not 14).
- Ceiling cite: widen to `pressure_sizing.py:186-194`.

Verified correct: 4 `pinned_resource_gate` sites; `ContentionFreeGate` present;
`recursive.py:215/222` on HEAD; no production caller of `wait_while_saturated`
(callers only in tests/); `_BUDGETS`/`SCAN_ROOTS` coupling + jscpd
`TREE_ROOTS` cross-check; cost defaults `:24,29` = 48/700 MiB; reservations
`:158,163`; initial ramp `recursive.py:407`; `CrawlState.ramp` at
`recursive.py:275`; `(0,0)`-yield `pressure_sizing.py:245-246`; loop-lag
`pressure_gates.py:199` + `pressure_probes.py:97-103`; `admission_stall.py`
(module is `packages/core/crawlutron/orchestration/admission_stall.py`) :25,49,78-79,125;
`caller_auth.py:149-154`. Nothing in the v2 "Corrections to the combined
review" section is itself wrong.

## Gate-writability

- Import-contract + time-seam source-scan: realizable as Bites/Rule/Registry
  with `scope=` walking `packages/core/admission/**` — contingent on M4 (concrete
  clock/sleeper/event injected from outside; gate must scan the top-level
  `packages/core/admission/` only, not `packages/core/crawlutron/admission/`).
- Floor mutation gate: the standing enforcement must be a **property test
  under mutation coverage** (an AST gate cannot judge the data-dependent
  "floor never zero" invariant). Clarify the acceptance table's phrasing.
- Seam-parity gate: blocked by M1 until scoped.
- `python_bearing_roots` auto-entry: correct; any future python-bearing tree
  needs a budget row in the same commit (existing governance).