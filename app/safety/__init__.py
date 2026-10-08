"""The audited implementation of every operation that touches untrusted or
unbounded data.

Each module here exists because the same shape was hand-rolled at more than one
site and the copies drifted. That is the whole test for membership in this
package: an operation belongs here when "how large", "how long", "how durable",
or "what exactly" needs one answer that every caller shares.

- ``bounded_io`` caps a read before it is materialised, so a body that claims to
  be small cannot cost more than the cap.
- ``safe_regex`` runs attacker-influenced patterns under a wall-clock bound.
  Static rejection of "pathological" regexes is bypassable by construction, so
  the bound is the guarantee rather than a heuristic list of shapes to refuse.
- ``bounded_ttl`` is the one locked, TTL'd, LRU-capped cache: the lock lives
  inside every operation, so "forgot the lock" is unrepresentable rather than
  reviewed.
- ``atomic_write`` is the single mkstemp + fsync + replace, so a crash mid-write
  leaves the old file rather than a truncated one.
- ``json_safe`` is the single rendering for values JSON cannot hold natively, so
  a datetime in a log line and the same datetime in a webhook body look alike.
- ``validation`` is the egress verdict: whether a resolved address is safe to
  send a request to, and whether a discovered script endpoint passes.

These are deliberately NOT spread across the layers that use them. A cap on a
read is needed by transport, extraction, search and the API alike; putting it in
any one of them makes the other three import a layer they have no business
depending on, and leaves four places to forget it.

Whether a URL may be crawled at all is ``app/admission``; what a crawl is allowed
to spend is ``app/vocabulary/limits``.
"""

from __future__ import annotations
