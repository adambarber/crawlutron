"""Core's registration of the admission package's sensing state with the one seam."""

from crawlutron.runtime import process_state
from crawlutron_admit.sensing.cgroup_sense import has_cache, reset_cache

__all__ = ["register"]


def register() -> None:
    """Bind the package's cgroup cache reset/probe to core's process-state registry."""
    process_state.register(__name__, reset_cache, has_cache)


register()
