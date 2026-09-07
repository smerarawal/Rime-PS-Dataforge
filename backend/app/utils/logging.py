"""Structured application logging with request/generation correlation."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Any

from backend.app.utils.timing import utcnow

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
        force=False,
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_operation(
    logger: logging.Logger,
    operation: str,
    *,
    conversation_id: str | None = None,
    request_id: str | None = None,
    generation_id: str | None = None,
    task_id: str | None = None,
    timestamp: datetime | None = None,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {
        "ts": (timestamp or utcnow()).isoformat(),
        "operation": operation,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if request_id:
        payload["request_id"] = request_id
    if generation_id:
        payload["generation_id"] = generation_id
    if task_id:
        payload["task_id"] = task_id
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    logger.info(json.dumps(payload, default=str))
