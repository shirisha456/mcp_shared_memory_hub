"""Structured logging.

One hard rule, and it is a correctness constraint rather than a preference:

    **Nothing may ever be written to stdout.**

Under the stdio transport, stdout carries the JSON-RPC frames exchanged with the
MCP host. A single stray byte on stdout corrupts the protocol stream and the
client's failure mode is an opaque parse error with no indication of the cause.

This module therefore installs exactly one handler, bound to stderr, and
``tests/unit/test_logging.py`` asserts that stdout stays empty. ``ruff`` rule
``T20`` bans ``print()`` across the codebase for the same reason.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any, Final

# Attributes LogRecord always carries. Anything outside this set was attached by
# a caller via ``extra=`` and belongs in the structured payload.
_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_HANDLER_NAME: Final[str] = "memhub-stderr"


class JsonFormatter(logging.Formatter):
    """Render a record as a single line of JSON.

    Fields passed via ``extra=`` are merged into the top level, so
    ``log.info("wrote", extra={"memory_id": mid})`` yields a queryable field
    rather than a formatted string someone has to regex later.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service_name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Human-readable format for local debugging. Still stderr-only."""

    def __init__(self, service_name: str) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s")
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
            return f"{base} | {rendered}"
        return base


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "json",
    service_name: str = "memhub",
    stream: Any | None = None,
) -> None:
    """Install the single stderr handler on the root logger.

    Idempotent: repeated calls replace the handler rather than stacking, so
    re-configuring in a test does not produce duplicated output.

    ``stream`` exists only so tests can capture output. It defaults to stderr
    and production code must never pass anything else.
    """
    target = sys.stderr if stream is None else stream

    if target is sys.stdout:
        raise ValueError(
            "Refusing to log to stdout: it carries the MCP JSON-RPC stream. "
            "See the module docstring."
        )

    formatter: logging.Formatter = (
        JsonFormatter(service_name) if log_format == "json" else TextFormatter(service_name)
    )

    handler = logging.StreamHandler(target)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
