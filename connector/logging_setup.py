"""Structured logging configuration.

Replaces the older ``logging.basicConfig`` setup. Emits one JSON object
per log record so log aggregators (CloudWatch Insights, Loki, Datadog)
can pivot by user, course, or request id without parsing free-form
strings.

Context propagation
-------------------
Three :mod:`contextvars` carry per-request identity through every async
hop without the caller having to thread them explicitly:

  * ``request_id_var``  - request correlation id (one per HTTP request)
  * ``user_id_var``     - LTI session subject when known
  * ``course_id_var``   - Canvas course id when known
  * ``tenant_var``      - tenant key for multi-campus deployments

Middleware sets these at request entry; the formatter reads them when
emitting each line. Downstream code can ignore context entirely - just
call ``logger.info("...")`` and the right fields appear.

Operators can choose between human-readable text logs (default in dev)
and JSON logs (default in prod) via ``LOG_FORMAT=text|json``. The JSON
shape is intentionally stable so anything downstream that grew up on it
keeps working.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Context variables - propagate request identity through async code without
# threading arguments. Default to None so log records emitted outside a
# request (workers, startup, shutdown) simply omit the field rather than
# crashing the formatter.
# ---------------------------------------------------------------------------
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None
)
course_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "course_id", default=None
)
tenant_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant", default=None
)


# Standard ``logging.LogRecord`` attribute names. Anything outside this set
# in ``record.__dict__`` is treated as a user-supplied "extra" and is
# emitted as a top-level JSON field. Keep this in sync with the stdlib if
# the Python version ever bumps - it has not changed since 3.2.
_RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName",
    }
)


def new_request_id() -> str:
    """Generate a fresh request id. UUID4 hex is short, opaque, and unique
    enough for correlation across a few months of logs without clashing
    with any other id space in the system."""
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    """Format a ``LogRecord`` as a single-line JSON document.

    Fields:
      * ``ts``        - ISO-8601 UTC timestamp with millisecond precision
      * ``level``     - "INFO", "WARNING", ...
      * ``logger``    - dotted logger name, e.g. "src.workers.canvas_watcher"
      * ``msg``       - the formatted log message
      * ``request_id``, ``user_id``, ``course_id``, ``tenant`` when set
      * exception info inline (``exc_type``, ``exc_msg``, ``exc_trace``)
      * any ``extra={...}`` keys the caller passed in
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _isoformat_utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Pull context vars at format time so each line reflects the current
        # async task's identity, not the task that submitted the log.
        for key, var in (
            ("request_id", request_id_var),
            ("user_id", user_id_var),
            ("course_id", course_id_var),
            ("tenant", tenant_var),
        ):
            value = var.get()
            if value is not None:
                payload[key] = value

        # Any caller-supplied ``extra={...}`` keys ride along at top level.
        for attr, value in record.__dict__.items():
            if attr in _RESERVED_ATTRS or attr.startswith("_"):
                continue
            try:
                json.dumps(value)  # cheap is-serializable probe
                payload[attr] = value
            except (TypeError, ValueError):
                payload[attr] = repr(value)

        if record.exc_info:
            exc_type, exc_value, _tb = record.exc_info
            payload["exc_type"] = exc_type.__name__ if exc_type else None
            payload["exc_msg"] = str(exc_value) if exc_value else None
            payload["exc_trace"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


def _isoformat_utc(epoch_seconds: float) -> str:
    """Format an epoch float as ISO-8601 in UTC with millisecond precision.

    ``logging.Formatter.formatTime`` uses local time by default and the
    microsecond field is awkward to suppress; producing the string by hand
    keeps log lines short and timezone-correct.
    """
    msec = int((epoch_seconds - int(epoch_seconds)) * 1000)
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(epoch_seconds))}.{msec:03d}Z"


class TextFormatter(logging.Formatter):
    """Human-friendly single-line text formatter for local dev.

    Same context fields as the JSON formatter but in a shape that's
    grep-friendly on a terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        ctx_parts = []
        for key, var in (
            ("rid", request_id_var),
            ("uid", user_id_var),
            ("cid", course_id_var),
        ):
            value = var.get()
            if value is not None:
                ctx_parts.append(f"{key}={value}")
        ctx = " ".join(ctx_parts)
        ctx_str = f" [{ctx}]" if ctx else ""

        base = (
            f"{_isoformat_utc(record.created)} {record.levelname:7s} "
            f"{record.name}{ctx_str} - {record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str | None = None) -> None:
    """Initialize the root logger with the chosen formatter.

    ``fmt`` defaults to the ``LOG_FORMAT`` env var, falling back to "json"
    when ``ENVIRONMENT`` is not "dev" and "text" otherwise. Idempotent:
    safe to call multiple times (subsequent calls reset handlers).
    """
    if fmt is None:
        fmt = os.environ.get("LOG_FORMAT") or (
            "text" if os.environ.get("ENVIRONMENT", "dev") == "dev" else "json"
        )

    root = logging.getLogger()
    # Clear any handlers basicConfig may have installed so we don't double-emit.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Silence the usual suspects - they're informative at WARNING and
    # firehose-loud at INFO. Same set as the old basicConfig.
    for noisy in (
        "botocore", "boto3", "urllib3", "httpcore", "httpx",
        "s3transfer", "python_multipart",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
