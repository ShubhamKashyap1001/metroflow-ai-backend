
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

# Overridable for tests / unusual deployments where /tmp isn't a good
# shared location for all worker processes to see (they must all
# resolve the same absolute path for the lock to mean anything).
_LOCK_DIR = os.environ.get("METROFLOW_LOCK_DIR", tempfile.gettempdir())

_state_lock = threading.Lock()
_held_fds: dict[str, tuple[int, str | None]] = {}
_warned_unavailable = False


def _lock_path(name: str) -> str:
    return os.path.join(_LOCK_DIR, f"metroflow_leader_{name}.lock")


def try_acquire(name: str, holder_id: str | None = None) -> bool:
    """Best-effort, non-blocking attempt to become the sole local
    holder of the lock named `name`.

    `holder_id` identifies the logical caller (LeaderElection already
    has a stable per-instance `self._holder_id` - the same token it
    uses for the Redis lease). Passing it lets repeated polling by the
    SAME caller take a fast path (return True without touching the
    filesystem again) while still requiring every call from a
    DIFFERENT caller to go through a real `flock()` attempt.

    BUGFIX (stale/incorrect "already held" shortcut): this used to be
    `if name in _held_fds: return True` - true idempotency for *the*
    caller that actually holds the lock, but ALSO a false positive for
    any OTHER caller in the same process asking about the same `name`.
    Verified empirically that this was masking a real, correct kernel
    check: a second `os.open()` + `fcntl.flock(..., LOCK_EX | LOCK_NB)`
    on the same path, even from the very same process (a different
    open file description), correctly raises EWOULDBLOCK - the OS
    itself already enforces the exclusivity this module exists to
    provide. The old shortcut short-circuited that real check before
    it ever ran, so two independent bidders sharing a process (e.g. a
    coding/test double-instantiation of `LeaderElection` for the same
    name, or any future call site that isn't the scheduler.py
    singleton) could both believe they'd won - exactly the kind of
    "assumed exclusivity that wasn't actually verified" this module's
    own docstring says it must never do.

    Returns True if this process now holds it - either it just won the
    flock, or `holder_id` matches whoever already holds it here
    (genuinely idempotent re-check, safe to call every election tick).
    Returns False if another local process - or another, differently
    identified caller in THIS process - currently holds it, or if
    exclusivity can't be verified at all (flock unavailable on this
    platform, or the lock file couldn't be opened). Never returns True
    on a guess.
    """
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
                return True  # same logical holder re-polling - idempotent
            # A differently-identified caller already thinks it holds
            # this name in this process. Don't trust that - fall
            # through and let a genuinely new flock() attempt on our
            # own fd settle it for real (see docstring above).

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
                # Normal contention: another local process (or another
                # identified caller in this one) holds it right now.
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
    """Best-effort release, e.g. on graceful shutdown, so a standby
    sibling process can take over immediately instead of waiting for
    this process to exit. Safe to call even if `name` isn't held.

    Only releases if `holder_id` matches whoever this process recorded
    as the holder (same identity contract as `try_acquire` above) - a
    caller that never actually won the lock can't release someone
    else's."""
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
    """True iff THIS process currently holds `name`'s local lock.

    With `holder_id` given, scoped to that specific caller (matches
    the identity contract above); without it, a broader "does ANY
    caller in this process hold it" query, used only for
    logging/status, never for granting exclusivity."""
    with _state_lock:
        held = _held_fds.get(name)
        if held is None:
            return False
        if holder_id is None:
            return True
        return held[1] == holder_id
