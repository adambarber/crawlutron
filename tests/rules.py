"""One declaration per property: the rule, its proof, and the two tests it generates.

A :class:`Rule` collapses detector + bite + test into one declaration because
the three halves drift apart."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from tests.chokepoint_walks import REPO_ROOT, SANCTIONED, app_trees, relative_posix

#: Where a bite proof's synthetic module is pretended to live.
_SYNTHETIC = REPO_ROOT / "app" / "_synthetic.py"

#: ``(tree, path, **per_module) -> offender strings``.
Detector = Callable[..., Iterable[str]]


@dataclass(frozen=True)
class Bites:
    """A violating snippet that must produce ``count`` offenders, naming ``marker``.

    The strongest proof: it pins how many findings a shape is worth and what the
    message says. ``at`` places one proof in another module."""

    source: str
    marker: str
    count: int = 1
    at: pathlib.Path | None = None


@dataclass(frozen=True)
class Trips:
    """A violating snippet that must produce exactly ``count`` offenders.

    For a source wrong in more than one place; ``at`` places the proof elsewhere,
    as on :attr:`Bites.at`."""

    source: str
    count: int
    at: pathlib.Path | None = None


@dataclass(frozen=True)
class Silent:
    """A source that must produce NO offender, optionally at a named module.

    The path-keyed detector's clean half: proved quiet elsewhere than where it
    fires."""

    source: str
    at: pathlib.Path | None = None


Proof = Bites | Trips | Silent | str


def _flat(produced: Proof | Sequence[Proof]) -> tuple[Proof, ...]:
    """A proof thunk's items as a tuple, whether it returned one or several.

    Every consumer wanting all items goes through here rather than re-deriving
    the one-or-many branch."""
    if isinstance(produced, (str, Bites, Trips, Silent)):
        return (produced,)
    return tuple(produced)


def _relative(path: pathlib.Path) -> str:
    """``path`` as a repo-relative posix string, or unchanged if it lies outside.

    :func:`relative_posix` raises for a path the repo does not own, but a proof's
    synthetic path must degrade to "matches nothing", not blow up."""
    try:
        return relative_posix(path)
    except ValueError:
        return path.as_posix()


@dataclass(frozen=True)
class Rule:
    """One property, the detector that decides it, and the proof it can fire.

    :param find/bite/premise: offender strings (empty = holds); required proof;
    derived set non-empty. :param excludes/skip: owner basename; exempt paths."""

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
    #: What a detector may legitimately inspect is passed as ``(*, exclude)``, so the ``...`` is load-bearing:
    #: a positional reading rejected every scope in the tree.
    scope: Callable[..., Iterable[tuple[pathlib.Path, ast.Module]]] | None = None

    def __post_init__(self) -> None:
        self._require_a_path_halved_proof()

    def _require_a_path_halved_proof(self) -> None:
        """A rule proved at a path it does not own must also be proved elsewhere.

        ``at`` exists so a path-keyed detector is proved in both halves; declaring
        one half alone is the failure this refuses."""
        placed = {proof.at for proof in self.proofs() if not isinstance(proof, str) and proof.at}
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

        One definition rather than calls at each site; built here as a tuple so a
        caller re-reads the same trees even if the files move underneath."""
        return tuple((self.scope or app_trees)(exclude=self.excludes))

    def _run(self, tree: ast.Module, path: pathlib.Path) -> list[str]:
        """Apply the rule's per-module parameters, then the detector."""
        per_path = self.kwargs_for(_relative(path)) if self.kwargs_for is not None else {}
        return list(self.find(tree, path, **per_path))

    def offenders(self, tree: ast.Module, path: pathlib.Path) -> list[str]:
        return self._run(tree, path)

    # -- the clean half ---------------------------------------------------

    def violations(self, trees: Sequence[tuple[pathlib.Path, ast.Module]]) -> list[str]:
        """Every offender across ``trees``, each naming the file it came from.

        Excludes and skips apply here too: a hand-built list must not drive a rule
        outside its sanctioned scope."""
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

        A rule usually has more than one way to be wrong; a single proof would leave
        the second spelling unwatched."""
        if not callable(self.bite):
            raise TypeError(
                f"{self.name}: bite must be a callable returning a proof, not a "
                f"{type(self.bite).__name__}. It is a thunk so that a registry of "
                f"seventy-odd rules does not build every proof at import time."
            )
        return _flat(self.bite())

    def looksalikes(self) -> tuple[str, ...]:
        """Code that resembles a violation but must not be flagged.

        Without it a rule could pass its firing cases by flagging every ``or`` in
        the tree; a :class:`Silent` is a per-module claim, not flattened here."""
        return tuple(item for item in self._clean_items() if isinstance(item, str))

    def _clean_items(self) -> tuple[str | Silent | Bites | Trips, ...]:
        """``clean``'s declared items; it is a thunk, so it is called here.

        Flattened once so both consumers read the same list."""
        return tuple(self.clean()) if self.clean is not None else ()

    def _where(self, proof: Proof) -> pathlib.Path:
        """Where one proof runs: its own ``at``, else the rule's shared path.

        The lookalike half has no override -- a lookalike is spared everywhere."""
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

        Without it a detector that silently matches nothing reads as a passing
        property -- the worst way for a gate to lie."""
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

        A loop inside one test (not parametrization) keeps one failure from hiding
        the other twenty."""
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
