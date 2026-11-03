"""Detector assertions: bite once, fire once, stay clean."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable

from tests._walk_primitives import REPO_ROOT


def assert_detector(
    detector,
    *,
    path: pathlib.Path,
    fires: dict[str, str] | None = None,
    trips: dict[str, int] | None = None,
    clean: tuple[str, ...] = (),
) -> None:
    """Prove a detector in one call, all claims exact: ``fires`` fires once and
    names its offender, ``trips`` counts offenders (rejecting 0, which belongs in
    ``clean``), and ``clean`` fires none -- all of it normalized to a list.
    """
    for src, why in (fires or {}).items():
        offenders = list(detector(ast.parse(src), path))
        assert len(offenders) == 1, f"expected exactly 1 offender for {src!r}, got {offenders!r}"
        assert why in offenders[0], f"{offenders[0]!r} does not say {why!r}"
    for src, count in (trips or {}).items():
        if count < 1:
            raise ValueError(
                f"trips[{src!r}] = {count}: a source that must not fire belongs in clean, not trips"
            )
        offenders = list(detector(ast.parse(src), path))
        assert len(offenders) == count, f"expected {count} offenders for {src!r}, got {offenders!r}"
    for src in clean:
        offenders = list(detector(ast.parse(src), path))
        assert not offenders, f"fired on the clean shape {src!r}: {offenders!r}"


def assert_fires_once(detector, fires: dict[str, str], path: pathlib.Path) -> None:
    """A ``*_bites`` proof's red half: each source fires the detector, saying why --
    a thin wrapper over :func:`assert_detector`, given its own name because several
    proofs cover only the red half and reading as a wrapper is honest about that.
    """
    assert_detector(detector, path=path, fires=fires)


def assert_stays_clean(detector, clean: tuple[str, ...], path: pathlib.Path) -> None:
    """A ``*_bites`` proof's clean half: sanctioned shapes must not fire."""
    assert_detector(detector, path=path, fires={}, clean=clean)


def assert_detectors_clean(
    tree: ast.Module, *, path: pathlib.Path, detectors: tuple[Callable[[ast.Module], list[str]], ...]
) -> None:
    """Run every ``detector`` on ``tree`` and assert all stay clean.

    Collapses the repeated ``parse -> extend offenders -> assert`` skeleton so a
    single change to the claim shape doesn't duplicate across test files.
    """
    offenders: list[str] = []
    for detector in detectors:
        offenders.extend(detector(tree))
    label = path.relative_to(REPO_ROOT)
    assert not offenders, f"{label}: {offenders}"
