"""One corrupt spill line costs nothing the crawl still owns (#C8).

The spill is buffered NDJSON: a crash mid-flush leaves a trailing partial line.
_spill_line raised TypeError on it and load_ordered propagated -- the ENTIRE
crawl response lost for one bad line among N-1 good ones.
"""

import asyncio
from pathlib import Path

from crawlutron.orchestration.engine import ResultSpill
from tests._assertions import assert_skipped_spill_line, load_spill_checked
from tests._result_builders import engine_result


def _spill_with_good_line(tmp_path) -> ResultSpill:
    """A spill carrying one good line, flushed -- the base both corrupt-line
    cases corrupt on disk."""
    spill = ResultSpill(path=str(tmp_path / "spill.ndjson"))

    async def _one():
        await spill.write(0, engine_result("https://a.test/", "rendered"))

    asyncio.run(_one())
    spill.flush()
    return spill


def test_a_partial_line_is_skipped_and_logged(caplog, tmp_path):
    """A spill with a truncated line on disk reassembles the good results and
    logs the skipped line -- one corrupt record never erases the other N-1.
    The file is built through the spill's own writer (constructor truncates a
    pre-existing path), then corrupted on disk as a crash mid-flush would."""
    spill = _spill_with_good_line(tmp_path)
    raw = Path(spill.path).read_text(encoding="utf-8")
    Path(spill.path).write_text(raw + raw[: len(raw) // 2] + "\n", encoding="utf-8")

    results = load_spill_checked(caplog, spill)

    assert_skipped_spill_line(caplog, served=results, expected=["https://a.test/"])
    spill.close()


def test_binary_corruption_is_skipped_like_any_corrupt_line(caplog, tmp_path):
    """Invalid UTF-8 bytes (a torn mid-flush write) raise UnicodeDecodeError --
    a ValueError subclass the original skip guard missed: one binary-corrupt
    line must not erase the N-1 good results either (adversarial pass 5)."""
    spill = _spill_with_good_line(tmp_path)
    with open(spill.path, "ab") as fh:
        fh.write(b'{"i": 1, "r": "\xff\xfe torn"}\n')
    spill.flush()

    results = load_spill_checked(caplog, spill)

    assert_skipped_spill_line(caplog, served=results, expected=["https://a.test/"])
    spill.close()
