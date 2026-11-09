"""The vocabulary a crawl result is written in: ``Classification`` (the
StrEnum in vocabulary/taxonomy, re-exported here as the single spelling),
``CrawlError`` (a union that includes it), and ``CrawlStrategyName``; the
runtime set is DERIVED from these, one declaration read many ways.
"""

from __future__ import annotations

import enum
import types
from typing import Literal, Union, get_args, get_origin

from crawlutron.vocabulary.taxonomy import CrawlClassification

# ``X | Y`` and ``Union[X, Y]`` are the same type with two spellings and two
# origins; a taxonomy alias written either way has to read the same here.
_UNION_ORIGINS = (Union, types.UnionType)

#: The page-outcome taxonomy: ONE declaration (the StrEnum), imported under
#: its historical alias so every ``Classification`` annotation stays valid.
#: A StrEnum IS a str, so wire comparisons and equality with plain strings
#: keep working unchanged.
Classification = CrawlClassification

# Pipeline conditions with no page-classification analogue (capacity, config):
# they are not page outcomes, so forcing them in would distort the taxonomy.
PIPELINE_ONLY_CRAWL_ERRORS = Literal[
    # Deliberately NOT spelled like its robots_disallowed Classification
    # member: this is the stable ERROR code, asserted disjoint from the set.
    "robots_txt_disallowed",
    # The site REFUSED to serve its robots policy (401/403/429), so no policy
    # was read; distinct from a policy that forbids the path.
    "robots_txt_refused",
    # The site's robots policy could not be reached at all (5xx, no answer).
    "robots_txt_unreachable",
    "browser_worker_unavailable",
    "unsupported_strategy",
    "retries_exhausted",
]

#: The stable failure code beside ``classification``: a declared union of the
#: two groups (hand-maintained, since mypy cannot compute ``*get_args()``).
CrawlError = Classification | PIPELINE_ONLY_CRAWL_ERRORS

CrawlStrategyName = Literal[
    "HTTP_DECLARED_CRAWLER",
    "HTTP_CFFI_IMPERSONATE",
    "BROWSER_HEADLESS",
    "BROWSER_HEADED_WARMED",
]


def literal_members(alias: object) -> frozenset[str]:
    """The string members of a declared type, total by origin: a ``Literal``
    yields its args, an Enum class its values, a union the union of what it
    holds, and anything else nothing -- the clause that makes a field sweep
    safe to point it at.
    """
    if isinstance(alias, type) and issubclass(alias, enum.Enum):
        return frozenset(member.value for member in alias)
    origin = get_origin(alias)
    if origin is Literal:
        return frozenset(arg for arg in get_args(alias) if isinstance(arg, str))
    if origin in _UNION_ORIGINS:
        members: set[str] = set()
        for arg in get_args(alias):
            members |= literal_members(arg)
        return frozenset(members)
    return frozenset()


def as_classification(
    value: str, *, default: Classification = Classification.unknown_error
) -> Classification:
    """Return ``value`` as a :data:`Classification`, or ``default`` if it is not
    one -- so an untriaged code from a string boundary degrades instead of
    widening the type. The table is the enum's own membership, derived.
    """
    return Classification(value) if value in Classification.__members__ else default
