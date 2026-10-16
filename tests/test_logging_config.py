"""Structured-logging sink invariants: the JSON formatter must attach where every
logger can reach it (the root), so ``extra`` survives and INFO events are not
dropped; the doc worker suppresses pdfminer chatter at its source.
"""

from __future__ import annotations

import contextlib
import json
import logging

import pytest

from app.runtime.logging_config import configure_service_logging


@pytest.fixture
def service_sink(tmp_path):
    """Attach service logging to a rotating JSONL file for one test.

    Returns the path; root state is restored afterwards so the added handlers
    never leak into later tests.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    log_file = tmp_path / "service.jsonl"
    root.handlers[:] = []
    try:
        configure_service_logging(service_name="crawlutron", log_level="info", log_file_path=str(log_file))
        yield log_file
    finally:
        for handler in root.handlers:
            with contextlib.suppress(Exception):
                handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _last_record(log_file):
    lines = [line for line in log_file.read_text().splitlines() if line.strip()]
    assert lines, "no record reached the JSONL sink"
    return json.loads(lines[-1])


def test_module_logger_extras_serialize_to_json(service_sink):
    """Module loggers (app.*) must emit extras as JSON fields, not bare text."""
    logging.getLogger("app.transport.http").warning(
        "http_strategy_result_error", extra={"error_code": "boom", "error_detail": "bad"}
    )
    record = _last_record(service_sink)
    assert record["message"] == "http_strategy_result_error"
    assert record["error_code"] == "boom"
    assert record["error_detail"] == "bad"
    assert record["service"] == "crawlutron"
    assert record["level"] == "WARNING"


def test_info_records_from_app_loggers_reach_the_sink(service_sink):
    """INFO-level service events must not be dropped on the way out."""
    logging.getLogger("app.api.job_runner").info("job_submitted")
    assert _last_record(service_sink)["message"] == "job_submitted"


def test_third_party_loggers_route_through_the_json_sink(service_sink):
    """Third-party namespaces inherit the same sink via root propagation."""
    logging.getLogger("pdfminer").warning("noise")
    assert _last_record(service_sink)["message"] == "noise"


def test_uvicorn_loggers_route_through_the_service_sink():
    """The shipped uvicorn log config routes uvicorn's loggers through the sink:
    uvicorn applies its config in ``Config.__init__`` before the app is imported, so
    the config is asserted the same way uvicorn applies it -- via ``dictConfig``.
    """
    import io
    import logging.config
    from pathlib import Path

    import app.main

    config_path = Path(app.main.__file__).resolve().parent.parent / "uvicorn-log-config.json"
    config = json.loads(config_path.read_text())
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    try:
        logging.config.dictConfig(config)
        stream_handlers = [
            handler
            for handler in root.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        assert len(stream_handlers) == 1, "the config must install exactly one stream sink"
        capture = io.StringIO()
        stream_handlers[0].stream = capture
        logging.getLogger("uvicorn.error").warning("uvicorn_started")
        record = json.loads(capture.getvalue().splitlines()[-1])
    finally:
        root.handlers[:] = saved_handlers

    assert record["logger"] == "uvicorn.error"
    assert record["message"] == "uvicorn_started"
    assert record["service"] == "crawlutron"


def test_doc_worker_suppresses_pdfminer_chatter():
    """The doc worker drops pdfminer's expected per-font WARNINGs at the source."""
    from app.orchestration.doc_pool import configure_doc_worker_logging

    pdfminer = logging.getLogger("pdfminer")
    saved_level = pdfminer.level
    try:
        configure_doc_worker_logging()
        assert pdfminer.level == logging.ERROR
    finally:
        pdfminer.setLevel(saved_level)
