"""The proof harness has to have teeth, or consolidating onto it loses proofs.

A harness that under-asserts turns "the detector fires" into a rubber stamp;
so it is tested the way the detectors are -- each refusal proved with a real
failing call."""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.chokepoint_walks import assert_detector, assert_fires_once, assert_stays_clean

PATH = pathlib.Path("synthetic.py")
_SOURCE = "value = 1\n"


def _calls(tree: ast.AST, name: str) -> bool:
    """Whether ``tree`` contains a call to a bare function of that name.

    The predicate every marker detector is really asking; spelling it into each
    one let walks disagree only by how they spelled the question."""
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(tree)
    )


def _fires_on_marker(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A detector whose entire behaviour is 'flag a call named marker'."""
    return [f"{path}:1 marker"] if _calls(tree, "marker") else []


def _never_fires(tree: ast.AST, path: pathlib.Path) -> list[str]:
    return []


def _yields_once(tree: ast.AST, path: pathlib.Path):
    """A generator-returning detector that actually discriminates.

    Detectors are split between ``list[str]`` and generators, so normalization
    lives here once; a proof that ``list()``-items each caller's detector is a
    chokepoint that leaks."""
    if _calls(tree, "marker"):
        yield f"{path}:1 marker"


def _always_fires_twice(tree: ast.AST, path: pathlib.Path) -> list[str]:
    return [f"{path}:1 first", f"{path}:2 second"]


def test_a_detector_that_stops_firing_fails_the_proof() -> None:
    """The harness must reject a detector that returns nothing.

    A hand-rolled ``assert offenders`` proves nothing: a narrowed detector
    satisfies it as long as the source string is gone too."""
    with pytest.raises(AssertionError):
        assert_fires_once(_never_fires, {"marker()\n": "marker"}, PATH)


def test_a_detector_that_fires_twice_fails_the_proof() -> None:
    """Exact cardinality is the point: two hits means the detector over-matches."""
    with pytest.raises(AssertionError):
        assert_fires_once(_always_fires_twice, {"marker()\n": "first"}, PATH)


def test_a_firing_detector_that_omits_the_reason_fails_the_proof() -> None:
    """Firing is not sufficient; the offender must name what it complains about.

    Without the reason half, a detector can start reporting a different, less
    specific finding and every proof still passes."""
    with pytest.raises(AssertionError):
        assert_fires_once(_fires_on_marker, {"marker()\n": "a reason this detector never emits"}, PATH)


def test_a_detector_that_fires_on_a_sanctioned_shape_fails_the_proof() -> None:
    """The clean half has to be able to fail, or it is decorative."""
    with pytest.raises(AssertionError):
        assert_stays_clean(_fires_on_marker, ("marker()\n",), PATH)


def test_assert_detector_runs_both_halves() -> None:
    """The combined entry point is what a bites test calls, so it must gate both.

    The halves fail in opposite directions (over-broad vs inert); ``fires`` is
    keyword-only and REQUIRED, so not declaring a bad source is an explicit
    ``fires={}``."""
    with pytest.raises(AssertionError):
        assert_detector(_never_fires, path=PATH, fires={"marker()\n": "marker"})
    with pytest.raises(AssertionError):
        assert_detector(_fires_on_marker, path=PATH, fires={}, clean=("marker()\n",))


def test_a_generator_returning_detector_is_counted_like_a_list() -> None:
    """Both halves, both detector shapes, same assertions.

    Without normalization the fires half raises ``TypeError`` on ``len()`` and
    the clean half silently passes -- ``not <generator>`` is always True."""
    assert_detector(_yields_once, path=PATH, fires={"marker()\n": "marker"}, clean=(_SOURCE,))
    with pytest.raises(AssertionError):
        assert_detector(_yields_once, path=PATH, fires={"marker()\n": "marker"}, clean=("marker()\n",))
    with pytest.raises(AssertionError):
        assert_detector(_yields_once, path=PATH, trips={"marker()\n": 2})


def test_assert_detector_passes_when_both_halves_hold() -> None:
    assert_detector(
        _fires_on_marker,
        path=PATH,
        fires={"marker()\n": "marker"},
        clean=(_SOURCE,),
    )


def test_a_detector_that_trips_the_wrong_number_of_times_fails_the_proof() -> None:
    """A multi-shape source must trip exactly the number of offenders claimed.

    The count must be exact: under-reporting hides findings and a count of zero
    is a detector gone inert on that shape."""
    with pytest.raises(AssertionError):
        assert_detector(_always_fires_twice, path=PATH, trips={"marker()\n": 3})
    with pytest.raises(AssertionError):
        assert_detector(_always_fires_twice, path=PATH, trips={"marker()\n": 1})


def test_trips_does_not_accept_an_empty_offender_list() -> None:
    """A count of 0 in ``trips`` is a contradiction, not a weaker claim.

    ``clean`` already spells "must not fire"; a second spelling can drift, and
    a mis-edited count would read as a passing proof."""
    with pytest.raises(ValueError, match="clean"):
        assert_detector(_always_fires_twice, path=PATH, trips={"marker()\n": 0})
