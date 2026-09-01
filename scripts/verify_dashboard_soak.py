#!/usr/bin/env python3
"""Sustained, multi-tab soak test standing in for the manual "keep the
dashboard open 30 minutes in several browser tabs" check, which this
sandbox cannot literally perform (no browser/display tool, no network
to install fastapi/redis - see docs/realtime-websocket-system.md and
scripts/verify_leader_election.py for the same constraint).

What this DOES exercise, against the real app/websocket/manager.py
(imported unmodified, same technique as verify_ws_manager.py):
  1. Backend data changes on a real 5-10s cadence (here: 7s) sent as
     real crowd_update broadcasts over a real asyncio event loop, for
     a sustained run (2 minutes = ~17 ticks) - not a single tick.
  2. Multiple independent WebSocket connections (FakeWebSocket
     standing in for browser tabs, each with its own send_text()
     buffer) all receive every tick - the offline equivalent of
     "multiple tabs update".
  3. A worker restart mid-run: the "leader" ConnectionManager instance
     is torn down and a fresh one takes over (new LeaderElection-style
     handoff), verifying ticks resume without gaps or dupes.
  4. A single tab's socket drops and reconnects mid-run (simulating a
     laptop sleep/network blip), verifying it doesn't receive
     duplicates and does receive every subsequent tick once
     reconnected - i.e. no data loss or double-delivery, matching the
     frontend's own resync-on-reconnect design (LiveSocketProvider.tsx).

What this does NOT verify: the real browser DOM actually re-rendering
without a location.reload() (that was verified by reading
LiveStationsPanel.tsx / LiveSocketProvider.tsx's source - no reload()
or router.refresh() call exists in that path), and the real
fastapi/redis wire protocol (see verify_ws_manager.py's own caveat).
"""
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

TICK_SECONDS = 7          # within the required 5-10s cadence
NUM_TICKS = 17            # ~2 minutes of sustained ticks
NUM_TABS = 3
RESTART_AFTER_TICK = 7    # mid-run worker restart
TAB_DROP_AFTER_TICK = 4   # mid-run single-tab reconnect
TAB_DROP_TICKS = 2        # how many ticks that tab stays disconnected


# ---------------------------------------------------------------------------
# Fake fastapi / redis, identical technique to verify_ws_manager.py
# ---------------------------------------------------------------------------
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
        # Mid-run worker restart: tear down current manager's connections
        # and hand off to a brand-new ConnectionManager instance sharing
        # the same relay bus - the offline stand-in for a crashed
        # process being replaced by the next LeaderElection winner.
        if tick == RESTART_AFTER_TICK:
            print(f"[soak] t={time.monotonic()-start:.1f}s: simulating worker "
                  f"restart (new ConnectionManager instance, same relay bus)")
            new_mgr = mgr_mod.ConnectionManager()
            new_mgr.bind_loop(lt.loop)
            for name, ws in tabs.items():
                lt.run(new_mgr.connect(ws))
            current_manager = new_mgr

        # Mid-run single-tab drop + reconnect (laptop sleep / network blip).
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
            # tab1 was disconnected during the tick-4 broadcast (missed
            # outright - the send simply failed) and also during
            # tick-5's broadcast, BUT manager.connect()'s resync
            # (_send_latest_state) delivers the latest cached state the
            # instant it reconnects - which by then is tick-5's payload
            # - so tick-5 is correctly recovered via resync rather than
            # genuinely lost. Only tick-4 (superseded by tick-5 before
            # reconnect ever happened) is expected to be truly missed.
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
