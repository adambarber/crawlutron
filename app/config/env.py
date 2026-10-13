"""Reading configuration from a mapping or the environment, without ever raising.

Every knob in the deployment arrives here: an environment string, or a mapping
in tests. A typo in one must not fail a policy open, so nothing in this module
raises -- it warns naming the variable and returns a bounded fallback.

The mapping faces and the environment faces are one function each. ``get_env_int``
is ``get_mapping_int`` bound to ``os.environ``, so a new option is added in one
place. Restating the signature per face is what let the two drift.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Mapping
from functools import partial


def parse_bool_value(raw: str | None, default: bool, *, env_name: str) -> bool:
    """Parse a boolean env value; warn (never silently flip) on garbage.

    Unset/empty -> ``default``. Recognized truthy: 1/true/yes/on; falsy:
    0/false/no/off (case-insensitive). Surrounding whitespace is ignored.
    Anything else warns naming the variable and returns ``default``, so
    ``RESPECT_ROBOTS=ture`` still keeps robots respected.
    """
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    logging.getLogger(__name__).warning(
        "invalid_bool_env for %s: %r (using default %r)",
        env_name,
        raw,
        default,
        extra={"env_name": env_name, "env_value": raw, "fallback": default},
    )
    return default


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var as a bool through the one chokepoint that says what "on" means."""
    return parse_bool_value(os.environ.get(name), default, env_name=name)


def _clamp(value, lo, hi):
    """Bound ``value`` to ``[lo, hi]`` when the bounds are provided."""
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


def _finite_or_none(value: float) -> float | None:
    """A finite float, or None when the parsed value cannot be honored."""
    return value if math.isfinite(value) else None


def in_range[T: (int, float)](lo: T, hi: T) -> Callable[[T], T | None]:
    """Refuse a value outside ``[lo, hi]`` instead of clamping it.

    For a value that is part of a contract: a deployment asking for 9 retries
    and silently getting 5 cannot tell anyone. Fed to ``validate=`` this warns
    and yields the fallback, so the misconfiguration is visible at startup.
    """

    def within(value: T) -> T | None:
        return value if lo <= value <= hi else None

    return within


def _as_int_default(value, *, name) -> int:
    """Coerce a caller-supplied default to a finite int; never raise.

    Absorbs anything un-coercible (None, "abc", inf, nan) so later arithmetic
    like ``limit // default`` cannot crash or blow up a repetition.
    """
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(value)
        return int(value)
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning("invalid_env_default", extra={"env_name": name, "fallback": 0})
        return 0


def _as_float_default(value, *, name) -> float:
    """Coerce a caller-supplied default to a finite float; never raise."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning("invalid_env_default", extra={"env_name": name, "fallback": 0.0})
        return 0.0
    if not math.isfinite(result):
        logging.getLogger(__name__).warning(
            "non_finite_env_default", extra={"env_name": name, "fallback": 0.0}
        )
        return 0.0
    return result


def _mapping_number[T: (int, float)](
    mapping: Mapping[str, str],
    name: str,
    fallback: T,
    *,
    parse: Callable[[str], T],
    invalid_event: str,
    validate: Callable[[T], T | None] | None = None,
    validate_event: str | None = None,
    lo: T | None = None,
    hi: T | None = None,
) -> T:
    """The one never-raise numeric read shared by the int and float faces.

    Both public readers differed only in the parse callable, the warning event
    and the float-only finiteness check; restating that body per type is how the
    two faces drifted.
    """
    raw = mapping.get(name)
    if raw is None:
        return fallback
    try:
        value = parse(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            invalid_event, extra={"env_name": name, "env_value": raw, "fallback": fallback}
        )
        return fallback
    if validate is not None and validate_event is not None:
        checked = validate(value)
        if checked is None:
            logging.getLogger(__name__).warning(
                validate_event, extra={"env_name": name, "env_value": raw, "fallback": fallback}
            )
            return fallback
        value = checked
    return _clamp(value, lo, hi)


def get_mapping_int(
    mapping: Mapping[str, str],
    name: str,
    # settings mapping or environment string, so a number, a string, or nothing. Coercion
    # stays total, so a wrongly-typed default is absorbed rather than raised.
    default: int | float | str | None,
    *,
    lo: int | None = None,
    hi: int | None = None,
    validate: Callable[[int], int | None] | None = None,
    validate_event: str | None = None,
) -> int:
    """Read an integer from a mapping, or from the environment; never crash, always an int.

    Hardened against every class of bad input: an unparseable value, a
    non-numeric default, or one outside ``validate`` all fall back with a
    warning, and the result is clamped to ``[lo, hi]``. A string default can no
    longer leak a ``str`` into downstream arithmetic.
    """
    return _mapping_number(
        mapping,
        name,
        _clamp(_as_int_default(default, name=name), lo, hi),
        parse=int,
        invalid_event="invalid_int_env",
        lo=lo,
        hi=hi,
        validate=validate,
        validate_event=validate_event,
    )


def get_mapping_float(
    mapping: Mapping[str, str],
    name: str,
    # The same real set as the int face, for the same reason.
    default: int | float | str | None,
    *,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Read a float from a mapping, or from the environment; never crash, always finite.

    Non-finite values from both the default and the environment are refused:
    ``float("inf")`` parses cleanly but ``int(limit * inf)`` overflows at import.
    """
    return _mapping_number(
        mapping,
        name,
        _clamp(_as_float_default(default, name=name), lo, hi),
        parse=float,
        invalid_event="invalid_float_env",
        validate=_finite_or_none,
        validate_event="non_finite_float_env",
        lo=lo,
        hi=hi,
    )


# The env faces are the mapping faces bound to this process's environment, not a
# second copy of their signatures; `get_env_int.__doc__` is the mapping face's.
get_env_int = partial(get_mapping_int, os.environ)
get_env_float = partial(get_mapping_float, os.environ)


__all__ = [
    "get_env_float",
    "get_env_int",
    "get_mapping_float",
    "get_mapping_int",
    "in_range",
    "parse_bool_env",
    "parse_bool_value",
]
