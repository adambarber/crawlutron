# `admission` — hardened v2.11 specification

Reconciles the v1 admission-library plan with the combined adversarial review
Every P0, P1, contested ruling, and migration gap is resolved decisively.
Grounded in the live tree at `main`. This document is the implementation
brief: an implementer writes the library from here without further design
decisions.

**v2.1 (fix round, adversarial review round 1):** all findings accepted —
the floor law's arithmetic corrected (`floor_fraction` is an integer
denominator `>= 1`; per-lane floor is `max(1, min(want_lane, floor_global //
lane_spread))`; OVERSIZE evaluates wanted lanes only), admission and
lease-taking made inseparable by construction (`async with gate.admit(want) as
granted:`), `Cost.weight` dropped, the wait seam reframed as an injected
`Signal` event-with-optional-timeout (source-scan gate keyed on top-level
`packages/core/src/admission/**` only), seam-parity scoped to the unsaturated band with the
saturated deltas declared intended, `Progress.last_decision` pinned as
granting-only, `PinnedGate` inventory corrected to 13, cold_probe pinned as a
global concurrent in-flight cap. Full rulings table: the v2.1 fix round.

**v2.2 (fix round, adversarial review round 2):** the door is **Model A** —
`async with gate.admit(want)` always yields a live `Granted` (parking
internally in `__aenter__`); terminal outcomes raise a typed
`AdmissionRefused(reason: IdleReason)` from `__aenter__`, and
`Admission = Granted | Idle` is repealed as the door type. `Progress` advances
`last_advance` on grant so the ordering law holds by construction; "can fit"
folds the ceiling into the OVERSIZE predicate; ramp growth gets a hysteresis
window; nested admits are prohibited, the lease over-counts unused grant, and
the state lock is released before the `with` body yields; `Idle` precedence is
STALE-first → OVERSIZE → RETRY_PRESSURE; the source-scan gate scope descriptor
is pinned to `trees_under(REPO_ROOT / "admission")`; cold-probe
lane-starvation is accepted. Full rulings table: the v2.2 fix round.

**v2.3 (fix round, adversarial review round 3):** the OVERSIZE trace is
flipped — a HEAVY-only want at `free = 600 MiB` **raises
`AdmissionRefused(IdleReason.OVERSIZE)`** (the only wanted lane cannot fit),
and every stale Model-B `Idle(...)` value phrasing is swept to the Model-A
`AdmissionRefused(reason)`/internal-park surface. The lease lifecycle
distinguishes pre-lease (park) cancellation from body cancellation (only body
cancellation releases; release no-ops on `self._lease is None`). `Granted`
gains `lease: Lease`; caller-callable `release()` is a supported early-return
(defaulting `__aexit__` to no-op). Releases count as progress (`last_advance`
updates on release), so a draining-but-alive saturated box no longer trips
`admission_stalled`. Waiter fairness (temporary unfairness among parked
admits) is accepted in v1; STALE-park is time-bounded by the remaining
`stale_bound`; nested-admit detection is pinned to a `contextvar`. Full
rulings table: `docs/reviews/admission-library-review-round3.md`.

**v2.4 (fix round, adversarial review round 4):** the release signal carries a
work-completion flag — `release(*, work_completed: bool = True)` — so only a
**completed** release (or a grant) advances `last_advance`; an uncompleted
early-return leaves progress frozen so the pinned 60s `admission_stalled` stop
still fires (B-1). The `threading.Lock` is **released before
`_wait_for_progress()` parks** and re-acquired after wake to re-decide, closing
the lock-vs-park deadlock (B-2). The STALE door's caller horizon is documented
(block up to `stale_bound`); the abnormal-`__aenter__` path runs the
generator's `finally` (release no-ops on `lease is None`); `Lease` gains a row
in the complete type surface; the residual "`with granted.lease()`" echo is
dropped. Full rulings table:
`docs/reviews/admission-library-review-round4.md`.

**v2.5 (fix round, adversarial review round 5):** the exceptioned-body exit
closes the round-4 blind hole reopened via the exception door — the
generator `finally` passes **`work_completed=False` on exceptioned body exit
and on body cancellation** (a raised body has not completed its grant; it
must not advance `last_advance`), using `sys.exc_info()[0]` to distinguish
normal vs exceptioned exit; only a normal exit or an explicit caller
`release(work_completed=True)` advances progress (R5-B1). The lock-park flow
is corrected to commit in-flight **under the lock** — enter-lease moves inside
the re-acquired lock so a grant is atomic with its reservation, closing the
interleaving overshoot between two re-awakened admits (R5-B2). The
`work_completed` contract is scoped to a **good-faith-caller** self-report
(the gate cannot verify the body's work; a caller that lies `True` can
suppress `admission_stalled`, out-of-scope) (R5-B3). The STALE-park timeout's
provenance is pinned: remaining `stale_bound` is measured **from staleness
onset and never reset per re-park**, so repeated re-STALE parks converge to
zero and terminate (R5-B4). The duplicate acceptance probe is collapsed so
collected-ID reconciliation cannot double-count. Rounds 1-4 resolutions stay
intact.

**v2.6 (final fix round, adversarial review round 6):** the crash-loop closure
claim is retracted (R6-B1, parent Option B). The kernel's StallGuard detects
**admission-gate liveness** (grants, completed releases, transactions), **not
engine output**: grant-advance (round-2 MAJOR-1) already mirrors production
doctrine (the legacy `allowed_jobs or page_count > page_count_before` counts
admits as progress), so a re-granting crash-loop keeps advancing `last_advance`
and the kernel **cannot and does not** promise to trip `admission_stalled` on a
caller's work-failure loop. The kernel's contract is: never `(0,0)` when work
is wanted, bounded parks, no capacity overshoot, gate-level liveness
StallGuarded. Engine crash-loop detection is delegated to the crawl-side
output-progress signal (`page_count` growth), where output is measurable. The
`work_completed` flag remains as honest-caller telemetry (prevents
release-masking; feeds the crawl-side output derivation) but is not a
kernel-side crash-loop detector. The unconditional-exceptioned-non-progress
asymmetry (R6-B3) and the every-normal-exit-is-completed re-scope (R6-B4) are
stated; `release()` is labelled synchronous (R6-M1). Historical changelog
entries (v2.1-v2.5) are kept verbatim as records of prior belief; the
crash-loop-relying claims in them (e.g. v2.4's "still fires") are superseded
by this entry and the corrected spec body.
Full rulings table: `docs/reviews/admission-library-review-round6.md`.

**v2.7 (fix round, adversarial review round 7):** the crawl-side delegation is
tightened (R7-B1). The delegated output detector is **`page_count` growth
ONLY** — the `allowed_jobs` term of the legacy `crawl_batches` record
(`admission_stall.py:129`, `if allowed_jobs or state.page_count >
page_count_before`) must **NOT** advance it, because each re-grant makes
`allowed_jobs` truthy before any output and the delegated record would inherit
exactly the kernel's grant-blindness (the crash-loop silently passes as
"progress"). The delegated record tightens to **`page_count >
page_count_before`** (§4.2), mirrored as migration unit 8 (§9) with a
seam-parity probe, and acceptance row 10 pins it
(`test_crawl_side_delegated_signal_ignores_allowed_jobs`). The normative §4.2
occurrence of the blind formula is corrected; the changelog's historical quote
(v2.6 line 88) is kept verbatim and **superseded** by this entry.
Full rulings table: `docs/reviews/admission-library-review-round7.md`.

**v2.8 (fix round, adversarial review round 8):** the crawl-side delegation is
scoped honestly (R8-B1): the delegated stop fires **only on a zero-admission
pass** — `admission_stall.py:119` consults `stall_guard.stalled()` solely when
`allowed_jobs` is falsy. It detects "no output AND no admission," NOT a
crash-loop that re-grants on every pass while producing no committed pages
(such a loop never reaches the `stalled()` check). R8-B2 corrects migration
unit 8's kernel/seam conflation: it is the **seam's** crawl-side stall detector
that consumes `(CrawlState).page_count`; the kernel keeps no caller-output
visibility (see 4.4), and the crawl-stall detector seam is not 9.6's
decision-identity seam-parity. R8-B3 states the batch-synchronous property
(the sole `page_count` increment at `recursive.py:344` runs to completion
inside `commit_batch` before the same-pass stall check) and that `page_count`
counts committed results including spilled permanent failures, not only
caller-returned pages. Historical changelog entries stay verbatim.
Full rulings table: `docs/reviews/admission-library-review-round8.md`.

**v2.9 (fix round, adversarial review round 9):** three reachability /
explicitness corrections. R9-M1: a cold no-admission pass parks at
`admission_stall.py:118` inside the admit and does not reach the `stalled()`
check; cold starvation terminates via the `stale_bound`→STALE bound, never the
60s `admission_stalled` stop. R9-M2: a box parked inside the admit
(RETRY_PRESSURE/cold) is bounded by `stale_bound`→STALE, not the 60s stop
(which fires only when the loop reaches a zero-admission pass at line 119);
post-migration that branch is fillable only by NON-admission empties (e.g. the
robots filter in `admit_recursive_batch`, `recursive.py:174`), never by the
admission gate returning 0. R9-M3: the batch-synchronous property lives once,
in 4.2, with the `admission_stall.py:128→129` anchor; migration unit 8 now
cross-references 4.2 instead of restating it. Historical changelog entries
stay verbatim. Rounds 1-8 resolutions stay intact.
Full rulings table: `docs/reviews/admission-library-review-round9.md`.

**v2.10 (fix round, adversarial review round 10):** the bound the admission
machinery cannot provide is named (R10-B1): **each granted job** on a
continuously-granting / continuously-alternating zero-output loop is bounded
by the crawl's own **per-job execution timeout** (`total_url_budget_seconds`
via `asyncio.wait_for` at `request_pipeline.py:167-187`), while the **loop**
itself is bounded only by the finite work surface (frontier drain /
`max_pages`) and whatever caller deadline the caller itself enforces — which
the kernel, and crawlutron, do not provide — so such a loop has no
admission-machinery wall-clock bound. R10-B2 qualifies the parked-stall bounding: `stale_bound`→STALE bounds a
parked admit only **provided the resource actually becomes stale**; a
healthy-sensor RETRY_PRESSURE park whose capacity is permanently held by a
never-releasing lease is **unbounded in v1** (a consequence of the rejected
TTL; bounded only by the caller's task timeout/deadline). R10-B3 adds the
one-phrase reachability pointer to 0 #4 ("fires only on a zero-admission
pass, see §4.4"). Historical changelog entries stay verbatim. Rounds 1-9
resolutions stay intact.
Full rulings table: `docs/reviews/admission-library-review-round10.md`.

**v2.11 (fix round, adversarial review round 11):** the R10-B1 sentence is
tightened to remove a false loop-scope guarantee (R11-B1). The v2.10 wording
presented the per-job execution timeout as "the ultimate bound on a
continuously-granting ... zero-output loop"; but the budget
(`total_url_budget_seconds`) is per-grant, re-armed fresh on each re-admit, so
it bounds **each granted job**, not the loop. The **loop** is bounded only by
the **finite work surface** (frontier drain / `max_pages`) and whatever caller
deadline the caller itself enforces — which the kernel, and crawlutron, do not
provide (no `wait_for`/deadline wraps `crawl_batches`; `CrawlPolicyLimits`
exposes no whole-crawl deadline) — so a zero-output loop that keeps being
granted work has no admission-machinery wall-clock bound. This strengthens the
existing R8-B1 "it cannot stop a loop that keeps being granted work." The v2.10
changelog's first sentence is re-scoped identically; historical changelog
entries stay verbatim. Rounds 1-10 resolutions stay intact.
Full rulings table: `docs/reviews/admission-library-review-round11.md`.

## 0. Scope and non-negotiables

The kernel is a **generic, typed package independent of the crawler, API
server, and crawlutron specifics** — zero `crawlutron.*` imports, enforced by
an import-contract gate (not a comment). Lives at a **top-level `packages/core/src/admission/`
package**, sibling to `packages/core/src/crawlutron/` under the UV workspace root.

**Non-negotiables (wire contracts — nothing below can be relaxed):**
1. **Wire contracts on every public door**: same decision → same observable
   output as the old knobs, byte-identical where a caller's semantics depend
   on it (seam-parity gate, §9).
2. **429 boundary**: `check_concurrency_quota` (caller-level
   `max_concurrency=4`, anonymous) stays a **`CallerQuota` outside the
   kernel** (`packages/core/src/crawlutron/service/caller_auth.py:149-154`). The kernel never
   429s.
3. **Webhook semaphore** stays an **`ExecutionWidth`** that queues and never
   429s.
4. **`admission_stalled` classification + 60s hard stop stay**
   (`admission_stall.py:25` `NO_PROGRESS_STALL_SECONDS: float = 60.0`;
   `classification=CrawlClassification.admission_stalled`,
   `admission_stall.py:78-79`); **the stop fires only on a zero-admission
   pass, see §4.4** (R10-B3).

**Naming hazard (correction to the review, see §10):** there is already a
`packages/core/src/crawlutron/admission/` package (URL/link admission — `admission.py`,
`admission_links.py`, `credentials.py`, `policy.py`). The new kernel is
`packages/core/src/admission/` at the **repo root**, a different tree. v2 mandates a one-line
notice in each package's module docstring (`packages/core/src/admission/…` =
"resource-adaptive paced-admission kernel"; `packages/core/src/crawlutron/admission/…` =
"URL admission") so the two "admission" names never collapse conceptually.

**Naming law**: no issue numbers in file/module/function names
(`test_saturation_floor_*`, never the ticket). Comments citing the legacy
defect are fine; names state the behavior.

## 1. The one semantic primitive (P0-2, P0-6, P1-6)

> **Admission returns a minimum-progress floor on saturation.**

For any non-empty `Want`, `admit` **NEVER returns zero total slots**. It
returns `max(1, floor_lane_spread)`, clamped by the resolved capacity (`min`)
and the resolved want (`min`), and it is the **sole site** in the kernel that
sleeps/yields (§8: one wait site, gate-side). Both dispatch paths consume this
one primitive; there is no downstream `batch_size` that can cancel the floor
because there is no downstream `batch_size`.

### Floor-vs-want clamp (explicit, invariant)
`granted_total = min(want.total, floor(want))` where
`floor(want) = max(1, ramp // floor_fraction)`, and **`floor_fraction` is a
positive integer `>= 1` — a *denominator*, exactly the legacy meaning**
(`saturated_dispatch_floor = max(1, ramp // 4)`, `pressure_sizing.py:249-253`).
The formula and the type table agree: the divisor never scales `ramp` up;
`ramp // floor_fraction <= ramp` always. A `floor_fraction` of `0` is a
construction error (`FloorDivisor` validates `>= 1`), killing the
`ZeroDivision` (B1). Stated as an invariant: **`1 <= granted_total <= want.total` for a wanted
lane that can fit** — the guarantee is scoped to "wanted and can fit," never
an absolute over an un-fittable lane (MAJOR-2). **"Can fit" is defined once:
`ceiling_lane >= 1 and free//cost_lane >= 1`.** The clamp caps the floor at
the want so a job asking for one item gets exactly one, never more; it never
drops below 1 while `want.total >= 1` and some wanted lane can fit.

### Floor-vs-hard-capacity precedence (P0-2, resolved)
The floor and the hard memory ceiling can conflict. The decision, decisive:

- **Hard capacity wins when the want itself cannot fit and the reservation is
  the binding constraint.** `capacity_lane = min(scale_slots, ceiling_lane,
  want_lane, free/cost)` — the ceiling is a hard upper bound. If
  `want_lane > ceiling_lane` for a lane, **no floor overrides the ceiling**; a
  floor-granted item that violates a hard ceiling is an OOM risk the design
  rejects.
- **The floor guarantees at least one slot only when one can fit.**
  `granted_lane = min(floor_lane, want_lane, ceiling_lane, free//cost_lane)`
  where the `free//cost` term is ≥1 only when the box can hold one item, and
  `floor_lane = max(1, min(want_lane, floor_global // lane_spread))` with
  `lane_spread` = number of **wanted** lanes (`want_lane >= 1`), minimum 1 —
  so a swept `floor_global = 1` with two wanted lanes still grants
  `granted_total >= 1` (B2/m3). **OVERSIZE is scoped to the wanted lanes
  and folds in the ceiling** (MAJOR-2):
  `AdmissionRefused(reason=IdleReason.OVERSIZE)` — the terminal, never-retried
  outcome — fires when **no wanted lane can fit** (`min(ceiling_lane, free //
  cost_lane) == 0` for every lane the `Want` requests) **and nothing is in
  flight**. "Can fit" = `ceiling_lane >= 1 and free//cost_lane >= 1` (defined
  once above). A zero-want lane never satisfies the predicate — but the
  **wanted** lanes decide it. A HEAVY-only `Want` (`_bucket_tasks_by_tier`
  yields `browser_jobs` with empty `http_jobs`, `request_pipeline.py:316-322`)
  at `free = 600 MiB` (HEAVY cost 700 MiB, GENERAL **not wanted**) **raises
  `AdmissionRefused(IdleReason.OVERSIZE)`**: GENERAL being un-wanted does not
  "fit" the request, and the one wanted lane, HEAVY, has `min(ceiling_heavy,
  600 // 700) = 0` (BLOCKER). The HEAVY minimum (`granted_heavy >= 1`) holds
  **only when one HEAVY unit can be serviced** (`free // cost_heavy >= 1`);
  when it cannot and nothing is in flight, OVERSIZE raises — never a silent
  `granted_heavy = 0`. If GENERAL were also wanted (mixed want), the HEAVY-only
  `free = 600` example would *not* OVERSIZE (GENERAL fits; progress via
  GENERAL) — the predicate is over the wanted set, so this is the correct
  contrast (B3). A wanted lane with `ceiling_lane == 0` and `free//cost >= 1`
  lands in OVERSIZE (or `STALE`-guarded) — it can no longer fall through to a
  zero `granted_total`. This surfaces P0-2's
  "terminal oversize/configuration condition."
- **When one can fit but the rest can't (want ≫ capacity), the floor still
  holds** at exactly 1 (or `floor` if it fits) — progress, not starvation.

> Ruling: **capacity wins over floor**; the floor is a *minimum-progress* law
> bounded above by capacity and want. The terminal `OVERSIZE` `IdleReason` (not
> retryable; raised as `AdmissionRefused`) is the escape hatch P0-2 demands when even one unit cannot fit
> with zero in flight.

## 2. Closed vocabularies

- `ResourceId` — `MEMORY`, `CPU`, `LOOP_LAG` (closed `StrEnum`). **LOOP_LAG
  keeps a multiplicative role** (§10, M4 correction): it is a throttle
  *multiplier* on the min-all result (`pressure_gates.py:199`
  `base * self._lag_probe.factor()`; `pressure_probes.py:97-103` `factor()`),
  **not** a third member of the min-all LIMITS array. `Limits` and `Sensing`
  are complete over `{MEMORY, CPU}` only; LOOP_LAG is modeled as
  `loop_lag_multiplier: Ratio` (a ratio in `[0,1]`, validated), read from the
  gate's lag probe, applied multiplicatively after min-all. This records the
  loop-lag dispatch-curve delta without changing its semantics.
- `LaneId` — `GENERAL` (HTTP), `HEAVY` (browser). Closed `StrEnum`.
  Crawlutron maps HTTP→GENERAL, BROWSER→HEAVY *at the seam only*
  (`_bucket_tasks_by_tier`, `request_pipeline.py:307-323`), never inside the
  kernel.

Both enums export `members()` / `from_value()` doors for the
complete-over-enum construction checks.

## 3. Value objects — the complete type surface (P1-1..P1-9)

All frozen (`@dataclass(frozen=True)` or slotted class), validated in
`__post_init__`, refusal-shaped literals at the boundary, `__all__` explicit —
the `NormalizedUrl` bar (`packages/core/src/crawlutron/models/normalized_url.py:161-320`):
construction-IS-admission, one chokepoint, refusal-shaped spelling via a
`refusal_literals()` door, foreign errors mapped explicitly.

| Type | Field(s) | Validated invariant |
|---|---|---|
| `Ratio` | `value: float` | `0 <= value <= 1`; refuses `nan`/`inf`. |
| `Scale` | `value: float` | `0 <= value <= 1`; refuses `nan`/`inf`. (kept distinct from `Ratio`, see §6.1) |
| `Multiplier` | `value: float` | `value >= 1.0` (the retype of `ramp_multiplier`). |
| `FloorDivisor` | `value: int` | `value >= 1` — the retype of `floor_fraction`, a **positive integer denominator**; **`0` is a construction error** (kills the `ZeroDivision`), and interval validation is replaced by integer `>= 1` (this is the semantics `ramp // floor_fraction` needs; a `0 < v <= 1` float would divide up, inverting the floor law — B1). |
| `Duration` | `value: float` | `value >= 0`. |
| `Bytes` | `value: int` | `value >= 0`. |
| `Cost` | `memory_bytes: Bytes` | `memory_bytes` non-negative (`Bytes`). **Single field, explicitly priced in bytes.** (P0-7). `weight` is dropped (M3): no consumer exists — the capacity equation consumes only `memory_bytes`; LOOP_LAG acts multiplicatively on *scale*, not on a per-unit cost weight. |
| `Count` | `value: int` | `value >= 0` for general counts. |
| `Instant` | `value: float` | Monotonic copy of the injected clock's last read; **not** a wall time; hidden constructor (`Instant._of(now: float)`); always produced by `Clock.now()`. (P1-8) |
| `Progress` | `last_advance: Instant`, `last_decision: Instant` | Both monotonic `Instant`s; `__post_init__` enforces `last_advance >= last_decision`. **`last_decision` is the timestamp of the last *granting* decision** — set only when `admit` returns a `Granted`, never on an `Idle`. This keeps the ordering law true under every admit outcome (an Idle is a decision that does not advance; updating `last_decision` on Idle would overtake `last_advance` and violate the invariant — M5). `StallGuard` keys on `last_advance` only. A granting decision also records `last_advance` (a `Granted` is progress — §4.4), so the ordering law holds by construction. |
| `Lease` | `lane: LaneId`, `amount: Count`, `gate: AdmissionGate` | Created only by a live `__aenter__` grant; records owner-gate identity; exposes **`release(*, work_completed: bool = True) -> bool`** (True on the first release; idempotent thereafter — **`release()` is synchronous: it performs no I/O and cannot be interrupted mid-bookkeeping, R6-M1**); rejects a release to a **wrong gate** (`WrongGateError`); `work_completed=False` marks an early-return that did not finish its work (progress frozen, B-1); **no TTL/expiry in v1** (fencing undefined — rejected explicitly). |

**Sum types** (structural, PEP 604 in type position, concrete classes at
runtime):
- `ScaleSource = MinAll | RideCpuWhenFree(threshold: Ratio)` — closes P1-5.
  `RideCpuWhenFree` carries the threshold; `MinAll` carries none. A `LaneSpec`
  takes a **`ScaleSource`**, so a MIN_ALL lane cannot carry a threshold and a
  RIDE lane cannot carry `None` — the inconsistent combination is
  unrepresentable, not merely refused.

### `__post_init__` completeness and validation (P1-3)
Every value object validates in `__post_init__`:
- `Ratio`, `Scale`: interval + finiteness.
- `FloorDivisor`: integer `>= 1`.
- `Multiplier`: ≥1.
- `Cost`: `Bytes` non-negative.
- `Limits(target: Ratio, hard: Ratio)`: **`target <= hard` enforced in
  `__post_init__`** — a swapped pair refuses at construction (P1-4).
- `Sensing(sensors)`: **complete over `ResourceId` MEMORY/CPU** —
  `__post_init__` asserts `set(sensors.keys()) == {MEMORY, CPU}` (one gauge +
  one limit per resource, enforced). (P1-3)
- `Allocation(lanes)`: **complete over `LaneId`** — `__post_init__` asserts
  `set(lanes.keys()) == {GENERAL, HEAVY}`. (P1-3)
- `Granted(allocation: Allocation, scale: Scale, paced: bool, lease: Lease)`:
  **`__post_init__` asserts `allocation.total >= 1`** — a zero-total `Granted`
  is unconstructible (P1-6) — **and requires a non-None `lease`** so an
  ungrantable "Granted" cannot exist. The body's `granted` **carries the lease
  handle** as `granted.lease` (MAJOR-4); the door never yields a `Granted`
  without a live lease. `paced` is telemetry here; the wait is gate-side
  (§8).
- `Budget`: narrowed to **memory-only single dimension** (§6.2).
- `Want(lanes)`: complete over `LaneId` and **rejects empty total**
  (`__post_init__` asserts `total >= 1`).

> Where the type system catches vs what tests must cover (P1-3/Pending):
> `__post_init__` guards make illegal states *fail at construction* (runtime),
> not *unrepresentable* (type-level). Sum types (`ScaleSource`) are genuinely
> structural; `Admission = Granted | Idle` is **repealed** as a door-type (a
> granted lease or an `AdmissionRefused` raise is the surface). This is the
> Python-appropriate contract; v2 states it plainly: **runtime construction
> guards are the law; property tests under mutation coverage pin the
> invariants they enforce** (see §9). No magic comments; a checker complaint
> that cannot be resolved structurally is a design error, not an inline
> `# type: ignore`.

## 4. The runtime state machine (P0-1, P0-3, P0-4, P0-8)

### 4.1 Ramp ownership and growth/decay (P0-1, resolved)
The gate owns the ramp privately. **No public reset; no caller mutation.**
`CrawlState.ramp` is removed from the crawl state (`recursive.py:275`) and
internalized into `AdmissionGate`; the caller-overwrite bug this design exists
to kill (`_resource_admitted_batch` growth/decay at `recursive.py:194-197`) is
structurally impossible because the caller holds no ramp reference.

- **Initial ramp**: constructed from `global_concurrency` (the v1 default;
  today `_start_recursive_state` sets `ramp=float(run.global_concurrency)` at
  `recursive.py:407`) and coupled with the cold posture (§7): a cold start gets
  a **small explicit `initial_ramp: Count`** (default
  `max(1, global_concurrency // 4)`), so fail-open-at-startup admits are
  bounded.
- **Growth trigger/amount/cadence**: growth applies only after `scale` is at
  full over a window — `scale >= 1 - ε` across `ramp_growth_window: Count`
  consecutive admission decisions — else **decay applies**; a box hovering at
  target must not thrash-grow/decay on alternating decisions (MINOR-1).
  Growth is `ramp = min(ramp_max, ramp * ramp_multiplier)` with
  `ramp_multiplier: Multiplier` (default `1.10`, the legacy `*1.10`), bounded
  `[ramp_floor, ramp_max]`. Cadence: the growth arm fires **once per admission
  decision** (after the window). `ε` and `ramp_growth_window` are
  `PacingPolicy` fields (defaults `ε = 0.001`, window = 3).
- **Decay with separated bounds**: when `scale < 1.0`,
  `ramp = max(ramp_floor, ramp * scale)` — the legacy
  `max(global_concurrency, ramp*scale)` where `global_concurrency` is the
  **named floor** (`recursive.py:197`). P0-1's point —
  `max(global_concurrency, ramp*scale)` conflates a maximum with a floor — is
  resolved by **naming the two bounds**: `ramp_floor = global_concurrency`
  (the floor it never decays below) and `ramp_max = RECURSIVE_RAMP_MAX` (the
  cap it never grows past, `recursive.py:261`). Both are `Count` fields on
  `PacingPolicy`; decays occur **once per sensing update** and **once per
  release** (an in-flight drop can pull the ramp down).
- **After stalls and idle periods**: a terminal `AdmissionRefused(
  IdleReason.OVERSIZE)` (or `STALE`-terminal raise) does **not** reset the
  ramp (the caller-overwrite bug would return); the ramp holds at its last
  value, and a **parked** admit (internal RETRY_PRESSURE/STALE park) wakes via
  the normal release/sensing signal. There is no `Idle(RETRY)` value — the
  park is internal to `__aenter__` (BLOCKER-1). Only the documented **quiescent reset door** —
  `gate.reset_ramp_for_quiescence()` — restores the cold initial ramp, called
  exclusively at a crawl edge, guarded so it cannot run mid-flight (asserts
  zero in-flight leases).
- **Expose a read-only snapshot** `gate.ramp() -> Count` (a copy) and
  `gate.progress() -> Progress`.

### 4.2 Lease/release accounting (P0-3, M2 — admission and lease-taking are
    inseparable)
**Admission and lease-taking are one operation, by construction (Model A).**
The public door is `async with gate.admit(want) as granted:`. `admit(want)`
is an `asynccontextmanager` whose `__aenter__` (1) makes the decision, (2)
parks **internally, inside `__aenter__`**, via the single `_wait_for_progress()`
(BLOCKER-1) while only retryable pressure/stale holds, (3) once a grant is
possible, **enters the lease** (commits the in-flight reservation) and yields
a **live `Granted`** to the body, and (4) releases exactly once on **body**
exit, exception, or **body** cancellation. **The body never sees an `Idle`, never matches a
sum: it holds a lease or the call raises.** **Terminal outcomes** —
`OVERSIZE` and `STALE`-terminal — **raise a typed
`AdmissionRefused(reason: IdleReason)` from `__aenter__`**; no lease exists
when the raise fires, so no cleanup is needed; `IdleReason` is a **public**
closed `StrEnum` for callers that need to distinguish. **There is no
caller-visible gap between the decision and the commit**, because there is no
lease entry distinct from the `async with` admit — the door is lease entry; a
`with granted.lease()` form does not exist in the v1 vocabulary (B-6). In-flight
accounting, `Granted.__post_init__`'s `total >= 1` law, and the OVERSIZE/
terminal predicate all hold because a committed lease always has a live scope
or a guaranteed release.

**Pre-lease (park) cancellation is distinct from body cancellation.** A
cancellation delivered while `__aenter__` is parked inside
`_wait_for_progress()` — the body never entered — does **not** invoke
`__aexit__` and there is **no lease to release**: release is a no-op on
`self._lease is None`, in-flight accounting was never committed, and the
park's signal-wait registration is cleaned up on pre-lease cancellation (the
gate unregisters the parked waiter so a late wake cannot signal a dead
park). **Lease (body) cancellation is the ONLY cancellation that releases.**
`AdmissionRefused` raised by `__aenter__` is **not** seen by `__aexit__` —
`__aexit__` only runs after `__aenter__` *yielded* its body, so a refusal
thrown during `__aenter__` never reaches `__aexit__` with a live `Granted`.
**The abnormal-`__aenter__` path DOES run the `asynccontextmanager`
generator's own `finally`** (empirically on 3.14; B-4); the release inside
that `finally` must **no-op on `lease is None`** — the concrete guard, not the
absence of `__aexit__`, is the safety that never releases a lease that never
existed.

Each lease records amount and owning gate (identity, not a caller string);
releases **exactly once idempotently** (`release() -> bool`, `True` on the
first; a second release is a no-op); updates in-flight accounting and bumps
the versioned wakeup (§4.3); rejects releases to the **wrong gate**
(`lease.release()` on a lease owned by a different gate raises
`WrongGateError`). **`release()` is synchronous** (R6-M1): it performs no I/O
and cannot be interrupted mid-bookkeeping (a `threading.Lock` around the
bookkeeping, no await point), so the `finally`'s release is never an await
site and a single delivered cancellation runs it to completion then
re-propagates. **No TTL/expiry in v1**: expiring a lease whose work
continues causes double accounting (fencing undefined). Rejected explicitly.

**Caller-callable `release()` is a supported early-return, carrying a
work-completion signal** (B-1): a body may call `granted.lease.release()`
early to return its allocation before the `with` exits (the versioned wakeup
fires, other admits may re-decide sooner). The signature is
**`release(*, work_completed: bool = True)`** — **`__aexit__` passes
`work_completed=True` on normal body exit**, and **`work_completed=False` on
exceptioned body exit and on body cancellation** (R5-B1): a raised/cancelled
body has **not** completed its grant and must not advance `last_advance`. The
generator `finally` distinguishes the two with **`sys.exc_info()[0]`** — it
is `None` on normal exit and the exception type on body-raise — and defaults
to `work_completed=False` in the `finally`, passing the caller the flip only
for the normal-exit path. A caller-callable early-return
that did **not** do its work passes **`work_completed=False`** (or relies on
the default only when it actually completed), so the release surface does
**not** advance `last_advance` — it leaves progress where the grant left it.
**The kernel does not claim to trip `admission_stalled` on a caller's
work-failure loop** (R6-B1, Option B): a grant is a transaction, not a proof of
output — a crash-looping engine that keeps re-granting keeps advancing
`last_advance` through its grants and the kernel's StallGuard (keyed on
`last_advance` only) does **not** fire (the production `allowed_jobs or
page_count > page_count_before` record counts an admit as progress — that
admitted term is precisely the grant-blindness the delegation must exclude,
see below). Detecting that loop is **delegated to the crawl-side output-progress
signal**. **Tightening (R7-B1): the delegated crawl-side detector is
`page_count` growth ONLY — the `allowed_jobs` term of the legacy `crawl_batches`
record (`admission_stall.py:129`) must NOT advance it, or the delegated record
inherits the kernel's grant-blindness (each re-grant makes `allowed_jobs`
truthy before any output, so the crash-loop passes as "progress" and the stall
never trips). Tighten to `page_count > page_count_before` on the delegated
record** — only actual produced output (a `page_count` increase) advances the
crawl-side stall detector. **Horizon (R8-B1): the crawl-side stop fires only
on a zero-admission pass** — `admission_stall.py:119` consults
`stall_guard.stalled()` solely when `allowed_jobs` is falsy. So the delegated
record is an output-progress *stop* that trips on the first pass that admits
nothing after 60s with no `page_count` growth; it does **not** trip a
crash-loop that re-grants on every pass while producing no committed pages
(such a loop never reaches the `stalled()` check). The delegation detects
**"no output AND no admission"**; it cannot stop a loop that keeps being
granted work. **Ultimate bound (R10-B1, scoped R11-B1):** each granted job on
   a continuously-granting / continuously-alternating zero-output loop is
   bounded by the crawl's own per-job execution timeout — in crawlutron, each
   granted job's `execute_crawl_chain` is wrapped in
   `asyncio.wait_for(..., timeout=budget)` with `budget =
   total_url_budget_seconds` (`request_pipeline.py:167-187`, mapped to
   `url_budget_exceeded`) — but each re-grant re-admits with a **fresh
   budget**, so that timeout bounds each *job*, not the *loop*. The **loop**
   itself is bounded only by the **finite work surface** (frontier drain /
   `max_pages`) and whatever caller deadline the caller itself enforces —
   which the kernel, and crawlutron, do not provide (no `wait_for`/deadline
   wraps `crawl_batches`; `CrawlPolicyLimits` has no whole-crawl deadline) —
   so a zero-output loop that keeps being granted work has **no
   admission-machinery wall-clock bound**. The admission machinery (kernel
   StallGuard and the delegated seam stop) cannot and does not bound it. **Batch-synchronous (R8-B3):** the crawl-side record is
batch-synchronous — `commit_batch` (where `state.page_count` increments,
`recursive.py:344`) runs to completion (`admission_stall.py:128`) before the
same-pass record check (`admission_stall.py:129`), so the record cannot lag
the work and a saturated-but-producing box never false-stalls. Note
`page_count` counts committed results including spilled permanent failures,
not only pages returned to the caller. **(Single home: this is the one
authoritative statement; migration unit 8 cross-references here, R9-M3.)** The
kernel's guarantee is gate liveness — never `(0,0)` when work is wanted,
bounded parks, no capacity overshoot, gate-level progress StallGuarded — not a
caller-work detector. `work_completed=False` here is honest-caller telemetry
(it prevents release-masking and feeds the crawl-side output derivation), not
a crash-loop tripwire. `__aexit__` then **no-ops
the second release** (release is idempotent), so early-return + implicit
body-exit release is harmless. This reconciles `test_release_is_idempotent`:
releasing twice — once by the body, once by `__aexit__` — is a no-op, and a
body that never calls `release()` still releases exactly once (completed) on
exit. **Only a completed release (`work_completed=True`: normal exit OR an explicit
caller early-return-with-work) OR a grant advances `last_advance`** (see 4.4).
`__aexit__` itself never reports completion for an exceptioned/cancelled body.

**Lease covers the full grant; unused allocation is not returned in v1
(over-count accepted)** — a body that consumes only part of `granted.allocation`
leaves the remainder counted in-flight until the body exits, by design (no
partial-release, MINOR-2a). **Nested admits inside a lease body are
prohibited in v1**: an `async with gate.admit(...)` executed while another of
the same gate's leases is live inside the same task is a **caller error** and
raises `NestedAdmitError` (no silent capacity starvation; MINOR-2b). The gate
never fabricates capacity for a nested admit. Detection is a **`contextvar`**
set at lease entry and cleared in `__aexit__`; the nested check reads it and
raises `NestedAdmitError` — concurrent admits from other tasks (no contextvar
set) are unaffected (MINOR-2).

### 4.3 Concurrency contract (P0-4, resolved)
- `admit` is **synchronous, non-blocking, atomic** with respect to other
  admissions and releases: it takes a `threading.Lock` around each
  decide-and-record step and holds it only while mutation of gate state
  occurs. **No external code or I/O under the state lock** — gauges are read
  *outside* the lock (their readings are inputs, snapshotted once per
  decision; a raising gauge is handled per §7), and no user callback runs
  under it. **The `threading.Lock` is released before `_wait_for_progress()`
  parks** — **the gate never holds the lock across an await** — and is
  **re-acquired after wake to re-decide** (B-2). The park decision flow is:
  **decide-under-lock → release lock → wait on `Signal` → re-acquire lock →
  re-decide → enter lease (commit in-flight) under the lock → release lock →
  yield to body.** (R5-B2): **the in-flight commit is atomic with the decision
  because both happen under the same re-acquired lock.** If the lease were
  entered *after* the lock was released, two re-awakened admits A and B could
  both re-decide against an in-flight total that neither has committed — B
  commits +m, then A commits +n → `I+m+n > capacity` overshoot. Holding the
  commit under the lock serializes decision+reservation, so correctness is
  trace-invariant under any interleaving (as §4.3 claims). A concurrent
  release (which takes the lock to mutate in-flight + bump the version) can
  therefore always reach the parked admit's wakeup — a park held under the
  lock would deadlock RETRY_PRESSURE (the event never fires). **The lock is
  also released before the `with` body yields** — the gate never holds its
  lock across a caller's body; the body runs lock-free (MINOR-2c).
- **Concurrent callers are safe**: many threads/coroutines may call `admit`
  and `release` concurrently; correctness (never exceed `capacity_lane`, floor
  never lost) is trace-invariant under any interleaving. The gate records
  `in_flight_lane` per lane and `want_requests` (per admission) — not a
  process-global counter.
- **Wakeup — versioned signal, no lost wakeup**: the gate keeps a monotonic
  `version: int` bumped on every release and every sensing change. **The wait
  owner is the gate, inside `__aenter__`, never the caller** (BLOCKER-1):
  `_wait_for_progress()` waits on the signal, then **re-checks the version** —
  if it changed between the decision-and-wait (an event arrived "immediately
  before waiting"), the gate re-decides immediately rather than sleeping — the
  classic lost-wakeup is closed (acceptance item 7). The wait path is the
  **single event-with-optional-timeout primitive**: `gate._wait_for_progress()`
  awaits the injected `Signal` (an `asyncio.Event`-equivalent) with an optional
  `Duration` timeout, then re-checks the version. The **concrete event/sleep
  primitive lives OUTSIDE `packages/core/src/admission/`** — injected from the seam; the kernel
  declares only a `Signal` Protocol (`.wait(timeout)`, `.set()`). The
  source-scan gate (§9.6) is satisfiable under exactly this reading: it scans
  the **top-level `packages/core/src/admission/**`** for direct
  `time.monotonic`/`asyncio.sleep` (and aliases); because the concrete
  blocking/awaiting primitive is never implemented in-package, the seam's
  event class (which may legitimately use `asyncio.sleep`) is out of scope.
  **Never keyed on `packages/core/src/crawlutron/admission/`.** It is the **one** gate-side
  yield (§8).
- **Versioned wakeup is global, not per-lane**: one monotonic `version: int`
  is bumped on every release/sensing change; a waiter in any lane wakes on any
  version change and re-decides inside `__aenter__`. **Spurious cross-lane
  wakes are acceptable** — wake-then-recheck is correct: a lane that wakes but
  still cannot fit parks again. No per-lane wakeup in v1.
- **Waiter fairness**: two parked admits wake on one release (global version),
  both re-decide; one grants, the other re-parks. A fresh admit arriving each
  release can keep winning while a given admit parks longer. **v1 accepts this
  temporary unfairness among parked admits** (correct liveness; bounded by the
  floor — a wanted-and-fittable lane still makes progress); **bounded-FIFO
  ordering is deferred** to a later version, mirroring the cold-probe
  lane-starvation acknowledgment (MINOR-5). No admitted *correctness*
  violation; the admission test item 7 asserts wake-then-recheck liveness, not
  ordering.
- **Decision rule and precedence** (Pending item, resolved; BLOCKER-1 +
  MINOR-3): `__aenter__` evaluates, in order — **STALE-first → OVERSIZE →
  RETRY_PRESSURE** (fail-closed on untrustworthy data; never terminal on stale
  evidence):
  1. **STALE** — want non-empty and stale past the staleness bound → **park is
     time-bounded**: `_wait_for_progress()` waits with the Signal's **optional
     timeout = remaining `stale_bound`**, and on elapse raises
     `AdmissionRefused(IdleReason.STALE)` (terminal). **Provenance (R5-B4):
     remaining `stale_bound` is measured from STALENESS ONSET** — `remaining =
     stale_bound − (now − staleness_onset)` — **and is never reset per
     re-park**: if a re-park re-awaits, it reuses the same elapsed account, so
     repeated re-STALE parks converge to zero and terminate rather than
     resetting the deadline each wake. This is distinct from
     RETRY_PRESSURE's **event-bounded** park (until a release/sensing wakeup).
     With a STALE resource and nothing in flight, no release bumps the version,
     so STALE-park terminates only via the timeout — that is the designed
     escape (MINOR-1). STALE parking does not clear on a release when the
     sensor is still stale.
     **Caller horizon (B-3):** for a STALE resource, `async with
     gate.admit(want)` **blocks up to the remaining `stale_bound`** before
     `AdmissionRefused(IdleReason.STALE)` surfaces. The 4.3 "synchronous,
     non-blocking" contract applies to the decision step and to RETRY_PRESSURE
     parking (event-bounded, no wall deadline); **the STALE door is the one
     bounded-blocking case** — its park has a monotonic timeout, never an
     infinite wait.
  2. **OVERSIZE** — no **wanted** lane can fit (`min(ceiling_lane,
     free//cost_lane) == 0`, MAJOR-2) and nothing is in flight → **terminal,
     raise `AdmissionRefused(IdleReason.OVERSIZE)`**, never retried. (A
     zero-want lane never satisfies the predicate — B3.)
  3. **RETRY_PRESSURE** — capacity exhausted by in-flight work → park
     internally until a release/sensing wakeup reopens capacity.
  `Granted` is returned otherwise. `IdleReason` is a **public closed
  `StrEnum`** carried on `AdmissionRefused` (the only reason surface a caller
  can match); the kernel internally uses a private decision-enum for the
  branch. A **combination probe** pins the precedence: stale + oversize
  together must raise/classify `STALE` (the outer, fail-closed arm), never
  `OVERSIZE`.

### 4.4 `Progress` and telemetry
`Progress(last_advance: Instant, last_decision: Instant)` is the single
progress record. **A granting decision also records `last_advance` — on grant,
`last_advance = max(last_advance, last_decision)`** (a `Granted` is progress;
MAJOR-1). This guarantees `last_advance >= last_decision` by construction and
prevents a stream of grants from falsely stalling the `StallGuard` (which keys
on `last_advance` only). `StallGuard` **reads only this record** (via the
gate's clock); `_wait_for_progress` parks update neither `last_advance` nor
`last_decision` (nothing was granted).

**Progress is defined as "a grant OR a completed release"** (B-1, refining
the round-3 "a grant OR a release"). A **completed release** —
`release(work_completed=True)`, which is `__aexit__`'s normal-body-exit
signal — advances `last_advance = max(last_advance, now)`, so a saturated box
actively draining *completed* leases, but never dropping enough to re-grant,
does **not** falsely trip `admission_stalled` at the 60s hard stop (a pinned
non-negotiable, §0 #4). An **uncompleted early-return release**
(`release(work_completed=False)`) — a body that admits and returns without
doing its work — **does not advance `last_advance` at the release surface**
(it leaves progress at its grant-time). This closes the round-3 release-masking
blind hole where an unconditional release-as-progress reported progress with
no completed work. **It does not by itself trip the 60s stop** (R6-B1): a later
grant re-advances `last_advance`, so whether the stop fires depends on the
grant surface, which the kernel does not use to detect caller work-failure
loops. `work_completed=False` is the honest-caller signal that feeds the
crawl-side output derivation.

**Parked-stall bounding (R9-M2):** a box parked **inside the admit**
(RETRY_PRESSURE park, §4.3; cold-probe park, §7.2) is **not** subject to the
60s `admission_stalled` stop — the loop suspends at `admission_stall.py:118`
and never reaches the `stalled()` check at line 119. Such a box is bounded by
the **`stale_bound`→STALE terminal**, **provided the resource actually becomes
stale** — a healthy-sensor RETRY_PRESSURE park whose capacity is permanently
held by a never-releasing lease is **unbounded in v1** (a consequence of the
rejected TTL; bounded only by the caller's own task timeout/deadline, see the
ultimate-bound note in 4.2), not the 60s stop, which fires only when
the loop reaches a zero-admission pass at line 119. Escape from the park
requires a release or sensing version-bump re-decide that surfaces STALE (§4.3).

**Exceptioned exit is non-progress unconditionally** (R6-B3): an exceptioned
body exit is treated as non-progress **even when the body performed real work
before raising** — a grant is the only mid-grant progress marker. A body that
works then raises freezes progress at its grant-time; the false-stall this
can cause for a genuine work-then-fail pattern is the **accepted fail-closed
cost** (the gate cannot audit a raised body's partial work).

**`work_completed` is a caller self-report** (R5-B3, re-scoped R6-B4): **the
gate treats EVERY normal body exit as completed work** — it cannot distinguish
an idle-but-normal body from a worked one, nor police a caller that lies
`work_completed=True`. A buggy-but-honest body that admits, does nothing, and
exits normally is credited completed and suppresses `admission_stalled`
without ever lying — the same mathematical surface as a liar. **Both are
accepted out-of-scope** (the gate cannot verify the body's work; it is not a
caller-work auditor), mirroring v1's `record_progress` trust posture. The
progress law detects honest-engine *gate* stalls (no grants, no completed
releases); it does not instrument the caller's output. `paced` moves out of `Granted` as a
caller-visible door bool into
`Progress`/telemetry (P0-8): `Granted` keeps `paced: bool` *informational*
only (see §6.4) — the wait itself is always gate-side.## 5. The scale law and capacity equation (P0-5, P0-6, P0-7)

### 5.1 Ratio→Scale function (the law, P0-5, resolved)
```
scale(usage, target, hard):
  usage <= target  -> Scale(1.0)
  usage >= hard    -> Scale(0.0)
  otherwise        -> Scale(1.0 - (usage - target)/(hard - target))
```
This is exactly today's `batch_scale` (`pressure_memory.py:81-97` — full speed
≤target, linear down target..hard, 0 ≥hard), now as a typed `Ratio→Scale` law.

**`headroom` (P0-5, one definition)**: `headroom(usage, target, hard) =
1 - usage` measured against the **hard ceiling** (usage as a fraction of the
hard cap), because that is the quantity the hard ceiling can safely admit
against. The alternative `(hard-usage)/(hard-target)` is the batch-scale
intermediate and is **not** called `headroom`. v2 names
`headroom = 1.0 - usage` (fraction of the resource still below hard) and keeps
the batch-scale linear ramp as a separate `admission_scale` function.

**Monotonicity invariants (gate-able properties, P0-5)**: `scale` is
**non-increasing in `usage`** (monotone), and the **allocation returned is
non-decreasing in `scale`**. Both are property-tested (parametrized over the
usage domain and a fixed `want`/capacity) under the mutation gate (§9).

**`RIDE_CPU_WHEN_FREE`** (§5.4): when `ScaleSource` is
`RideCpuWhenFree(threshold)` and `cpu_headroom > threshold` (default `0.5`, the
legacy `http_scale_factor` CPU rule, `pressure_gates.py:224-228`),
HTTP/GENERAL tasks **ride the CPU scale** (escaping the
browser-held-memory bottleneck); else they respect combined pressure
(`batch_scale_factor`, min-all). `ScaleSource` is per-lane-selection on the
gate; a lane omitted defaults to `MinAll`.

### 5.2 Capacity equation and cost units (P0-7, resolved)
```
capacity_lane = min( scale_slots(lane),
                     ceiling(lane),        # LaneSpec.ceiling: Count
                     want.lanes[lane],     # clamp to the request
                     free_bytes // cost_per_unit(lane) )
```
where `scale_slots(lane) = round(ceiling(lane) * scale)` and
`cost_per_unit(lane) = Cost.memory_bytes` (GENERAL default `48 MiB`, HEAVY
default `700 MiB` — the legacy weights, `pressure_sizing.py:24,29`).
`free_bytes` is **recomputed per admit** (in-flight accounting is
authoritative), never carried stale. **All costs in `Cost(memory_bytes:
Bytes)`** — the unitless `Cost.units: int` of v1 is replaced (P0-7). The
min-all between lanes is not used — capacity is **per-lane**; the global
resource ceiling is enforced by the per-lane `free_bytes` term (in-flight
bytes are summed across lanes against the one hard budget).

### 5.3 Per-lane split and the HEAVY minimum (P0-6, resolved)
A total-only floor starves the expensive HEAVY lane (GENERAL 48 MiB vs HEAVY
700 MiB; today `_dispatch_paced` gives saturated HEAVY only `browser_jobs[:1]`,
`request_pipeline.py:348`). v2 makes the floor **per-lane**, not total-only:

- `LaneSpec.ceiling` is per-lane (GENERAL ceiling = `max_http_concurrency`,
  HEAVY ceiling = `max_browser_concurrency`, the legacy
  `pressure_sizing.py:186-194`).
- The **HEAVY-lane minimum guarantee**: for a `Want` requesting HEAVY work
  under a saturated gate, `granted_heavy >= min(1, want_heavy, ceiling_heavy,
  free//cost_heavy)` — a saturated gate still dispatches **at least one
  browser job per pass** when one can fit, the named replacement for
  `browser_jobs[:1]`. Stated as an invariant: **the floor spread is per-lane;
  the HEAVY lane never gets a smaller floor than a fraction that fit can
  fill, and never starves while free bytes admit one unit.**
- **Floor-vs-want clamp per lane**: `floor_lane = max(1, min(want_lane,
  floor_global // lane_spread))` where `floor_global = max(1, ramp //
  floor_fraction)` (see §1: `floor_fraction` is an integer denominator `>= 1`)
  and **`lane_spread` = the number of *wanted* lanes** (`{lane : want_lane >=
  1}`, minimum 1 — m3). The inner `min(want_lane, …)` caps each lane's spread
  at its own want; the outer `max(1, …)` guarantees each *wanted* lane gets at
  least one slot when its want and the hard ceiling permit it. **Invariants**:
  **`1 <= granted_total <= want.total`** (a swept `floor_global = 1` with two
  wanted lanes grants `max(1, min(w, 1//2)) = max(1, 0) = 1` per lane — B2)
  and, when HEAVY is wanted and can fit, **`granted_heavy >= 1`**. A
  **zero-want lane contributes weight to neither** `lane_spread` nor
  `granted_total`.

### 5.4 One wait site (P0-8, resolved)
`admit` sleeps **gate-side, inside `__aenter__`** (via the injected `Signal`
event-with-optional-timeout, §4.3/M4), after arming the versioned wakeup,
**until a live `Granted` can be returned — the door never returns `Idle`**
(BLOCKER-1). There is
**exactly one wait site** in the kernel: the gate's `_wait_for_progress()`
(`Signal` + version re-check), always invoked by the gate, never the caller.
The caller never sleeps; there is no
caller-side `wait_while_saturated` (its fate, §6.5). Legacy
`_dispatch_paced`'s `saturated_dispatch_floor` and the recursive
`asyncio.sleep(0)` (of `crawl_batches`, `admission_stall.py:125`) are
absorbed: the floor is internalized (the `(0,0)`-yield at
`pressure_sizing.py:245-246` and the post-admit overwrite at
`recursive.py:222` become impossible — `admit` never returns 0 total);
`paced` becomes telemetry. **Reachability of the zero-admission branch
(R9-M2):** because `admit` never returns 0 total, the crawl-side
`if not allowed_jobs` branch at `admission_stall.py:119` — the 60s-stop
trigger — is fillable post-migration only by **NON-admission empties**: a
pass that *granted* work but produced none of it back, e.g. the robots filter
in `admit_recursive_batch` (`recursive.py:174`) rejecting every granted item.
The gate itself never fills that branch by returning zero; a reader must not
assume the admission-0 path reaches the 60s stop.

## 6. The contested rulings, decided

### 6.1 Ratio/Scale merge? → **Keep separate.**
Frozen dataclasses enforce nothing at a call site without a static checker,
but the repo's contract is **mypy/pyright + runtime cost**: `Ratio` is a
*threshold/measured fraction*; `Scale` is an *output multiplier*. Swapping
them is a semantic bug even within `[0,1]` — a `Scale(0.7)` is not a `Ratio`
target. **Two different operations and at least one invariant differ**
(`scale` non-increasing in usage vs `headroom` non-increasing in usage), so
collapsing would blur the reviews-2/4 line. v2 keeps both, both `[0,1]`, both
validated, with the boundary contract stated: **`Scale` is produced by the
kernel and consumed by the capacity equation; `Ratio` is produced by
gauges/limits and consumed as the argument to `scale()`.** The static-checker
contract (mypy/pyright) is the enforcement; runtime validation is defense in
depth.

### 6.2 Budget → **Narrow to memory-only.**
All five agree the mapping-sum-of-bytes across non-byte resources is a
category error. v2 **drops the general `Budget`** and keeps a memory-only
accounting inside the gate: total `hard_bytes` + per-lane reservations (the
legacy `browser_pool_reserve_bytes`/`doc_pool_reserve_bytes`,
`pressure_sizing.py:158,163`). "Budget" as a public class **is deleted**; the
surviving concept is `Limits` (per-resource target/hard) + the per-lane
`Cost`. Memory is the one resource with a defensible byte-accounting;
CPU/LOOP_LAG contribute via `Ratio`/`Multiplier`, never a sum-of-bytes.

### 6.3 `Want` constructor → **`Want.of`**, require all lanes, reject empty.
`Want.at_least` is misleading — it zero-fills, not lower-bounds. v2 uses
`Want.of(general=…, heavy=…)` (keyword, all-lane, complete over `LaneId`),
and **`Want({})`/`Want.of()` with total 0 refuses at construction** (P1-3
completeness + the empty-want guard). There is no "empty `Want`" in the
system.

### 6.4 `Granted.scale` → **informational only.**
The caller never applies `scale`; release uses lease identity/accounting,
never a caller-supplied scale. `Granted.scale: Scale` is for
observability/telemetry. `paced: bool` is likewise informational (§4.4). The
capacity the caller trusts is the **per-lane amount in the
`Granted.allocation`**, not a scale it re-applies.

### 6.5 `wait_while_saturated` fate → **absorbed; the recursive-contract
    test survives.**
`wait_while_saturated` is removed as a public door (its only definition sites
are `pressure_memory.py:156`, `pressure_gates.py:201`; **no production crawl
path calls it** — production dispatch uses `WeightedAdmission.admit` + the
recursive `asyncio.sleep(0)`). Its park-semantics fold into the gate's one
`_wait_for_progress()` (§5.4). The **doctrine that the recursive path never
world-parks is preserved**: `tests/_recursive_harnesses.py:57-59`
(`ContentionFreeGate.wait_while_saturated` raising
`AssertionError("the recursive loop must not stop the world for admission")`)
becomes the kernel's "GENERAL/recursive callers never park" contract,
asserted against the migrated harness. No silent park-semantics change (M2's
named fate) — the v2 gate-only yield is the same non-blocking guarantee, now
centralized. The recursive "never world-park" guarantee is the same guarantee
§5.4's single gate-side yield provides: the only blocking is
`_wait_for_progress()` inside the event-driven admit, never a caller-facing
`wait_while_saturated`.

### 6.6 Monotonic time → **applies everywhere in the kernel; wall-clock
    informational only.**
`Clock.now() -> Instant` (monotonic) is the **only** timeline for stall
detection, retry deadlines, and progress. Wall-clock timestamps may remain
informational (telemetry) if passed an explicitness. `Progress.last_advance`
and `StallGuard` reads are monotonic `Instant`s. `time.monotonic`/
`asyncio.sleep` never appear inside the kernel (source-scan gate, §9).

## 7. Gauge semantics — fail-closed doctrine (P0-9, P0-10, P1-9)

### 7.1 Failure → hard-saturated (P0-9, resolved)
Repo doctrine is fail-closed. A **raising gauge** is treated as
**hard-saturated** for that resource: `usage = Ratio(1.0)` (floor grant), so
a failure cannot over-admit. The raise is logged (through the gate's injected
observer, never to a crawlutron log) and fed to the gate's failure counter;
repeated failures drive the resource into the **`stale`** state (§7.2).

> Ruling: propagate **nothing** to the admit caller; a gauge raising does not
> turn `admit` into a raising door. It turns that resource hard-saturated,
> which is safe (no over-admission) and recoverable (next sensing can clear
> it).

### 7.2 Explicit gauge states (P0-10, resolved)
Each `ResourceGauge` is classified into exactly one of:
- **healthy** — a current, real sample exists (`usage` trustworthy);
- **unhealthy** — the sensor raised; behavior = hard-saturated (§7.1);
- **cold** — no sample yet (startup), or a sample older than the cold-probe
  bound; behavior = **bounded probing allowance only, no ramp growth**;
- **stale** — samples missing past the staleness bound; behavior = **stop
  new admission**.

**Cold posture (P0-10, one coupling, resolved; m6):** cold permits only a
**bounded probing allowance** — a small **`cold_probe: Count` cap on
*concurrent* cold in-flight units**: it bounds how many jobs may be in flight
*at once* during cold (each release reopens the probe), not a cumulative
admit budget; **global**, not per-lane — one `cold_in_flight` counter is
summed against the one global hard budget; a cold admit **parks internally**
when `cold_in_flight >= cold_probe` until a release drops it (RETRY_PRESSURE
is internal parking, never a refusal value). If cold also represents a
bounded probe that cannot be served within `stale_bound`, the terminal
`AdmissionRefused(IdleReason.STALE)` surfaces on elapse. Default `initial_ramp` (so fail-open-at-startup is bounded by
the small initial ramp; **a global cold probe may be lane-starved — cheap
GENERAL cold admits can fill `cold_in_flight` and refuse the HEAVY cold probe;
accepted in v1**, MINOR-5; today cold CPU returns full scale via
`cpu.batch_scale_factor()=1.0` with usage 0, `pressure_gates.py:218-230`, and
`crawl_batches`/recursive only offsets it by `concurrency_multiplier=1` at
`recursive.py:192-193`). Cold **prevents ramp growth** — the growth arm
(`scale >= 1.0`) is suppressed while any resource is cold or unhealthy. The
coupling is now explicit: **`initial_ramp` (cold) + `cold_probe` (cap) +
"no ramp growth while cold"** together bound the startup admit rate.
**Cold no-admission reachability (R9-M1):** a cold no-admission pass parks at
`admission_stall.py:118` inside the admit and does **not** reach the
`stalled()` check at line 119 — so cold starvation terminates via the
**`stale_bound`→STALE bound** (7.2), never the 60s `admission_stalled` stop
(it never gets the loop to a zero-admission pass that would consult
`stalled()`). This is consistent with the round-1 MINOR-5 cold-lane-starvation
acknowledgment.
**Prolonged staleness stops new admission**: once stale past
`stale_bound: Duration`, `__aenter__` raises
`AdmissionRefused(IdleReason.STALE)` for that resource's
lanes (retryable until the sensor recovers; terminal after the bound).
**Which resources may differ**: MEMORY is never cold in the legacy sense
(`read_memory_stats` always returns a real total, `pressure_memory.py:50-78`;
`MemoryGate._read_usage` is fail-closed `1.0` on a zero total, line 204), so
MEMORY has **no cold state** — healthy or stale only. CPU genuinely has cold
(first-signal) and LOOP_LAG has cold (probe not yet started). MEMORY
staleness is the binding termination; CPU/LOOP_LAG cold only caps growth.

**`is_warm` (P1-9, consumed):** `ResourceGauge.is_warm() -> bool` is **kept
and consumed** — it feeds the cold/stale classification (§7.2) and the "no
ramp growth while cold" gate. It is no longer a dead declared member
(`CpuGate.is_warm`, `pressure_gates.py:111-119`; `ResourceGauge` currently has
none — the v1 plan declared it; v2 makes it a real Protocol member).

## 8. The door and time seams

- `Want`, `Allocation` — complete over `LaneId`, validated (P1-3).
- **`Admission = Granted | Idle` is repealed as the door type** (BLOCKER-1).
  The door is `async with gate.admit(want) as granted:` — a **live `Granted`
  lease** or a **raised `AdmissionRefused(reason: IdleReason)`**. The kernel
  keeps a *private* decision-enum internally for its own branch; callers never
  match a sum at the door. `IdleReason` is the **public closed `StrEnum`**
  carried on `AdmissionRefused` (`OVERSIZE`, `RETRY-PRESSURE`-style parking is
  internal and not surfaced as a value, `STALE` terminal).
- `AdmissionGate` — the single decision point; owns capacity, floor
  (per-lane), pacing (ramp), progress, in-flight; the public door is
  **`async with gate.admit(want) as granted:`** (an `asynccontextmanager` —
  admission-and-lease are inseparable, M2; **`__aenter__` parks internally until
  a grant, never the caller**, BLOCKER-1). Constructor takes the injected
  `Clock`/`Signal` and the `PacingPolicy` (`yield_interval`, `ramp_max`,
  `ramp_multiplier`, `floor_fraction` (an int denominator `>= 1`), `ramp_floor`,
  `ramp_growth_window`, `cold_probe`, `stale_bound`).
- `StallGuard(gate)` — **no `clock` param** (P1-7): `StallGuard.__init__(gate)`
  reads the gate's own injected clock and `Progress` record only. The
  two-clock bug (`AdmissionStallGuard.__init__` defaulting its own
  `time.monotonic`, `admission_stall.py:49`) is killed.
- `Clock` / `Signal` — the only time/seam surfaces; **both declared as
  Protocols in the kernel, their concrete implementations injected from the
  seam** (M4). No `time.monotonic`, no `asyncio.sleep`, no concrete
  `asyncio.Event` construction inside `packages/core/src/admission/**` (source-scan gate, §9.6).

## 9. Migration plan (every gap closed — M1..M7)

1. **`tests/_admission_doubles.py` migration is its own unit.** Concrete
   call-site inventory (grep-verified): `pinned_resource_gate` at **4 call
   sites** — `test_admission_scaling.py:43`, `test_admission.py:227,238,249`;
   `PinnedGate` constructions at **13 sites** —
   `test_admission_policy.py:154,160,164,170,179,185,194,211,216,221,225`
   (11), `test_admission_scaling.py:193,216` (2). Plus the second double
   `ContentionFreeGate` in `tests/_recursive_harnesses.py` (constructor + the
   two test modules using the recursive harness). **Every caller migrates in
   the same commit.** `PinnedGate` (currently subclasses production
   `ResourceGate` skipping `__init__`, `_admission_doubles.py:47-54`) becomes
   a **pure `FakeResourceGauge`** (a `Protocol` implementer) — no
   `ResourceGate` base, no skipped init. The doubles exercise the **same lease
   and wakeup contract as the real gate**: `Granted` carries `lease: Lease`
   (MAJOR-4); a `granted.lease.release(work_completed=True)` called twice —
   once by the body, once by `__aexit__` — is idempotent; a completed release
   bumps the version and a parked admit re-decides (not sleeps). The red names the inventory count.
2. **`wait_while_saturated` fate** — absorbed into the gate's one
   `_wait_for_progress()`; the recursive "never world-park" contract survives
   as a test (M5, §6.5). The old park-tests
   (`tests/admission/test_admission.py`, `tests/orchestration/
   test_resource_gauge.py`) migrate to the kernel's **internal-park** admit
   semantics (the gate parks in `__aenter__` and yields a `Granted`; the old
   park behavior is absorbed into `_wait_for_progress` — there is no
   caller-visible `Idle(RETRY)` value).
3. **The derivation layer needs a home** — `build_admission_policy`,
   `max_http_concurrency_for`, `TaskRamBudget` already live in
   `packages/core/src/crawlutron/runtime/pressure_sizing.py:119-196` (the seam side). v2 keeps
   them **seam-side**, producing `LaneSpec.ceiling` and `Cost` from the memory
   limit + fixed pools; they produce kernel inputs and never import the
   kernel's internals. The seam also owns HTTP→GENERAL / BROWSER→HEAVY mapping
   (§2) and `http_scale_factor`→`RideCpuWhenFree`.
4. **Loop-lag multiplicative delta recorded and preserved**:
   `batch_scale_factor = min(mem, cpu) × lag.factor()`
   (`pressure_gates.py:199`) ⇒ v2 keeps LOOP_LAG as `loop_lag_multiplier`
   multiplied after min-all, **not** a third LIMITS ratio (§2). Documented in
   the seam; the closed-loop-lag dispatch curve is a known, recorded behavior
   delta in v2's design notes, with a seam-parity test (§9.6) asserting the
   old knob's decision ≈ the new kernel's at equal observed pressure.
5. **Line-number drift re-pinned from the live tree**: the v1 plan's cited
   `recursive.py:215/222` is **correct on this HEAD** (215
   `batch_size = max(1, int(batch_size*scale))`; 222
   `batch_size = admit_http + admit_browser`). Pre-deletion, the drift table
   is re-derived from the live tree (`git grep` the four `pressure_*` sites
   and `recursive.py` floor lines) and pinned by string in each deletion
   commit (string-drift doctrine).
6. **Ratchet + import-contract + source-scan + mutation gate work** (M6):
   - **New top-level root auto-enters the derived scan surface**:
     `python_bearing_roots()` walks for python dirs;
     `test_every_declared_scan_root_has_a_budget_row`
     (`test_file_size.py:88-91`) asserts `set(_BUDGETS) ==
     set(python_bearing_roots())`, and `test_scan_roots_equal_every_python_
     bearing_tree` (`test_scan_roots.py:16`) pins
     `SCAN_ROOTS == ("crawlutron", "crawlutron_sdk", "scripts", "tests")`.
     **Both must add `admission` in the same commit** as the new package, and
     `_BUDGETS["admission"] = 0` (`test_file_size.py:18-23`). This is also
     governed by the invariant-scope law (SCAN_ROOTS is derived,
     `_walk_primitives.python_bearing_roots()`).
   - **Zero-crawlutron-import gate**: a new invariant (modeled on
     `test_engine_import_seam.py` / `test_one_clock.py`'s path-keyed
     `Registry`/`Rule` from `tests/rules.py`) AST-scans every
     `packages/core/src/admission/**/*.py` and fails on any `import crawlutron` /
     `from crawlutron import …`, naming file:line. It is a gate, not a
     comment.
   - **Source-scan gate**: a second `Rule` (same `Bites`/`Registry` shape)
     fails on any `time.monotonic`/`asyncio.sleep` (and aliases) appearing in
     `packages/core/src/admission/**`, forcing the `Clock`/`Signal` seams.
   - **Mutation gate on the floor law**: a scratch-tree mutation proof (per
     the canon) that a mutant removing `max(1, …)` from `floor_global`, or
     dropping a lane's `min(want, capacity)` clamp, reds a named test. `-n0`
     or scratch tree.
   - **Seam-parity test (scoped — M1)** before deleting `pressure_sizing`
     sites. Parity is asserted on the **unsaturated band** (`usage <= target`:
     full capacity both sides, the old `scale = 1.0` path) **and on lane-fit
     determination** (which lane the admitted work lands in for a given
     `(want, mem, cpu, lag, free)`). **Byte-identical outputs**: the per-lane
     `granted Allocation` for a non-saturated admit and the bucket/tier
     assignment. **Declared, intended behavioral deltas (not parity
     violations)** in the saturated regime: old `(0,0)` → new floor `>= 1`
     (the whole point of the kernel), and caller-side park
     (`wait_while_saturated`) → gate-side yield. The test asserts the two
     produce identical lane decisions wherever the old path returned a
     fully-granted batch, and asserts the *delta* (floor vs `(0,0)`) holds
     exactly at saturation.
   - **Source-scan gate scope (M4 + MINOR-4 descriptor)**: the
     `time.monotonic`/`asyncio.sleep`-and-aliases `Rule` scans **top-level
     `packages/core/src/admission/**` only** with the exact descriptor
     **`scope = lambda *, exclude: trees_under(REPO_ROOT / "admission")`**
     (`trees_under` from `tests/chokepoint_walks.py`; `REPO_ROOT`
     `tests/_walk_primitives.py`). The default `Rule` scope is `app_trees`
     (crawlutron-only — `tests/rules.py:131`, `tests/_walk_trees.py:69-80`)
     and is **NOT** the right scope: an implementation that omits the explicit
     descriptor would sweep `packages/core/src/crawlutron/admission/` instead, the exact
     collision the gate exists to stop. **Never keyed on `packages/core/src/crawlutron/admission/`**
     (the URL-admission package must not be swept). Satisfiable because the
     concrete `Signal`/clock implementations are injected from outside the
     package; the seam's event class (which legitimately awaits) is out of
     scope.
7. **Release sequencing**: **cut `0.4.0rc0` first** (tree is ready; CHANGELOG
   empty — M7), then the library, then the seam migration, then the
   transport-pooling spec.
8. **Crawl-side delegation tightening (R7-B1) — the output-progress record is
   `page_count`-only.** The legacy record that backs engine-level
   `admission_stalled` detection is `crawl_batches`'s progress hook
   (`packages/core/src/crawlutron/orchestration/admission_stall.py:129`):
   `if allowed_jobs or state.page_count > page_count_before:
   stall_guard.record_progress()`. **The `allowed_jobs` term is the thing the
   delegation must exclude**: under the kernel's floor law every re-grant makes
   `allowed_jobs` truthy before any output, so the `or allowed_jobs` arm
   re-admits the crash-loop through the delegated detector even though the
   kernel-side StallGuard correctly stopped advancing. The migration rewrites
   this record to **`page_count > page_count_before` only** — progress requires
   actual produced pages, not an admit. **Horizon (R8-B1, mirror):** the
   crawl-side stop fires only on a zero-admission pass — `admission_stall.py:119`
   consults `stall_guard.stalled()` solely when `allowed_jobs` is falsy — so the
   tightened record trips on the first no-admission pass after 60s with no
   `page_count` growth, and does not trip a loop that keeps being granted work
   (which never reaches the `stalled()` check). The delegation detects "no
   output AND no admission," not a continuously-granting zero-page loop.
   **Seam scope (R8-B2):** this is a **seam-side** change — the **seam's**
   crawl-side stall detector consumes `(CrawlState).page_count` as its
   caller-side output-progress source, replacing the raw `allowed_jobs` gate
   truthiness; **the kernel itself keeps no caller-output visibility (see 4.4)**
   and must not be read as importing `CrawlState` (that would violate the
   import-contract gate, 6/9.6). This crawl-stall-detector seam is distinct
   from 9.6's *decision-identity* seam-parity; relabel per row 10 (output-progress
   mutation gate), not 9.6's parity.
   **Output-progress probe** (R8-B2, distinct from 9.6's decision-identity
   seam-parity): a granted-but-not-completed pass must **NOT** advance the
   delegated record — `allowed_jobs` truthy with `page_count` unchanged → no
   `record_progress()` (mutant "record_progress on allowed_jobs" reds,
   acceptance row 10). The record is batch-synchronous per 4.2 (single home —
   R9-M3): see 4.2 for the `admission_stall.py:128→129` + `recursive.py:344`
   anchors; no restated claim here.

## 10. Acceptance-test program (10 items) mapped to gates

All new tests go through production doors, name the mutant each kills, and
list the attack category (§ Testing law). Every run randomized; global
`timeout=120`; hang-safe where a yield/retry is under test (bounded
`asyncio.wait_for`, assert the bound exceeded). The floor-law mutation gate is
a **property test under mutation coverage, not an AST gate** — an AST scan
cannot judge the data-dependent "floor never zero" invariant; mutants that
drop `max(1, …)`, invert the `//`, or zero a wanted lane must red a named
property test (gate-writability, review round 1).

| # | Acceptance item | Concrete test (behavior name) | Gate enforces |
|---|---|---|---|
| 1 | Ramp init/growth/decay/bounds/caller isolation | `test_ramp_starts_at_initial_and_never_exceeds_ramp_max`; `test_ramp_decays_to_floor_never_below`; `test_ramp_grows_only_when_scale_at_full_and_warm`; `test_caller_cannot_mutate_ramp` (no `CrawlState.ramp`; `gate.ramp()` returns a copy; mutating the copy changes nothing) | mutation gate on `max(1,…)`/`ramp_max` clamp |
| 2 | Saturation where floor and hard capacity conflict | `test_floor_is_clamped_by_want_and_capacity`; `test_granted_total_in_1_to_want_total` (property over the usage/capacity domain); `test_per_lane_floor_never_zero_when_lane_wanted` (a swept `floor_global = 1` with two wanted lanes still grants `>= 1` total — B2/m3) | floor-law property test under mutation coverage |
| 3 | Permanent incapacity with zero work in flight | `test_oversize_want_raises_refused_with_reason_oversize` (`AdmissionRefused(IdleReason.OVERSIZE)` raised from `__aenter__`, never retried — BLOCKER-1); `test_oversize_evaluates_wanted_lanes_only` (**a HEAVY-only want at `free = 600 MiB` RAISES `AdmissionRefused(OVERSIZE)`** — only wanted lane cannot fit; BLOCKER, superseding the round-1 "does not OVERSIZE" phrasing now made false by the wanted-lanes predicate); `test_zero_ceiling_lane_raises_oversize` (a wanted lane with `ceiling 0` and `free//cost >= 1` raises — not a silent zero-grant — MAJOR-2); `test_heavy_only_want_grants_at_least_one_when_it_fits` (HEAVY grants ≥1 **only when `free // cost_heavy >= 1`**) | `AdmissionRefused`/`IdleReason` invariant; ceiling-fit predicate |
| 4 | Release on success/exception/cancellation; duplicate harmless; early-return | `test_release_is_idempotent` (caller-callable `release()` is a supported early-return; `__aexit__` no-ops the second release); `test_lease_releases_on_exception`; `test_lease_releases_on_cancellation` (**body** cancellation only — pre-lease park cancellation releases nothing, MAJOR); `test_pre_lease_cancellation_releases_nothing`; `test_release_to_wrong_gate_refused`; `test_early_return_release_without_work_does_not_advance_progress` (`release(work_completed=False)` does not advance `last_advance` at the release surface — honest-telemetry; does **not** imply the 60s stop fires on a re-grant loop, R6-B1); `test_exceptioned_body_does_not_advance_progress` (an immediately-raising body releases `work_completed=False` via `sys.exc_info()` in the `finally`, so the release does not advance `last_advance` — mutant "release passes True unconditionally" reds — R5-B1) — **both pin the release surface only; kernel-side crash-loop detection is not claimed (Option B)** | lease idempotence + wrong-gate rejection + pre-lease/body cancellation distinction + work-completion progress law (normal-exit vs exceptioned/cancellation) |
| 5 | Concurrent admits/releases never exceed the limit | `test_concurrent_admit_release_never_exceeds_capacity` (threads + shared gate, seeded interleavings); `test_parked_admit_wakes_while_other_thread_releases` (a parked admit awaiting while another thread releases must wake, **not deadlock** — the lock must be free across the park — B-2) | capacity-equation invariant; `-n0`-safe; lock-park concurrency probe |
| 6 | Cold/stale sensing; no ramp growth on missing evidence | `test_cold_start_grants_bounded_probe_and_no_ramp_growth`; `test_stale_sensor_stops_new_admission`; `test_memory_is_never_cold`; `test_stale_precedes_oversize_and_pressure` (a stale + oversize combination classifies `STALE`, never `OVERSIZE` — the fail-closed outer arm, MINOR-3) | cold/stale posture gate |
| 7 | Wakeup on release and sensing change incl. event-before-wait | `test_release_wakes_parked_admit_yielding_granted` (the gate parks in `__aenter__`, then yields a `Granted` once a release reopens capacity — BLOCKER-1); `test_event_before_wait_is_not_lost` (version bump → re-decide inside `__aenter__`, not sleep); `test_wait_is_bounded_nonblocking` (the bound enforced **inside `__aenter__`**, via `asyncio.wait_for`) | versioned-wakeup macro |
| 8 | Stall detection across large wall-clock adjustments | `test_stall_independent_of_wall_clock` (drive the injected monotonic clock far; `Progress`/`StallGuard` abort at the bound); `test_draining_saturated_box_is_not_stalled` (**completed** releases advance `last_advance`, so a draining-but-alive saturated box does not trip `admission_stalled` at the 60s stop — **gate-liveness, R6-B1**) | monotonic-`Instant` source-scan; aligns with `test_one_clock` family; progress = grant OR **completed** release (mutation-guarded by the row-4 probes `test_early_return_release_without_work…` and `test_exceptioned_body_does_not_advance_progress` — listed once, in row 4, so collected-ID reconciliation cannot double-count, R5-cosmetic) |
| 9 | Every production caller and double identified by inventory | `test_admission_doubles_inventory` (the **4+13**+harness count — 4 `pinned_resource_gate` sites, 13 `PinnedGate` constructions, plus `ContentionFreeGate`; every name must have migrated to `FakeResourceGauge`/kernel gates in the same commit) | private-imports + import-contract gate |
| 10 | Crawl-side delegated output signal ignores admits (R7-B1/R8-B1) | `test_crawl_side_delegated_signal_ignores_allowed_jobs` (the delegated `page_count`-only record does **not** advance on a granted-but-uncompleted pass — `allowed_jobs` truthy with `page_count` unchanged produces no `record_progress()`; mutant "record_progress on `allowed_jobs`" reds — R7-B1) | **output-progress** mutation gate (migration unit 8) — distinct from 9.6's decision-identity seam-parity (R8-B2) |

**Non-negotiable wire gates restated**: seam-parity test (§9.6) proves the
old-knob/new-kernel decision identity; the 429 boundary + webhook-width +
`admission_stalled` behaviors are pinned (`test_callerlane_quota_is_external`,
`test_webhook_semaphore_never_429s`, `test_admission_stalled_classification`)
and must stay green through the seam migration.

## Rulings table

| Finding | Ruling | Reason (one line) |
|---|---|---|
| P0-1 ramp ownership | Gate owns ramp privately; caller `CrawlState.ramp` deleted; read-only snapshot + quiescent reset only | Caller-owned ramp (recursive.py:194-197) is the overwrite bug the design kills; structural removal, not a reset guard |
| P0-2 floor vs hard capacity | Capacity wins; floor is a *minimum-progress* law bounded by want & capacity; terminal `AdmissionRefused(IdleReason.OVERSIZE)` when one unit can't fit | A floor-granted item past a hard ceiling is an OOM risk; terminal oversize is the escape hatch, never retry-forever |
| P0-3 lease/release | Every `Granted` carries a lease; exactly-once idempotent, scoped, cancellation-safe, wrong-gate rejected; no TTL/fencing in v1 | Fencing undefined → double accounting; idempotent scope handles success/exception/cancel |
| P0-4 concurrency | `admit` sync/nonblocking/atomic under a state lock; no external code/I/O under lock; versioned wakeup closes lost-wakeup; `Idle` reasons retryable vs terminal | Stateless→stateful model must be pinned; threads safe, events before wait not lost |
| P0-5 scale law | `scale()` = today's `batch_scale` as typed Ratio→Scale; `headroom` = `1-usage` (hard-relative); monotonicity gate-able | Legacy `batch_scale` (pressure_memory.py:81-97) is the law; `headroom` one definition, not two |
| P0-6 per-lane split | Per-lane floors + HEAVY minimum ≥1 when one fits; floor-vs-want clamp `min(want,floor)` | `browser_jobs[:1]` (request_pipeline.py:348) shows total-only floors starve HEAVY |
| P0-7 capacity/cost | `capacity_lane = min(scale_slots, ceiling, want_lane, free//cost)`; free recomputed per admit; `Cost(memory_bytes: Bytes)` | Cost is a byte quantity; a unitless `int` is a category error |
| P0-8 yield ownership | Gate-side, internal, one `_wait_for_progress()`; `paced`→telemetry | One wait site; caller never sleeps; no caller-side park |
| P0-9 gauge failure | Raising gauge → hard-saturated (fail-closed); propagate nothing | Fail-closed doctrine; safe and recoverable |
| P0-10 cold/stale | healthy/unhealthy/cold/stale; cold = bounded probe + no ramp growth coupled to small `initial_ramp`; stale stops admission; MEMORY never cold | Cold CPU returns full scale today (pressure_gates.py:218-230) with bounds only via `concurrency_multiplier`; MEMORY always senses (pressure_memory.py) |
| P1-1 floor_fraction | Retype as validated `FloorDivisor` (int `>= 1` denominator), construction refuses 0 — the v2.1 correction of the v2 `Fraction` (0,1] retype that would have inverted the floor law (B1) | `0`→ZeroDivision, negatives silently floor — raw number crossing the door; a `0 < v <= 1` float divides up |
| P1-2 ramp_multiplier | Retype as validated `Multiplier(≥1)`; batch ≤ `ramp_max`, growth ×multiplier | Multiplier is not a work Count; `Count×Count` is nonsense |
| P1-3 completeness | `__post_init__` enforces `Sensing`/`Allocation`/`Want` completeness; `Budget`→memory-only | Prose completeness is no check; memory is the only defensible byte sum |
| P1-4 `Limits` target≤hard | `__post_init__` rejects swapped pair | A swapped field constructs today; make it unrepresentable |
| P1-5 ride_threshold | `ScaleSource = MinAll | RideCpuWhenFree(threshold)` sum type | Comment-is-not-check; sum type makes the inconsistent combination unrepresentable |
| P1-6 `Granted` zero | `Granted.__post_init__` asserts `allocation.total ≥ 1`; floor formula = property test under mutation | Floor is a tested law; a zero-total `Granted` is a lie unconstructible by type |
| P1-7 `StallGuard(clock=…)` | Drop the param; read the gate's clock + `Progress` | The defaulted `time.monotonic` (admission_stall.py:49) recreates the two-clock bug |
| P1-8 `Instant` | Add monotonic `Instant` for `Clock.now()`, `Progress.last_advance/last_decision` | "Duration is the only time unit" contradicted by `now()->float`; monotonic by contract |
| P1-9 `is_warm` | Keep and consume → feeds cold/stale classification + no-growth-while-cold | A declared-and-never-consumed member is dead design; cold posture needs it |
| Ratio/Scale | Keep separate | Two distinct semantic domains + different monotonic invariants; collapse blurs them |
| Budget scope | Drop general; narrow to memory-only per-lane | Sum-of-bytes across non-byte resources is a category error; memory is the defensible one |
| `Want` ctor | `Want.of`, all lanes, reject empty | `at_least` zero-fills, not lower-bounds |
| `Granted.scale` | Informational only | Caller applies per-lane amounts; release uses lease identity, not caller scale |
| Monotonic time | Kernel timeline always monotonic `Instant`; wall-clock informational | Stall detection and retries must never wall-clock |
| M1 doubles | Migrate as its own unit with named inventory (4+13+harness) | PinnedGate subclasses/skips `__init__` (doubles.py:47-54); under Protocols it's a pure fake |
| M2 `wait_while_saturated` | Absorbed into gate yield; recursive never-park contract survives | No prod path calls it; keep the not-world-park doctrine (recursive harness assert) |
| M3 derivation layer | Stays seam-side in `pressure_sizing.py`, produces LaneSpec inputs | Already sited; "home" = keep seam-side, produce ceiling/cost/mapping |
| M4 loop-lag delta | Preserve multiplicative role via `loop_lag_multiplier`; not a third min-all LIMITS member | `base × lag.factor()` (pressure_gates.py:199) is multiplicative today; record the dispatch-curve delta |
| M5 line drift | Re-pin from live tree pre-deletion; plan's 215/222 correct on this HEAD | String-drift doctrine; drift table re-derived at migration time |
| M6 ratchet/gates | Add `admission` to `SCAN_ROOTS`+`_BUDGETS` in same commit; add import-contract/time-seam source-scan + floor mutation + seam-parity gates | Derived scan surface (`test_scan_roots.py:16`, `test_file_size.py:88-91`) auto-claims new roots; gates not comments |
| M7 release sequencing | rc0 → library → seam migration → pooling spec | Tree ready; CHANGELOG empty |

## Corrections to the combined review (grounded in the live tree)

1. **`recursive.py:215/222` line numbers are correct on the current tree**
   (the review's M5 warned of ~20 line drift). Live read:
   `_resource_admitted_batch` straddles `recursive.py:177-227`; the
   floor-cancel is `recursive.py:215`
   (`batch_size = max(1, int(batch_size*scale))`) and the post-admit
   overwrite `recursive.py:222`
   (`batch_size = admit_http + admit_browser`). v2 keeps the plan's cites;
   the drift table is re-pinned only at deletion time.
2. **The "category error" reading of `headroom` in P0-5 is reframed**: the
   two formulas coexist in the live tree (`headroom_fraction()`,
   `pressure_memory.py:168-178` uses `1-usage` as hard-relative;
   `batch_scale`, `pressure_memory.py:81-97`, is the linear ramp). v2 does
   **not** collapse them; it names `headroom = 1-usage` (hard-relative) and
   keeps the linear ramp as the separate `admission_scale` law. This matches
   reviews 2/4's push-back and is grounded in both functions already being
   distinct.
3. **The review's P0-10 claim of fail-open at startup needs the exact
   coupling named**: the live coupling is `concurrency_multiplier=1` when
   `not cpu.is_warm()` (`recursive.py:192-193`) and `ramp = global_concurrency`
   initial (`recursive.py:407`) — a bounded initial ramp already exists. v2
   hardens this into the `initial_ramp`+`cold_probe`+"no growth while cold"
   triple.
4. **The review's M1 inventory omits `tests/_recursive_harnesses.py:
   ContentionFreeGate`** (a second admission double with a
   `wait_while_saturated` that raises an AssertionError). v2 adds it to the
   inventory and **preserves that assert as the "recursive path never
   world-parks" contract** (§6.5) — the review's M2 missed that a test already
   encodes the no-park doctrine.
5. **The review's M6 ratchet framing is incomplete**: the deeper gate is the
   **derived scan surface** — `test_scan_roots.py:16` pins `SCAN_ROOTS` as a
   4-tuple and `test_file_size.py:88-91` requires
   `set(_BUDGETS) == set(python_bearing_roots())`. A new `packages/core/src/admission/` root
   auto-claims both and **both must change in the same commit**; just adding
   a `_BUDGETS` row without updating the `SCAN_ROOTS` tuple-reds incorrectly.
   v2 states both (plus the jscpd root cross-check, `test_file_size.py:77-85`).