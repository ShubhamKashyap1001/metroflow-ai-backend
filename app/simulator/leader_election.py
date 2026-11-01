import asyncio
import logging
import threading
import time
import uuid
from typing import Awaitable, Callable

from app.core import cache
from app.simulator import local_lock

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 15
DEFAULT_POLL_SECONDS = 3

class LeaderElection:

    def __init__(
        self,
        name: str,
        run_loop: Callable[[], Awaitable[None]],
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.name = name
        self._lock_key = f"leader_election:{name}"
        self._run_loop = run_loop
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._holder_id = uuid.uuid4().hex
        self._election_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._redis_ever_reachable = False
        self._last_worker_error: str | None = None

        self._redis_down_since: float | None = None

        self._last_connected_at: float | None = None

        self._metrics_lock = threading.Lock()
        self._heartbeat_ticks_total = 0
        self._leadership_acquired_total = 0
        self._leadership_lost_total = 0
        self._worker_crashes_total = 0
        self._last_heartbeat_ts: float | None = None

    def get_metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            return {
                "name": self.name,
                "state": self.status()["state"],
                "heartbeat_ticks_total": self._heartbeat_ticks_total,
                "leadership_acquired_total": self._leadership_acquired_total,
                "leadership_lost_total": self._leadership_lost_total,
                "worker_crashes_total": self._worker_crashes_total,
                "last_heartbeat_ts": self._last_heartbeat_ts,
            }

    @property
    def holder_id_short(self) -> str:
        return self._holder_id[:8]

    def start(self) -> None:
        if self._election_task is not None and not self._election_task.done():
            return
        self._election_task = asyncio.create_task(self._election_loop())

    async def stop(self) -> None:
        if self._election_task is not None:
            task, self._election_task = self._election_task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._stop_worker()
        cache.release_lock(self._lock_key, self._holder_id)
        local_lock.release(self.name, self._holder_id)

    def is_active(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    def status(self) -> dict:
        if self._election_task is None:
            return {"running": False, "state": "not_started", "holder_id": self.holder_id_short}
        if self.is_active():
            return {"running": True, "state": "leader", "holder_id": self.holder_id_short}
        if self._last_worker_error is not None:
            return {
                "running": False,
                "state": "crashed",
                "error": self._last_worker_error,
                "holder_id": self.holder_id_short,
            }
        return {"running": False, "state": "standby", "holder_id": self.holder_id_short}

    async def _election_loop(self) -> None:
        try:
            while True:
                await self._election_tick()
                await asyncio.sleep(self._poll_seconds)
        except asyncio.CancelledError:
            raise

    async def _election_tick(self) -> None:
        redis_status = cache.redis_status()
        state = redis_status.get("state")

        if state == "connected":
            self._redis_ever_reachable = True
            self._redis_down_since = None
            self._last_connected_at = time.monotonic()
        elif state == "unreachable" and self._redis_ever_reachable and self._redis_down_since is None:

            self._redis_down_since = self._last_connected_at or time.monotonic()

        outage_exceeds_lease_window = (
            self._redis_down_since is not None
            and (time.monotonic() - self._redis_down_since) >= self._lease_seconds
        )

        if state == "disabled" or (
            state == "unreachable" and (not self._redis_ever_reachable or outage_exceeds_lease_window)
        ):

            won_local = local_lock.try_acquire(self.name, self._holder_id)
            if won_local:
                if not self.is_active():
                    reason = (
                        "disabled" if state == "disabled"
                        else "unreachable since startup" if not self._redis_ever_reachable
                        else f"unreachable for >= {self._lease_seconds}s (Phase 7B fallback)"
                    )
                    logger.info(
                        "[leader_election:%s] Redis %s - acquired the local "
                        "file lock, running as the sole local leader (start "
                        "Redis too for correctness across multiple hosts).",
                        self.name, reason,
                    )
                    self._record_leadership_acquired()
                self._record_heartbeat()
                await self._start_worker()
            else:
                if self.is_active():
                    logger.info(
                        "[leader_election:%s] another local process holds the "
                        "file lock - stepping down.", self.name,
                    )
                    self._record_leadership_lost()
                await self._stop_worker()
            return

        if local_lock.is_held(self.name, self._holder_id):
            local_lock.release(self.name, self._holder_id)

        won = cache.try_acquire_or_renew_lock(self._lock_key, self._holder_id, self._lease_seconds)
        if won:
            if not self.is_active():
                logger.info("[leader_election:%s] acquired leadership (holder=%s).",
                            self.name, self.holder_id_short)
                self._record_leadership_acquired()
            self._record_heartbeat()
            await self._start_worker()
        else:
            if self.is_active():
                logger.info("[leader_election:%s] lost leadership - stepping down.", self.name)
                self._record_leadership_lost()
            await self._stop_worker()

    def _record_heartbeat(self) -> None:
        with self._metrics_lock:
            self._heartbeat_ticks_total += 1
            self._last_heartbeat_ts = time.time()

    def _record_leadership_acquired(self) -> None:
        with self._metrics_lock:
            self._leadership_acquired_total += 1

    def _record_leadership_lost(self) -> None:
        with self._metrics_lock:
            self._leadership_lost_total += 1

    async def _start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            task = asyncio.create_task(self._run_loop())
            task.add_done_callback(self._on_worker_done)
            self._worker_task = task

    async def _stop_worker(self) -> None:
        if self._worker_task is not None:
            task, self._worker_task = self._worker_task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _on_worker_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            logger.info("[leader_election:%s] worker stopped (cancelled).", self.name)
            return
        exc = task.exception()
        if exc is not None:
            self._last_worker_error = str(exc)
            with self._metrics_lock:
                self._worker_crashes_total += 1
            logger.error("[leader_election:%s] worker crashed: %s", self.name, exc, exc_info=exc)
        else:
            logger.warning(
                "[leader_election:%s] worker exited without being cancelled - "
                "this shouldn't happen since the loop runs forever.", self.name,
            )
