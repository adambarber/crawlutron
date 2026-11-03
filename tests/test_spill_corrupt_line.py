"""One corrupt spill line costs nothing the crawl still owns (#C8).

The spill is buffered NDJSON: a crash mid-flush leaves a trailing partial line.
_spill_line raised TypeError on it and load_ordered propagated -- the ENTIRE
crawl response lost for one bad line among N-1 good ones.
"""

import asyncio

from crawlutron.orchestration.engine import ResultSpill
from tests._assertions import assert_skipped_spill_line, load_spill_checked
from tests._result_builders import engine_result


def test_a_partial_line_is_skipped_and_logged(caplog, tmp_path):
    """A spill with a truncated line on disk reassembles the good results and
    logs the skipped line -- one corrupt record never erases the other N-1.
    The file is built through the spill's own writer (constructor truncates a
    pre-existing path), then corrupted on disk as a crash mid-flush would."""
    path = tmp_path / "spill.ndjson"
    spill = ResultSpill(path=str(path))

    async def _one():
        await spill.write(0, engine_result("https://a.test/", "rendered"))

    asyncio.run(_one())
    spill.flush()
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw + raw[: len(raw) // 2] + "\n", encoding="utf-8")  # the truncated twin

    results = load_spill_checked(caplog, spill)

    assert_skipped_spill_line(caplog, served=results, expected=["https://a.test/"])
    spill.close()
