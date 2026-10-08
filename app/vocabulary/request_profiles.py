"""The ``request_profile`` vocabulary and its ceilings.

The same closure the browser profile has (see ``app.vocabulary.browser``), for
the same reason: every caller-supplied mapping on ``CrawlPolicy`` is closed, so a
caller learns from one declaration which keys exist rather than from a rejected
request. What the profile DOES to a request is in ``app.transport``.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# ``request_profile`` vocabulary and ceilings.
#
# The same closure the browser profile got, for the same reason. Every other
# caller-supplied mapping on ``CrawlPolicy`` is closed -- ``headers`` refuses a
# banned name, ``credentials`` refuses a colliding origin -- and this one was
# left as ``dict[str, Any]``, so ``{"cookie_jarr": "per_host"}`` disabled the
# per-host jar and reported success. A key nothing reads is the same failure as
# a key spelled wrong, so the set below holds only keys with a real reader, and
# a test asserts the reader exists.
#
# The keys are also grouped by how they are read, because that grouping is what
# makes a malformed value a refusal at the boundary rather than a ``TypeError``
# somewhere deeper: pools are lists of strings, the rest are single scalars, and
# only ``cookies`` is nested.
# ---------------------------------------------------------------------------

#: The keys a ``request_profile`` may carry, read off the consumers.
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

#: Profile keys read as a single string. ``cookie_namespace`` and ``cookie_jar``
#: belong here too: a mapping reaching either of them fails deeper in, where the
#: refusal is a ``TypeError`` rather than a message naming the key.
REQUEST_PROFILE_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {"cookie_jar", "cookie_namespace", "fingerprint_seed", "user_agent_family"}
)

#: Profile keys read as a pool the header builder picks one entry from. These
#: land in a ``dict[str, str]``, so a non-string entry puts a ``dict`` or a
#: ``list`` in ``User-Agent`` -- and ``[None]`` raises ``TypeError`` from inside
#: the builder, which is a 500.
REQUEST_PROFILE_POOL_KEYS: Final[frozenset[str]] = frozenset({"accept_languages", "user_agents"})

#: Most entries in one ``user_agents`` / ``accept_languages`` pool. The pool is
#: sampled, not concatenated, so this bounds the payload rather than the header.
MAX_PROFILE_POOL_ENTRIES: Final[int] = 64

#: Longest ``cookie_namespace``. A namespace is a key in a process-wide jar and
#: is echoed in diagnostics, so it is a size knob before it is an isolation knob.
MAX_COOKIE_NAMESPACE_LENGTH: Final[int] = 128

#: Most hosts a static cookie map may name, and most cookies in one host's map.
#:
#: The ceiling is per host as well as in total because one host is the common
#: case -- the map is keyed by host precisely so a caller seeds the hosts it
#: means to visit, so a single host carrying 200,000 pairs is the shape that
#: hurts.
#:
#: Unbounded this is worse than a large header. ``content_cache_key`` serialises
#: the whole header map into the cache key, so every distinct cookie string is a
#: distinct cache slot holding a full response body. A 500-host map produced a
#: 14,088-character ``Cookie`` header.
MAX_PROFILE_COOKIE_HOSTS: Final[int] = 32
MAX_PROFILE_COOKIE_PAIRS: Final[int] = 32
