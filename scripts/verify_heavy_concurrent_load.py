"""Ad-hoc heavy-load stress test (not part of the curated verify_*.py
suite): many simulated worker processes, many clients each, MANY
events published concurrently and as fast as possible from multiple
threads at once (real thread concurrency, not just asyncio
interleaving) against a REAL local Redis relay. Looks specifically for
duplicate, missing, and delayed deliveries under load.

Requires a real Redis reachable at REDIS_URL.
"""
import asyncio
import json
import os
import statistics
import sys
import threading
import time

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("CACHE_ENABLED", "True")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.websocket.manager import ConnectionManager  # noqa: E402
from app.websocket import events  # noqa: E402

N_WORKERS = 5
CLIENTS_PER_WORKER = 8
EVENTS_PER_PUBLISH_THREAD = 150
PUBLISH_THREADS_PER_WORKER = 4  # real OS threads hammering notify() concurrently


class FakeWebSocket:
    def __init__(self, name):
        self.name = name
        self.received = []
        self._recv_times = []

    async def accept(self):
        return None

    async def send_text(self, payload):
        self.received.append(json.loads(payload))
        self._recv_times.append(time.monotonic())


async def run():
    loop = asyncio.get_event_loop()

    workers = []
    for w in range(N_WORKERS):
        mgr = ConnectionManager()
        mgr.bind_loop(loop)
        mgr.start_relay()
        workers.append(mgr)

    # Let every relay thread actually subscribe before publishing.
    await asyncio.sleep(1.5)

    all_clients = []
    for w, mgr in enumerate(workers):
        for c in range(CLIENTS_PER_WORKER):
            ws = FakeWebSocket(f"worker{w}-client{c}")
            await mgr.connect(ws, user_id=None)
            all_clients.append(ws)

    total_expected = N_WORKERS * PUBLISH_THREADS_PER_WORKER * EVENTS_PER_PUBLISH_THREAD
    send_times = {}  # event_id -> send monotonic time
    send_times_lock = threading.Lock()

    def publisher(mgr, worker_idx, thread_idx):
        for i in range(EVENTS_PER_PUBLISH_THREAD):
            eid = f"w{worker_idx}-t{thread_idx}-e{i}"
            t0 = time.monotonic()
            with send_times_lock:
                send_times[eid] = t0
            # notify() is the sync/thread-safe entry point real service
            # code (alert_service, notification_service, etc.) calls
            # from FastAPI's threadpool - exactly what's being stressed
            # here: many real OS threads, across many "processes",
            # hammering it concurrently.
            # STATION_ALERT is intentionally NOT one of the coalesced
            # event types (see _COALESCE_MERGE_KEY in manager.py) - it
            # is dispatched instantly, one WS message per notify()
            # call, which is what per-event dup/missing/delay counting
            # below assumes. (crowd_update/train_position are merged
            # by design and would be the wrong event type to use here.)
            mgr.notify(events.STATION_ALERT, {"event_marker": eid})

    threads = []
    t_start = time.monotonic()
    for w, mgr in enumerate(workers):
        for t in range(PUBLISH_THREADS_PER_WORKER):
            th = threading.Thread(target=publisher, args=(mgr, w, t))
            threads.append(th)
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t_publish_done = time.monotonic()

    # Give the relay + event loop time to fully drain across every
    # worker/client before grading.
    await asyncio.sleep(5.0)
    t_drain_done = time.monotonic()

    print(f"=== Heavy concurrent load: {N_WORKERS} workers x "
          f"{CLIENTS_PER_WORKER} clients, {total_expected} total events "
          f"published from {len(threads)} concurrent OS threads ===")
    print(f"publish wall time: {t_publish_done - t_start:.2f}s, "
          f"drain wait: {t_drain_done - t_publish_done:.2f}s")

    failures = []
    all_delays = []

    for ws in all_clients:
        markers = [m["data"].get("event_marker") for m in ws.received if "event_marker" in m.get("data", {})]
        counts = {}
        for m in markers:
            counts[m] = counts.get(m, 0) + 1
        dupes = {k: v for k, v in counts.items() if v > 1}
        missing = total_expected - len(set(markers))
        if dupes:
            failures.append(f"{ws.name}: {len(dupes)} duplicate event ids (e.g. {list(dupes.items())[:3]})")
        if len(set(markers)) != total_expected:
            failures.append(f"{ws.name}: expected {total_expected} distinct events, got {len(set(markers))} (missing {total_expected - len(set(markers))})")

    # Delay analysis: for each client, look at recv time vs publish
    # time for every marker it did receive.
    for ws in all_clients:
        for m, t_recv in zip(
            [msg["data"].get("event_marker") for msg in ws.received if "event_marker" in msg.get("data", {})],
            ws._recv_times,
        ):
            with send_times_lock:
                t_send = send_times.get(m)
            if t_send is not None:
                all_delays.append(t_recv - t_send)

    if all_delays:
        all_delays.sort()
        p50 = statistics.median(all_delays)
        p99 = all_delays[int(len(all_delays) * 0.99)]
        worst = all_delays[-1]
        print(f"delivery latency: p50={p50*1000:.1f}ms p99={p99*1000:.1f}ms worst={worst*1000:.1f}ms")
        if worst > 5.0:
            failures.append(f"worst-case delivery delay {worst:.2f}s exceeds 5s threshold")

    if failures:
        print(f"\n{len(failures)} FAILURE(S) FOUND:")
        for f in failures[:30]:
            print(f"  [FAIL] {f}")
        if len(failures) > 30:
            print(f"  ... and {len(failures) - 30} more")
        sys.exit(1)
    else:
        print("\nNo duplicate, missing, or excessively delayed events across "
              f"{len(all_clients)} clients on {N_WORKERS} simulated workers "
              f"under concurrent multi-thread publish load. PASS.")


if __name__ == "__main__":
    asyncio.run(run())
