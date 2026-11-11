"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.policy_suggestions import (
    build_policy_suggestions,
    policy_suggestions,
)

__all__ = ["build_policy_suggestions", "policy_suggestions"]
