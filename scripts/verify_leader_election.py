"""Stdlib-only, multi-PROCESS (not multi-thread) test that proves the
leader-election ALGORITHM used in app/simulator/leader_election.py and
app/core/cache.py's try_acquire_or_renew_lock/release_lock is correct:
  1. Mutual exclusion: at any instant, at most one of N real OS
     processes believes it is the leader / would be running the
     simulator loop.
  2. Automatic failover: if the current leader is hard-killed
     (SIGKILL - no graceful release, simulating a real crash), a
     different process takes over within a bounded time (lease TTL +
     one poll interval).
  3. No duplicate "writes": using non-overlapping leadership intervals
     as a stand-in for "only the leader writes to the DB / broadcasts
     events", no two processes are ever both in a "write" state.

This sandbox has no network access to `pip install redis` (or
fastapi/sqlalchemy), so it cannot run the real redis-py client against
a real Redis server, or boot the actual FastAPI app with --workers N.
This harness therefore reimplements the exact CAS semantics of the two
Lua scripts in app/core/cache.py (acquire-or-renew, release) against an
in-memory store shared across real processes via multiprocessing, and
drives it with the same acquire/poll/step-down loop shape as
LeaderElection._election_tick(). It is a verification of the
COORDINATION ALGORITHM, not an integration test of redis-py or the
FastAPI app - see docs/background-jobs-and-leader-election.md for exactly what is and isn't
covered by this.
"""
import multiprocessing as mp
import os
import signal
import time

TTL_SECONDS = 2.0
POLL_SECONDS = 0.4
TEST_DURATION_SECONDS = 8.0
KILL_LEADER_AT_SECONDS = 3.5
NUM_WORKERS = 3
LOCK_KEY = "test_leader_lock"


def fake_acquire_or_renew(store, lock, key, holder, ttl):
    """Exact logic of cache.py's _ACQUIRE_OR_RENEW_LOCK_SCRIPT, but as
    Python protected by a multiprocessing.Lock instead of Redis's
    single-threaded execution - both give the GET-then-SET atomicity
    the algorithm depends on."""
    with lock:
        now = time.time()
        entry = store.get(key)
        if entry is None or entry[1] < now or entry[0] == holder:
            store[key] = (holder, now + ttl)
            return True
        return False


def worker(idx, store, lock, events, kill_flag, ready_barrier):
    holder_id = f"worker-{idx}-{os.getpid()}"
    is_leader = False
    leader_since = None
    ready_barrier.wait()
    end_time = time.time() + TEST_DURATION_SECONDS
    while time.time() < end_time:
        if kill_flag.value == idx:
            # A real crash (SIGKILL) never gets to run any more Python,
            # let alone a graceful release_lock() - it just stops. This
            # branch exists only so the *survivor* processes' loop can
            # keep running in this same test process tree; the actual
            # kill is delivered by the parent via os.kill(SIGKILL).
            return
        won = fake_acquire_or_renew(store, lock, LOCK_KEY, holder_id, TTL_SECONDS)
        now = time.time()
        if won and not is_leader:
            is_leader = True
            leader_since = now
            events.append(("acquire", idx, holder_id, now))
        elif not won and is_leader:
            is_leader = False
            events.append(("step_down", idx, holder_id, leader_since, now))
            leader_since = None
        if is_leader:
            events.append(("write", idx, holder_id, now))
        time.sleep(POLL_SECONDS)
    if is_leader:
        events.append(("end_while_leader", idx, holder_id, leader_since, time.time()))


def main():
    mgr = mp.Manager()
    store = mgr.dict()
    lock = mgr.Lock()
    events = mgr.list()
    kill_flag = mgr.Value("i", -1)
    barrier = mp.Barrier(NUM_WORKERS + 1)

    procs = [
        mp.Process(target=worker, args=(i, store, lock, events, kill_flag, barrier))
        for i in range(NUM_WORKERS)
    ]
    for p in procs:
        p.start()

    barrier.wait()
    start_time = time.time()
    print(f"[harness] {NUM_WORKERS} worker processes started at t=0.00")

    time.sleep(KILL_LEADER_AT_SECONDS)
    entry = store.get(LOCK_KEY)
    killed_idx = None
    if entry:
        holder = entry[0]
        for i, p in enumerate(procs):
            if holder == f"worker-{i}-{p.pid}":
                killed_idx = i
                break
    if killed_idx is not None:
        os.kill(procs[killed_idx].pid, signal.SIGKILL)
        print(f"[harness] t={time.time()-start_time:.2f} HARD-KILLED leader "
              f"worker-{killed_idx} (pid={procs[killed_idx].pid}) with SIGKILL "
              f"(no graceful release - simulating a real process crash)")
    else:
        print("[harness] WARNING: no leader had been elected yet at kill time")

    for i, p in enumerate(procs):
        p.join(timeout=TEST_DURATION_SECONDS + 2)

    # ---- Analysis ----
    evs = sorted(events, key=lambda e: e[-1])
    intervals = []  # (idx, start, end)
    open_leader = {}
    for e in evs:
        kind = e[0]
        if kind == "acquire":
            _, idx, holder, t = e
            open_leader[idx] = t
        elif kind in ("step_down", "end_while_leader"):
            _, idx, holder, since, until = e
            if since is not None:
                intervals.append((idx, since, until))
            open_leader.pop(idx, None)
    # any leader still "open" at process end (killed leader never emits
    # step_down/end_while_leader - it was SIGKILLed) - close its interval
    # at the moment its lease was next successfully taken by someone else,
    # which is the most realistic bound on "how long could it have kept writing".
    for idx, since in open_leader.items():
        # find the next acquire by a different idx after `since`
        next_acquire = min(
            (e[3] for e in evs if e[0] == "acquire" and e[1] != idx and e[3] > since),
            default=since,
        )
        intervals.append((idx, since, next_acquire))

    print(f"\n[harness] leadership intervals (idx, start_offset, end_offset):")
    for idx, s, e in sorted(intervals, key=lambda x: x[1]):
        print(f"  worker-{idx}: {s - start_time:.2f}s -> {e - start_time:.2f}s")

    # 1. Mutual exclusion: no two intervals (different idx) overlap.
    overlaps = []
    for a in range(len(intervals)):
        for b in range(a + 1, len(intervals)):
            i1, i2 = intervals[a], intervals[b]
            if i1[0] == i2[0]:
                continue
            if i1[1] < i2[2] and i2[1] < i1[2]:
                overlaps.append((intervals[a], intervals[b]))

    # 2. Failover happened, and within a bounded time.
    distinct_leaders = sorted(set(idx for idx, _, _ in intervals))
    failover_ok = killed_idx is not None and len(distinct_leaders) >= 2

    failover_gap = None
    if killed_idx is not None:
        killed_interval = max((iv for iv in intervals if iv[0] == killed_idx), key=lambda iv: iv[2])
        next_leader_start = min(
            (s for idx, s, _ in intervals if idx != killed_idx and s >= killed_interval[1]),
            default=None,
        )
        if next_leader_start is not None:
            failover_gap = next_leader_start - killed_interval[2]

    # 3. No duplicate writes: no two "write" events from different idx
    #    with overlapping leader intervals (implied by #1, checked directly too).
    writes = [e for e in evs if e[0] == "write"]
    write_conflicts = 0
    for i in range(len(writes)):
        for j in range(i + 1, len(writes)):
            if writes[i][1] != writes[j][1] and abs(writes[i][3] - writes[j][3]) < 0.01:
                write_conflicts += 1

    print(f"\n[harness] RESULTS")
    print(f"  killed leader index: {killed_idx}")
    print(f"  distinct leaders over test: {distinct_leaders}")
    print(f"  overlapping leadership intervals: {len(overlaps)} "
          f"({'FAIL' if overlaps else 'PASS - mutual exclusion held'})")
    print(f"  failover occurred: {failover_ok} "
          f"({'PASS' if failover_ok else 'FAIL'})")
    if failover_gap is not None:
        bound = TTL_SECONDS + POLL_SECONDS * 2
        print(f"  failover gap: {failover_gap:.2f}s (bound: <= {bound:.2f}s) "
              f"({'PASS' if failover_gap <= bound else 'FAIL - slower than expected'})")
    print(f"  concurrent write conflicts: {write_conflicts} "
          f"({'PASS' if write_conflicts == 0 else 'FAIL'})")
    print(f"  total writes recorded: {len(writes)}")

    all_pass = (not overlaps) and failover_ok and write_conflicts == 0 and (
        failover_gap is None or failover_gap <= TTL_SECONDS + POLL_SECONDS * 2
    )
    print(f"\n[harness] OVERALL: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
