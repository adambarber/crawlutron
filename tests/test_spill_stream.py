"""M10: _ResultSpill.load_ordered single-pass reassembly contract."""

import inspect
import json

from app.crawl.engine import _ResultSpill


class TestLoadOrderedContract:
    """M10: load_ordered assembles results in one pass, in submission order."""

    def test_shuffled_writes_reassemble_in_submission_order(self):
        spill = _ResultSpill()
        try:
            for i in (5, 0, 3, 1, 4, 2):
                spill._fh.write(json.dumps({"i": i, "r": {"n": i}}) + "\n")
            spill.count = 6
            assert spill.load_ordered() == [{"n": i} for i in range(6)]
        finally:
            spill.close()

    def test_sparse_indices_keep_every_result_in_position(self):
        spill = _ResultSpill()
        try:
            for i in (0, 2, 9):
                spill._fh.write(json.dumps({"i": i, "r": {"n": i}}) + "\n")
            spill.count = 3
            out = spill.load_ordered()
            assert [r["n"] for r in out] == [0, 2, 9]
        finally:
            spill.close()

    def test_empty_spill_returns_empty_list(self):
        spill = _ResultSpill()
        try:
            assert spill.load_ordered() == []
        finally:
            spill.close()

    def test_single_pass_no_tuple_rows_or_full_sort(self):
        src = inspect.getsource(_ResultSpill.load_ordered)
        assert "rows" not in src
        assert ".sort(" not in src
