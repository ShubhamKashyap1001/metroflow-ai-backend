"""In-memory ring buffer of recent application log records.

This is wired up as a `logging.Handler` on the root logger (see
`install()`, called once from app/main.py at startup), so every entry
returned by `get_recent_logs()` is a genuine record the process itself
emitted - the crowd/train simulators, request handlers, the scheduler,
uvicorn, etc. Nothing here is synthesized: /admin/logs is a read-only
window onto whatever the app actually logged, capped to the last
MAX_LOG_ENTRIES records so memory stays bounded on a long-running
process.
"""
import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock

MAX_LOG_ENTRIES = 500

_buffer: deque[dict] = deque(maxlen=MAX_LOG_ENTRIES)
_lock = Lock()

# Render-Free logging fix: raising the ROOT logger to INFO below (so
# this app's own INFO logs are visible/captured) has a side effect on
# any third-party library logger that has no level of its own set -
# it inherits root's effective level instead of the interpreter's
# normal WARNING default. httpx (used internally by the google-genai
# client for every chatbot request - see app/services/chatbot_service.py)
# logs one "HTTP Request: ..." line at INFO per outbound call once
# that happens, which would otherwise have been silent. This is exactly
# the kind of incidental per-request log volume this fix targets, so
# known-chatty third-party loggers are pinned back to WARNING
# explicitly here - this changes nothing about this app's OWN loggers
# (nothing under `app.*` is touched) or their levels.
_NOISY_THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


def _quiet_third_party_loggers() -> None:
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class BufferLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "id": f"{record.created}-{record.relativeCreated}",
                "timestamp": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
        except Exception:
            # A logging handler must never itself raise - that would
            # take down whatever code triggered the log call.
            return
        with _lock:
            _buffer.append(entry)


_handler = BufferLogHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_handler.setLevel(logging.INFO)


def install() -> None:
    """Idempotently attach the buffer handler to the root logger.

    Safe to call more than once (e.g. under a reloader) - won't
    double-attach.
    """
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _quiet_third_party_loggers()


def get_recent_logs(limit: int = 100, level: str | None = None) -> list[dict]:
    """Newest-first slice of the buffer, optionally filtered by level."""
    with _lock:
        entries = list(_buffer)
    if level:
        entries = [e for e in entries if e["level"] == level.upper()]
    entries.reverse()
    return entries[:limit]


def log_counts() -> dict[str, int]:
    with _lock:
        entries = list(_buffer)
    counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0, "DEBUG": 0}
    for e in entries:
        counts[e["level"]] = counts.get(e["level"], 0) + 1
    return counts
