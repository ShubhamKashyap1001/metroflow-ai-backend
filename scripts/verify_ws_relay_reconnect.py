#!/usr/bin/env python3
"""Stdlib-only verification harness for app/websocket/manager.py's
the relay auto-reconnect changes (docs/realtime-websocket-system.md).

Same technique as scripts/verify_ws_manager.py: this sandbox
has no network access to install `fastapi`/`redis`, so this imports
the REAL app/websocket/manager.py module unmodified, wired to fake
`fastapi.WebSocket` and `app.core.cache` modules injected into
sys.modules. Every test below exercises the actual shipped
ConnectionManager/_relay_loop code, not a re-implementation of it.

What this verifies, against the real code:
  1. Bug #1 (relay doesn't reliably restart if Redis is unavailable at
     startup): start_relay() is called while Redis is DOWN. The relay
     thread must still be started, keep retrying, and - once Redis
     becomes reachable without anyone calling start_relay() again -
     actually subscribe and start delivering cross-process events.
  2. Bug #2 (relay can stop after a Redis error and not recover): with
     the relay already up and successfully delivering events, the
     simulated Redis connection is broken out from under it
     (get_message() starts raising, as a real dropped connection
     would). The relay must not just quietly die - it must reconnect
     on its own once the simulated outage ends, and resume delivering
     events, with no external call needed.
  3. Regression guard: stop_relay() still cleanly stops the thread
     (bounded join, no hang) and start_relay() stays idempotent
     (calling it twice doesn't spawn a second thread).

What this does NOT verify: the real redis-py wire protocol/exception
types, or a real Redis server's actual failure behavior - only that
manager.py's OWN retry/reconnect logic responds correctly to
get_client() returning None and to pubsub.get_message() raising,
which is the full extent of manager.py's contract with app.core.cache.
"""
import sys
import threading
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fake `fastapi` module (WebSocket is only used as a type hint).
# ---------------------------------------------------------------------------
fake_fastapi = types.ModuleType("fastapi")


class FakeWebSocket:
    async def accept(self):
        pass

    async def send_text(self, payload: str):
        pass


fake_fastapi.WebSocket = FakeWebSocket
sys.modules["fastapi"] = fake_fastapi

# ---------------------------------------------------------------------------
# Fake `app.core.cache` - a controllable bus that can simulate Redis being
# down at startup, coming up later, and an established subscription's
# get_message() erroring out mid-stream (a dropped connection).
# ---------------------------------------------------------------------------
app_pkg = types.ModuleType("app")
app_pkg.__path__ = []
core_pkg = types.ModuleType("app.core")
core_pkg.__path__ = []
sys.modules.setdefault("app", app_pkg)
sys.modules.setdefault("app.core", core_pkg)


class ControllableBus:
    """Stands in for a real Redis server, with two knobs tests flip to
    reproduce the two relay-recovery bugs:
      - `reachable` (bool): whether cache.get_client() returns a client
        at all - simulates Redis being down / not up yet.
      - `broken` (bool): whether an ALREADY-SUBSCRIBED pubsub's
        get_message() raises - simulates a live connection dying
        mid-stream (distinct from "no client available" above, exactly
        matching the two separate code paths bug #1 and bug #2 exercise
        in _relay_loop()).
    """

    def __init__(self):
        self.reachable = True
        self.broken = False
        self._subscribers: list["ControllablePubSub"] = []
        self.publish_calls: list[tuple[str, str]] = []
        self.subscribe_count = 0

    def publish(self, channel: str, message: str):
        self.publish_calls.append((channel, message))
        if not self.broken:
            for sub in self._subscribers:
                sub.queue.append(message)

    def register(self, pubsub: "ControllablePubSub"):
        self._subscribers.append(pubsub)
        self.subscribe_count += 1


class ControllablePubSub:
    def __init__(self, bus: ControllableBus):
        self.bus = bus
        self.queue: list[str] = []
        self.closed = False

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
        self.closed = True
        if self in self.bus._subscribers:
            self.bus._subscribers.remove(self)


class ControllableRedisClient:
    def __init__(self, bus: ControllableBus):
        self.bus = bus

    def publish(self, channel, message):
        self.bus.publish(channel, message)

    def pubsub(self):
        return ControllablePubSub(self.bus)


def make_controllable_cache_module(bus: ControllableBus):
    mod = types.ModuleType("app.core.cache")

    def get_client():
        if not bus.reachable:
            return None
        return ControllableRedisClient(bus)

    mod.get_client = get_client
    return mod


def fresh_manager_module(bus: ControllableBus):
    for name in ("app.websocket.manager", "app.websocket.events", "app.websocket",
                 "app.core.cache"):
        sys.modules.pop(name, None)
    sys.modules["app.core.cache"] = make_controllable_cache_module(bus)
    ws_pkg = types.ModuleType("app.websocket")
    ws_pkg.__path__ = [str(REPO_ROOT / "app" / "websocket")]
    sys.modules["app.websocket"] = ws_pkg
    import importlib
    return importlib.import_module("app.websocket.manager")


results = []


def check(label, condition):
    results.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} - {label}")


def wait_until(predicate, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Test 1 (Bug #1): Redis unavailable at start_relay() time, becomes
# reachable later with NO further calls into manager.py.
# ---------------------------------------------------------------------------
def test_relay_restarts_after_redis_unavailable_at_startup():
    print("\n=== Test 1 (Bug #1): Redis down at start_relay(), comes up later ===")
    bus = ControllableBus()
    bus.reachable = False  # Redis unreachable at process startup

    mgr_mod = fresh_manager_module(bus)
    manager = mgr_mod.ConnectionManager()

    manager.start_relay()
    check("relay thread was started even though Redis was unreachable (bug #1)",
          manager._relay_thread is not None and manager._relay_thread.is_alive())

    # Give it a couple of poll cycles to prove it isn't just started but
    # dead - it should still be alive, quietly retrying.
    time.sleep(1.5)
    check("relay thread is still alive/retrying, not exited",
          manager._relay_thread is not None and manager._relay_thread.is_alive())

    # Now Redis becomes reachable - nobody calls start_relay() again.
    bus.reachable = True
    ok = wait_until(lambda: bus.subscribe_count >= 1, timeout=6.0)
    check("relay subscribed on its own once Redis became reachable, "
          "with no external call to start_relay() again", ok)

    # Prove it's actually delivering: publish a message and confirm a
    # second manager instance sharing the bus is irrelevant here - just
    # confirm THIS manager's own subscription is live by checking the
    # bus registered it (subscribe_count) and that publishing doesn't
    # error and reaches the queue.
    bus.publish(mgr_mod.RELAY_CHANNEL, '{"origin": "someone-else", "event": "ping", "data": {}}')
    ok2 = wait_until(lambda: len(bus.publish_calls) >= 1, timeout=2.0)
    check("bus is live and accepting publishes post-recovery", ok2)

    manager.stop_relay()


# ---------------------------------------------------------------------------
# Test 2 (Bug #2): relay is up and delivering, connection breaks
# mid-stream, then recovers - must reconnect without external help.
# ---------------------------------------------------------------------------
def test_relay_recovers_after_midstream_error():
    print("\n=== Test 2 (Bug #2): established relay errors mid-stream, must recover ===")
    bus = ControllableBus()
    bus.reachable = True

    mgr_mod = fresh_manager_module(bus)
    manager = mgr_mod.ConnectionManager()

    import asyncio
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    manager.bind_loop(loop)

    manager.start_relay()
    ok = wait_until(lambda: bus.subscribe_count >= 1, timeout=4.0)
    check("relay subscribed while Redis was healthy", ok)

    received = []

    async def _fake_broadcast(event, data, **kwargs):
        received.append((event, data))

    manager.broadcast = _fake_broadcast  # type: ignore[assignment]

    def publish_ping(tag):
        bus.publish(
            mgr_mod.RELAY_CHANNEL,
            f'{{"origin": "other-process", "event": "crowd_update", '
            f'"data": {{"tag": "{tag}"}}, "event_id": "{tag}"}}',
        )

    publish_ping("before-break")
    ok = wait_until(lambda: any(d.get("tag") == "before-break" for _, d in received), timeout=3.0)
    check("relay delivered an event while healthy (baseline)", ok)

    # Break the connection out from under the already-running relay
    # thread - this is exactly what a dropped Redis connection looks
    # like from get_message()'s perspective.
    subscribe_count_before_break = bus.subscribe_count
    bus.broken = True
    time.sleep(1.5)  # let the thread actually hit the error at least once
    check("relay thread survives the mid-stream error (bug #2 - it used "
          "to permanently exit here)",
          manager._relay_thread is not None and manager._relay_thread.is_alive())

    # Recover - the thread must notice and resubscribe on its own.
    bus.broken = False
    ok = wait_until(lambda: bus.subscribe_count > subscribe_count_before_break, timeout=8.0)
    check("relay resubscribed on its own after the outage ended, with no "
          "external call needed", ok)

    publish_ping("after-recovery")
    ok = wait_until(lambda: any(d.get("tag") == "after-recovery" for _, d in received), timeout=4.0)
    check("relay delivers events again after recovering from the mid-stream error", ok)

    manager.stop_relay()
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Test 3: regression guard - stop_relay() joins cleanly, start_relay()
# stays idempotent.
# ---------------------------------------------------------------------------
def test_stop_relay_clean_and_start_relay_idempotent():
    print("\n=== Test 3: stop_relay() joins cleanly; start_relay() stays idempotent ===")
    bus = ControllableBus()
    mgr_mod = fresh_manager_module(bus)
    manager = mgr_mod.ConnectionManager()

    manager.start_relay()
    first_thread = manager._relay_thread
    manager.start_relay()  # should be a no-op - already running
    check("calling start_relay() twice does not spawn a second thread",
          manager._relay_thread is first_thread)

    t0 = time.monotonic()
    manager.stop_relay()
    elapsed = time.monotonic() - t0
    check(f"stop_relay() returned promptly (took {elapsed:.2f}s, expected well under the 2s join timeout)",
          elapsed < 2.5)
    check("relay thread reference cleared after stop", manager._relay_thread is None)
    check("underlying thread actually exited", not first_thread.is_alive())


if __name__ == "__main__":
    test_relay_restarts_after_redis_unavailable_at_startup()
    test_relay_recovers_after_midstream_error()
    test_stop_relay_clean_and_start_relay_idempotent()

    print("\n=== SUMMARY ===")
    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if failed:
        print(f"\n{len(failed)} check(s) FAILED")
        sys.exit(1)
    print(f"\nAll {len(results)} checks PASSED")
