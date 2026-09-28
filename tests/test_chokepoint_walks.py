"""The proof harness has to have teeth, or consolidating onto it loses proofs.

`assert_fires_once` / `assert_stays_clean` / `assert_detector` are how every
AST detector in this tree is shown to still detect. That makes them the one piece
of test code whose failure mode is silent in the worst direction: a harness that
under-asserts turns "the detector fires" into a rubber stamp, and every detector
proof written against it still goes green while the detector has stopped working.

So the harness is tested the way the detectors are -- each of its four refusals
is proved with a real failing call, not described in a docstring.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from tests.chokepoint_walks import assert_detector, assert_fires_once, assert_stays_clean

PATH = pathlib.Path("synthetic.py")
_SOURCE = "value = 1\n"


def _fires_on_marker(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A detector whose entire behaviour is 'flag a call named marker'."""
    return (
        [f"{path}:1 marker"]
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "marker"
            for node in ast.walk(tree)
        )
        else []
    )


def _never_fires(tree: ast.AST, path: pathlib.Path) -> list[str]:
    return []


def _yields_once(tree: ast.AST, path: pathlib.Path):
    """A generator-returning detector that actually discriminates.

    Detectors in this tree are split between ``list[str]`` and generators, which
    is why the hand-rolled proofs each had to remember ``list(...)`` before they
    could count. A proof helper that requires every caller to normalize its own
    detector is a chokepoint that leaks: the day a detector is written as a
    generator, every proof calling it raises TypeError instead of running.
    Normalization belongs here, once.
    """
    if any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "marker"
        for node in ast.walk(tree)
    ):
        yield f"{path}:1 marker"


def _always_fires_twice(tree: ast.AST, path: pathlib.Path) -> list[str]:
    return [f"{path}:1 first", f"{path}:2 second"]


def test_a_detector_that_stops_firing_fails_the_proof() -> None:
    """The harness must reject a detector that returns nothing.

    This is the assertion a hand-rolled ``assert offenders`` gets wrong: it
    proves nothing about WHY, and a detector narrowed to nothing still satisfies
    it as long as the source string happens to be gone too.
    """
    with pytest.raises(AssertionError):
        assert_fires_once(_never_fires, {"marker()\n": "marker"}, PATH)


def test_a_detector_that_fires_twice_fails_the_proof() -> None:
    """Exact cardinality is the point: two hits means the detector over-matches."""
    with pytest.raises(AssertionError):
        assert_fires_once(_always_fires_twice, {"marker()\n": "first"}, PATH)


def test_a_firing_detector_that_omits_the_reason_fails_the_proof() -> None:
    """Firing is not sufficient; the offender must name what it is complaining about.

    Without the reason half, a detector can start reporting a different, less
    specific finding and every proof still passes -- the assertion degrades to
    "something was flagged" while the message a human reads in CI changes under
    them.
    """
    with pytest.raises(AssertionError):
        assert_fires_once(_fires_on_marker, {"marker()\n": "a reason this detector never emits"}, PATH)


def test_a_detector_that_fires_on_a_sanctioned_shape_fails_the_proof() -> None:
    """The clean half has to be able to fail, or it is decorative."""
    with pytest.raises(AssertionError):
        assert_stays_clean(_fires_on_marker, ("marker()\n",), PATH)


def test_assert_detector_runs_both_halves() -> None:
    """The combined entry point is what a bites test calls, so it must gate both.

    Both halves matter and they fail in opposite directions: a missing clean
    half lets a detector grow over-broad, a missing fires half lets it go inert.
    Whichever half the combined call forgot is the half that regresses unseen.

    ``fires`` is keyword-only and REQUIRED, so a proof cannot quietly declare
    only its clean half: the way to say "this detector has no synthetic bad source"
    is an explicit empty ``fires={}``, which is a claim someone made rather than
    one the signature supplied. That is why the clean-half case below passes
    ``fires={}`` explicitly instead of relying on a default.
    """
    with pytest.raises(AssertionError):
        assert_detector(_never_fires, path=PATH, fires={"marker()\n": "marker"})
    with pytest.raises(AssertionError):
        assert_detector(_fires_on_marker, path=PATH, fires={}, clean=("marker()\n",))


def test_a_generator_returning_detector_is_counted_like_a_list() -> None:
    """Both halves, both detector shapes, same assertions.

    Without normalization the fires half raises TypeError on ``len()`` and the
    clean half silently passes -- ``not <generator>`` is always True, so a
    generator detector over-matching would look clean. That asymmetry is the
    dangerous one: a proof that cannot fail.
    """
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

    Several detectors legitimately report one offender per bad shape in a
    synthetic file (four hand-rolled envelopes, a uuid4 call AND a hex slice), so
    "exactly one" is not a universal rule. But the count still has to be exact: a
    detector that reports one offender where the shape has three is under-reporting
    what a reader would expect to find, and a count of zero is a detector that has
    gone inert on that shape. The count is the assertion, so it cannot be loose.
    """
    with pytest.raises(AssertionError):
        assert_detector(_always_fires_twice, path=PATH, trips={"marker()\n": 3})
    with pytest.raises(AssertionError):
        assert_detector(_always_fires_twice, path=PATH, trips={"marker()\n": 1})


def test_trips_does_not_accept_an_empty_offender_list() -> None:
    """A count of 0 in ``trips`` is a contradiction, not a weaker claim.

    ``clean`` already expresses "must not fire"; letting ``trips={"src": 0}`` say
    it too would give the same fact two spellings that can drift, and would make a
    mis-edited count read as a passing proof.
    """
    with pytest.raises(ValueError, match="clean"):
        assert_detector(_always_fires_twice, path=PATH, trips={"marker()\n": 0})
