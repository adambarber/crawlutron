"""What the operator configured, and the one way it becomes a value.

``env`` is the chokepoint every configuration read passes through: parse a
``CRAWLUTRON_*`` variable once, with its type, its range and its failure mode
decided in one place rather than at each call site that wants the value.
``settings`` is the assembled result -- ``CrawlerSettings`` and the policy
defaults -- so a caller reads a named field instead of assembling numbers out of
environment lookups.

They are one package because they are one question in two steps. A reader looking
for "what is the default timeout, and what happens if the operator sets it to
nonsense" needs both files, and having them at the root meant having them in a
flat list of nineteen modules with nothing to say which belonged together.

Ranges are not here: ``in_range`` and the validate/clamp pair are the range
predicates, and they live in ``app.safety`` beside the other audited primitives.
This package asks what was configured; that one asks whether the answer is within
contract.
"""

from __future__ import annotations
