#!/usr/bin/env python3
import asyncio
import json
import sys
import threading
import time
import types
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

fake_fastapi = types.ModuleType("fastapi")

class FakeWebSocket:

    def __init__(self, name: str, fail_mode: str | None = None):
        self.name = name
        self.fail_mode = fail_mode  
        self.sent: list[str] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, payload: str):
        if self.fail_mode == "raise":
            raise ConnectionResetError("simulated dead peer")
        if self.fail_mode == "hang":
            await asyncio.sleep(999)  
        self.sent.append(payload)

fake_fastapi.WebSocket = FakeWebSocket
sys.modules["fastapi"] = fake_fastapi

app_pkg = types.ModuleType("app")
app_pkg.__path__ = []  
core_pkg = types.ModuleType("app.core")
core_pkg.__path__ = []
sys.modules.setdefault("app", app_pkg)
sys.modules.setdefault("app.core", core_pkg)

class FakeBus:

    def __init__(self):
        self._subscribers: dict[str, list["FakePubSub"]] = {}
        self.publish_calls: list[tuple[str, str]] = []

    def publish(self, channel: str, message: str):
        self.publish_calls.append((channel, message))
        for sub in self._subscribers.get(channel, []):
            sub.queue.append(message)

    def _register(self, channel: str, pubsub: "FakePubSub"):
        self._subscribers.setdefault(channel, []).append(pubsub)

class FakePubSub:
    def __init__(self, bus: FakeBus):
        self.bus = bus
        self.queue: list[str] = []
        self.closed = False

    def subscribe(self, channel: str):
        self.bus._register(channel, self)

    def get_message(self, timeout: float = 1.0):

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.queue:
                data = self.queue.pop(0)
                return {"type": "message", "data": data}
            time.sleep(0.005)
        return None

    def close(self):
        self.closed = True

class FakeRedisClient:
    def __init__(self, bus: FakeBus):
        self.bus = bus

    def publish(self, channel, message):
        self.bus.publish(channel, message)

    def pubsub(self):
        return FakePubSub(self.bus)

def make_fake_cache_module(bus: FakeBus | None):
    mod = types.ModuleType("app.core.cache")
    client = FakeRedisClient(bus) if bus is not None else None
    mod.get_client = lambda: client
    return mod

def fresh_manager_module(bus: FakeBus | None):
    for name in ("app.websocket.manager", "app.websocket.events", "app.websocket",
                 "app.core.cache"):
        sys.modules.pop(name, None)
    sys.modules["app.core.cache"] = make_fake_cache_module(bus)
    ws_pkg = types.ModuleType("app.websocket")
    ws_pkg.__path__ = [str(REPO_ROOT / "app" / "websocket")]
    sys.modules["app.websocket"] = ws_pkg
    import importlib
    return importlib.import_module("app.websocket.manager")

class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=5):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)

results = []

def check(label, condition):
    results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")

def wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()

def test_coalescing_and_empty_local_publish():
    print("\n=== Test 1+3: coalescing, and relay publish with 0 local clients ===")
    bus = FakeBus()
    mgr_mod = fresh_manager_module(bus)
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    events_mod = sys.modules["app.websocket.events"]

    manager.notify(events_mod.CROWD_UPDATE, {
        "updates": [{"station_id": 1, "current_count": 10}],
        "timestamp": "t1",
    })
    manager.notify(events_mod.CROWD_UPDATE, {
        "updates": [{"station_id": 2, "current_count": 20}],
        "timestamp": "t2",
    })
    manager.notify(events_mod.CROWD_UPDATE, {
        "updates": [{"station_id": 1, "current_count": 15}],  
        "timestamp": "t3",
    })

    ok = wait_until(lambda: len(bus.publish_calls) >= 1, timeout=2.0)
    check("relay publish happened despite 0 local connections (bug #2 fix)", ok)

    if ok:
        channel, message = bus.publish_calls[-1]
        payload = json.loads(message)
        updates = payload["data"]["updates"]
        by_station = {u["station_id"]: u for u in updates}
        check("exactly one publish call (3 notify() calls coalesced into 1)",
              len(bus.publish_calls) == 1)
        check("merged update has both stations", set(by_station.keys()) == {1, 2})
        check("last-value-wins for station 1 (15, not 10)",
              by_station.get(1, {}).get("current_count") == 15)

    lt.stop()

def test_dedup():
    print("\n=== Test 2: de-duplication ===")
    bus = FakeBus()
    mgr_mod = fresh_manager_module(bus)
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    ws = FakeWebSocket("client-1")
    lt.run(manager.connect(ws))

    same_id = uuid.uuid4().hex
    lt.run(manager.broadcast("crowd_update", {"x": 1}, _event_id=same_id))
    lt.run(manager.broadcast("crowd_update", {"x": 1}, _event_id=same_id))  
    lt.run(manager.broadcast("crowd_update", {"x": 2}, _event_id=uuid.uuid4().hex))  

    check("duplicate event_id delivered exactly once, distinct one still delivered "
          f"(got {len(ws.sent)} sends, expected 2)", len(ws.sent) == 2)

    lt.stop()

def test_cross_process_relay():
    print("\n=== Test 4: cross-process relay dispatch (broadcast + targeted) ===")
    bus = FakeBus()

    mod_a = fresh_manager_module(bus)
    manager_a = mod_a.ConnectionManager()
    lt_a = LoopThread()
    manager_a.bind_loop(lt_a.loop)
    manager_a.start_relay()

    mod_b = fresh_manager_module(bus)
    manager_b = mod_b.ConnectionManager()
    lt_b = LoopThread()
    manager_b.bind_loop(lt_b.loop)
    manager_b.start_relay()

    ws_a_anon = FakeWebSocket("A-anon")
    ws_b_anon = FakeWebSocket("B-anon")
    ws_b_user7 = FakeWebSocket("B-user7")
    ws_b_user8 = FakeWebSocket("B-user8")
    lt_a.run(manager_a.connect(ws_a_anon))
    lt_b.run(manager_b.connect(ws_b_anon))
    lt_b.run(manager_b.connect(ws_b_user7, user_id="7"))
    lt_b.run(manager_b.connect(ws_b_user8, user_id="8"))

    lt_a.run(manager_a.broadcast("station_alert", {"msg": "flooding"}))

    ok = wait_until(lambda: len(ws_b_anon.sent) == 1, timeout=2.0)
    check("process B's anonymous client received A's broadcast via relay", ok)
    check("process A's own client received it exactly once (direct, not double-delivered)",
          len(ws_a_anon.sent) == 1)

    lt_a.run(manager_a.broadcast_to_user("7", "notification", {"title": "hi user 7"}))

    ok2 = wait_until(lambda: len(ws_b_user7.sent) == 2, timeout=2.0)
    check("targeted notify reached user 7's connection on process B", ok2)

    check("targeted notify did NOT also reach user 8's connection",
          len(ws_b_user8.sent) == 1)
    check("targeted notify did NOT also reach B's anonymous connection",
          len(ws_b_anon.sent) == 1)

    manager_a.stop_relay()
    manager_b.stop_relay()
    lt_a.stop()
    lt_b.stop()

def test_reaper():
    print("\n=== Test 5: stale-connection reaping ===")
    bus = FakeBus()
    mgr_mod = fresh_manager_module(bus)
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    healthy = FakeWebSocket("healthy")
    dead = FakeWebSocket("dead", fail_mode="raise")
    lt.run(manager.connect(healthy))
    lt.run(manager.connect(dead))

    stale_time = time.monotonic() - (mgr_mod.STALE_AFTER_SECONDS + 5)
    manager._last_seen[healthy] = stale_time
    manager._last_seen[dead] = stale_time

    lt.run(manager._reap_stale())

    check("dead connection was dropped by the reaper", dead not in manager.active_connections)
    check("healthy connection was kept", healthy in manager.active_connections)
    check("healthy connection's last_seen was refreshed (won't be re-probed immediately)",
          manager._last_seen.get(healthy, 0) > stale_time)

    lt.stop()

if __name__ == "__main__":
    test_coalescing_and_empty_local_publish()
    test_dedup()
    test_cross_process_relay()
    test_reaper()

    print("\n=== SUMMARY ===")
    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if failed:
        print(f"\n{len(failed)} check(s) FAILED")
        sys.exit(1)
    print(f"\nAll {len(results)} checks PASSED")
