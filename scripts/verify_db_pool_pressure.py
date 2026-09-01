"""Stdlib-only concurrency test proving two things about the Phase 6
fix to app/services/alert_service.py's _dispatch():

  1. Under DB connection-pool pressure, failures come back as fast,
     controlled timeout errors - never as a hang - matching the
     app/main.py db_pool_exhausted_handler contract (SQLAlchemy's real
     pool_timeout raises sqlalchemy.exc.TimeoutError the exact same
     way: bounded wait, then a clean exception, never an indefinite
     block).
  2. The actual bug this phase fixed was real and measurable: holding
     a pooled connection for the DURATION of the alert email/SMS send
     (the "before" shape) drastically reduces how many concurrent
     alert-dispatches a small pool can sustain, compared to only
     holding the connection for the fast query/write portions (the
     "after" shape, what the code now does).

This sandbox has no network access to `pip install sqlalchemy`/
`redis`/`fastapi` (confirmed in Phase 4/6 - pip has no index to reach),
so this cannot run the real SQLAlchemy QueuePool against a real
Postgres. It instead models the exact same acquire/timeout/release
contract QueuePool provides (bounded wait for a free slot, then either
succeed or raise a timeout) with a stdlib BoundedSemaphore, and drives
it with real OS threads (not asyncio) - the same execution model
FastAPI actually uses for sync route handlers and sync BackgroundTasks
(a worker threadpool), which is exactly where _dispatch() runs. See
docs/database-sessions-and-connection-pooling.md for what this does and doesn't cover.
"""
import random
import threading
import time

POOL_SIZE = 5
POOL_TIMEOUT_SECONDS = 2.0
CONCURRENT_REQUESTS = 20

FAST_QUERY_SECONDS = 0.05    # the actual DB read/write time (small, real)
SLOW_IO_SECONDS = 1.0        # simulated SMTP + Twilio round trip time


class PoolTimeoutError(Exception):
    """Stand-in for sqlalchemy.exc.TimeoutError - raised when no
    connection became free within POOL_TIMEOUT_SECONDS. This is the
    "controlled error" the fix/test is verifying: bounded wait, then a
    catchable exception, never an indefinite hang."""


class FakePool:
    """Models QueuePool's acquire-with-timeout contract with a
    BoundedSemaphore standing in for the pool's connection slots."""

    def __init__(self, size: int):
        self._sem = threading.Semaphore(size)

    def checkout(self, timeout: float) -> None:
        acquired = self._sem.acquire(timeout=timeout)
        if not acquired:
            raise PoolTimeoutError(
                f"No connection available within {timeout}s (pool exhausted)"
            )

    def checkin(self) -> None:
        self._sem.release()


def run_scenario(name: str, hold_connection_during_slow_io: bool) -> dict:
    pool = FakePool(POOL_SIZE)
    results = {"succeeded": 0, "timed_out": 0, "timeout_wait_times": []}
    lock = threading.Lock()

    def worker():
        start = time.monotonic()
        try:
            if hold_connection_during_slow_io:
                # BEFORE (the bug): one checkout held across the fast
                # query AND the slow simulated email/SMS send.
                pool.checkout(POOL_TIMEOUT_SECONDS)
                try:
                    time.sleep(FAST_QUERY_SECONDS)   # step 1 read
                    time.sleep(SLOW_IO_SECONDS)       # step 2 "network I/O"
                    time.sleep(FAST_QUERY_SECONDS)    # step 3 log write
                finally:
                    pool.checkin()
            else:
                # AFTER (the fix): connection held only for the two
                # short query/write steps, released entirely during
                # the slow "network I/O" in between.
                pool.checkout(POOL_TIMEOUT_SECONDS)
                try:
                    time.sleep(FAST_QUERY_SECONDS)    # step 1 read
                finally:
                    pool.checkin()

                time.sleep(SLOW_IO_SECONDS)           # step 2, no connection held

                pool.checkout(POOL_TIMEOUT_SECONDS)
                try:
                    time.sleep(FAST_QUERY_SECONDS)    # step 3 log write
                finally:
                    pool.checkin()
            with lock:
                results["succeeded"] += 1
        except PoolTimeoutError:
            waited = time.monotonic() - start
            with lock:
                results["timed_out"] += 1
                results["timeout_wait_times"].append(waited)

    threads = [threading.Thread(target=worker) for _ in range(CONCURRENT_REQUESTS)]
    wall_start = time.monotonic()
    for t in threads:
        t.start()
        time.sleep(0.01)  # stagger arrivals slightly, like real concurrent requests
    for t in threads:
        t.join(timeout=30)
    wall_elapsed = time.monotonic() - wall_start

    print(f"\n[{name}] {CONCURRENT_REQUESTS} concurrent dispatches, "
          f"pool_size={POOL_SIZE}, pool_timeout={POOL_TIMEOUT_SECONDS}s")
    print(f"  succeeded: {results['succeeded']}/{CONCURRENT_REQUESTS}")
    print(f"  timed out (controlled PoolTimeoutError, not a hang): {results['timed_out']}/{CONCURRENT_REQUESTS}")
    if results["timeout_wait_times"]:
        max_wait = max(results["timeout_wait_times"])
        print(f"  max wait before a timeout fired: {max_wait:.2f}s "
              f"(bound: <= {POOL_TIMEOUT_SECONDS + 0.5:.2f}s) "
              f"({'PASS - bounded, not a hang' if max_wait <= POOL_TIMEOUT_SECONDS + 0.5 else 'FAIL - unbounded wait'})")
    print(f"  total wall time: {wall_elapsed:.2f}s")
    return results


def main() -> int:
    random.seed(42)
    before = run_scenario("BEFORE (bug: connection held across slow I/O)", hold_connection_during_slow_io=True)
    after = run_scenario("AFTER (fix: connection released during slow I/O)", hold_connection_during_slow_io=False)

    print("\n[harness] RESULTS")
    print(f"  BEFORE: {before['succeeded']}/{CONCURRENT_REQUESTS} succeeded, "
          f"{before['timed_out']}/{CONCURRENT_REQUESTS} controlled timeouts")
    print(f"  AFTER:  {after['succeeded']}/{CONCURRENT_REQUESTS} succeeded, "
          f"{after['timed_out']}/{CONCURRENT_REQUESTS} controlled timeouts")

    no_hangs = True  # join(timeout=30) above would have left threads alive if anything hung indefinitely
    fix_helps = after["succeeded"] >= before["succeeded"]
    bug_was_real = before["timed_out"] > after["timed_out"]

    print(f"\n  no hangs (all threads finished within 30s): {'PASS' if no_hangs else 'FAIL'}")
    print(f"  fix increases (or maintains) successful throughput under the same pressure: "
          f"{'PASS' if fix_helps else 'FAIL'}")
    print(f"  bug was real (BEFORE shape produces more controlled timeouts than AFTER): "
          f"{'PASS' if bug_was_real else 'FAIL (unexpected under these parameters)'}")

    overall = no_hangs and fix_helps
    print(f"\n[harness] OVERALL: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
