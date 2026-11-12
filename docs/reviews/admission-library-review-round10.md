# Adversarial review round 10 — `admission` v2.9 spec (final verification)

**Verdict: defects-found — two MINORs + one sub-MINOR note; the three round-9
corrections are all genuinely RESOLVED; the full rounds-1–8 regression sweep
is intact.** Reviewer: ses_e313934e8ffetoIN5T3ogrwdnQ. The doc is "one
sentence (in two places) from the implementation brief" — same class of
"state-the-answer" gap rounds 3–6 repeatedly pinned, no design inversion, no
acceptance-row/normative contradiction. B-1 and B-2 are two clauses of ONE
gap: the admission machinery's ultimate-bounding limit for a healthy-sensor
dead loop is unstated where it is granted, and overstated as STALE-bounded
where it parks.

## B-1 (MINOR) — the continuous-grant alternation's crawl-side ultimate bound
is never named

**Trace:** a box alternates grant-passes and parked-passes with no output. On a
grant pass, `__aenter__` advances `last_advance` (grant is progress) and the
seam's line-129 record fires (`allowed_jobs` truthy) — kernel StallGuard
quiet, seam record quiet. On a parked pass, the loop suspends at line 118 and
never reaches line 119's `stalled()`. Neither detector fires — exactly as
R6-B1/R8-B1 rule (delegation boundary restated). The doc never states the
crawl-side ultimate bound that actually terminates such an alternation: no
mention of a crawl/job-level timeout or caller's outer deadline anywhere in §0
non-negotiables or §9 migration units (grep `timeout|deadline|job-level|outer`
returns only Signal event timeout, STALE-park timeout, pytest `timeout=120`,
monotonic "retry deadlines").

**Minimal fix (one sentence in §4.2 after the R8-B1 horizon):** "The ultimate
bound on a continuously-granting / continuously-alternating zero-output loop
is the crawl's own job-level timeout and the caller's outer deadline; the
admission machinery (kernel StallGuard and the delegated seam stop) cannot and
does not bound it."

## B-2 (MINOR) — the "bounded by stale_bound→STALE" claim for parked admits
overstates for a healthy-sensor park

**Trace:** the STALE arm (§4.3 rule 1, §7.2) is keyed on the *sensor* being
stale — "samples missing past the staleness bound." R9-M2's clause (§4.4
638-639) generalizes "a box parked inside the admit (RETRY_PRESSURE park,
§4.3; cold-probe park, §7.2) is bounded by the stale_bound→STALE terminal."
The parent's cold-specific hypothesis — a sensor-HEALTHY box whose cold probe
is merely FULL — is NOT representable: §7.2 defines cold as "no sample yet
(startup) or a sample older than the cold-probe bound," so a healthy sensor is
not cold; a persistently-cold box does raise STALE via §7.2:861. R9-M1's cold
claim is therefore correct. BUT the RETRY_PRESSURE half of R9-M2 overstates: a
box parked RETRY_PRESSURE (capacity exhausted by in-flight leases) on a
*healthy* sensor never satisfies "stale past the staleness bound," so the
STALE terminal cannot fire. If in-flight work never releases (a hanging lease
— the no-TTL ruling at §4.2:434 explicitly rejects TTL/fencing) and the sensor
stays healthy, the box parks at line 118 with no release wakeup, never STALE,
never reaching line 119's 60s stop → **unbounded**.

**Minimal fix (qualify R9-M2's sentence in §4.4):** "bounded by the
`stale_bound`→STALE terminal *provided the resource actually becomes stale*; a
healthy-sensor RETRY_PRESSURE park whose capacity is permanently held by a
never-releasing lease is unbounded in v1 (a consequence of the rejected TTL)
and is bounded only by the caller's own task timeout/deadline."

## B-3 (sub-MINOR) — §0 #4 reachability qualifier

Every analytic mention of `60s|admission_stalled` carries the "fires only when
the loop reaches a zero-admission pass at line 119" qualifier or explicitly
delegates (changelog v2.9, §4.4, §5.4, §7.2, unit 8, R8-B1). The sole
exception is the §0 #4 non-negotiable (168-171) which pins the mechanism
without the qualifier — not a reconstruction risk (it cites the live
`admission_stall.py:25/:78-79`), but a one-phrase pointer makes the chain
airtight.

**Minimal fix:** in §0 #4, add "the stop fires only on a zero-admission pass,
see §4.4".

## Cleared

- R9-M1/M2/M3 all RESOLVED with quoted v2.9 evidence; live anchors confirmed.
- Rounds-1–8 sweep INTACT item-by-item; leftover greps CLEAN; changelog
  verbatim PRESERVED (v2.1–v2.8 byte-matched, v2.9 records the three
  corrections).
- B-4 full-read coherence: no additional contradictions, acceptance-row/
  normative splits, or Model-A coherence breaks; three added clauses mutually
  consistent.

## Gate-readiness

Two one-sentence patches in two places + one pointer, exact text supplied by
the reviewer. After v2.10, the reviewer's own recommendation: a round-11
one-pass verify, and "the document earns all-clear and ships." No downstream
decision impact.