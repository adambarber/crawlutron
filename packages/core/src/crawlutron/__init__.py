"""Crawlutron service package."""

from __future__ import annotations

try:
    from crawlutron._version import __version__
except ImportError:  # sdist consumer before the build hook ran
    __version__ = ""

__all__ = ["__version__"]
