import ast
import pathlib


def assert_detector(
    detector,
    *,
    path: pathlib.Path,
    fires: dict[str, str] | None = None,
    trips: dict[str, int] | None = None,
    clean: tuple[str, ...] = (),
) -> None:
    """Prove a detector. Three orthogonal claims, one call, all exact.

    ``fires``  -- source -> the substring its single offender must name. The
    strongest claim: fires once, and says what it found.
    ``trips``  -- source -> how many offenders it must produce. For sources that
    legitimately contain the bad shape more than once (four hand-rolled cache
    envelopes; a ``uuid4`` call AND a ``.hex`` slice are two findings, not one).
    ``clean``  -- sources that must produce none.

    One call covering all of them because they fail in OPPOSITE directions and a
    bites test that can only easily express one is how a detector rots with the
    suite green: drop ``fires`` and nothing notices the detector has stopped
    detecting; drop ``clean`` and it has quietly grown over-broad. Every claim is
    exact, because ``assert offenders`` -- the hand-rolled form this replaced --
    proves only that a detector is not inert, and is equally satisfied by one that
    matches everything and by one whose message has degraded to a generic string.

    ``trips`` rejects 0 rather than accepting it: "must not fire" already has a
    spelling, and a count of 0 as a second one would let the same fact drift
    between two forms and make a mis-edited count read as a passing proof.

    Detector output is normalized to a list HERE rather than at each call site.
    The tree's detectors are split between ``list[str]`` and generators, and that
    split is worse than cosmetic: ``not <generator>`` is always True, so an
    over-matching generator detector would satisfy a hand-rolled ``assert not
    offenders`` while reporting everything. Normalizing once means a caller
    cannot get that wrong, and a new detector can be written either way.
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
    """A ``*_bites`` proof's red half: each source fires the detector, saying why.

    Thin wrapper over :func:`assert_detector` so the assertion exists in one
    place; kept as its own name because a number of proofs cover only the red
    half and reading as a wrapper is honest about that.
    """
    assert_detector(detector, path=path, fires=fires)


def assert_stays_clean(detector, clean: tuple[str, ...], path: pathlib.Path) -> None:
    """A ``*_bites`` proof's clean half: sanctioned shapes must not fire."""
    assert_detector(detector, path=path, fires={}, clean=clean)
