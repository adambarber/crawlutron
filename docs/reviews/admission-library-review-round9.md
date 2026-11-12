# Adversarial review round 9 — `admission` v2.8 spec (final verification)

**Verdict: defects-found — three MINORs; two of the three round-8 corrections
RESOLVED (R8-B1, R8-B2), one PARTIAL (R8-B3).** Reviewed by
ses_e313d76a8ffeYmrNZP4g2ce4g3 against `docs/design/admission-library-v2.md`
(v2.8, ~1105 lines) with live-tree verification (`admission_stall.py:119/128/129`
confirmed; `recursive.py:344` confirmed as the sole `page_count` mutation,
reached from `_commit_recursive_result`, incremented for every non-transient
result including permanent failures). Nothing blocks shipping; three
doc-coverage gaps mean "not as-is." All findings are wording/explicitness gaps
for the implementer — none invert a normative decision or an acceptance row.

## MINOR-1 — Cold no-admission never reaches the 60s stop; the doc never says so

**Trace (live `admission_stall.py:114-130` against §7.2):** the kernel's admit
parks internally inside `__aenter__` (RETRY_PRESSURE event-bounded park §4.3;
cold-probe park §7.2 when `cold_in_flight >= cold_probe`). The loop's
`allowed_jobs = await stages.start_runner(...)` at line 118 suspends on that
park and does not return until a grant or terminal raise. A box that never
admitted (cold starvation) is suspended at line 118 — never reaches line 119,
and `stalled()` does not fire after 60s. Termination for cold starvation is
§7.2's `stale_bound`→STALE bound, not the 60s `admission_stalled` stop.
Consistent with round-1 MINOR-5 cold-lane-starvation acknowledgment — but the
doc never states it; §4.2's horizon and §0 #4's "60s hard stop stays" read as
if a no-progress cold box trips.

**Minimal fix:** one clause in §7.2 (or §4.2): "A cold no-admission pass parks
at `admission_stall.py:118` inside the admit and does not reach the
`stalled()` check; cold starvation terminates via the `stale_bound`→STALE
bound, never the 60s `admission_stalled` stop."

## MINOR-2 — Parked-stall (RETRY_PRESSURE, no releases/grants): neither
detector fires; coverage gap unstated

**Trace:** a box parked RETRY_PRESSURE — kernel StallGuard keys on
`last_advance` going stale but nothing in the kernel invokes it to cancel the
park (§4.3: event-bounded "no wall deadline"; §4.4: parks update neither
field); crawl-side, the loop is awaited at line 118, so line 119 is never
reached. **During the park, neither detector fires.** Escape requires a
release or sensing version-bump re-decide that surfaces STALE. The
"never wakes" sub-case parks indefinitely — the 60s #4 non-negotiable does not
hold for it. Root for MINOR-1+2: the doc absorbs the recursive sleep(0)/park
into the gate (§5.4) but never reconciles that this collapses the reachability
of the "zero-admission pass" kept as the 60s-stop trigger — post-migration the
line-119 branch is fillable only by NON-admission empties (e.g. robots filter
in `admit_recursive_batch`), never by the admission gate returning 0.

**Minimal fix:** one clause in §4.4 (or §7.2): "A box parked inside the admit
(RETRY_PRESSURE/cold) is bounded by the `stale_bound`→STALE terminal, not the
60s `admission_stalled` stop, which fires only when the loop reaches a
zero-admission pass at line 119."

## MINOR-3 — R8-B3 checklist miss: batch-synchronous duplicated across 4.2 AND
unit 8; `:128` anchor absent

The writer's own authoritative checklist required "once (4.2 or unit 8)" and
the `recursive.py:344` + `admission_stall.py:128/129` anchors. The property is
stated correctly but in BOTH sections; the doc cites `:129` but never `:128`
(the `await commit_batch`). Duplication benign (both phrasings accurate);
missing `:128` cosmetic since `:344` + `:129` pin the claim — but a checklist
miss under the "or = strictly-one" reading.

**Minimal fix:** drop the unit-8 sentence (reference §4.2) or add
"(`admission_stall.py:128→129`)" to the 4.2 anchor.

## Cleared

- R8-B1 horizon RESOLVED (both places, byte-matched to live line 119).
- R8-B2 seam scope RESOLVED (0 grep hits for kernel-consumes; row-10 relabel
  exact).
- Rounds-1–7 regression sweep INTACT; leftover greps CLEAN; changelog-verbatim
  PRESERVED.
- Two-stall-detector separation coherent (kernel gate-liveness vs seam
  engine-output); Model-A coherence intact across 14 rounds;
  `page_count`-counts-failures claim byte-accurate.

## Gate-readiness

Three one-pass patches (two reachability-explicitness clauses, one
dedup/anchor fix) with no downstream decision impact. The reviewer's explicit
position: "Apply them and this is the implementation brief."