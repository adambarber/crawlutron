"""One declaration per property: the rule, its proof, and the two tests they generate.

Every property in this repository was written the same way: a ``_something_offenders``
function that walks the AST and returns offender strings, a ``test_something`` that
asserts the list is empty, and a ``test_something_detector_bites`` that feeds the
detector a snippet of deliberately wrong code and asserts it fires. That is 71
detectors, 93 bite proofs and roughly 10,900 lines of invariant test, and the three
halves drift apart: a detector can be rewritten so the bite no longer fails, and
the bite is a separate function that nothing forces anyone to update.

A :class:`Rule` collapses the triple into one declaration, and the two tests are
generated from the registry rather than written. Two consequences, both of which
are the point:

* **A rule cannot be registered without a bite proof.** ``bite`` has no default,
  so ``Rule(name=..., find=...)`` raises ``TypeError`` at import time. The
  invariant that every detector is known to be capable of firing is enforced by
  the type, not by a reviewer remembering to ask. This is the failure the repo
  actually had: bite proofs existed, but nothing tied a detector to one.
* **The tests cannot drift from the rules.** There is one ``test_no_rule_violations``
  and one ``test_every_rule_bites``, parametrized over the registry. Renaming a
  rule or adding one changes the parametrization; there is no hand-written gate to
  leave behind pointing at the old name.

The bite proof parses one small synthetic source. It deliberately does not mutate
production code: a test that edits ``app/`` to prove a detector works cannot run
under ``-n auto`` alongside the suite that is reading those same files, and the
detector's job is to decide about an AST, not to be surprised by a file's name.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from tests.chokepoint_walks import REPO_ROOT, SANCTIONED, app_trees, relative_posix
from tests.matching import Matcher

#: Where a bite proof's synthetic module is pretended to live. The path is part of
#: what a detector may legitimately inspect (a rule scoped to one directory reads
#: it), so it has to name a real directory rather than a bare basename, and it has
#: to be absolute because offenders are reported relative to the repo root.
#: Individual rules override it with :attr:`Rule.proof_path` when a detector
#: resolves imports or package structure relative to where the module sits.
_SYNTHETIC = REPO_ROOT / "app" / "_synthetic.py"

#: ``(tree, path, **per_module) -> offender strings``. The trailing keyword
#: arguments are the ones a rule supplies through :attr:`Rule.kwargs_for`; a
#: detector that takes none simply ignores them, which is what lets a plain
#: two-argument walk be swept through the same path as a parameterized one.
Detector = Callable[..., Iterable[str]]


@dataclass(frozen=True)
class Bites:
    """A violating snippet that must produce ``count`` offenders, naming ``marker``.

    The strongest of the three proof shapes: it pins *how many* findings the shape
    is worth and *what* the message has to say. A detector that matches everything
    satisfies "it fired", so requiring an exact count and a specific word is what
    turns a proof into a specification.

    ``count`` defaults to 1, which is the right default for a rule with a single
    natural finding. A shape wrong in two places -- a path written and a path
    unlinked -- is two findings, and pinning the count without a ``Trips`` beside
    it means either the framework cannot express both halves or the migration drops
    one. Nearly every bite proof in the invariant suite asserts a count *and* a
    marker, so this is the form the framework actually has to carry.

    The marker is checked against a real offender rather than against the first
    one, because the two claims fail independently: a detector can hit the count
    while every message it emits has decayed to a bare path, and that is how a gate
    stops explaining itself without ever going red.

    ``at`` places this one proof in a module other than the rule's default. It is
    per proof rather than per rule because the claim itself is per proof: a detector
    keyed on a file's path has to be *proved* at that path and *proved quiet* at
    another, and a single rule-level path could state only one of those halves. That
    asymmetry is the detector's entire content -- one ignoring paths would fire on
    the sanctioned code too -- so whichever half the shared path dropped would go
    unwatched.
    """

    source: str
    marker: str
    count: int = 1
    at: pathlib.Path | None = None


@dataclass(frozen=True)
class Trips:
    """A violating snippet that must produce exactly ``count`` offenders.

    For a source that is wrong in more than one place: a ``uuid4()`` call *and*
    the ``.hex`` slice on it are two findings, not one. A count of zero is
    rejected — "must not fire" already has a spelling (:attr:`Rule.clean`), and
    allowing a second one for the same fact lets the claim drift between forms
    and turns a mis-edited count into a passing proof.

    ``at`` places this one proof in a module other than the rule's default, for the
    reason spelled on :attr:`Bites.at`: a detector keyed on a file's path has to be
    proved both where it fires and where it must not, and those are two paths.
    """

    source: str
    count: int
    at: pathlib.Path | None = None


@dataclass(frozen=True)
class Silent:
    """A source that must produce NO offender, optionally at a named module.

    The shape a path-keyed detector's clean half has: it must be proved quiet
    somewhere OTHER than where it fires, because "correct" and "correct in this
    file" are different claims and only the second one is the property. A ``clean``
    string cannot state that -- it is always run at the rule's one proof path -- so
    without this a detector whose entire content is a path comparison could be
    proved only in the half that matters less.

    It is a proof rather than a ``Trips`` of count zero on purpose: the count is
    the claim here, so spelling it as a count invites a later edit to make it one.
    """

    source: str
    at: pathlib.Path | None = None


Proof = Bites | Trips | Silent | str


def _flat(produced: Proof | Sequence[Proof]) -> tuple[Proof, ...]:
    """A proof thunk's items as a tuple, whether it returned one or several.

    The single-proof form is a convenience in the declaration, never a difference
    in what is proved, so every consumer that wants to inspect all of them goes
    through here rather than re-deriving the one-or-many branch.
    """
    if isinstance(produced, (str, Bites, Trips, Silent)):
        return (produced,)
    return tuple(produced)


def _relative(path: pathlib.Path) -> str:
    """``path`` as a repo-relative posix string, or unchanged if it lies outside.

    :func:`relative_posix` deliberately raises for a path the repo does not own,
    which is right for a sweep over ``app/`` but not for the synthetic paths a
    proof runs against. Scope has to degrade to "matches nothing" there, not blow
    up, or a rule could not be proved at all.
    """
    try:
        return relative_posix(path)
    except ValueError:
        return path.as_posix()


@dataclass(frozen=True)
class Rule(Matcher):
    """One property, the detector that decides it, and the proof it can fire.

    :param find: ``(tree, path) -> offender strings``. An empty iterable means the
        property holds for that file.
    :param bite: ``() -> proof`` (or several) for code that **violates** the
        property: a bare source, a :class:`Bites` marker check, or a :class:`Trips`
        count. Required, with no default, so a rule without a proof cannot be
        constructed.
    :param clean: ``() -> lookalikes`` that resemble a violation but must be spared.
        Optional, and omitted far more often than not — but a rule with a broad
        trigger (anything containing an ``or``) needs it, and the declaration is the
        only place that need can be recorded. A rule whose detector keys on a path
        is the one case where omitting it is not merely allowed but wrong: it is
        proved at the path it governs, and without a :class:`Silent` at another it
        has never been shown to spare the other ninety files. That is a missing
        proof, not a missing test, so it is refused at construction.
    :param premise: ``() -> None`` asserting the rule's trigger set is non-empty.
        Optional, and required in substance by the minority of rules that DERIVE
        what they look for — the sync methods of a store, the fields of a dataclass,
        the module names in a tree — rather than naming it as a literal. A derived
        trigger that came back empty would leave the detector matching nothing,
        which reads as a property holding. The check belongs in the declaration
        rather than in an ``assert`` inside the detector: there it is one line
        among the walk's own logic, and ``python -O`` removes it.
    :param excludes: basenames sanctioned as the rule's own owner, excluded from the
        clean check for the same reason the owner of a chokepoint is.
    :param skip: repo-relative paths exempt from this rule. Separate from
        :attr:`excludes` because a chokepoint is usually named by its path: two
        modules may share a basename, and a basename exclusion written for one of
        them silently excuses the other.
    :param kwargs_for: per-module keyword arguments for :attr:`find`, keyed on the
        repo-relative path. A rule whose detector takes parameters declares here
        which module gets which, so "which path holds the definition" is a fact of
        the rule rather than a decision re-made at every call site.
    :param scope: ``(*, exclude) -> trees`` naming the tree this rule governs.
        Defaults to :func:`app_trees`, which is right for most rules and silently
        wrong for the rest: a rule about the *test* tree -- no test may write into
        ``app/``, no test may import a sibling helper by bare name -- swept
        production code, found nothing, and reported green. That is the failure a
        registry hides best, because a rule enforcing nothing looks exactly like a
        rule holding. The walker is handed the rule's own exclude set, so a scope
        can honour the owner exemption rather than being handed a name it has no
        way to apply.
    :param proof_path: where a bite proof's synthetic module is pretended to live.
        Defaults to the shared ``app/_synthetic.py``; a detector that resolves
        imports or package structure from the path needs a path inside the right
        package instead. A rule keyed on ONE module's path states it per proof with
        ``at=``, because such a rule must be proved both where it fires and where it
        must stay quiet -- two paths, so one rule-level path could hold only one
        half and the other would go unwatched.
    :param summary: one line naming the property, used in failure text. Prefer the
        property sentence to the mechanism: "every URL egress door applies the
        refusal" tells a reader what is wrong, "calls safe_public_url_or_none"
        tells them how it was decided.
    """

    name: str
    find: Detector
    bite: Callable[[], Proof | Sequence[Proof]]
    summary: str = ""
    excludes: frozenset[str] = field(default=SANCTIONED)
    clean: Callable[[], Sequence[str | Silent]] | None = None
    premise: Callable[[], None] | None = None
    skip: frozenset[str] = frozenset()
    kwargs_for: Callable[[str], dict[str, object]] | None = None
    proof_path: pathlib.Path = _SYNTHETIC
    scope: Callable[[frozenset[str]], Sequence[tuple[pathlib.Path, ast.Module]]] | None = None

    def __post_init__(self) -> None:
        self._require_a_path_halved_proof()

    def _require_a_path_halved_proof(self) -> None:
        """A rule proved at a path it does not own must also be proved elsewhere.

        ``at`` exists so a path-keyed detector can be proved in both halves: it
        fires in its own module, and is silent everywhere else. Declaring the first
        half alone is the failure this refuses -- the rule looks fully proved while
        the sweep that matters (the ninety other files, which the ``path != owner``
        guard makes the detector ignore) has never been run once.

        Checked against the proofs rather than by inspecting the detector, because
        "is this detector path-keyed" is a question about a function's body that no
        caller can answer. What a caller CAN state is which path it proved, and that
        is the fact worth holding them to.
        """
        placed = {proof.at for proof in self.proofs() if getattr(proof, "at", None)}
        if not placed:
            return
        elsewhere = {self._where(item) for item in self._clean_items() if isinstance(item, Silent)}
        if elsewhere - placed:
            return
        raise TypeError(
            f"{self.name}: a bite proof places this rule at {sorted(str(p) for p in placed)}, "
            f"so it needs a Silent proving it stays quiet somewhere else. A detector that "
            f"ignores paths it is not meant to own is the whole content of a path-keyed "
            f"rule, and without the second half that half is never run."
        )

    def trees(self) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
        """The tree this rule governs, with its own owner exemptions applied.

        One definition rather than a ``app_trees(exclude=...)`` written at each
        call site, because the two call sites -- :meth:`scan` and the registry's
        combined sweep -- are exactly where a scope would be honoured by one and
        dropped by the other.
        """
        return (self.scope or app_trees)(exclude=self.excludes)

    def _run(self, tree: ast.Module, path: pathlib.Path) -> list[str]:
        """Apply the rule's per-module parameters, then the detector."""
        per_path = self.kwargs_for(_relative(path)) if self.kwargs_for is not None else {}
        return list(self.find(tree, path, **per_path))

    def offenders(self, tree: ast.Module, path: pathlib.Path) -> list[str]:
        return self._run(tree, path)

    def matches(self, actual: tuple[ast.Module, pathlib.Path]) -> bool:
        """A ``(tree, path)`` pair satisfies the rule when nothing fires on it."""
        tree, path = actual
        return not self.offenders(tree, path)

    def describe(self, actual: tuple[ast.Module, pathlib.Path]) -> str:
        tree, path = actual
        found = self.offenders(tree, path)
        wanted = self.summary or self.name
        return f"{path}: expected {wanted}, found {len(found)}: {'; '.join(found)}"

    # -- the clean half ---------------------------------------------------

    def violations(self, trees: Sequence[tuple[pathlib.Path, ast.Module]]) -> list[str]:
        """Every offender across ``trees``, each naming the file it came from.

        The rule's own ``excludes`` and ``skip`` are applied here rather than only
        by :meth:`scan`. A caller that hands a rule a hand-built list would otherwise
        be able to drive it outside its sanctioned scope, which would make the
        scope a property of the call site instead of of the rule -- and a scope
        that lives at the call site is a scope that drifts.
        """
        found: list[str] = []
        for path, tree in trees:
            if path.name in self.excludes or _relative(path) in self.skip:
                continue
            found.extend(self._run(tree, path))
        return found

    def check_clean(self, trees: Sequence[tuple[pathlib.Path, ast.Module]]) -> None:
        """Assert the property holds across every tree not sanctioned for this rule."""
        found = self.violations(trees)
        if not found:
            return
        wanted = self.summary or self.name
        raise AssertionError(f"{len(found)} violation(s) of {self.name} ({wanted}):\n  " + "\n  ".join(found))

    def scan(self) -> None:
        """Check the property across the tree this rule declares."""
        self.check_clean(self.trees())

    # -- the bite half ----------------------------------------------------

    def proofs(self) -> tuple[Proof, ...]:
        """Every violating snippet this rule claims its detector rejects.

        A rule usually has more than one way to be wrong — the same fallback
        spelled against the alias and against the root function, say — and a
        single proof would leave the second spelling unwatched.
        """
        if not callable(self.bite):
            raise TypeError(
                f"{self.name}: bite must be a callable returning a proof, not a "
                f"{type(self.bite).__name__}. It is a thunk so that a registry of "
                f"seventy-odd rules does not build every proof at import time."
            )
        return _flat(self.bite())

    def looksalikes(self) -> tuple[str, ...]:
        """Code that resembles a violation but must not be flagged.

        The half of the proof that stops a detector crying wolf. Without it a
        rule can pass its firing cases by flagging every ``or`` in the tree, and
        the cost of that rule being wrong is paid in false positives until
        somebody deletes it.

        A :class:`Silent` in ``clean`` is not a lookalike and is not flattened
        here -- it is a claim about a specific module, so it stays a proof and
        ``check_bites`` runs it. Keeping the two apart is the point: a lookalike
        promises "spared everywhere", and folding a path-specific claim into that
        tuple would quietly assert it somewhere the rule does not even run.
        """
        return tuple(item for item in self._clean_items() if isinstance(item, str))

    def _clean_items(self) -> tuple:
        """``clean``'s declared items. It is a thunk, so it is called here.

        Flattened once so both consumers -- the lookalike half and the
        ``Silent`` half -- read the same list, rather than each re-invoking a
        lambda that builds every proof in the rule.
        """
        return tuple(self.clean()) if self.clean is not None else ()

    def _where(self, proof: Proof) -> pathlib.Path:
        """Where one proof runs: its own ``at``, else the rule's shared path.

        The lookalike half has no override because "must not fire" is not a claim
        about a particular module -- a lookalike is spared everywhere the rule
        runs, which is the whole point of it.
        """
        return getattr(proof, "at", None) or self.proof_path

    def _parse(self, source: str, *, kind: str) -> ast.Module:
        try:
            return ast.parse(source)
        except SyntaxError as exc:
            raise AssertionError(
                f"{self.name}: its {kind} proof does not parse ({exc}). A proof that cannot "
                f"be parsed proves nothing, so this is a failure of the proof, not of the rule."
            ) from exc

    def _check_one(self, proof: Proof) -> None:
        """Run a single proof and explain, in the rule's own words, what it wanted."""
        if isinstance(proof, str):
            found = self.offenders(self._parse(proof, kind="bite"), self.proof_path)
            if found:
                return
            raise AssertionError(
                f"{self.name}: the bite proof did NOT fire on {proof!r}. This rule is "
                f"enforcing nothing. Either the detector is too weak or the proof no "
                f"longer violates the property."
            )

        if isinstance(proof, Bites):
            found = self.offenders(self._parse(proof.source, kind="bite"), self._where(proof))
            if not found:
                raise AssertionError(
                    f"{self.name}: the bite proof did NOT fire on {proof.source!r}. This rule "
                    f"is enforcing nothing. Either the detector is too weak or the proof no "
                    f"longer violates the property."
                )
            if len(found) != proof.count:
                raise AssertionError(
                    f"{self.name}: expected {proof.count} offender(s) for {proof.source!r}, got "
                    f"{len(found)}: {found!r}. A detector that fires several times on one "
                    f"shape is usually over-broad, and the lookalike proofs exist to catch "
                    f"that — check them before widening this rule."
                )
            named = [hit for hit in found if proof.marker in hit]
            if not named:
                raise AssertionError(
                    f"{self.name}: no offender for {proof.source!r} says "
                    f"{proof.marker!r} — got {found!r}. A proof that only checks firing "
                    f"is satisfied by a detector whose message has decayed into a generic "
                    f"string, which is how a gate stops explaining itself without failing."
                )
            return

        if isinstance(proof, Silent):
            found = self.offenders(self._parse(proof.source, kind="bite"), self._where(proof))
            if found:
                raise AssertionError(
                    f"{self.name}: {proof.source!r} must not fire at {self._where(proof)}, got "
                    f"{found!r}. A path-keyed rule is 'correct in this module', so its clean "
                    f"half has to be proved somewhere other than where it fires."
                )
            return

        if proof.count < 1:
            raise ValueError(
                f"{self.name}: Trips({proof.source!r}) = {proof.count}: a source that must "
                f"not fire belongs in clean, not in trips."
            )
        found = self.offenders(self._parse(proof.source, kind="bite"), self._where(proof))
        if len(found) != proof.count:
            raise AssertionError(
                f"{self.name}: expected {proof.count} offenders for {proof.source!r}, got "
                f"{len(found)}: {found!r}"
            )

    def check_bites(self) -> None:
        """Assert the detector rejects what the rule forbids and spares what it resembles.

        The half that makes the rule worth having. Without it a detector that
        silently matches nothing — a typo in a callee name, a walk that returns
        early — reads as a passing property, which is the worst way for a gate to
        lie: green, and enforcing nothing.
        """
        if self.premise is not None:
            self.premise()
        for proof in self.proofs():
            self._check_one(proof)
        for item in self._clean_items():
            if isinstance(item, Silent):
                self._check_one(item)
        for source in self.looksalikes():
            found = self.offenders(self._parse(source, kind="clean"), self.proof_path)
            if found:
                raise AssertionError(
                    f"{self.name}: fired on a lookalike that must be spared: {source!r} -> "
                    f"{'; '.join(found)}. A detector that flags innocent code gets deleted, "
                    f"and the property it was written to hold goes with it."
                )

    def __repr__(self) -> str:
        return f"Rule({self.name!r})"


@dataclass(frozen=True)
class Registry:
    """A named collection of rules, so a file can own one without owning the tests."""

    rules: tuple[Rule, ...]

    def scan(self) -> None:
        for rule in self.rules:
            rule.scan()

    def check_all_clean(self) -> None:
        """Every rule at once, so one run reports every violation rather than the first.

        Running rules in a loop inside a single test rather than relying on
        parametrization is deliberate: a rule that fails should not hide the other
        twenty that also fail. ``raise`` on the first would make fixing a broken
        tree a sequence of one-line changes instead of one pass.
        """
        failures: list[str] = []
        for rule in self.rules:
            found = rule.violations(rule.trees())
            if found:
                failures.append(f"  {rule.name}: {len(found)} violation(s)\n    " + "\n    ".join(found))
        if failures:
            raise AssertionError(f"{len(failures)} rule(s) violated:\n" + "\n".join(failures))

    def check_all_bite(self) -> None:
        failures: list[str] = []
        for rule in self.rules:
            try:
                rule.check_bites()
            except AssertionError as exc:
                failures.append(f"  {exc}")
        if failures:
            raise AssertionError(
                f"{len(failures)} rule(s) have no working bite proof:\n" + "\n".join(failures)
            )

    def __iter__(self) -> Iterator[Rule]:
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


def rule_ids(rule: Rule) -> str:
    """pytest id for one rule: its name, plus its bite when that is what failed."""
    return rule.name


def registry_ids(registry: Registry) -> list[str]:
    """pytest ids for a whole registry."""
    return [rule.name for rule in registry]
