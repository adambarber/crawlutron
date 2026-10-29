"""Crawlutron service package."""

try:
    from crawlutron._version import __version__
except ImportError:  # sdist consumer before the build hook ran
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
