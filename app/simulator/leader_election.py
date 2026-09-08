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
    """Ensures at most one process (across all API workers/replicas)
    runs `run_loop` at a time, using a Redis lease as the shared source
    of truth. `run_loop` is a zero-argument async callable that runs
    forever (like the existing simulator run_forever() functions) until
    cancelled.
    """

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
        # Phase 7B: set the moment Redis transitions from "connected"
        # to "unreachable" (a real, mid-flight outage - not "never
        # configured", which never sets this at all since it's never
        # been connected in the first place). None while Redis is up
        # or has never been up. Used to gate the fail-closed ->
        # fail-safe-local-lock transition below on a full lease
        # window having elapsed, so we only ever fall back once any
        # pre-outage lease is guaranteed expired.
        self._redis_down_since: float | None = None
        # The last tick at which Redis was observed "connected". Used
        # to backdate `_redis_down_since` to when the outage actually
        # started (as best we can know it) rather than to whenever we
        # merely first NOTICED it, which matters whenever the gap
        # between ticks is itself close to `lease_seconds` (e.g. a
        # slow/delayed poll) - without backdating, a slow first
        # observation would restart the grace-period clock from that
        # late observation and could double the real failover time.
        self._last_connected_at: float | None = None

        # Leader/heartbeat monitoring for app/core/metrics.py's
        # simulator collector. Plain ints/dicts guarded by a stdlib
        # Lock, NOT prometheus_client Counters/Gauges - this module
        # has to stay importable with nothing beyond
        # app.core.cache/app.simulator.local_lock in the offline
        # verification harnesses under scripts/verify_leader_election*.py
        # (see their own module docstrings), which inject fake
        # stand-ins for those two. app/core/metrics.py reads these
        # back via get_metrics_snapshot() at scrape time and
        # republishes them as real Prometheus series.
        #
        # A "heartbeat" here is every tick where THIS process
        # successfully proves/renews that it's the leader - a Redis
        # lease acquire-or-renew, or (Redis unavailable) winning the
        # same-host local-lock fallback - i.e. exactly the two `won`/
        # `won_local` branches in _election_tick() below that lead to
        # _start_worker(). A leader that stops heartbeating without
        # cleanly stepping down (state stuck on "leader" while
        # last_heartbeat_ts stalls) is the failure mode this exists to
        # make visible - the lease itself will still expire and let
        # another process take over, but this is what lets a
        # dashboard/alert notice the stall rather than relying on that
        # eventual failover alone.
        self._metrics_lock = threading.Lock()
        self._heartbeat_ticks_total = 0
        self._leadership_acquired_total = 0
        self._leadership_lost_total = 0
        self._worker_crashes_total = 0
        self._last_heartbeat_ts: float | None = None

    def get_metrics_snapshot(self) -> dict:
        """Point-in-time snapshot for app/core/metrics.py's Prometheus
        collector. Never called from the election loop itself - only
        from collect(), at scrape time."""
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
        """Begin bidding for leadership. Idempotent - calling this
        again while already running/bidding is a no-op."""
        if self._election_task is not None and not self._election_task.done():
            return
        self._election_task = asyncio.create_task(self._election_loop())

    async def stop(self) -> None:
        """Stop bidding, stop the worker loop if we're currently
        leading, and best-effort release the lease so the next standby
        process doesn't have to wait out the full TTL before taking
        over (used on graceful shutdown / admin "stop simulator")."""
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
        """True iff THIS process is currently leading (actively
        running the loop), not merely bidding in standby."""
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
            # First tick that NOTICES a real, mid-flight outage - but
            # backdate the grace-period clock to the last tick that
            # actually saw Redis connected (best available estimate of
            # when the outage really started), not to right now. If we
            # started the clock from "now" instead, a delayed/slow
            # first observation (this tick lagging the true outage
            # start by close to a full lease_seconds itself) would
            # require ANOTHER full lease_seconds from here before
            # falling back - up to ~2x the intended fail-safe window,
            # which is exactly what the module docstring's "once Redis
            # has been unreachable for at least one full lease window"
            # promises will NOT happen. Falling back to `time.monotonic()`
            # only if we somehow never recorded a connected tick (should
            # not happen once `_redis_ever_reachable` is True, but keeps
            # this defensive).
            self._redis_down_since = self._last_connected_at or time.monotonic()

        # Phase 7B: a real outage only gets the same local-lock
        # fallback as "never configured" once it's been down for at
        # least a full lease window - before that, any lease acquired
        # right before the outage could still legitimately be held
        # (not yet expired) by a process we simply can't currently ask,
        # so falling back immediately would risk a genuine split-brain.
        # After a full lease window with no way to renew/re-verify
        # anything, no valid lease can possibly still exist anywhere,
        # so it's safe to stop waiting and start coordinating locally.
        outage_exceeds_lease_window = (
            self._redis_down_since is not None
            and (time.monotonic() - self._redis_down_since) >= self._lease_seconds
        )

        if state == "disabled" or (
            state == "unreachable" and (not self._redis_ever_reachable or outage_exceeds_lease_window)
        ):
            # No Redis coordination possible (or none configured).
            # Phase 7A: this used to fail OPEN here - every process
            # that reached this branch just assumed "I must be alone"
            # and started its own copy of the loop, which is exactly
            # how N `uvicorn --workers N` processes with no Redis
            # produced N duplicate simulators (docs/background-jobs-and-leader-election.md).
            # Fail CLOSED instead: fall back to a same-host,
            # kernel-enforced file lock (app/simulator/local_lock.py)
            # so only one of these processes can ever become the local
            # leader, even with Redis fully unavailable. If file
            # locking itself isn't available either, try_acquire()
            # itself fails closed (returns False) rather than letting
            # us assume an exclusivity we can't actually verify.
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

        # Redis has been reachable at some point - use it as the sole
        # source of truth from here on. Release any local-lock fallback
        # we may have been holding so it doesn't linger unnecessarily
        # once Redis is available.
        if local_lock.is_held(self.name, self._holder_id):
            local_lock.release(self.name, self._holder_id)

        # If Redis is down right now but still inside the lease-window
        # grace period (a fresh, real outage - not "never configured",
        # and not yet long enough to safely fall back), acquisition
        # below simply fails and we fail CLOSED (step down / stay
        # standby) rather than risk two processes both believing they
        # lead while a pre-outage lease could still legitimately exist.
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
        """Call exactly once per tick that this process successfully
        proved/renewed leadership (a Redis lease win, or a local-lock
        win when Redis is unavailable) - see `get_metrics_snapshot()`
        in __init__ for what this represents."""
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
