#!/usr/bin/env python3
"""Stdlib-only verification harness for the WebSocket connection-cap
change to app/websocket/manager.py (WS_MAX_CONNECTIONS /
WS_MAX_CONNECTIONS_PER_USER - see ConnectionManager.connect()/_reject()).

Why this exists instead of a real pytest run against app/main.py's
/ws/monitor route: this sandbox has no network access, so `fastapi`
and `redis` (both already pinned in requirements.txt) cannot be
installed here - the same, recurring environment constraint documented
in scripts/verify_ws_manager.py. This script reuses that exact
technique: it imports the REAL app/websocket/manager.py module
unmodified, by injecting minimal fake `fastapi` and `app.core.cache`
modules into sys.modules first, so every check below exercises the
actual shipped ConnectionManager.connect()/_reject()/broadcast() code,
not a re-implementation of it.

What this verifies, against the real code:
  1. A connection below the configured limit is accepted (added to
     active_connections, websocket.accept() called).
  2. A connection at/above the configured limit is rejected cleanly:
     websocket.accept() is never called, websocket.close() IS called
     with WS close code 1013 ("try again later"), and connect()
     returns False.
  3. A rejected connection is never stored in active_connections (or
     any of the manager's other per-connection dicts).
  4. Already-connected clients are completely unaffected by a
     rejection - their own connections/state are untouched.
  5. A per-user sub-limit rejects a single user's connections once
     that user is at MAX_CONNECTIONS_PER_USER, without affecting the
     global limit or other users.
  6. A client that disconnects (server calls manager.disconnect()) is
     removed from active_connections, freeing a slot for a new
     connection.
  7. A failed send (dead peer) is detected and removed by broadcast().
  8. broadcast() still reaches every valid connected client.
  9. A single broadcast()'s fan-out (the asyncio.gather over
     active_connections) is bounded by MAX_CONNECTIONS - i.e. it can
     never exceed the connection cap, so it can't create an unbounded
     number of concurrent send tasks.
"""
import asyncio
import sys
import threading
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fake `fastapi` module - same minimal stand-in as verify_ws_manager.py, with
# `close()` added since _reject() now needs it.
# ---------------------------------------------------------------------------
fake_fastapi = types.ModuleType("fastapi")


class FakeWebSocket:
    def __init__(self, name: str, fail_mode: str | None = None):
        self.name = name
        self.fail_mode = fail_mode  # None | "raise" | "hang"
        self.sent: list[str] = []
        self.accepted = False
        self.closed_with: tuple[int, str] | None = None

    async def accept(self, subprotocol=None):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None):
        self.closed_with = (code, reason or "")

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


def make_fake_cache_module():
    # No relay under test here - Redis unreachable is fine, manager.py
    # degrades to local-only delivery, which is all these checks need.
    mod = types.ModuleType("app.core.cache")
    mod.get_client = lambda: None
    return mod


def fresh_manager_module(max_connections: int, max_per_user: int):
    """Re-imports app.websocket.manager fresh, with app.core.config
    UNAVAILABLE (so manager.py takes its except-ImportError fallback
    path and reads WS_MAX_CONNECTIONS/WS_MAX_CONNECTIONS_PER_USER from
    plain env vars instead - see manager.py's module-level try/except).
    This also confirms the module stays importable in this offline
    harness exactly like before this change."""
    import os

    for name in (
        "app.websocket.manager", "app.websocket.events", "app.websocket",
        "app.core.cache", "app.core.config", "app.core",
    ):
        sys.modules.pop(name, None)
    core_pkg = types.ModuleType("app.core")
    core_pkg.__path__ = []
    sys.modules["app.core"] = core_pkg
    sys.modules["app.core.cache"] = make_fake_cache_module()
    ws_pkg = types.ModuleType("app.websocket")
    ws_pkg.__path__ = [str(REPO_ROOT / "app" / "websocket")]
    sys.modules["app.websocket"] = ws_pkg
    os.environ["WS_MAX_CONNECTIONS"] = str(max_connections)
    os.environ["WS_MAX_CONNECTIONS_PER_USER"] = str(max_per_user)
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


# ---------------------------------------------------------------------------
# Tests 1-4: global connection cap.
# ---------------------------------------------------------------------------
def test_global_connection_cap():
    print("\n=== Test 1-4: global WS_MAX_CONNECTIONS cap ===")
    mgr_mod = fresh_manager_module(max_connections=3, max_per_user=10)
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    check("module-level MAX_CONNECTIONS picked up the env override",
          mgr_mod.MAX_CONNECTIONS == 3)

    ws1, ws2, ws3, ws4 = (FakeWebSocket(f"c{i}") for i in range(1, 5))

    ok1 = lt.run(manager.connect(ws1))
    ok2 = lt.run(manager.connect(ws2))
    ok3 = lt.run(manager.connect(ws3))
    check("connections below the limit are accepted (connect() -> True)",
          ok1 is True and ok2 is True and ok3 is True)
    check("accepted connections had websocket.accept() called",
          ws1.accepted and ws2.accepted and ws3.accepted)
    check("all 3 accepted connections are tracked in active_connections",
          len(manager.active_connections) == 3
          and all(w in manager.active_connections for w in (ws1, ws2, ws3)))

    # 4th connection is AT the limit - must be rejected cleanly.
    ok4 = lt.run(manager.connect(ws4))
    check("a connection at/above the limit is rejected (connect() -> False)",
          ok4 is False)
    check("rejected connection's websocket.accept() was never called",
          ws4.accepted is False)
    check("rejected connection was closed with WS close code 1013",
          ws4.closed_with is not None and ws4.closed_with[0] == 1013)
    check("rejected connection is NOT stored in active_connections",
          ws4 not in manager.active_connections)
    check("rejected connection has no entry in _connection_users",
          ws4 not in manager._connection_users)
    check("already-connected clients are unaffected by the rejection",
          len(manager.active_connections) == 3
          and all(w in manager.active_connections for w in (ws1, ws2, ws3)))

    # Disconnecting one frees a slot for a new connection.
    manager.disconnect(ws2, reason="client_close")
    check("disconnected client removed from active_connections",
          ws2 not in manager.active_connections and len(manager.active_connections) == 2)
    ws5 = FakeWebSocket("c5")
    ok5 = lt.run(manager.connect(ws5))
    check("a new connection is accepted once a slot is freed", ok5 is True)

    lt.stop()


# ---------------------------------------------------------------------------
# Test 5: per-user sub-limit.
# ---------------------------------------------------------------------------
def test_per_user_cap():
    print("\n=== Test 5: WS_MAX_CONNECTIONS_PER_USER sub-limit ===")
    mgr_mod = fresh_manager_module(max_connections=50, max_per_user=2)
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    u1a, u1b, u1c = (FakeWebSocket(f"u1-{i}") for i in range(3))
    u2a = FakeWebSocket("u2-a")

    ok_a = lt.run(manager.connect(u1a, user_id="user-1"))
    ok_b = lt.run(manager.connect(u1b, user_id="user-1"))
    check("user's first two connections (at the per-user limit) are accepted",
          ok_a is True and ok_b is True)

    ok_c = lt.run(manager.connect(u1c, user_id="user-1"))
    check("that same user's 3rd connection is rejected (per-user cap)",
          ok_c is False)
    check("rejected per-user connection closed with code 1013",
          u1c.closed_with is not None and u1c.closed_with[0] == 1013)
    check("rejected per-user connection not stored anywhere",
          u1c not in manager.active_connections)

    ok_other = lt.run(manager.connect(u2a, user_id="user-2"))
    check("a DIFFERENT user is unaffected by user-1 being at its cap",
          ok_other is True)

    lt.stop()


# ---------------------------------------------------------------------------
# Tests 6-9: broadcast bounding + dead-connection cleanup, on top of the cap.
# ---------------------------------------------------------------------------
def test_broadcast_bounded_and_dead_connection_cleanup():
    print("\n=== Test 6-9: broadcast fan-out bounding + dead-connection cleanup ===")
    mgr_mod = fresh_manager_module(max_connections=5, max_per_user=10)
    manager = mgr_mod.ConnectionManager()
    lt = LoopThread()
    manager.bind_loop(lt.loop)

    healthy = [FakeWebSocket(f"h{i}") for i in range(3)]
    dead = FakeWebSocket("dead", fail_mode="raise")
    for ws in healthy:
        lt.run(manager.connect(ws))
    lt.run(manager.connect(dead))

    lt.run(manager.broadcast("station_alert", {"msg": "test"}))

    check("broadcast reached every healthy connected client",
          all(len(ws.sent) == 1 for ws in healthy))
    check("a failed send's dead connection was removed from active_connections",
          dead not in manager.active_connections)
    check("healthy connections remain tracked after the dead one was reaped",
          all(ws in manager.active_connections for ws in healthy))

    # Fan-out bound: active_connections (and therefore the number of
    # concurrent _send_one tasks a single broadcast() gather() creates)
    # can never exceed MAX_CONNECTIONS, since connect() itself refuses
    # to grow past it.
    at_capacity = list(healthy)
    while len(manager.active_connections) < mgr_mod.MAX_CONNECTIONS:
        ws = FakeWebSocket(f"filler{len(manager.active_connections)}")
        accepted = lt.run(manager.connect(ws))
        if not accepted:
            break
        at_capacity.append(ws)
    overflow = FakeWebSocket("overflow")
    overflow_ok = lt.run(manager.connect(overflow))
    check("connection count never exceeds MAX_CONNECTIONS, bounding a single "
          "broadcast's fan-out (task count) to at most that many",
          len(manager.active_connections) == mgr_mod.MAX_CONNECTIONS
          and overflow_ok is False)

    lt.stop()


if __name__ == "__main__":
    test_global_connection_cap()
    test_per_user_cap()
    test_broadcast_bounded_and_dead_connection_cleanup()

    print("\n=== SUMMARY ===")
    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if failed:
        print(f"\n{len(failed)} check(s) FAILED")
        sys.exit(1)
    print(f"\nAll {len(results)} checks PASSED")
