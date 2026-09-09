"""Standalone JSON-structured logging for the browser-crawler service.

This module replaces the previous ``local_service_logging`` shim that was
imported from the parent monorepo's ``shared/python`` directory. The shim
deliberately exposes the same call surface as the original so the rest of
the codebase can import ``configure_service_logging`` without checking
whether the shared logging helper is present.

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


def configure_service_logging(
    *,
    service_name: str,
    log_level: str = "info",
    log_file_path: str | None = None,
    log_file_max_bytes: int = 10 * 1024 * 1024,
    log_file_backup_count: int = 3,
) -> logging.Logger:
    """Configure a service-named logger with JSON line output.

    Args:
        service_name: dotted name used for the logger and the ``service`` field.
        log_level: one of ``"debug"``/``"info"``/``"warning"``/``"error"``/``"critical"``.
        log_file_path: optional path to also tee logs to a rotating file. The
            file's parent directories are created automatically. ``None`` keeps
            output on stderr only.
        log_file_max_bytes: maximum bytes per log file before rotation.
        log_file_backup_count: number of rotated files to keep.
    """
    logger = logging.getLogger(service_name)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    if not logger.handlers:
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        if log_file_path:
            os.makedirs(os.path.dirname(log_file_path) or ".", exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=log_file_max_bytes,
                backupCount=log_file_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(_JsonFormatter())
            logger.addHandler(file_handler)
    logger.propagate = False
    return logger


__all__ = ["configure_service_logging"]
