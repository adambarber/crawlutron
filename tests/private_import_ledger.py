"""Every name in ``app/`` reached privately, declared with why it may stay that way.

Two kinds of private reach are recorded here, and they are not equally bad.

**A test reaching into ``app``** is the visible cost of a module having no level
above its insides. 43 of the 49 structural commits that moved ``app/`` also moved
``tests/``, and that ratio is what this file exists to shrink.

**A module reaching into a sibling** is worse, because it means the module
boundary is a convention nobody is holding. Six modules do it, thirteen reaches
in all: ``app/orchestration/engine.py`` reaches into ``app/orchestration/robots.py`` for four
private classes and two pattern helpers, and ``app/transport/http.py``
reaches into ``content_cache_key`` and ``request_profile``. Nothing checked that,
and nothing stopped it growing.

Both are gated the same blunt way: **the name appears here, with a kind and a
reason, or the run is red.** A gate that weighs the merit of a reach is a
convention, and conventions decay. What the bluntness buys is that a new weld
cannot land without somebody writing down why it is acceptable.

One table, not two, because a name can be reached from both sides -- and
``app.transport.pooled_clients._drain_pending_client_closes`` is, by a
test and by two production modules -- and one name is one declaration whichever
hand reaches for it. The ``side`` field records who reaches, not what they are
allowed to.

Kinds, and what each one claims:

``harness lifecycle``
    Process or executor teardown the caller must drive itself, because it owns the
    thing being torn down. Real: a harness must stop the world it started.

``chokepoint state``
    A cap, a cache, a lock, a counter. Not a missing seam -- a constant is private
    because nobody outside the module may change it, and promoting one would be
    the alias this file exists to prevent: public and still unwelded. The check
    that a name really is state and not a hole: it appears in a public default, a
    public return type, or nowhere at all. A constant a caller is expected to
    *set* is not state, it is a missing seam.

``private type``
    A private class, or a private method on a public one. A private method is an
    implementation step by definition: the class is public and the method is not.

``implementation step``
    A step inside an algorithm rather than a contract. A test reaching one is asserting a
    mechanism; the fix is to rewrite the test to drive the contract above, not to promote
    the step.

``substituted seam``
    The suite replaces the name rather than reading it -- a collaborator swapped for a
    double, or a cap shrunk until the path behind it is reachable inside a test. This is
    the largest single category of reach in the suite, and the one the gate could not see
    at all until ``monkeypatch.setattr(module, "_name", ...)`` was recognised as a
    spelling rather than treated as a string. It stays private on purpose: a public
    collaborator is a collaborator production can be broken by, so the reach is correct
    and the fix for it is nothing. Promoting the name would quiet the gate and lose the
    property.
``unreviewed``
    Recorded from the tree when this file was created. No one has argued for it
    yet, and these are the debt.

The names are verified against both trees in both directions on every run, so the
*names* cannot drift from reality and only the reasons are hand-maintained. That
guard is what stops this becoming another hand-kept mirror of generated truth.
"""

from __future__ import annotations

from tests._private_import_decl import Declaration

# Every private reach, keyed `module.name` across both trees.
from tests._private_import_rows_a import DECLARED as _ROWS_A
from tests._private_import_rows_b import DECLARED as _ROWS_B

DECLARED: dict[str, Declaration] = {**_ROWS_A, **_ROWS_B}
