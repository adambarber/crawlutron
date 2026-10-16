"""The audited implementation of every operation that touches untrusted or unbounded data.

A module belongs here when "how large / how long / how durable / what exactly" needs one
answer every caller shares -- bounded_io, safe_regex, bounded_ttl, atomic_write, json_safe, validation.
"""

from __future__ import annotations
