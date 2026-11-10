# Governance architecture

crawlutron's safety rules are not conventions enforced by review; they are
live, executable invariants that arm, bite, and are audited for staleness.
This page documents the rule framework, the bite-proof discipline, the
pre-commit hook chain, and the invariant suite — each grounded in the live
tree.

## Rule framework (tests/invariants/, tests/rules)

Every rule is declared as a `Rule` whose detector body keys on names the app
tree defines, plus a proof thunk that must fire. The suite derives every
declared rule from the tree itself (not a hand-maintained module list), so a
left-out module is a rule nothing audits
(`tests/invariants/test_rule_arming.py:62-78`, `declared_rules`). Hand-rolled
detectors registered outside any `Rule(...)` are audited too
(`test_rule_arming.py:112-126`, `hand_rolled_armings`).

## Bite-proof discipline

A rule that matches nothing reports green forever, so every rule must prove
it bites: `test_every_rule_bites` (`test_rule_arming.py:248-259`) executes
every rule's `check_bites()` proof thunk and requires it to fire, naming its
marker; the sweep found at least 80 rules (`test_rule_arming.py:254`). The
arm audit (`test_rule_arming.py:262-269`,
`test_every_rule_is_still_armed_on_a_name_the_app_tree_has`) fails on any
rule keyed on a private name the app tree no longer defines — a renamed-away
detector is the disarmed state, and planting one is how the bite is
demonstrated (`test_rule_arming.py:152-168`, `:171-187`).

Factored spellings are audited by value, not variable name: a rule holding a
module constant is armed on the constant's value
(`test_rule_arming.py:190-199`). Private (underscore) spellings the detector
compares against are collected and checked against `app_names()`
(`test_rule_arming.py:85-96`, `_private_keys`; `:129-145`, `missing_armings`).

## Pre-commit hook chain (.pre-commit-config.yaml)

All gates run whole-tree through `scripts/gate_fanout`, which starts every
gate child at once and lets each wait only on its own child, so wall time is
the slowest gate, not the sum; each child is hard-killed after 60 s
(`scripts/gate_fanout.py:21`). The declared hooks
(`.pre-commit-config.yaml:86-268`):

- `ruff check crawlutron tests scripts` — whole-tree style/safety lint; inline `noqa` is banned by project rule (`:89-97`).
- `ruff format --check crawlutron tests scripts` — formatter as a gate; `check` reports drift instead of rewriting (`:104-112`).
- `basedpyright` whole tree — zero annotation lies; fails close (`:132-140`).
- `jscpd` at 2 lines / 30 tokens, threshold 0 — zero copy/clone tolerance, no exemption path (`:152-163`).
- `helper-clone-sweep` — 2/30 logic clones across helper files the per-tree gate cannot see (`:169-177`).
- `mypy` — `mypy.ini`'s `files` is the scope (`:183-191`).
- `vulture` dead code, min-confidence 90, zero tolerance, no ignore-names/excludes/whitelist (`:205-213`).
- `commit-citation` (commit-msg) — every commit message cites an issue (`:217-227`).
- `coverage` (pre-push) — measured-descriptive surface only (**no threshold, no fail-under, no ratchet** until the owner rules one) (`:244-252`).
- `safety-invariants` — the chokepoint safety predicates, the real architecture gate (`:256-263`).

## Coverage: an explicit stated refusal

No coverage badge or coverage ratchet is claimed. The pre-push `coverage`
hook is measured-descriptive only — no threshold, no fail-under, no ratchet
until the owner rules one (`.pre-commit-config.yaml:244-252`). Coverage is a
measurement, not a safety boundary; the safety boundary is the invariant
suite and the fail-closed SSRF/auth guards.
