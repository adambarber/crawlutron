# `admission` — a resource-adaptive paced-admission kernel (plan v1)

Status: **reviewed; reconciliation pending.** This document is the design as
proposed; the five external reviews and their synthesis live in
`docs/reviews/admission-library-combined-review.md`. A reconcile agent merges
this plan with the combined review into the hardened v2 specification; the v2
document supersedes this one.

## Why

The flaky-test family that triggered the parallelism investigation
(owner directive 2026-11-12, issue #887) shares one genus: parallel machinery
whose state is process-global or coupling-sensitive, where admission pacing is
split across five cooperating sites with no single owner. The validated
diagnosis (UNDERSTAND + VALIDATE agents):

- `WeightedAdmission.admit` returns `(0,0)` under pinned saturation; the
  recursive path's batch floor (`recursive.py:215`) is cancelled by the
  post-admit overwrite (`recursive.py:222`), so the recursion live-locks until
  the 60s stall guard fails the remaining frontier. The sync path is protected
  by a `saturated_dispatch_floor` the recursive path does not have.
- Admission pacing is not one thing: `pressure_gates` (wait/batch-scale),
  `pressure_sizing` (`WeightedAdmission.admit` + `(0,0)`+yield),
  `recursive._resource_admitted_batch` (re-derives then cancels its floor),
  `admission_stall` (second no-progress policy), `_dispatch_paced` (third,
  sync-only floor).
- Tests drive time by monkeypatching `asyncio.sleep` and injecting a gate
  clock — an `assert waits` can pass on the admit-yield alone, never touching
  `wait_while_saturated`.

## The semantic primitive

> **Admission returns a minimum-progress floor on saturation.**

For any non-empty `Want`, `admit` NEVER returns zero slots: it returns at least
the floor (`max(1, ramp // floor_fraction)`), and it is the sole site that
sleeps/yields. Both dispatch paths consume the one primitive; nothing downstream
can cancel the floor because there is no downstream `batch_size`.

## The library

A generic, typed package independent of the crawler, the API server, and
crawlutron specifics. Zero crawlutron imports (an import-contract gate, not a
comment). Lives at a top-level `packages/core/admission/` package member of the UV workspace.

### Closed vocabularies

- `ResourceId` — `MEMORY`, `CPU`, `LOOP_LAG` (closed StrEnum).
- `LaneId` — `GENERAL`, `HEAVY` (closed StrEnum; crawlutron maps HTTP→GENERAL,
  BROWSER→HEAVY at the seam).

### Value objects (one construction chokepoint each)

`Ratio` ([0,1] fraction), `Scale` (dispatch multiplier [0,1]), `Duration`
(non-negative seconds), `Bytes` (non-negative), `Cost` (positive resource
units), `Count` (non-negative). All frozen, slotted, validated in
`__post_init__`, refusal-shaped literals at the boundary, `__all__` explicit —
the `NormalizedUrl` bar (construction-IS-admission; foreign errors mapped
explicitly; `raw`/`value` as the durable identity; eq/hash on the value).

### Sensing

- `ResourceGauge` (Protocol): `usage() -> Ratio`, `is_warm() -> bool`.
- `Limits(target: Ratio, hard: Ratio)`.
- `Sensing` — one gauge and one limit per resource, complete over `ResourceId`
  (enforced).

### Economics

- `Budget` — process resource budget (total + per-resource reservations).
- `ScaleSource` — `MIN_ALL` | `RIDE_CPU_WHEN_FREE`.
- `LaneSpec(cost, ceiling, scale_source, ride_threshold?)`.
- `PacingPolicy(yield_interval, ramp_max, ramp_multiplier, floor_fraction)`.

### The door

- `Want` (complete over `LaneId`), `Allocation` (complete over `LaneId`).
- `Granted(allocation, scale, paced)` | `Idle()` — closed sum `Admission`.
- `AdmissionGate` — the single decision point; owns capacity, floor, pacing,
  progress; `admit(want) -> Admission`; never zero for non-empty want.
- `StallGuard(gate, bound, clock?)` — reads the gate's progress record only.
- `Clock` / `Sleeper` — the only time seams; no `time.monotonic`, no
  `asyncio.sleep` inside the kernel.

## Crawlutron migration (what thins)

`WeightedAdmission` → `AdmissionGate.admit`; the `recursive.py:215/222` floor
overwrite is deleted; `saturated_dispatch_floor` internalizes into `admit`;
`_ADMIT_YIELD_SECONDS` becomes `PacingPolicy.yield_interval`;
`http_scale_factor` becomes `RIDE_CPU_WHEN_FREE`; `AdmissionStallGuard` becomes
`StallGuard` reading one progress record; the `asyncio.sleep` monkeypatch dies
with `tests/_admission_doubles.py` rewritten around injected clock+sleeper.

Non-negotiables: wire contracts on every public door; the 429 boundary
(`check_concurrency_quota`, anonymous `max_concurrency=4`) stays a CallerQuota
outside the kernel; the webhook semaphore stays an ExecutionWidth that queues
and never 429s; the `admission_stalled` classification and 60s hard stop stay.

## Acceptance bar

The library must be as correct, safe, and buttoned up as
`packages/core/crawlutron/models/normalized_url.py`: construction IS admission, refusal
vocabulary is public (`refusal_literals()`), foreign errors map explicitly,
values are immutable and compare by canonical identity, `__all__` is exact.
The v2 specification adds the runtime state machine the reviews demand
(ramp ownership, floor-vs-capacity precedence, lease/release, concurrency
contract, Idle decision rule) and the acceptance-test program.