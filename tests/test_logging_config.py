"""Structured-logging sink invariants.

The service's JSON formatter must attach where every module and third-party
logger can reach it (the root logger) so ``extra={...}`` context survives and
INFO-level service events are not silently dropped. The document-extraction
worker suppresses pdfminer's per-font parser chatter at the source because it
runs in a child process with no other logging configuration.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.logging_config import _BC_OWNED_HANDLER, configure_service_logging


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
    # Drop handlers an earlier import attached (app.main configures one at
    # import time from the environment) so this test owns a dedicated sink.
    root.handlers[:] = [h for h in root.handlers if not getattr(h, _BC_OWNED_HANDLER, False)]
    try:
        configure_service_logging(
            service_name="browser-crawler", log_level="info", log_file_path=str(log_file)
        )
        yield log_file
    finally:
        for handler in root.handlers:
            try:
                handler.close()
            except Exception:
                pass
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _last_record(log_file):
    lines = [line for line in log_file.read_text().splitlines() if line.strip()]
    assert lines, "no record reached the JSONL sink"
    return json.loads(lines[-1])


def test_module_logger_extras_serialize_to_json(service_sink):
    """Module loggers (app.*) must emit extras as JSON fields, not bare text."""
    logging.getLogger("app.crawl.strategies.http").warning(
        "http_strategy_result_error", extra={"error_code": "boom", "error_detail": "bad"}
    )
    record = _last_record(service_sink)
    assert record["message"] == "http_strategy_result_error"
    assert record["error_code"] == "boom"
    assert record["error_detail"] == "bad"
    assert record["service"] == "browser-crawler"
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
    """The shipped uvicorn log config routes uvicorn's loggers through the sink.

    uvicorn applies its logging config in ``Config.__init__`` -- before the app
    is imported -- so the service's JSON sink has to exist by then. The config
    uvicorn loads is asserted here the same way uvicorn loads it: applied with
    ``dictConfig``, then a uvicorn record must arrive JSON-formatted with the
    service field via root propagation.
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
    assert record["service"] == "browser-crawler"


def test_doc_worker_suppresses_pdfminer_chatter():
    """The doc worker drops pdfminer's expected per-font WARNINGs at the source."""
    from app.crawl.extraction import _configure_doc_worker_logging

    pdfminer = logging.getLogger("pdfminer")
    saved_level = pdfminer.level
    try:
        _configure_doc_worker_logging()
        assert pdfminer.level == logging.ERROR
    finally:
        pdfminer.setLevel(saved_level)
