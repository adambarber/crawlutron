# Adversarial review round 3 — `admission` v2.2 spec (verification)

**Verdict: defects-found (do not implement as written).** Reviewed by
ses_e315fc472ffe3cGg3w4L4udqgH against `docs/design/admission-library-v2.md`
(full read), the round-1/round-2 findings files, and live-tree grounding.
Model A, the Progress grant-advance, and the ceiling-fit predicate are
correctly landed; every round-1 resolution is INTACT. The blockers are: the
OVERSIZE trace is internally self-contradictory (predicate vs example bullet
vs acceptance item), plus four MAJOR "state-the-answer" gaps and four MINOR
stale-Model-B phrasings.

## BLOCKER — OVERSIZE trace contradiction (§1 vs §10 item 3)

Trace: `want = {HEAVY: 700 MiB}`, `free = 600 MiB`, nothing in flight. §1's
fixed predicate (`min(ceiling_lane, free // cost_lane) == 0` over the *wanted*
lanes) → HEAVY `600//700 = 0` → **raise `AdmissionRefused(OVERSIZE)`** — and
§4.3 rule 2 agrees. But §1's example bullet (lines 118-123) and §10 item 3's
`test_oversize_evaluates_wanted_lanes_only` both still assert "a HEAVY-only
want with `free = 600 MiB` does **not** OVERSIZE" — the round-1 B3 *pre-fix*
conclusion, pasted into a spec whose wanted-lanes predicate inverted it. The
example's own premise ("one can be serviced") is false at 600<700, so the
sentence is internally broken regardless of intent.

**Fix:** flip §1's bullet and §10 item 3 — a HEAVY-only want at free=600 MiB
**raises `AdmissionRefused(OVERSIZE)`** (the wanted HEAVY lane cannot fit);
keep the "zero-want lane never satisfies the predicate" half, which is what B3
actually fixed.

## MAJOR — lease lifecycle: pre-lease (park) cancellation not distinguished (§4.2)

§4.2 "releases exactly once on body exit, exception, or cancellation" conflates
two paths: a cancellation inside `_wait_for_progress()` in `__aenter__` (body
never entered → `__aexit__` not invoked → no lease to release) vs a
cancellation during the body (→ `__aexit__` → must release). The spec states
no `__aexit__` release-guard for `lease is None`, no park-signal-wait cleanup
on pre-lease cancellation, and doesn't say `AdmissionRefused` from
`__aenter__` is not seen by `__aexit__`.

**Fix:** one clause in §4.2 — pre-lease (park) cancellation enters no lease;
`__aexit__` not invoked; release no-ops on `self._lease is None`; lease
(body) cancellation is the only release-on-cancellation; `AdmissionRefused`
raised by `__aenter__` is distinguishable from a body exception by protocol.

## MAJOR — no fairness among concurrent parked admits (§4.3 / §10 item 7)

Two parked admits wake on one release (global versioned wakeup), both
re-decide; one grants, the other re-parks. Correct liveness, but a fresh admit
arriving each release can keep winning and a given admit parks indefinitely.
No FIFO/fair clause anywhere ("progress, not starvation" is the floor-grants
doctrine, not waiter fairness). §7.2 accepts cold-probe starvation explicitly,
so the design accepts unfairness — but has not said so for the admit wait.

**Fix:** one clause in §4.3 wakeup — either bounded-FIFO/fair ordering among
parked admits, or explicit v1 acceptance of temporary unfairness (mirroring
MINOR-5's acknowledgment).

## MAJOR — `Granted` / lease object identity contradictory (§3 vs §4.2 vs §9.1)

§3's complete type surface declares `Granted(allocation, scale, paced)` — no
lease field. §4.2/§9.1 reference `granted.lease()` / "a `Granted.lease`
released twice is idempotent". Either `granted` *is* the lease or *carries*
`.lease`; the type surface doesn't reflect it. Also unstated: must the body
ever call `release()` itself, or is release purely implicit on body exit
(leaving `test_release_is_idempotent`'s caller-callable release ambiguous)?

**Fix:** add `lease: Lease` to `Granted` in §3 (or state plainly that
`granted` is the lease handle); state whether caller-callable `release()` is a
supported early-return or a defensive helper only.

## MAJOR — `progress = grants` can misclassify an in-flight-draining saturated
box as stalled (§4.4 + §0 #4)

When capacity is exhausted by in-flight work actively draining but never
dropping enough to re-grant, releases bump the version yet no grant happens;
`last_advance` stays frozen and `StallGuard` classifies `admission_stalled` at
the 60s hard stop — even though leases are exiting and the box is draining.

**Fix:** one line in §4.4 — either count releases (or in-flight drain) as
progress, or explicitly accept the over-classification as fail-closed.

## MINORs

1. **STALE-park vs RETRY_PRESSURE-park timeout regimes unstated (§4.3 rule
   1/3):** STALE-park must be time-bounded to the remaining `stale_bound`
   (raise on elapse); RETRY_PRESSURE-park is event-bounded (until a
   release/sensing wakeup). With a STALE resource and nothing in flight,
   nothing releases and the version never bumps — the wait ends only via the
   timeout. State that STALE parking uses the optional timeout.
2. **Nested-admit detection mechanism unspecified (§4.2):** the prohibition is
   scoped "inside the same task" (correctly disambiguated from concurrent
   admits), but the mechanism is unstated. Pin a `contextvar`/task-local set
   on lease entry / cleared on `__aexit__` so the prohibition provably doesn't
   forbid concurrent admits and fails loud.
3. **Stale Model-B `Idle(...)` value phrasings:** §4.1 (`Idle(RETRY)` wakes),
   §7.2 (`refused Idle(RETRY_PRESSURE)` — RETRY_PRESSURE is internal parking,
   not a refusal value), §9.2, rulings table (`Idle(OVERSIZE)`), §3 line 207
   (`Admission = Granted | Idle` still listed as a structural sum type in the
   complete surface), §10 item 3 test name (`test_zero_ceiling_lane_surfaces_
   oversize_or_idle` — "or_idle" stale; it raises only). Reconcile all to
   `AdmissionRefused(reason)` or internal-park language.
4. **B-5 severity (from reviewer self-review):** progress=grants is a *stated*
   contract; the MAJOR classification is defensible via the 60s hard-stop
   interaction but could be MINOR. Parent ruling: keep MAJOR (accept-or-state
   must land; it touches the admission_stalled wire behavior).