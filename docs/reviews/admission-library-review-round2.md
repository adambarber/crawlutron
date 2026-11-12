# Adversarial review round 2 — `admission` v2.1 spec

**Verdict: defects-found (do not implement as written).** Reviewed by
ses_e316b2d73ffe5NBRUrp7boJGQJ against `docs/design/admission-library-v2.md`
(v2.1) and the live tree at main. Round-1 blockers B1/B2 and the M2/M3/M4 seam
fixes are genuinely resolved; 13-count and all other m-claims re-verify live
(4 `pinned_resource_gate` sites, 13 `PinnedGate` constructions,
`ContentionFreeGate` present). Two contradictions remain that an implementer
cannot write from: the door shape (BLOCKER-1) and the `Progress` ordering law
(MAJOR-1), plus MAJOR-2 (ceiling-0 cell) and five MINORs.

## BLOCKER-1 — §4.2/§4.3/§5.4/§8: door shape self-contradictory about `Idle`
and wait ownership

v2.1 changed the door to `async with gate.admit(want) as granted:` (§4.2:
"immediately enters the lease … returns the lease handle to the `with` body"),
but §8 still declares `Admission = Granted | Idle` as the door value ("match on
`Granted()` / `Idle()` directly") and §4.3 has the **caller** park after
receiving `Idle(RETRY)` while §5.4 has the **gate** park internally. Three
mutually inconsistent models:

- **Model A** (§4.2/§5.4): `__aenter__` decides, parks internally until it can
  grant, yields a **live `Granted` lease**; the body never sees an `Idle`.
  Closest to the M2 fix; makes §8's `Idle` result and its reason StrEnum
  unreachable at the door.
- **Model B** (§4.3/§8): `admit` returns `Granted | Idle`, caller matches and
  waits. Then §4.2's "enters the lease" cannot hold for an `Idle` and the
  inseparability commitment is void for every non-granting outcome.
- §5.4 additionally splits the wait owner against §4.3.

Acceptance tests inconsistent with every model: `test_oversize_want_returns_
idle_with_reason_oversize`, `test_oversize_is_not_retryable` (item 3),
`test_idle_wakes_on_release` (item 7) assume caller-visible matchable `Idle`
(Model B); `test_wait_is_bounded_nonblocking` (item 7) cannot be written until
the bound's location is decided (inside `__aenter__` vs caller/body re-entry).

Concrete trace: `want = {HEAVY: 700 MiB}`, `free = 600 MiB`, nothing in flight
must reach terminal `Idle(OVERSIZE)`. Under Model A it parks forever unless a
terminal marker exists — none specified. Under Model B the body receives
`Idle(OVERSIZE)` and must match it, but "release on body exit" applies to a
value that never entered a lease.

**Minimal fix (chosen):** pick Model A. `async with gate.admit(want)` **always
yields a live `Granted`**, parking internally via the single
`_wait_for_progress` when only retryable pressure/stale holds; delete/repeal
§8's `Admission = Granted | Idle` as the **door** type (keep an internal
decision-enum); terminal outcomes (`OVERSIZE`, `STALE`-terminal) surface as a
**typed refusal exception raised from `__aenter__`** — no lease exists when the
raise fires, so no cleanup is needed; `IdleReason` becomes the exception's
closed reason enum (public, matchable by callers that need it). The body never
matches a sum; it holds a lease or the call raises. Rewrite the three `Idle`
acceptance tests to the new surface. State the wait owner once: the gate,
inside `__aenter__`, never the caller.

## MAJOR-1 — §3 `Progress` / §4.4 (M5 residual): the ordering law fails on a
grant

`last_decision` is pinned granting-only, but nothing says a **grant** also
advances `last_advance` (§4.4 advances it only via `record_progress`).
record_progress at t0 → grant at t2 (no record_progress yet) →
`Progress(last_advance=t0, last_decision=t2)` violates `last_advance >=
last_decision` → `__post_init__` refuses construction.

**Fix:** one line in §4.4 — "a granting decision also records `last_advance`
(a `Granted` is progress; on grant, `last_advance = max(last_advance,
last_decision)`)". Law holds by construction; StallGuard keys on
`last_advance` only.

## MAJOR-2 — §1/§5.3/§4.3: `ceiling_lane = 0` breaks the invariant and
escapes every Idle branch

`granted_lane = min(floor_lane, want_lane, ceiling_lane, free//cost_lane)`
zeroes on a zero ceiling, but the OVERSIZE predicate keys only on
`free // cost_lane == 0`. A wanted lane with `ceiling 0` and `free//cost >= 1`
is neither OVERSIZE nor granted → `granted_total == 0` → falls to `Granted`
whose `__post_init__` asserts `total >= 1` — no legal outcome.

**Fix:** fold the ceiling into the fit predicate — OVERSIZE fires when
`min(ceiling_lane, free // cost_lane) == 0` for every wanted lane and nothing
is in flight; define "can fit" once at §1's precedence bullet (`ceiling_lane
>= 1 and free//cost_lane >= 1`); scope `1 <= granted_total` to "a wanted lane
that can fit" — the actual guarantee.

## MINORs

1. **§4.1 ramp oscillation at scale ≈ 1.0** (distinct from m5's floor
   parking): growth once per admission at `scale >= 1.0` (×1.10), decay once
   per sensing AND per release at `scale < 1.0` — a box hovering at target
   thrash-grows/decays. Bounded `[ramp_floor, ramp_max]`, not a correctness
   breach; spec a hysteresis (e.g. growth requires `scale >= 1.0` over a
   window or `. >= 1 - ε`).
2. **§4.2/§4.3 full-grant over-commit + nested admits**: the lease commits the
   full allocation; a body consuming part over-counts in-flight; nested
   `async with gate.admit(...)` inside the body sees the parent's full grant
   and is capacity-starved. State: (a) lease covers the full grant, unused
   allocation is not returned in v1 (accept over-count); (b) nested admits
   prohibited or supported — declare; (c) the plain `threading.Lock` is
   released before the `with` body yields.
3. **§4.3 Idle-condition precedence**: STALE overlaps OVERSIZE and
   RETRY_PRESSURE freely; fail-closed doctrine implies STALE wins (refuse on
   untrustworthy data, retryable). State precedence explicitly
   (recommend STALE-first → OVERSIZE → RETRY_PRESSURE) and pin a combination
   probe.
4. **§2/§9.6 source-scan scope**: the standard `Rule`/`Registry` defaults to
   `app_trees` (crawlutron-only); the top-level `packages/core/admission/**` scan needs an
   explicit descriptor — `scope = lambda *, exclude:
   trees_under(REPO_ROOT / "admission")` — or an implementer sweeps
   `packages/core/crawlutron/admission/` instead (the exact collision the fix gates
   against). Note `app_trees` is not the right default.
5. **§7.2 cold-probe fairness**: the global `cold_in_flight` counter can be
   saturated by cheap GENERAL cold admits, refusing the HEAVY cold probe.
   Coherent as specified; accept it in one clause or add a per-lane floor on
   the probe cap.
6. **m2 residual**: §5.3 still reads `pressure_sizing.py:192-194`; widen to
   `:186-194` (cosmetic, ordered in round 1).

## Verification note

`pinned_resource_gate` is aliased `_pinned_resource_gate` at the 4 call sites;
the count matched regardless. `test_scan_roots`/`_BUDGETS` coupled-derivation
claim verified: `python_bearing_roots()` will auto-claim the new top-level
`admission` root, so `SCAN_ROOTS` and `_BUDGETS` must change in the same commit
as the package (correct as written in §9.6).