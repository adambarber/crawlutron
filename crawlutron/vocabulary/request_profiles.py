"""The ``request_profile`` vocabulary and its ceilings.

The same closure the browser profile has: every caller-supplied ``CrawlPolicy``
mapping is closed, so callers learn valid keys from one declaration.
"""

from __future__ import annotations

from typing import Final

# --- request_profile vocabulary and ceilings ---
# Keys with a real reader, grouped by how they are read, asserted by test.
REQUEST_PROFILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "accept_languages",
        "cookie_jar",
        "cookie_namespace",
        "cookies",
        "fingerprint_rotation",
        "fingerprint_seed",
        "user_agent_family",
        "user_agents",
    }
)

#: Profile keys read as a single boolean.
REQUEST_PROFILE_FLAG_KEYS: Final[frozenset[str]] = frozenset({"fingerprint_rotation"})

#: The closed ``cookie_jar`` vocabulary; anything else silently disables
#: cookies, so it is refused at the wire edge.
COOKIE_JAR_VALUES: Final[frozenset[str]] = frozenset({"per_host", "session", "none"})

#: Profile keys read as a single string. ``cookie_namespace`` and ``cookie_jar``
#: belong here so a mapping is refused by name, not as a deeper ``TypeError``.
REQUEST_PROFILE_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {"cookie_jar", "cookie_namespace", "fingerprint_seed", "user_agent_family"}
)

#: Profile keys read as a pool the header builder picks one entry from: a
#: non-string entry puts a ``list`` in ``User-Agent``, which is a 500, not a refusal.
REQUEST_PROFILE_POOL_KEYS: Final[frozenset[str]] = frozenset({"accept_languages", "user_agents"})

#: Most entries in one ``user_agents`` / ``accept_languages`` pool. The pool is
#: sampled, not concatenated, so this bounds the payload rather than the header.
MAX_PROFILE_POOL_ENTRIES: Final[int] = 64

#: Longest ``cookie_namespace``. A namespace is a key in a process-wide jar and
#: is echoed in diagnostics, so it is a size knob before it is an isolation knob.
MAX_COOKIE_NAMESPACE_LENGTH: Final[int] = 128

#: Most hosts a static cookie map may name, and most cookies in one map:
#: the ceiling is per host and total, since ``content_cache_key`` caches
MAX_PROFILE_COOKIE_HOSTS: Final[int] = 32
MAX_PROFILE_COOKIE_PAIRS: Final[int] = 32
