#!/usr/bin/env python3
"""Tests, against the REAL app/simulator/leader_election.py and the
REAL app/simulator/local_lock.py (imported unmodified - not
reimplemented, unlike scripts/verify_leader_election.py's
multi-process harness, which reimplements the CAS algorithm because it
has to run as separate OS processes), the two scenarios requested:

  1. Redis failure -> recovery: does coordination stay correct
     (exactly one leader, no split-brain) all the way through
     connected -> unreachable (past the lease-window grace period,
     engaging the local-lock fallback) -> connected again (handing
     back to Redis cleanly)?
  2. API worker restart: if the CURRENT leader's process dies (task
     cancelled with no graceful release - simulating SIGKILL, and its
     held local-lock fd force-closed the way the OS would on process
     exit, since this harness can't literally kill a process and rely
     on kernel fd cleanup within one Python process), does a surviving
     worker take over automatically?

Only `app.core.cache` is faked (a controllable in-memory stand-in for
Redis, matching the exact state/return-value contract of the real
redis_status()/try_acquire_or_renew_lock()/release_lock() - see
app/core/cache.py). `app.simulator.local_lock` is NOT faked - it's the
real module, using real fcntl.flock() against a real temp file, so the
local-lock fallback path is exercised for real, not simulated.

Why not scripts/verify_real_redis_outage.py instead: that script needs
an actual Redis server (`pip install redis`, a running redis-server)
which this sandbox has no network to obtain - see that script's own
docstring and docs/background-jobs-and-leader-election.md.
"""
import asyncio
import importlib
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

LEASE_SECONDS = 2
POLL_SECONDS = 0.3


class FakeRedisCoordinator:
    """Shared, controllable stand-in for Redis, driving the exact
    state machine app/core/cache.redis_status() documents: "disabled" |
    "unreachable" | "connected". `up` toggles whether Redis is
    reachable right now; `ever_connected` mirrors the real module's
    behaviour of never going back to "never configured" once it has
    been up at least once."""

    def __init__(self):
        self.up = True
        self.ever_connected = True
        self._lock = threading.Lock()
        self._store: dict[str, tuple[str, float]] = {}
        self.write_log: list[tuple[str, float]] = []  # (holder_id, time) - stand-in for a DB write

    def redis_status(self) -> dict:
        if not self.up:
            return {"connected": False, "state": "unreachable", "error": "simulated outage"}
        return {"connected": True, "state": "connected"}

    def try_acquire_or_renew_lock(self, key, holder_id, ttl_seconds) -> bool:
        if not self.up:
            return False
        with self._lock:
            now = time.time()
            entry = self._store.get(key)
            if entry is None or entry[1] < now or entry[0] == holder_id:
                self._store[key] = (holder_id, now + ttl_seconds)
                return True
            return False

    def release_lock(self, key, holder_id) -> bool:
        if not self.up:
            return False
        with self._lock:
            entry = self._store.get(key)
            if entry is not None and entry[0] == holder_id:
                del self._store[key]
                return True
            return False


def install_fake_cache(coordinator: FakeRedisCoordinator):
    for name in ("app.core.cache", "app.simulator.leader_election", "app.simulator.local_lock",
                 "app.core", "app.simulator", "app"):
        sys.modules.pop(name, None)
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = [str(REPO_ROOT / "app")]
    sys.modules["app"] = app_pkg
    core_pkg = types.ModuleType("app.core")
    core_pkg.__path__ = [str(REPO_ROOT / "app" / "core")]
    sys.modules["app.core"] = core_pkg
    fake_cache = types.ModuleType("app.core.cache")
    fake_cache.redis_status = coordinator.redis_status
    fake_cache.try_acquire_or_renew_lock = coordinator.try_acquire_or_renew_lock
    fake_cache.release_lock = coordinator.release_lock
    sys.modules["app.core.cache"] = fake_cache
    sim_pkg = types.ModuleType("app.simulator")
    sim_pkg.__path__ = [str(REPO_ROOT / "app" / "simulator")]
    sys.modules["app.simulator"] = sim_pkg
    return importlib.import_module("app.simulator.leader_election")


results = []


def check(label, ok):
    results.append((label, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {label}")


async def main():
    lock_dir = tempfile.mkdtemp(prefix="metroflow_local_lock_test_")
    os.environ["METROFLOW_LOCK_DIR"] = lock_dir

    coordinator = FakeRedisCoordinator()
    le_mod = install_fake_cache(coordinator)
    local_lock_mod = sys.modules["app.simulator.local_lock"]
    # local_lock caches _LOCK_DIR at import time from the env var read
    # at module load - force it to pick up our temp dir since we set
    # the env var only just before importing.
    local_lock_mod._LOCK_DIR = lock_dir

    LeaderElection = le_mod.LeaderElection
    LOOP_NAME = "test_dashboard_loop"

    events: list[tuple[str, str, float]] = []  # (kind, holder_short, time)
    events_lock = threading.Lock()

    def make_worker(idx):
        async def run_loop():
            try:
                while True:
                    with events_lock:
                        events.append(("write", f"w{idx}", time.monotonic()))
                    await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                raise
        return run_loop

    workers = [
        LeaderElection(LOOP_NAME, make_worker(i), lease_seconds=LEASE_SECONDS, poll_seconds=POLL_SECONDS)
        for i in range(3)
    ]

    def active_leaders():
        return [w for w in workers if w.is_active()]

    def leader_states_str():
        return ", ".join(f"w{i}={'LEAD' if w.is_active() else 'standby'}" for i, w in enumerate(workers))

    start = time.monotonic()
    for w in workers:
        w.start()

    async def settle(seconds):
        await asyncio.sleep(seconds)

    # ---- Phase 0: baseline, Redis up ----
    await settle(1.5)
    leaders = active_leaders()
    check("Phase 0 (Redis up): exactly one leader elected", len(leaders) == 1)
    print(f"  t={time.monotonic()-start:.1f}s states: {leader_states_str()}")

    # ---- Phase 1: Redis failure ----
    print(f"\n[test] t={time.monotonic()-start:.1f}s: Redis FAILS now")
    coordinator.up = False
    # During the grace window (< lease_seconds since outage was first
    # observed), the real module fails CLOSED - watch for any window
    # with >1 or 0 active leaders longer than the fail-closed period.
    max_concurrent_during_outage = 0
    zero_leader_ticks = 0
    outage_check_deadline = time.monotonic() + LEASE_SECONDS + 4  # past the fallback threshold
    while time.monotonic() < outage_check_deadline:
        n = len(active_leaders())
        max_concurrent_during_outage = max(max_concurrent_during_outage, n)
        if n == 0:
            zero_leader_ticks += 1
        await asyncio.sleep(0.1)
    check("Phase 1 (Redis down, past grace window): never more than 1 concurrent leader "
          f"(observed max={max_concurrent_during_outage})", max_concurrent_during_outage <= 1)
    leaders_after_fallback = active_leaders()
    check("Phase 1: exactly one leader active via local-lock fallback after grace window",
          len(leaders_after_fallback) == 1)
    print(f"  t={time.monotonic()-start:.1f}s states: {leader_states_str()}")

    fallback_leader_idx = workers.index(leaders_after_fallback[0]) if leaders_after_fallback else None

    # ---- Phase 2: API worker restart (kill the current leader) ----
    if fallback_leader_idx is not None:
        killed = workers[fallback_leader_idx]
        print(f"\n[test] t={time.monotonic()-start:.1f}s: simulating API worker restart - "
              f"hard-killing current leader w{fallback_leader_idx} (task cancelled with "
              f"no graceful release; its local-lock fd force-released the way the OS "
              f"would on real process exit)")
        # Simulate SIGKILL: cancel the election task directly (not
        # via .stop(), which would gracefully release the lease/lock -
        # a real crash gets neither).
        if killed._election_task is not None:
            killed._election_task.cancel()
        if killed._worker_task is not None:
            killed._worker_task.cancel()
        # The one piece a real OS process-crash gives for free that an
        # in-process task-cancel cannot: the kernel releases every
        # flock() the process held, immediately, unconditionally. We
        # replicate exactly that (not a graceful release_lock call).
        local_lock_mod.release(LOOP_NAME, killed._holder_id)

        await settle(LEASE_SECONDS + POLL_SECONDS * 3)
        leaders_after_restart = active_leaders()
        check("Phase 2: a surviving worker took over after the restart "
              "(realtime coordination continues)",
              len(leaders_after_restart) == 1 and leaders_after_restart[0] is not killed)
        print(f"  t={time.monotonic()-start:.1f}s states: {leader_states_str()}")
    else:
        check("Phase 2: a leader existed to restart", False)

    # ---- Phase 3: Redis recovers ----
    print(f"\n[test] t={time.monotonic()-start:.1f}s: Redis RECOVERS now")
    coordinator.up = True
    max_concurrent_during_handback = 0
    handback_deadline = time.monotonic() + LEASE_SECONDS + 3
    while time.monotonic() < handback_deadline:
        n = len(active_leaders())
        max_concurrent_during_handback = max(max_concurrent_during_handback, n)
        await asyncio.sleep(0.1)
    check("Phase 3 (Redis recovered): never more than 1 concurrent leader during handback "
          f"(observed max={max_concurrent_during_handback})", max_concurrent_during_handback <= 1)
    final_leaders = active_leaders()
    check("Phase 3: exactly one leader active post-recovery", len(final_leaders) == 1)
    still_holding_local_lock = [
        i for i, w in enumerate(workers) if local_lock_mod.is_held(LOOP_NAME, w._holder_id)
    ]
    check("Phase 3: no worker still holds the local-lock fallback once Redis is back "
          f"(holding={still_holding_local_lock})", still_holding_local_lock == [])
    print(f"  t={time.monotonic()-start:.1f}s states: {leader_states_str()}")

    # ---- Global check across the whole run: no concurrent "writes" ----
    with events_lock:
        writes = [e for e in events if e[0] == "write"]
    conflicts = 0
    for i in range(len(writes)):
        for j in range(i + 1, len(writes)):
            if writes[i][1] != writes[j][1] and abs(writes[i][2] - writes[j][2]) < 0.02:
                conflicts += 1
    check(f"Whole run: zero concurrent duplicate writes across all phases "
          f"(total writes={len(writes)}, conflicts={conflicts})", conflicts == 0)

    for w in workers:
        if w._election_task is not None and not w._election_task.done():
            await w.stop()
        else:
            local_lock_mod.release(LOOP_NAME, w._holder_id)

    all_ok = all(ok for _, ok in results)
    print(f"\n[test] OVERALL: {'PASS' if all_ok else 'FAIL'} ({sum(ok for _,ok in results)}/{len(results)} checks passed)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
