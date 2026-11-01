
import errno
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

try:
    import fcntl
    _FLOCK_AVAILABLE = True
except ImportError:  # pragma: no cover - non-POSIX platform (e.g. native Windows)
    fcntl = None  # type: ignore[assignment]
    _FLOCK_AVAILABLE = False

_LOCK_DIR = os.environ.get("METROFLOW_LOCK_DIR", tempfile.gettempdir())

_state_lock = threading.Lock()
_held_fds: dict[str, tuple[int, str | None]] = {}
_warned_unavailable = False

def _lock_path(name: str) -> str:
    return os.path.join(_LOCK_DIR, f"metroflow_leader_{name}.lock")

def try_acquire(name: str, holder_id: str | None = None) -> bool:
    global _warned_unavailable

    if not _FLOCK_AVAILABLE:
        if not _warned_unavailable:
            logger.error(
                "[local_lock] fcntl.flock is not available on this platform - "
                "cannot safely coordinate across processes without Redis. "
                "Failing CLOSED (the guarded loop will not start here) rather "
                "than risk duplicate simulators. Configure REDIS_URL for a "
                "coordinated multi-worker deployment on this platform.",
            )
            _warned_unavailable = True
        return False

    with _state_lock:
        held = _held_fds.get(name)
        if held is not None:
            _fd, current_holder = held
            if current_holder == holder_id:
                return True  

        path = _lock_path(name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            logger.error("[local_lock] could not open lock file %s: %s", path, exc)
            return False

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):

                return False
            logger.error("[local_lock] flock() failed unexpectedly on %s: %s", path, exc)
            return False

        _held_fds[name] = (fd, holder_id)
        logger.info(
            "[local_lock] acquired local exclusive lock for '%s' (pid=%s, path=%s).",
            name, os.getpid(), path,
        )
        return True

def release(name: str, holder_id: str | None = None) -> None:
    with _state_lock:
        held = _held_fds.get(name)
        if held is None:
            return
        fd, current_holder = held
        if current_holder != holder_id:
            return
        del _held_fds[name]
    try:
        if _FLOCK_AVAILABLE:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    logger.info("[local_lock] released local exclusive lock for '%s' (pid=%s).", name, os.getpid())

def is_held(name: str, holder_id: str | None = None) -> bool:
    with _state_lock:
        held = _held_fds.get(name)
        if held is None:
            return False
        if holder_id is None:
            return True
        return held[1] == holder_id
