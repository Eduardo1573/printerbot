"""Structured logging with secret redaction.

Usage: ``log.info("print_job_completed", extra={"fields": {"job_id": 5}})``
or the ``event()`` helper below.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_REDACTED = "***"


class RedactingFilter(logging.Filter):
    """Replaces any known secret value that might sneak into a log record."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= 4]

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self._scrub(record.getMessage())
        record.args = None
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = {k: self._scrub(v) if isinstance(v, str) else v for k, v in fields.items()}
        if record.exc_info and record.exc_info[1] is not None:
            # Render the traceback now so it can be scrubbed as well.
            record.exc_text = self._scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            base += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        return base


def setup_logging(level: str, fmt: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter(secrets))
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # aiogram logs every handled update at INFO; keep the signal-to-noise high.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("mail.log").setLevel(logging.WARNING)


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields})
