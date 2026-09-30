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
    """

    source: str
    marker: str
    count: int = 1


@dataclass(frozen=True)
class Trips:
    """A violating snippet that must produce exactly ``count`` offenders.

    For a source that is wrong in more than one place: a ``uuid4()`` call *and*
    the ``.hex`` slice on it are two findings, not one. A count of zero is
    rejected — "must not fire" already has a spelling (:attr:`Rule.clean`), and
    allowing a second one for the same fact lets the claim drift between forms
    and turns a mis-edited count into a passing proof.
    """

    source: str
    count: int


Proof = Bites | Trips | str


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
        only place that need can be recorded.
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
        package instead.
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
    clean: Callable[[], Sequence[str]] | None = None
    skip: frozenset[str] = frozenset()
    kwargs_for: Callable[[str], dict[str, object]] | None = None
    proof_path: pathlib.Path = _SYNTHETIC
    scope: Callable[[frozenset[str]], Sequence[tuple[pathlib.Path, ast.Module]]] | None = None

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
        produced = self.bite()
        if isinstance(produced, (str, Bites, Trips)):
            return (produced,)
        return tuple(produced)

    def looksalikes(self) -> tuple[str, ...]:
        """Code that resembles a violation but must not be flagged.

        The half of the proof that stops a detector crying wolf. Without it a
        rule can pass its firing cases by flagging every ``or`` in the tree, and
        the cost of that rule being wrong is paid in false positives until
        somebody deletes it.
        """
        return tuple(self.clean()) if self.clean is not None else ()

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
            found = self.offenders(self._parse(proof.source, kind="bite"), self.proof_path)
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

        if proof.count < 1:
            raise ValueError(
                f"{self.name}: Trips({proof.source!r}) = {proof.count}: a source that must "
                f"not fire belongs in clean, not in trips."
            )
        found = self.offenders(self._parse(proof.source, kind="bite"), self.proof_path)
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
        for proof in self.proofs():
            self._check_one(proof)
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
