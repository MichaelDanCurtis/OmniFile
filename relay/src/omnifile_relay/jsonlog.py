"""Structured JSON logging without personal data.

Only an allowlist of ``extra`` fields is emitted, so an accidental ``extra={"user": ...}``
never reaches the log. Fields that may appear: event, outcome, reason, action, intent_id
(random, single-use, not personal), status, latency_ms, count, request_id.
Never logged: tokens, objectIds, tenant ids, names, file ids, IP addresses, serviceUrl.
"""

from __future__ import annotations

import json
import logging
import sys
import time

ALLOWED_FIELDS = (
    "event",
    "outcome",
    "reason",
    "action",
    "intent_id",
    "status",
    "latency_ms",
    "count",
    "request_id",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in ALLOWED_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                out[name] = value
        if record.exc_info and record.exc_info[0] is not None:
            # The exception type only: messages can carry request data.
            out["exc_type"] = record.exc_info[0].__name__
        return json.dumps(out, separators=(",", ":"))


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's access log prints client IPs and paths; we log our own request events instead.
    logging.getLogger("uvicorn.access").disabled = True
