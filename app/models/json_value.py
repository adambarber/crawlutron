"""``JsonValue``: the app's name for a payload that really is arbitrary JSON.

This module exists because the typing ratchet names two remedies for a position
whose key set no annotation declares -- declare a key set, or say the payload is
the system type it is -- and only one of them was available. ``JsonValue``
appeared nowhere in ``app/``, so every ``dict[str, Any]`` was scored as a defect
including the ones that are genuinely open, and a metric that cannot be
satisfied honestly does not sit still: it gets satisfied by deleting the token
without either move, which makes the code less typed rather than more.

``JsonValue`` is the honest move, and it is a real narrowing rather than a
rename. ``Any`` is an EXIT from the type system: it accepts an attribute read, a
call, a subscript of a key nobody declared, and it composes with nothing, so a
single ``Any`` on a wire payload silences the checker for every value that
reaches it afterwards. ``JsonValue`` is CLOSED -- null, the four scalars, arrays
and objects of the same -- so the same read is a build failure, and the failure
names the shape that was wrong.

Where it pays: the boundary that produces arbitrary JSON. ``json.loads`` is
typed ``-> Any`` in the standard library, which means an unvalidated response
body, a config file and a spill line all arrive as ``Any`` and then stay ``Any``
for the rest of the program. One annotation at that door converts it, and
everything downstream of it is checked.

This is NOT a way to launder an undeclared record. A record with a key set the
code depends on needs a ``TypedDict``: ``JsonValue`` says "any of these shapes",
so reading a key off it yields ``JsonValue``, not the declared type, and the
reader is the one who has to check. ``tests/test_typing_budget.py`` asserts the
union is closed and is not ``Any`` precisely so that distinction cannot erode.

Declared once here and imported by name everywhere else, because a recursive
alias spelled at each use site is two spellings of one thing -- which is the
defect this whole exercise is about.
"""

from __future__ import annotations

# The project floor is 3.11, so the 3.12 type alias is written as a string forward
# reference: the same deferral one release earlier, with the recursion still resolving.
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
"""Any value JSON can carry, and nothing else.

Recursive on purpose: a JSON object may nest arbitrarily, and a non-recursive
``dict[str, Any]`` would put ``Any`` straight back into every nested level --
the same exit, one indirection deeper.
"""

__all__ = ["JsonValue"]
