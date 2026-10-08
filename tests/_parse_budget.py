"""The lxml parse budget, measured against the real parser.

lxml parsing is invisible: nothing in a green suite distinguishes a page that
parsed once from a page that parsed four times, and the difference is real CPU
on every request. These tests therefore bracket ``extraction.parse_count()`` --
the counter production increments -- rather than replacing the parser, which
would measure a patched function and would only see the parses that sat behind
the patch.
"""

from dataclasses import dataclass, field

from app.extraction.content import parse_count


@dataclass
class ParseBudget:
    """Parses observed since the last mark, without any production reset.

    ``parse_count`` is cumulative, so a stage is measured by difference. That
    keeps the counter honest (production only ever increments it) and lets a
    budget span several pipeline phases that production cannot name.
    """

    _mark: int = field(default_factory=parse_count)

    def mark(self) -> None:
        """Start a fresh budget from here."""
        self._mark = parse_count()

    @property
    def parses(self) -> int:
        """How many parses happened since construction or the last mark."""
        return parse_count() - self._mark
