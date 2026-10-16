"""Repository tooling, not application code and not tests.

The gates in ``.pre-commit-config.yaml`` are enforcement; their implementation lives
here (outside ``tests/``) so ``scripts/gate`` is the one entry point every hook uses.
"""
