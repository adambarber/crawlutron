"""Repository tooling, not application code and not tests.

The gates in ``.pre-commit-config.yaml`` are enforcement: they decide whether a
commit lands. Keeping their implementation inside ``tests/`` meant the suite that
*measures* the codebase was also carrying the code that *judges* it, so the size
of ``tests/`` stopped describing how much of the repository is actually under
test. ``scripts/gate`` is the single entry point every hook dispatches through;
the commands it execs are its siblings here.
"""
