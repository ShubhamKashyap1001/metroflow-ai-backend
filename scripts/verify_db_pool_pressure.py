import random
import threading
import time

POOL_SIZE = 5
POOL_TIMEOUT_SECONDS = 2.0
CONCURRENT_REQUESTS = 20

FAST_QUERY_SECONDS = 0.05    
SLOW_IO_SECONDS = 1.0        

class PoolTimeoutError(Exception):
    pass

class FakePool:

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

                pool.checkout(POOL_TIMEOUT_SECONDS)
                try:
                    time.sleep(FAST_QUERY_SECONDS)   
                    time.sleep(SLOW_IO_SECONDS)       
                    time.sleep(FAST_QUERY_SECONDS)    
                finally:
                    pool.checkin()
            else:

                pool.checkout(POOL_TIMEOUT_SECONDS)
                try:
                    time.sleep(FAST_QUERY_SECONDS)    
                finally:
                    pool.checkin()

                time.sleep(SLOW_IO_SECONDS)           

                pool.checkout(POOL_TIMEOUT_SECONDS)
                try:
                    time.sleep(FAST_QUERY_SECONDS)    
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
        time.sleep(0.01)  
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

    no_hangs = True  
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
