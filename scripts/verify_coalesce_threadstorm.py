"""Tests ConnectionManager.notify()'s coalescing path (see
COALESCE_WINDOW_SECONDS / _buffer_for_coalesce in manager.py) under
concurrent calls from real OS threads - simulating several check-in
requests landing on FastAPI's threadpool at (almost) the same instant,
which is exactly how crowd_service triggers a crowd_update today.

notify() is documented as "sync, thread-safe, fire-and-forget" and is
called from plain `def` (non-async) request handlers running on
FastAPI's threadpool executor, NOT on the asyncio event loop. This
schedules work onto the loop via asyncio.run_coroutine_threadsafe, so
this test reproduces that exact shape: a real asyncio event loop
running in the main thread, and several worker OS threads calling
manager.notify() concurrently, the way uvicorn's threadpool really
would.
"""
import asyncio
import json
import os
import sys
import threading
import time

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("CACHE_ENABLED", "False")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.websocket.manager import ConnectionManager  # noqa: E402
from app.websocket import events  # noqa: E402


class FakeWebSocket:
    def __init__(self, name: str):
        self.name = name
        self.received: list[dict] = []

    async def accept(self):
        return None

    async def send_text(self, payload: str):
        self.received.append(json.loads(payload))


async def run():
    loop = asyncio.get_event_loop()
    manager = ConnectionManager()
    manager.bind_loop(loop)

    tab = FakeWebSocket("dashboard-tab")
    await manager.connect(tab)

    NUM_STATIONS = 12
    NUM_THREADS = 12
    barrier = threading.Barrier(NUM_THREADS)

    def worker(i: int):
        # Every real request-handling thread hits the barrier at once,
        # then calls notify() - reproducing a genuine "request storm"
        # (several check-ins landing in the same instant) rather than
        # calls that happen to be staggered enough to never race.
        barrier.wait()
        manager.notify(
            events.CROWD_UPDATE,
            {"updates": [{"station_id": i, "current_count": 100 + i}], "timestamp": "t1"},
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_STATIONS)]
    for t in threads:
        t.start()

    # Let the event loop actually run while the OS threads execute -
    # asyncio.run_coroutine_threadsafe needs the loop to be spinning
    # to pick up each scheduled coroutine.
    start = time.monotonic()
    while any(t.is_alive() for t in threads) and time.monotonic() - start < 5:
        await asyncio.sleep(0.01)
    for t in threads:
        t.join(timeout=2)

    # Coalesce window is 0.2s - wait comfortably past it.
    await asyncio.sleep(0.6)

    print("=== Threadpool request-storm coalescing ===")
    print(f"{NUM_STATIONS} concurrent threads called notify() at once.")
    print(f"Dashboard tab received {len(tab.received)} WebSocket message(s).")

    failures = []
    if len(tab.received) == 0:
        failures.append("Tab received ZERO messages - notify() from worker threads was lost entirely.")
    elif len(tab.received) > 1:
        failures.append(
            f"Expected exactly 1 coalesced message merging all {NUM_STATIONS} station "
            f"updates, but got {len(tab.received)} separate messages - coalescing did not "
            f"merge concurrent threadpool calls."
        )
    else:
        updates = tab.received[0]["data"]["updates"]
        station_ids = sorted(u["station_id"] for u in updates)
        expected_ids = list(range(NUM_STATIONS))
        if station_ids != expected_ids:
            failures.append(
                f"Coalesced message is missing station updates: got {station_ids}, "
                f"expected {expected_ids} - some concurrent notify() calls were dropped "
                f"under the race, not merged."
            )
        else:
            print(f"All {NUM_STATIONS} station updates merged into a single message: OK.")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nRequest-storm coalescing check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
