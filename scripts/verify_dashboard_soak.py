#!/usr/bin/env python3
import asyncio
import importlib
import json
import sys
import threading
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TICK_SECONDS = 7          
NUM_TICKS = 17            
NUM_TABS = 3
RESTART_AFTER_TICK = 7    
TAB_DROP_AFTER_TICK = 4   
TAB_DROP_TICKS = 2        

class FakeWebSocket:
    def __init__(self, name):
        self.name = name
        self.sent: list[str] = []
        self.accepted = False
        self.alive = True

    async def accept(self):
        self.accepted = True

    async def send_text(self, payload):
        if not self.alive:
            raise ConnectionResetError("simulated drop")
        self.sent.append(payload)

fake_fastapi = types.ModuleType("fastapi")
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

    def publish(self, channel, message):
        self.publish_calls.append((channel, message))
        for sub in self._subscribers.get(channel, []):
            sub.queue.append(message)

    def _register(self, channel, pubsub):
        self._subscribers.setdefault(channel, []).append(pubsub)

class FakePubSub:
    def __init__(self, bus):
        self.bus = bus
        self.queue: list[str] = []

    def subscribe(self, channel):
        self.bus._register(channel, self)

    def get_message(self, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.queue:
                return {"type": "message", "data": self.queue.pop(0)}
            time.sleep(0.005)
        return None

    def close(self):
        pass

class FakeRedisClient:
    def __init__(self, bus):
        self.bus = bus

    def publish(self, channel, message):
        self.bus.publish(channel, message)

    def pubsub(self):
        return FakePubSub(self.bus)

def fresh_manager_module(bus):
    for name in ("app.websocket.manager", "app.websocket.events", "app.websocket", "app.core.cache"):
        sys.modules.pop(name, None)
    mod = types.ModuleType("app.core.cache")
    client = FakeRedisClient(bus)
    mod.get_client = lambda: client
    sys.modules["app.core.cache"] = mod
    ws_pkg = types.ModuleType("app.websocket")
    ws_pkg.__path__ = [str(REPO_ROOT / "app" / "websocket")]
    sys.modules["app.websocket"] = ws_pkg
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

def check(label, ok):
    results.append((label, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {label}")

def main():
    bus = FakeBus()
    lt = LoopThread()

    mgr_mod = fresh_manager_module(bus)
    events_mod = sys.modules["app.websocket.events"]
    manager = mgr_mod.ConnectionManager()
    manager.bind_loop(lt.loop)

    tabs = {f"tab{i}": FakeWebSocket(f"tab{i}") for i in range(NUM_TABS)}
    for ws in tabs.values():
        lt.run(manager.connect(ws))

    print(f"[soak] {NUM_TABS} browser tabs connected; ticking every {TICK_SECONDS}s "
          f"for {NUM_TICKS} ticks (~{NUM_TICKS * TICK_SECONDS}s)")

    start = time.monotonic()
    dropped_tab = None
    current_manager = manager

    for tick in range(1, NUM_TICKS + 1):

        if tick == RESTART_AFTER_TICK:
            print(f"[soak] t={time.monotonic()-start:.1f}s: simulating worker "
                  f"restart (new ConnectionManager instance, same relay bus)")
            new_mgr = mgr_mod.ConnectionManager()
            new_mgr.bind_loop(lt.loop)
            for name, ws in tabs.items():
                lt.run(new_mgr.connect(ws))
            current_manager = new_mgr

        if tick == TAB_DROP_AFTER_TICK:
            dropped_tab = "tab1"
            tabs[dropped_tab].alive = False
            print(f"[soak] t={time.monotonic()-start:.1f}s: {dropped_tab} drops "
                  f"(simulated network blip)")
        if dropped_tab and tick == TAB_DROP_AFTER_TICK + TAB_DROP_TICKS:
            tabs[dropped_tab].alive = True
            new_ws = tabs[dropped_tab]
            lt.run(current_manager.connect(new_ws))
            print(f"[soak] t={time.monotonic()-start:.1f}s: {dropped_tab} reconnects")
            dropped_tab = None

        payload = {"updates": [{"station_id": 1, "current_count": 100 + tick}],
                   "timestamp": f"tick-{tick}"}
        lt.run(current_manager.broadcast_everywhere(events_mod.CROWD_UPDATE, payload))
        time.sleep(TICK_SECONDS)

    lt.stop()

    print(f"\n[soak] RESULTS after ~{NUM_TICKS * TICK_SECONDS}s sustained run")
    tick_ids_expected = {f"tick-{t}" for t in range(1, NUM_TICKS + 1)}
    all_ok = True
    for name, ws in tabs.items():
        received_ts = []
        for raw in ws.sent:
            msg = json.loads(raw)
            if msg.get("event") == events_mod.CROWD_UPDATE:
                received_ts.append(msg["data"]["timestamp"])
        dupes = len(received_ts) - len(set(received_ts))
        if name == "tab1":

            missed = tick_ids_expected - set(received_ts)
            expected_missed = {f"tick-{TAB_DROP_AFTER_TICK}"}
            ok = dupes == 0 and missed == expected_missed
            check(f"{name}: only the superseded tick was missed (recovered via "
                  f"reconnect resync otherwise), zero duplicates (missed={sorted(missed)})", ok)
        else:
            missed = tick_ids_expected - set(received_ts)
            ok = dupes == 0 and not missed
            check(f"{name}: received every one of the {NUM_TICKS} ticks with zero duplicates "
                  f"(missed={sorted(missed)}, dupes={dupes})", ok)
        all_ok = all_ok and ok

    print(f"\n[soak] OVERALL: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
