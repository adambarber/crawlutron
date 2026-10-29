"""The lxml parse budget, bracketing ``extraction.parse_count()``.

Parsing is invisible in a green suite, so tests bracket the production counter
rather than replace the parser, which would measure a patch and miss real ones.
"""

from dataclasses import dataclass, field

from crawlutron.extraction.content import parse_count


@dataclass
class ParseBudget:
    """Parses since the last mark; ``parse_count`` is cumulative and only ever
    incremented by production, so a stage is measured by difference -- letting a
    budget span phases production cannot name.
    """

    _mark: int = field(default_factory=parse_count)

    def mark(self) -> None:
        """Start a fresh budget from here."""
        self._mark = parse_count()

    @property
    def parses(self) -> int:
        """How many parses happened since construction or the last mark."""
        return parse_count() - self._mark
