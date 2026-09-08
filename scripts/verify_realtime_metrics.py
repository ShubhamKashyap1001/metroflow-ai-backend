#!/usr/bin/env python3
"""Stdlib-only verification harness for the two additions covered by
this change:

  1. WebSocket connection/reconnect/event-failure metrics - the plain
     counters added to app/websocket/manager.py's ConnectionManager
     (get_metrics_snapshot()).
  2. Simulator leader/heartbeat monitoring - the plain counters added
     to app/simulator/leader_election.py's LeaderElection
     (get_metrics_snapshot()).

Why this exists instead of a real pytest run against app/core/metrics.py's
Prometheus collector: this sandbox has no network access, so
`fastapi`, `redis`, and `prometheus_client` (all already pinned in
requirements.txt) cannot be installed here - the same, recurring
environment constraint documented in scripts/verify_ws_manager.py and
scripts/verify_leader_election.py. This script imports the REAL
app/websocket/manager.py and app/simulator/leader_election.py modules
unmodified, by injecting a minimal fake `fastapi` and a fake
`app.core.cache` into sys.modules first - reusing exactly the
technique scripts/verify_ws_manager.py already established. Neither
manager.py nor leader_election.py needed a new hard import to add
these counters (see their own module docstrings for why: they stay
importable here on purpose), so this harness proves the real,
shipped counter logic end-to-end without needing prometheus_client at
all - that package is only ever touched by app/core/metrics.py's
collect(), which is exercised by a separate, normal pytest test
(tests/test_metrics_realtime.py) in an environment where it's
installed.

What this DOES verify, against the real code:
  A. connect() increments connects_total; a full
     connect -> disconnect -> reconnect cycle is reflected correctly
     in connects_total/disconnects_total/active_connections.
  B. disconnect(reason=...) buckets correctly, including the default
     ("unknown") for a caller that doesn't pass one.
  C. A failed broadcast() send is counted once in
     event_send_failures_total[event] AND drops the connection with
     reason="send_failed" - not double-counted, not miscategorized.
  D. The stale-connection reaper drops with reason="stale_reaped".
  E. LeaderElection: heartbeat_ticks_total increments once per
     successful local-lock-fallback tick (Redis unavailable in this
     harness, matching real Redis-down behaviour), and exactly one
     leadership_acquired_total is recorded on the acquire edge, not on
     every subsequent renewing tick.
  F. LeaderElection: stepping down (another local process wins solethe
     lock) records leadership_lost_total exactly once on that edge.
  G. LeaderElection: a worker task that raises records
     worker_crashes_total.
"""
import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fake `fastapi` module - see scripts/verify_ws_manager.py for why a bare
# placeholder WebSocket type is sufficient.
# ---------------------------------------------------------------------------
fake_fastapi = types.ModuleType("fastapi")


class FakeWebSocket:
    def __init__(self, name: str, fail_mode: str | None = None):
        self.name = name
        self.fail_mode = fail_mode  # None | "raise"
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

# ---------------------------------------------------------------------------
# Fake `app.core.cache` - Redis "always unreachable" for this harness (both
# manager.py's relay and leader_election.py's lease acquisition just need
# get_client()/redis_status()/try_acquire_or_renew_lock()/release_lock() to
# exist; None/"unreachable"/False are exactly how a real down Redis behaves).
# ---------------------------------------------------------------------------
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
    # local_lock.py is pure stdlib (fcntl/os/tempfile) - import the real one.
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


# ---------------------------------------------------------------------------
# A, B: connect -> disconnect -> reconnect
# ---------------------------------------------------------------------------
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

    # Disconnect (e.g. the client closed cleanly).
    manager.disconnect(ws1, reason="client_close")
    snap2 = manager.get_metrics_snapshot()
    check("disconnect(reason=...) buckets under that reason",
          snap2["disconnects_total"] == {"client_close": 1})
    check("active_connections drops back to 0", snap2["active_connections"] == 0)
    check("connects_total is untouched by disconnect", snap2["connects_total"] == 1)

    # Reconnect - from the server's side this is just another connect() call
    # on a brand-new WebSocket (see manager.py's counters docstring for why
    # there's no separate "reconnect" counter).
    ws2 = FakeWebSocket("client-1-reconnected")
    lt.run(manager.connect(ws2))
    snap3 = manager.get_metrics_snapshot()
    check("reconnect increments connects_total to 2", snap3["connects_total"] == 2)
    check("reconnect brings active_connections back to 1", snap3["active_connections"] == 1)
    check("disconnects_total is untouched by the reconnect",
          snap3["disconnects_total"] == {"client_close": 1})

    # Disconnect with no reason at all - must not raise, must bucket as
    # "unknown" (backward-compat default for any call site that forgets).
    manager.disconnect(ws2)
    snap4 = manager.get_metrics_snapshot()
    check("disconnect() with no reason buckets under 'unknown'",
          snap4["disconnects_total"].get("unknown") == 1)

    lt.stop()


# ---------------------------------------------------------------------------
# C: event-send-failure metrics via broadcast()
# ---------------------------------------------------------------------------
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

    # A second broadcast of a DIFFERENT event, still only the healthy
    # connection around - no new failures should be recorded.
    lt.run(manager.broadcast("train_position", {"y": 2}))
    snap2 = manager.get_metrics_snapshot()
    check("no spurious failure recorded for an event nothing failed on",
          snap2["event_send_failures_total"] == {"crowd_update": 1})

    lt.stop()


# ---------------------------------------------------------------------------
# D: reaper -> stale_reaped
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# E, F, G: LeaderElection heartbeat/leadership/crash counters
# ---------------------------------------------------------------------------
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
        # status()/get_metrics_snapshot()'s "state" only reports
        # anything beyond "not_started" once _election_task is set -
        # normally done by start(), which also spins up its own
        # perpetual polling loop. This test drives _election_tick()
        # directly instead (for deterministic, single-step control),
        # so it sets the same "bidding has begun" marker start() would
        # have set, without start()'s own background loop.
        election._election_task = asyncio.ensure_future(asyncio.sleep(3600))

        snap0 = election.get_metrics_snapshot()
        check("starts with zero heartbeats/acquired/lost/crashes",
              (snap0["heartbeat_ticks_total"], snap0["leadership_acquired_total"],
               snap0["leadership_lost_total"], snap0["worker_crashes_total"])
              == (0, 0, 0, 0))
        check("last_heartbeat_ts starts unset", snap0["last_heartbeat_ts"] is None)

        # Redis is "disabled" in this harness's fake cache module, so
        # every tick takes the local-lock fallback branch - the real
        # code path exercised when Redis is unreachable/never
        # configured (Phase 7A in leader_election.py's own docstring).
        await election._election_tick()
        snap1 = election.get_metrics_snapshot()
        check("first successful tick records exactly one heartbeat",
              snap1["heartbeat_ticks_total"] == 1)
        check("first successful tick records exactly one leadership_acquired",
              snap1["leadership_acquired_total"] == 1)
        check("state is now 'leader'", snap1["state"] == "leader")
        check("last_heartbeat_ts is now set", snap1["last_heartbeat_ts"] is not None)
        first_heartbeat_ts = snap1["last_heartbeat_ts"]

        # A second successful (renewing) tick: heartbeat increments
        # again, but leadership_acquired must NOT (it's an edge, not a
        # per-tick count) - this is the crux of test E.
        await election._election_tick()
        snap2 = election.get_metrics_snapshot()
        check("second tick increments heartbeat_ticks_total to 2",
              snap2["heartbeat_ticks_total"] == 2)
        check("second (renewing) tick does NOT double-count leadership_acquired",
              snap2["leadership_acquired_total"] == 1)
        check("last_heartbeat_ts advances on the renewing tick",
              snap2["last_heartbeat_ts"] >= first_heartbeat_ts)

        # Force a step-down: make the local lock appear held by someone
        # else, so this election loses the race on its next tick.
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

        # Test G: a worker crash is recorded.
        crash_election = le_mod.LeaderElection(
            "test_loop_crash", crashing_loop, lease_seconds=5, poll_seconds=1,
        )
        crash_election._election_task = asyncio.ensure_future(asyncio.sleep(3600))
        await crash_election._election_tick()  # acquires leadership, starts the crashing worker
        # Let the crashing task actually run and hit its done-callback.
        for _ in range(50):
            if crash_election.get_metrics_snapshot()["worker_crashes_total"] > 0:
                break
            await asyncio.sleep(0.02)
        snap4 = crash_election.get_metrics_snapshot()
        check("a worker task that raises records worker_crashes_total",
              snap4["worker_crashes_total"] == 1)

        le_mod.local_lock.release("test_loop_crash", crash_election._holder_id)
        # Not calling crash_election._stop_worker() here: the worker
        # task already finished (with the simulated exception) before
        # we'd cancel it, and awaiting an already-finished task
        # re-raises that same exception rather than CancelledError -
        # pre-existing behaviour of _stop_worker(), out of scope for
        # this verification (which only exercises the new counters).
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
