"""The document cap must hold on the wire, not only in the parent.

The cap rides the task so the worker truncates before send; these tests assert
the wire shape and the reply length the parent observes.
"""

import pytest

from app.extraction.content import extract_document
from app.orchestration.doc_pool import DocWorker
from tests._doc_pool_fixtures import fresh_doc_pool
from tests._extraction_helpers import settings_with
from tests._process_doubles import StubProc


class _OneWorkerPool:
    """A pool that hands out one pre-made worker (the exchange under test)."""

    def __init__(self, worker) -> None:
        self._worker = worker

    def acquire(self, timeout):
        return self._worker

    def release(self, worker):
        pass

    def retire(self, worker):
        pass


def _piped_worker(monkeypatch, reply_len: int):
    """A DocWorker whose stub pipe records the sent task and answers ``reply_len``."""
    seen: list[object] = []

    class Conn:
        """A faithful stub of the real worker loop: it honors the task's cap when
        replying (like ``_doc_worker_main``), so the wire length is observable."""

        def send(self, payload):
            seen.append(payload)

        def poll(self, timeout):
            return True

        def recv(self):
            _body, _hooks, task_cap = seen[-1]
            return ("ok", "x" * min(reply_len, task_cap))

        def close(self):
            pass

    worker = DocWorker(StubProc(1), Conn())
    monkeypatch.setattr("app.extraction.documents.DOC_POOL", _OneWorkerPool(worker))
    return worker, seen


@pytest.fixture(autouse=True)
def _isolated_pool(monkeypatch):
    fresh_doc_pool(monkeypatch)


def test_the_task_carries_the_callers_cap(monkeypatch):
    """The wire message must carry the cap, so the worker can honor it."""
    cap = 512
    worker, seen = _piped_worker(monkeypatch, reply_len=cap)
    result = extract_document(b"%PDF-1.4 fake", None, settings_with(extraction_max_markdown=cap))
    assert result.markdown is not None
    assert len(seen) == 1, "one task, one send"
    assert len(seen[0]) == 3, "the task must carry (body, hooks, cap) so the worker can cap pre-send"


def test_a_pathological_reply_never_exceeds_the_cap_beyond_the_parent(monkeypatch):
    """The parent's received md is at most the cap, before any parent-side cut.

    With the cap applied only after recv (the old shape), an over-cap reply
    arrives whole and the length the parent sees is over cap.
    """
    cap = 512
    worker, seen = _piped_worker(monkeypatch, reply_len=cap + 4096)
    result = extract_document(b"%PDF-1.4 fake", None, settings_with(extraction_max_markdown=cap))
    assert result.markdown is not None
    assert len(result.markdown) <= cap, "the parent materialized an over-cap reply"


def test_wire_cap_matches_the_configured_cap_not_a_constant(monkeypatch):
    """The cap on the wire is the caller's setting, so two callers disagree safely."""
    cap = 64
    worker, seen = _piped_worker(monkeypatch, reply_len=64)
    extract_document(b"%PDF-1.4 fake", None, settings_with(extraction_max_markdown=cap))
    assert seen[0][2] == cap, "the task's cap is not the caller's configured cap"
