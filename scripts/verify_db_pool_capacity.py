"""Phase 17E verification script, in two parts:

  1. WORKER x POOL vs POSTGRES CAPACITY - runs the real
     `check_pool_capacity()` from app/database/database.py (imported
     directly, not re-implemented) against this project's actual
     configured pool numbers (DB_POOL_SIZE=15, DB_MAX_OVERFLOW=25 ->
     40/process) across a range of worker counts and common Postgres
     `max_connections` tiers, so the exact point at which a deployment
     becomes unsafe is visible, not just asserted.

  2. CONCURRENT REQUEST PRESSURE - the same acquire-with-timeout
     BoundedSemaphore model Phase 6's scripts/verify_db_pool_pressure.py
     already validated for the alert-dispatch bug, reused here against
     the real ceiling (40) and the real DB_POOL_TIMEOUT (10s), with
     MORE concurrent requests than the pool can hold at once - proving
     the failure mode under real pressure is a fast, bounded, catchable
     error (matching app/main.py's db_pool_exhausted_handler contract),
     never an indefinite hang, and that ordinary (non-alert) concurrent
     traffic well under the ceiling sails through cleanly.

Same sandbox constraint as every prior phase: no network access to
`pip install`, so this cannot drive a real SQLAlchemy QueuePool against
a real Postgres. Part 1 imports and executes the actual shipped
arithmetic (not a re-implementation). Part 2 models QueuePool's
documented acquire/timeout/release contract with real OS threads
(FastAPI's actual execution model for sync route handlers).
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.database import check_pool_capacity  # noqa: E402

DB_POOL_SIZE = 15
DB_MAX_OVERFLOW = 25
DB_POOL_TIMEOUT = 10.0
DB_CONNECTION_RESERVE = 10
PER_PROCESS_CEILING = DB_POOL_SIZE + DB_MAX_OVERFLOW  # 40, matches settings defaults


def part1_worker_capacity_matrix() -> bool:
    print("=" * 72)
    print("PART 1 - worker x pool ceiling vs. Postgres max_connections")
    print(f"  (DB_POOL_SIZE={DB_POOL_SIZE}, DB_MAX_OVERFLOW={DB_MAX_OVERFLOW} -> "
          f"{PER_PROCESS_CEILING}/process, DB_CONNECTION_RESERVE={DB_CONNECTION_RESERVE})")
    print("=" * 72)

    # Common real-world Postgres max_connections values: a small
    # managed/free-tier instance, a mid-size default, and a larger
    # tuned instance.
    postgres_tiers = [20, 100, 200]
    worker_counts = [1, 2, 4, 8]

    all_ok = True
    header = f"{'workers':>8} | " + " | ".join(f"max_conn={t:>4}" for t in postgres_tiers)
    print(header)
    print("-" * len(header))
    for workers in worker_counts:
        row = [f"{workers:>8} |"]
        for max_conn in postgres_tiers:
            try:
                ceiling, safe = check_pool_capacity(
                    pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW,
                    web_concurrency=workers, reserve=DB_CONNECTION_RESERVE,
                    max_connections=max_conn,
                )
                row.append(f" OK ({ceiling}<={safe}) ")
            except RuntimeError:
                row.append(f" REFUSED ({workers * PER_PROCESS_CEILING}) ")
        print(" ".join(row))

    # Sanity-check the boundary this project actually ships with:
    # a single worker against a typical 100-connection Postgres must
    # pass (this is the shipped default, it must not refuse to boot
    # out of the box).
    try:
        check_pool_capacity(
            pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW,
            web_concurrency=1, reserve=DB_CONNECTION_RESERVE,
            max_connections=100,
        )
        print("\n[part1] shipped defaults (1 worker, max_connections=100): OK - PASS")
    except RuntimeError as exc:
        print(f"\n[part1] shipped defaults unexpectedly REFUSED: {exc} - FAIL")
        all_ok = False

    # And confirm it DOES refuse a genuinely unsafe scaling scenario
    # (4 workers against a small 20-connection server) rather than
    # silently allowing it.
    try:
        check_pool_capacity(
            pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW,
            web_concurrency=4, reserve=DB_CONNECTION_RESERVE,
            max_connections=20,
        )
        print("[part1] 4 workers vs. max_connections=20 unexpectedly ALLOWED - FAIL")
        all_ok = False
    except RuntimeError:
        print("[part1] 4 workers vs. max_connections=20 correctly REFUSED - PASS")

    return all_ok


class FakePool:
    def __init__(self, size: int):
        self._sem = threading.Semaphore(size)

    def checkout(self, timeout: float) -> None:
        if not self._sem.acquire(timeout=timeout):
            raise TimeoutError(f"No connection available within {timeout}s (pool exhausted)")

    def checkin(self) -> None:
        self._sem.release()


def _run_concurrent_requests(pool: FakePool, request_count: int, hold_seconds: float) -> dict:
    results = {"succeeded": 0, "timed_out": 0, "wait_times": []}
    lock = threading.Lock()

    def worker():
        start = time.monotonic()
        try:
            pool.checkout(DB_POOL_TIMEOUT)
            try:
                time.sleep(hold_seconds)  # the actual query/write duration
            finally:
                pool.checkin()
            with lock:
                results["succeeded"] += 1
        except TimeoutError:
            with lock:
                results["timed_out"] += 1
                results["wait_times"].append(time.monotonic() - start)

    threads = [threading.Thread(target=worker) for _ in range(request_count)]
    for t in threads:
        t.start()
        time.sleep(0.002)  # stagger arrivals, like real concurrent traffic
    for t in threads:
        t.join(timeout=DB_POOL_TIMEOUT + 20)  # generous bound; a hang would blow this
    return results


def part2_concurrent_pressure() -> bool:
    print("\n" + "=" * 72)
    print(f"PART 2 - concurrent request pressure against the real ceiling "
          f"({PER_PROCESS_CEILING} connections, {DB_POOL_TIMEOUT}s pool_timeout)")
    print("=" * 72)

    all_ok = True

    # Scenario A: ordinary load, well under the ceiling. Every request
    # should succeed with essentially no contention - proves normal
    # traffic isn't affected by the pool existing at all.
    pool = FakePool(PER_PROCESS_CEILING)
    normal_load = 20  # half the ceiling
    result = _run_concurrent_requests(pool, normal_load, hold_seconds=0.05)
    print(f"\n[scenario A] {normal_load} concurrent requests, well under the "
          f"{PER_PROCESS_CEILING}-connection ceiling, {result['succeeded']}/{normal_load} succeeded")
    scenario_a_ok = result["succeeded"] == normal_load and result["timed_out"] == 0
    print(f"  {'PASS' if scenario_a_ok else 'FAIL'}")
    all_ok &= scenario_a_ok

    # Scenario B: a sustained burst that genuinely outpaces the pool's
    # drain rate - not just briefly over capacity. With 40 slots each
    # held ~2s, this pool can complete ~20 requests/sec; 300 requests
    # arriving almost at once means the backlog take ~13s to clear -
    # longer than DB_POOL_TIMEOUT (10s) - so this scenario is
    # calibrated to actually produce real timeouts, not merely claim
    # to (an earlier version of this script used a burst that drained
    # before anyone waited long enough to time out, which proved
    # nothing). Some requests MUST be turned away here, but as FAST,
    # CONTROLLED timeouts - never a hang - matching
    # app/main.py's db_pool_exhausted_handler contract.
    pool = FakePool(PER_PROCESS_CEILING)
    burst_load = 300
    result = _run_concurrent_requests(pool, burst_load, hold_seconds=2.0)
    max_wait = max(result["wait_times"]) if result["wait_times"] else 0.0
    print(f"\n[scenario B] {burst_load} concurrent requests (burst) against the "
          f"{PER_PROCESS_CEILING}-connection ceiling:")
    print(f"  succeeded: {result['succeeded']}/{burst_load}")
    print(f"  controlled timeouts (not hangs): {result['timed_out']}/{burst_load}")
    bounded = max_wait <= DB_POOL_TIMEOUT + 0.5
    print(f"  max wait before a timeout fired: {max_wait:.2f}s (bound: <= {DB_POOL_TIMEOUT + 0.5:.1f}s) "
          f"- {'PASS' if bounded else 'FAIL'}")
    no_hang = (result["succeeded"] + result["timed_out"]) == burst_load
    print(f"  every request accounted for (none stuck/lost): "
          f"{'PASS' if no_hang else 'FAIL'}")
    all_ok &= bounded and no_hang

    return all_ok


def main() -> int:
    ok1 = part1_worker_capacity_matrix()
    ok2 = part2_concurrent_pressure()
    overall = ok1 and ok2
    print("\n" + "=" * 72)
    print(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    print("=" * 72)
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
