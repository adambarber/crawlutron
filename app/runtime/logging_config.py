"""Standalone JSON-structured logging for the crawlutron service.

The module exposes one call surface: ``configure_service_logging`` returns a
configured logger, and every entry point wires it the same way.

For deployments that want richer logging (rotating files, structured
JSON, JSON-Lines with HTTP context), swap ``configure_service_logging``
with a more capable implementation in your own integration.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import UTC, datetime
from typing import Any

_RESERVED_LOGRECORD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
}


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Keeps the record's ``service`` attribute (added by ``configure_service_logging``)
    and any extra kwargs passed via ``logger.info(..., extra={...})`` so the
    structured fields are visible to downstream tooling.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Pull extras (anything that isn't a reserved LogRecord attribute).
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _ServiceFieldFilter(logging.Filter):
    """Stamp the service name onto records that did not already carry one.

    Attached to the root handlers so a record emitted from any namespace (an
    ``app.*`` module or a third-party library) carries the ``service`` field the
    JSON formatter reports.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self._service_name
        return True


# duplicate JSON handlers on top of existing ones.
_BC_OWNED_HANDLER = "_bc_service_handler"


def configure_service_logging(
    *,
    service_name: str,
    log_level: str = "info",
    log_file_path: str | None = None,
    log_file_max_bytes: int = 10 * 1024 * 1024,
    log_file_backup_count: int = 3,
) -> logging.Logger:
    """Configure JSON line output for the whole service.

    Handlers are attached to the root logger so every namespace reaches them;
    the returned service-named logger is kept for callers that hold it (its
    level mirrors the configured one).

    Args:
        service_name: value reported in the ``service`` field (and the name of
            the logger returned to the caller).
        log_level: one of ``"debug"``/``"info"``/``"warning"``/``"error"``/``"critical"``.
        log_file_path: optional path to also tee logs to a rotating file. The
            file's parent directories are created automatically. ``None`` keeps
            output on stderr only.
        log_file_max_bytes: maximum bytes per log file before rotation.
        log_file_backup_count: number of rotated files to keep.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    root = logging.getLogger()
    root.setLevel(level)
    # The stream sink is installed once only: uvicorn's --log-config may already have
    # attached a JSON handler at process start using the same formatter.
    if not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.handlers.RotatingFileHandler)
        and isinstance(handler.formatter, _JsonFormatter)
        for handler in root.handlers
    ):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(_JsonFormatter())
        stream_handler.addFilter(_ServiceFieldFilter(service_name))
        setattr(stream_handler, _BC_OWNED_HANDLER, True)
        root.addHandler(stream_handler)
    if log_file_path and not any(
        isinstance(handler, logging.handlers.RotatingFileHandler) for handler in root.handlers
    ):
        os.makedirs(os.path.dirname(log_file_path) or ".", exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=log_file_max_bytes,
            backupCount=log_file_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        file_handler.addFilter(_ServiceFieldFilter(service_name))
        setattr(file_handler, _BC_OWNED_HANDLER, True)
        root.addHandler(file_handler)
    return logger


__all__ = ["configure_service_logging"]
