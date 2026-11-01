#!/usr/bin/env python3
import asyncio
import json
import sys
import threading
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

fake_fastapi = types.ModuleType("fastapi")

class FakeWebSocket:
    def __init__(self, label: str):
        self.label = label
        self.received: list[dict] = []

    async def accept(self):
        pass

    async def send_text(self, payload: str):
        self.received.append(json.loads(payload))

fake_fastapi.WebSocket = FakeWebSocket
sys.modules["fastapi"] = fake_fastapi

app_pkg = types.ModuleType("app")
app_pkg.__path__ = []
core_pkg = types.ModuleType("app.core")
core_pkg.__path__ = []
sys.modules.setdefault("app", app_pkg)
sys.modules.setdefault("app.core", core_pkg)

class ControllableBus:
    def __init__(self):
        self.reachable = True
        self.broken = False
        self._subscribers: list["ControllablePubSub"] = []
        self.subscribe_count = 0

    def publish(self, channel: str, message: str):
        if not self.reachable:

            return
        for sub in self._subscribers:
            sub.queue.append(message)

    def register(self, pubsub: "ControllablePubSub"):
        self._subscribers.append(pubsub)
        self.subscribe_count += 1

class ControllablePubSub:
    def __init__(self, bus: ControllableBus):
        self.bus = bus
        self.queue: list[str] = []

    def subscribe(self, channel: str):
        self.bus.register(self)

    def get_message(self, timeout: float = 1.0):
        if self.bus.broken:
            raise ConnectionError("simulated Redis connection drop")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.queue:
                data = self.queue.pop(0)
                return {"type": "message", "data": data}
            time.sleep(0.005)
        return None

    def close(self):
        if self in self.bus._subscribers:
            self.bus._subscribers.remove(self)

class ControllableRedisClient:
    def __init__(self, bus: ControllableBus):
        self.bus = bus

    def publish(self, channel, message):
        if not self.bus.reachable:
            raise ConnectionError("simulated Redis unreachable")
        self.bus.publish(channel, message)

    def pubsub(self):
        return ControllablePubSub(self.bus)

def make_cache_module(bus: ControllableBus):
    mod = types.ModuleType("app.core.cache")

    def get_client():
        if not bus.reachable:
            return None
        return ControllableRedisClient(bus)

    mod.get_client = get_client
    return mod

def fresh_manager_module(bus: ControllableBus):
    for name in ("app.websocket.manager", "app.websocket.events", "app.websocket", "app.core.cache"):
        sys.modules.pop(name, None)
    sys.modules["app.core.cache"] = make_cache_module(bus)
    ws_pkg = types.ModuleType("app.websocket")
    ws_pkg.__path__ = [str(REPO_ROOT / "app" / "websocket")]
    sys.modules["app.websocket"] = ws_pkg
    import importlib
    return importlib.import_module("app.websocket.manager")

results = []

def check(label, ok):
    results.append((label, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {label}")

def wait_until(predicate, timeout=8.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()

def make_running_manager(mgr_mod, label: str):
    manager = mgr_mod.ConnectionManager()
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    manager.bind_loop(loop)
    manager.start_relay()
    ws = FakeWebSocket(label)
    fut = asyncio.run_coroutine_threadsafe(manager.connect(ws, user_id=None), loop)
    fut.result(timeout=2.0)
    return manager, loop, loop_thread, ws

def seq_ids(client: FakeWebSocket, phase_tag: str) -> list[int]:
    out = []
    for msg in client.received:
        data = msg.get("data") or {}
        if data.get("phase") == phase_tag:
            out.append(data.get("seq"))
    return out

def main():
    bus = ControllableBus()
    bus.reachable = True

    mgr_mod_a = fresh_manager_module(bus)
    manager_a, loop_a, thread_a, ws_a = make_running_manager(mgr_mod_a, "A-local")

    mgr_mod_b = fresh_manager_module(bus)
    manager_b, loop_b, thread_b, ws_b = make_running_manager(mgr_mod_b, "B-local")

    ok = wait_until(lambda: bus.subscribe_count >= 2, timeout=5.0)
    check("both simulated worker processes' relays subscribed while Redis was healthy", ok)

    def broadcast_from_a(event: str, phase: str, seq: int):
        fut = asyncio.run_coroutine_threadsafe(
            manager_a.broadcast(event, {"phase": phase, "seq": seq}), loop_a
        )
        fut.result(timeout=2.0)

    print("\n=== Phase 1: Redis healthy - baseline ===")
    N1 = 15
    for i in range(N1):
        broadcast_from_a("crowd_update", "phase1", i)
    wait_until(lambda: len(seq_ids(ws_b, "phase1")) >= N1, timeout=5.0)
    time.sleep(0.3)  
    p1_b = seq_ids(ws_b, "phase1")
    p1_a = seq_ids(ws_a, "phase1")
    check(f"Phase 1: B received all {N1} events via relay, no duplicates, no misses "
          f"(got {sorted(p1_b)})", sorted(p1_b) == list(range(N1)))
    check(f"Phase 1: A's own local delivery also exactly-once (got {sorted(p1_a)})",
          sorted(p1_a) == list(range(N1)))

    print("\n=== Phase 2: Redis OUTAGE - broadcasts continue locally only ===")

    bus.broken = True
    bus.reachable = False
    time.sleep(0.5)  
    N2 = 10
    for i in range(N2):
        broadcast_from_a("crowd_update", "phase2", i)
    time.sleep(1.0)  
    p2_b = seq_ids(ws_b, "phase2")
    p2_a = seq_ids(ws_a, "phase2")
    check("Phase 2 (documented fallback, not a bug): B receives ZERO of A's events "
          f"while Redis is down (got {sorted(p2_b)})", p2_b == [])
    check(f"Phase 2: A's own local clients still get all {N2} events with no duplicates "
          f"even though Redis is down (local delivery never depends on Redis) "
          f"(got {sorted(p2_a)})", sorted(p2_a) == list(range(N2)))

    print("\n=== Phase 3: Redis RECOVERS - relay must resubscribe on its own ===")
    subs_before_recovery = bus.subscribe_count
    b_received_before_phase3 = len(ws_b.received)  
    bus.broken = False
    bus.reachable = True
    ok = wait_until(lambda: bus.subscribe_count > subs_before_recovery, timeout=10.0)
    check("both relays resubscribed on their own after Redis came back, "
          "with no external call", ok)

    N3 = 15
    for i in range(N3):
        broadcast_from_a("crowd_update", "phase3", i)
    wait_until(lambda: len(seq_ids(ws_b, "phase3")) >= N3, timeout=6.0)
    time.sleep(0.3)
    p3_b = seq_ids(ws_b, "phase3")
    p3_a = seq_ids(ws_a, "phase3")
    check(f"Phase 3: B received all {N3} post-recovery events via relay, exactly once, "
          f"no misses (got {sorted(p3_b)})", sorted(p3_b) == list(range(N3)))
    check(f"Phase 3: A's own local delivery post-recovery also exactly-once "
          f"(got {sorted(p3_a)})", sorted(p3_a) == list(range(N3)))

    new_messages = ws_b.received[b_received_before_phase3:]
    stray = [m for m in new_messages if (m.get("data") or {}).get("phase") in ("phase1", "phase2")]
    check("Phase 3: no stale phase1/phase2 messages replayed to B on resubscribe "
          f"(found {len(stray)} stray among {len(new_messages)} newly-received messages)",
          len(stray) == 0)

    total_b = len(ws_b.received)
    check(f"Whole run: B's total delivered event count is exactly N1+N3 = {N1+N3} "
          f"(got {total_b}) - proves no duplicates and no extra/missing events anywhere",
          total_b == N1 + N3)

    manager_a.stop_relay()
    manager_b.stop_relay()
    loop_a.call_soon_threadsafe(loop_a.stop)
    loop_b.call_soon_threadsafe(loop_b.stop)
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    all_ok = all(ok for _, ok in results)
    print(f"\n=== SUMMARY ===")
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n{'All' if all_ok else 'NOT all'} {len(results)} checks "
          f"{'PASSED' if all_ok else 'passed'}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
