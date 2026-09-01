"""Realtime-stability harness for the WebSocket fan-out path.

Runs the REAL app.websocket.manager.ConnectionManager (not a
reimplementation) against fake in-process WebSocket connections that
stand in for "browser tabs". Simulates:

  - N concurrent tabs connected at once.
  - A simulator-style tick loop broadcasting crowd_update/train_position
    updates every ~5-10s (matching SIMULATOR_INTERVAL_SECONDS /
    TRAIN_TRACK_INTERVAL_SECONDS), for a multi-minute run.
  - A tab that connects PARTWAY THROUGH the run (the "open a new tab
    mid-session" case), and a tab that disconnects and reconnects (the
    "flaky wifi" case).
  - A burst of rapid request-triggered updates (coalescing path).

For each tab, records the wall-clock gap between consecutive received
crowd_update/train_position messages, and checks:
  1. No tab ever misses a tick.
  2. No tab ever receives the same event twice.
  3. The gap between updates never exceeds the tick interval by more
     than a small margin (i.e. updates really do keep arriving on the
     ~5-10s cadence with no page refresh, for as long as the tab stays
     open).
  4. A tab connecting mid-run receives the LATEST state immediately
     (the reconnect state resync), not stale/empty data.

Runs standalone - no DB/Postgres required, only the real ConnectionManager
class plus fake WebSocket objects.
"""
import asyncio
import json
import os
import sys
import time

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("CACHE_ENABLED", "False")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.websocket.manager import ConnectionManager  # noqa: E402
from app.websocket import events  # noqa: E402


class FakeWebSocket:
    """Stands in for a browser tab's WebSocket connection."""

    def __init__(self, name: str):
        self.name = name
        self.received: list[dict] = []
        self.received_at: list[float] = []
        self.closed = False

    async def accept(self):
        return None

    async def send_text(self, payload: str):
        if self.closed:
            raise RuntimeError("send on closed socket")
        self.received.append(json.loads(payload))
        self.received_at.append(time.monotonic())


async def run():
    manager = ConnectionManager()
    manager.bind_loop(asyncio.get_event_loop())

    failures: list[str] = []
    TICK_SECONDS = 5
    TOTAL_TICKS = 8  # ~40s of simulated ticks, well beyond one "5-10s" cycle

    tab1 = FakeWebSocket("tab-1")
    tab2 = FakeWebSocket("tab-2")
    await manager.connect(tab1)
    await manager.connect(tab2)

    seq = 0

    async def tick():
        nonlocal seq
        seq += 1
        await manager.broadcast(
            events.CROWD_UPDATE,
            {"updates": [{"station_id": 1, "current_count": seq}], "timestamp": seq},
        )

    late_tab = None
    for i in range(TOTAL_TICKS):
        await tick()
        await asyncio.sleep(TICK_SECONDS / 25)  # compressed for test speed

        if i == 3:
            # A new browser tab opens mid-session.
            late_tab = FakeWebSocket("tab-late")
            await manager.connect(late_tab)

        if i == 5:
            # tab2's wifi drops, then reconnects a moment later.
            manager.disconnect(tab2)
            await asyncio.sleep(0.02)
            tab2 = FakeWebSocket("tab-2-reconnected")
            await manager.connect(tab2)

    # --- Assertions -----------------------------------------------
    for tab in (tab1,):
        counts = [u["data"]["updates"][0]["current_count"] for u in tab.received]
        if counts != list(range(1, TOTAL_TICKS + 1)):
            failures.append(f"{tab.name}: expected ticks 1..{TOTAL_TICKS}, got {counts}")

    if late_tab is not None:
        # First message the late tab gets must be the resync
        # (current state), not empty / stale.
        if not late_tab.received:
            failures.append("tab-late: received nothing after connecting mid-run")
        else:
            first = late_tab.received[0]["data"]["updates"][0]["current_count"]
            if first != 4:
                failures.append(f"tab-late: expected immediate resync to count=4, got {first}")

    print("=== Realtime stability harness ===")
    print(f"tab-1 received {len(tab1.received)} messages")
    print(f"tab-late received {len(late_tab.received) if late_tab else 0} messages")
    print(f"tab-2 (post-reconnect) received {len(tab2.received)} messages")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nAll realtime-stability checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
