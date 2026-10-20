"""The private-import ledger's shape: kinds, sides and one declaration row."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple

Kind = Literal[
    "unreviewed",
    "harness lifecycle",
    "chokepoint state",
    "private type",
    "implementation step",
    "substituted seam",
]
Side = Literal["test", "app"]


class Declaration(NamedTuple):
    kind: Kind
    reason: str
    side: tuple[Side, ...] = ()


def _d(kind: Kind, reason: str, side: Sequence[Side] = ()) -> Declaration:
    """One declaration, normalising ``side`` to the immutable tuple field.

    Rows read as literals (``["test"]``) while the field stays hashable."""
    return Declaration(kind, reason, tuple(side))


_CHOKEPOINT_STATE = _d(
    "chokepoint state",
    "Module-private data -- a cap, a cache, a lock, a counter. Not a missing seam.\n"
    "A constant is private because nobody outside the module may change it, and promoting\n"
    "one would be exactly the alias this file exists to prevent: the name would be public\n"
    "and still unwelded. A caller reading a configured cap is white-box by nature and fine;\n"
    "what would not be fine is the caller being able to write it.\n"
    "\n"
    "The check that this is really state and not a hole: the name appears in a public\n"
    "default, a public return type, or nowhere at all. A constant a caller is expected to\n"
    "*set* is not chokepoint state, it is a missing seam, and it belongs in the public\n"
    "surface instead.",
)

_IMPLEMENTATION_STEP = _d(
    "implementation step",
    "A step inside an algorithm rather than a contract. A test reaching one is asserting a\n"
    "mechanism, and the fix is to rewrite the test to drive the contract above and delete\n"
    "this row -- not to promote the step, which would make the mechanism part of the\n"
    "module's promise.",
)

_LIFECYCLE_EXTRACTION_EXECUTOR = _d(
    "harness lifecycle",
    "The suite starts the doc-extraction pool through this module, so only this\n"
    "module can stop it. conftest teardown is the one caller that legitimately reaches\n"
    "inside, because it owns the thing being torn down.\n",
)

_LIFECYCLE_SHUTDOWN_DOC_EXTRACTION_EXECUTOR = _d(
    "harness lifecycle",
    "The counterpart to the executor above, for the same reason: session teardown\n"
    "cannot ask a public seam to stop a pool it did not create.\n",
)

_LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES = _d(
    "harness lifecycle",
    "Draining superseded client closes is shutdown work, and shutdown work is owned by\n"
    "the module holding the pending set. It stays private because each caller is\n"
    "draining a pool it did not create -- the same argument the conftest rows make,\n"
    "with the difference that these are production callers. So if a third appears the\n"
    "answer is a public drain entry point, not a third reach.\n",
)

_PROCESS_WIDE_JAR = _d(
    "chokepoint state",
    "A process-wide singleton, reached by request_profile and main. Declared only\n"
    "until #75 decides whether cookie-jar ownership moves: if two modules are\n"
    "entitled to the same jar then the jar is a shared resource with no public\n"
    "accessor, which is a hole rather than a private name.\n",
)


_SUBSTITUTED_COLLABORATOR = _d(
    "substituted seam",
    "The suite replaces this name with a double rather than reading what it returns.\n"
    "A read asserts on a mechanism; a substitution asserts that the code under test\n"
    "takes this collaborator at all, which is a contract -- and the only way to observe\n"
    "that contract from outside the module is to *be* the collaborator.\n"
    "\n"
    "It stays private on purpose. A public collaborator is one production can reach too,\n"
    "and a double production can reach is a double production can be broken by. So the\n"
    "reach is correct and the fix for it is nothing; promoting the name would quiet this\n"
    "gate and lose the property that made the gate worth having.\n",
    side=("test",),
)


_SUBSTITUTED_CAP = _d(
    "substituted seam",
    "A private cap, timeout or retry delay the suite shrinks until the path behind it is\n"
    "reachable inside a test. The shipped value is minutes, or tens of megabytes; a test\n"
    "that waits that long does not run, so the suite substitutes a value small enough for\n"
    "the guard to fire or the retry to happen.\n"
    "\n"
    "This is deliberately not `chokepoint state`, which is about *reading* module-private\n"
    "data. Here the suite writes a name no caller may set: raising it is not a missing\n"
    "seam, it is a caller moving a bound the module owns. The name stays private and the\n"
    "substitution is recorded.\n",
    side=("test",),
)


_OWNED_POOL_MACHINERY = _d(
    "harness lifecycle",
    "A pool's own machinery, driven by the test that exists to prove it.\n"
    "\n"
    "Two pools are reached this way and the argument is identical for both: the\n"
    "doc-extraction pool (``tests/crawl/test_doc_pool.py``) and the HTTP client pool\n"
    "(``tests/crawl/test_http_client_pool.py``). Each test exists to prove an acquire\n"
    "lock, a spawn-once rule and a close path, and none of that is reachable from\n"
    "above -- ``extract_content`` hands back a document rather than a worker handle, a\n"
    "request hands back a response rather than a pooled client. So there is no seam\n"
    "these tests could work through instead.\n"
    "\n"
    "The reach is honest and the verdict is that nothing needs promoting. What these\n"
    "rows must not become is a public pool API: a caller that can acquire and retire\n"
    "workers, or evict a client out from under an in-flight close, is a caller that\n"
    "can wedge the pool -- which is the failure the locks and the escalation exist to\n"
    "contain. The names stay private and each pool keeps its own test.\n"
    "\n"
    "Held as `harness lifecycle` in the sense the kind already means -- the suite\n"
    "drives a world it started and cannot reach any other way. Filed as a possible\n"
    'sixth kind ("the owner\'s own test") in #86, deferred rather than grown for the\n'
    "three rows that would justify it today.\n",
    side=("test",),
)


_PRIVATE_TYPE = _d(
    "private type",
    "A private class, or a private method on a public one. A private *method* is an\n"
    "implementation step by definition: the class is public and the method is not. A private\n"
    "*class* is a judgement -- it stays private if nothing constructs it from outside, and\n"
    "becomes public if it has a lifecycle the caller owns, as `_ResultSpill` does.",
)
_INSTANCE_STATE = _d(
    "chokepoint state",
    "An instance-private member -- a lock, a cache index, a byte counter -- that a test\n"
    "reads because it is proving the member's own invariant. The instance is the test's\n"
    "subject, so the reach is bound to the class as its owner: `cache._index` and\n"
    "`ContentCache._index` are the same weld. No public surface exists for it without\n"
    "promoting the index itself, which would make a mechanism part of the class' promise --\n"
    "the alias the ledger exists to stop.",
    side=("test",),
)


_APP_INTERNAL_WELD = _d(
    "implementation step",
    "A private reached by a sibling module in the same package -- shared vocabulary within\n"
    "a layer, not a layering inversion. The name stays private because no module outside the\n"
    "package may touch it either; the weld is cross-module, so it is declared rather than\n"
    "invisible to the from-import spelling that carries it.",
    side=("app",),
)
