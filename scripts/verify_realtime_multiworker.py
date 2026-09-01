"""Realtime-stability test across TWO simulated API worker processes,
each with its own ConnectionManager instance, wired to a REAL local
Redis instance for the cross-process relay (see docs/realtime-websocket-system.md).

This is the actual "multiple browser tabs" production scenario: a load
balancer can put tab A's socket on worker 1 and tab B's socket on
worker 2. An event raised on worker 1 (a simulator tick, or a
request-triggered alert that happened to land on worker 1) must still
reach tab B on worker 2, continuously, for as long as both tabs stay
open - with no duplicate deliveries and no dropped ticks.

Requires a real Redis reachable at REDIS_URL (redis://localhost:6379/0
by default) - this is NOT a mock.
"""
import asyncio
import json
import os
import sys
import time

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("CACHE_ENABLED", "True")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

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

    # Two independent manager instances = two independent worker
    # processes. Each starts its OWN relay subscriber thread against
    # the same real Redis instance, exactly like two real uvicorn
    # workers would.
    worker1 = ConnectionManager()
    worker1.bind_loop(loop)
    worker1.start_relay()

    worker2 = ConnectionManager()
    worker2.bind_loop(loop)
    worker2.start_relay()

    # Give both relay threads a moment to actually subscribe before
    # anything is published - otherwise the first publish could race
    # a subscriber that hasn't attached yet (a real startup race, but
    # not what this test is checking).
    await asyncio.sleep(1.0)

    tab_a = FakeWebSocket("tab-A-on-worker1")
    await worker1.connect(tab_a)

    tab_b = FakeWebSocket("tab-B-on-worker2")
    await worker2.connect(tab_b)

    TICKS = 6
    for i in range(1, TICKS + 1):
        # Ticks alternate which "worker" happens to be the elected
        # simulator leader for that tick - in production this can
        # change over time as leadership fails over.
        leader = worker1 if i % 2 else worker2
        await leader.broadcast(
            events.CROWD_UPDATE,
            {"updates": [{"station_id": 1, "current_count": i}], "timestamp": i},
        )
        await asyncio.sleep(0.5)  # real network round-trip through Redis

    # Let the last publish's relay round-trip land.
    await asyncio.sleep(0.5)

    worker1.stop_relay()
    worker2.stop_relay()

    failures = []

    def counts(tab):
        return [u["data"]["updates"][0]["current_count"] for u in tab.received]

    a_counts = counts(tab_a)
    b_counts = counts(tab_b)

    print("=== Multi-worker (real Redis relay) realtime stability ===")
    print("tab-A (worker1) received:", a_counts)
    print("tab-B (worker2) received:", b_counts)

    expected = list(range(1, TICKS + 1))
    if a_counts != expected:
        failures.append(f"tab-A: expected {expected}, got {a_counts}")
    if b_counts != expected:
        failures.append(f"tab-B: expected {expected}, got {b_counts}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nBoth tabs, on different simulated workers, received every "
          "tick exactly once via the real Redis relay - no drops, no "
          "duplicates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
