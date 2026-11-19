"""Crawlutron service package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    __version__ = _distribution_version("crawlutron-core")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
