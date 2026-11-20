"""Crawlutron service package."""

from __future__ import annotations

from importlib.metadata import version as _distribution_version

# The version is owned by release-please in pyproject.toml and read from the
# installed distribution -- never supplied by code or a fallback literal.
__version__ = _distribution_version("crawlutron-core")

__all__ = ["__version__"]
