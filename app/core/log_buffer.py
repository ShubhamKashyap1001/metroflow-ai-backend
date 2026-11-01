import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock

MAX_LOG_ENTRIES = 500

_buffer: deque[dict] = deque(maxlen=MAX_LOG_ENTRIES)
_lock = Lock()

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

            return
        with _lock:
            _buffer.append(entry)

_handler = BufferLogHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_handler.setLevel(logging.INFO)

def install() -> None:
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _quiet_third_party_loggers()

def get_recent_logs(limit: int = 100, level: str | None = None) -> list[dict]:
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
