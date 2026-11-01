#!/usr/bin/env python3
import asyncio
import sys
import types
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
        self.subprotocol: str | None = None

    async def accept(self, subprotocol: str | None = None):
        self.accepted = True
        self.subprotocol = subprotocol

    async def send_text(self, payload: str):
        if self.fail_mode == "raise":
            raise ConnectionResetError("simulated dead peer")
        self.sent.append(payload)

fake_fastapi.WebSocket = FakeWebSocket
sys.modules["fastapi"] = fake_fastapi

app_pkg = types.ModuleType("app")
app_pkg.__path__ = []
core_pkg = types.ModuleType("app.core")
core_pkg.__path__ = []
sys.modules.setdefault("app", app_pkg)
sys.modules.setdefault("app.core", core_pkg)

def make_fake_cache_module():
    mod = types.ModuleType("app.core.cache")
    mod.get_client = lambda: None
    mod.redis_status = lambda: {"state": "disabled"}
    mod.try_acquire_or_renew_lock = lambda *a, **k: False
    mod.release_lock = lambda *a, **k: None
    return mod

def fresh_manager_module():
    for name in ("app.websocket.manager", "app.websocket.events", "app.websocket",
                 "app.core.cache"):
        sys.modules.pop(name, None)
    sys.modules["app.core.cache"] = make_fake_cache_module()
    ws_pkg = types.ModuleType("app.websocket")
    ws_pkg.__path__ = [str(REPO_ROOT / "app" / "websocket")]
    sys.modules["app.websocket"] = ws_pkg
    import importlib
    return importlib.import_module("app.websocket.manager")

def fresh_leader_election_module():
    for name in ("app.simulator.leader_election", "app.simulator.local_lock",
                 "app.simulator", "app.core.cache"):
        sys.modules.pop(name, None)
    sys.modules["app.core.cache"] = make_fake_cache_module()
    sim_pkg = types.ModuleType("app.simulator")
    sim_pkg.__path__ = [str(REPO_ROOT / "app" / "simulator")]
    sys.modules["app.simulator"] = sim_pkg
    import importlib

    return importlib.import_module("app.simulator.leader_election")

results = []

def check(label, condition):
    results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")

class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()

    def run(self, coro, timeout=5):
        return self.loop.run_until_complete(coro)

    def stop(self):
        self.loop.close()

def test_connect_disconnect_reconnect():
    print("\n=== Test A/B: connect -> disconnect -> reconnect ===")
    mgr_mod = fresh_manager_module()
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    snap0 = manager.get_metrics_snapshot()
    check("starts with zero connects/disconnects/active",
          snap0 == {"connects_total": 0, "disconnects_total": {},
                    "event_send_failures_total": {}, "active_connections": 0})

    ws1 = FakeWebSocket("client-1")
    lt.run(manager.connect(ws1))
    snap1 = manager.get_metrics_snapshot()
    check("connect() increments connects_total to 1", snap1["connects_total"] == 1)
    check("connect() reflects in active_connections", snap1["active_connections"] == 1)

    manager.disconnect(ws1, reason="client_close")
    snap2 = manager.get_metrics_snapshot()
    check("disconnect(reason=...) buckets under that reason",
          snap2["disconnects_total"] == {"client_close": 1})
    check("active_connections drops back to 0", snap2["active_connections"] == 0)
    check("connects_total is untouched by disconnect", snap2["connects_total"] == 1)

    ws2 = FakeWebSocket("client-1-reconnected")
    lt.run(manager.connect(ws2))
    snap3 = manager.get_metrics_snapshot()
    check("reconnect increments connects_total to 2", snap3["connects_total"] == 2)
    check("reconnect brings active_connections back to 1", snap3["active_connections"] == 1)
    check("disconnects_total is untouched by the reconnect",
          snap3["disconnects_total"] == {"client_close": 1})

    manager.disconnect(ws2)
    snap4 = manager.get_metrics_snapshot()
    check("disconnect() with no reason buckets under 'unknown'",
          snap4["disconnects_total"].get("unknown") == 1)

    lt.stop()

def test_event_send_failure_metrics():
    print("\n=== Test C: event-send-failure metrics ===")
    mgr_mod = fresh_manager_module()
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    healthy = FakeWebSocket("healthy")
    dead = FakeWebSocket("dead", fail_mode="raise")
    lt.run(manager.connect(healthy))
    lt.run(manager.connect(dead))

    lt.run(manager.broadcast("crowd_update", {"x": 1}))

    snap = manager.get_metrics_snapshot()
    check("exactly one send failure recorded for crowd_update",
          snap["event_send_failures_total"] == {"crowd_update": 1})
    check("the failed connection was dropped with reason=send_failed",
          snap["disconnects_total"] == {"send_failed": 1})
    check("the healthy connection is still active", snap["active_connections"] == 1)
    check("the healthy connection actually received the broadcast", len(healthy.sent) == 1)

    lt.run(manager.broadcast("train_position", {"y": 2}))
    snap2 = manager.get_metrics_snapshot()
    check("no spurious failure recorded for an event nothing failed on",
          snap2["event_send_failures_total"] == {"crowd_update": 1})

    lt.stop()

def test_reaper_metrics():
    print("\n=== Test D: reaper records stale_reaped ===")
    mgr_mod = fresh_manager_module()
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    dead = FakeWebSocket("dead", fail_mode="raise")
    lt.run(manager.connect(dead))
    import time
    manager._last_seen[dead] = time.monotonic() - (mgr_mod.STALE_AFTER_SECONDS + 5)

    lt.run(manager._reap_stale())

    snap = manager.get_metrics_snapshot()
    check("reaper-dropped connection recorded as stale_reaped",
          snap["disconnects_total"] == {"stale_reaped": 1})

    lt.stop()

def test_leader_heartbeat_metrics():
    print("\n=== Test E/F/G: simulator leader/heartbeat metrics ===")
    le_mod = fresh_leader_election_module()

    async def idle_forever():
        await asyncio.Event().wait()

    async def crashing_loop():
        raise RuntimeError("simulated worker crash")

    async def scenario():
        election = le_mod.LeaderElection(
            "test_loop", idle_forever, lease_seconds=5, poll_seconds=1,
        )

        election._election_task = asyncio.ensure_future(asyncio.sleep(3600))

        snap0 = election.get_metrics_snapshot()
        check("starts with zero heartbeats/acquired/lost/crashes",
              (snap0["heartbeat_ticks_total"], snap0["leadership_acquired_total"],
               snap0["leadership_lost_total"], snap0["worker_crashes_total"])
              == (0, 0, 0, 0))
        check("last_heartbeat_ts starts unset", snap0["last_heartbeat_ts"] is None)

        await election._election_tick()
        snap1 = election.get_metrics_snapshot()
        check("first successful tick records exactly one heartbeat",
              snap1["heartbeat_ticks_total"] == 1)
        check("first successful tick records exactly one leadership_acquired",
              snap1["leadership_acquired_total"] == 1)
        check("state is now 'leader'", snap1["state"] == "leader")
        check("last_heartbeat_ts is now set", snap1["last_heartbeat_ts"] is not None)
        first_heartbeat_ts = snap1["last_heartbeat_ts"]

        await election._election_tick()
        snap2 = election.get_metrics_snapshot()
        check("second tick increments heartbeat_ticks_total to 2",
              snap2["heartbeat_ticks_total"] == 2)
        check("second (renewing) tick does NOT double-count leadership_acquired",
              snap2["leadership_acquired_total"] == 1)
        check("last_heartbeat_ts advances on the renewing tick",
              snap2["last_heartbeat_ts"] >= first_heartbeat_ts)

        le_mod.local_lock.release("test_loop", election._holder_id)
        le_mod.local_lock.try_acquire("test_loop", "some-other-process")
        await election._election_tick()
        snap3 = election.get_metrics_snapshot()
        check("losing the lock records exactly one leadership_lost",
              snap3["leadership_lost_total"] == 1)
        check("state is now 'standby'", snap3["state"] == "standby")
        check("heartbeat_ticks_total does not grow on a losing tick",
              snap3["heartbeat_ticks_total"] == 2)

        le_mod.local_lock.release("test_loop", "some-other-process")

        crash_election = le_mod.LeaderElection(
            "test_loop_crash", crashing_loop, lease_seconds=5, poll_seconds=1,
        )
        crash_election._election_task = asyncio.ensure_future(asyncio.sleep(3600))
        await crash_election._election_tick()  

        for _ in range(50):
            if crash_election.get_metrics_snapshot()["worker_crashes_total"] > 0:
                break
            await asyncio.sleep(0.02)
        snap4 = crash_election.get_metrics_snapshot()
        check("a worker task that raises records worker_crashes_total",
              snap4["worker_crashes_total"] == 1)

        le_mod.local_lock.release("test_loop_crash", crash_election._holder_id)

        crash_election._worker_task = None
        await election._stop_worker()
        for e in (election, crash_election):
            e._election_task.cancel()
            try:
                await e._election_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

if __name__ == "__main__":
    test_connect_disconnect_reconnect()
    test_event_send_failure_metrics()
    test_reaper_metrics()
    test_leader_heartbeat_metrics()

    print("\n=== SUMMARY ===")
    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if failed:
        print(f"\n{len(failed)} check(s) FAILED")
        sys.exit(1)
    print(f"\nAll {len(results)} checks PASSED")
