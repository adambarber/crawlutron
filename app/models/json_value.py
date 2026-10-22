"""``JsonValue``: the app's name for a payload that is really arbitrary JSON -- a CLOSED union
(null, scalars, arrays, objects of the same), not an ``Any`` exit, so reading a key yields
``JsonValue`` and the reader checks. Declared here once, imported by name; not a record launder.
"""

from __future__ import annotations

# A PEP 695 alias, not a plain one: the recursion makes pydantic's evaluation of the
# plain string-forward form diverge; the type keyword is the spelling that builds.
type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
"""Any value JSON can carry, and nothing else.

Recursive on purpose: a JSON object may nest arbitrarily, and a non-recursive
``dict[str, Any]`` would put ``Any`` straight back into every nested level --
the same exit, one indirection deeper.
"""

__all__ = ["JsonValue"]
