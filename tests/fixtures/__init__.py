"""Pytest fixtures, one per file.

A fixture is a decision about how a test is set up, and a test is the only thing
that reads it. Putting each in its own module here keeps that decision legible on
its own -- what it arranges, what it restores, and why leaving it installed
would be wrong -- instead of buried in ``conftest.py`` among fixtures belonging
to other subjects, or welded to a test module that happens to use it first.

Two properties make this a directory rather than a convention:

- **Discovery is by registration, not by location.** pytest does not scan a
  ``fixtures`` directory the way it scans a ``conftest.py``, so every module here
  is named in ``tests/conftest.py``. Adding a fixture is therefore a two-step
  change, and the registration is the visible list of what the whole suite can
  depend on without looking.
- **Helpers are not fixtures.** A plain function that adapts a double to a
  call shape belongs with the other helpers (``tests/_pool_harnesses.py`` and
  its siblings), where it is imported by the tests that use it. Putting it here
  would make it discoverable by every test that does not, which is the coupling
  this directory exists to avoid.

The underscore-prefixed ``tests/_*.py`` modules are the other half: shared
helpers and doubles with no pytest lifecycle. The line between them is whether
the thing has a setup/teardown contract pytest has to honour.
"""

__all__: list[str] = []
