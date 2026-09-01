# Background jobs & leader election

MetroFlow runs three continuous background loops: the crowd simulator
(`csv_replay_simulator.py`), the train tracker (`train_simulator.py`),
and the crowd-retention job (`retention.py`, see
[crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md)).
Each must run **exactly once** across the whole deployment, no matter
how many worker processes or container replicas are running. This doc
covers how that's guaranteed, and the edge cases that took more than
one pass to get right.

## The problem

When the API runs as more than one process (`uvicorn --workers N`,
multiple container replicas, or any load-balanced deployment), a naive
"start the loop on app startup" approach starts it **once per
process** instead of once total. With N processes:

- N× independent writes to `crowd_logs`/`station_crowd_state`/
  `train_location` per tick — real data corruption, since the "current"
  value for a station becomes whichever of the N racing writers
  committed last.
- N× overcrowding notifications for the same event.
- N× WebSocket broadcasts per tick.
- N× retention passes running the same rollup/delete logic
  concurrently against the same rows.

A module-level `asyncio.Task` guard (`if _crowd_task is None or
_crowd_task.done(): ...`) only protects against a second call *within
the same process* — it does nothing across processes, since each
worker has its own independent memory. This is invisible in the common
single-worker local-dev case (`uvicorn app.main:app`, no `--workers`
flag), which is exactly why it can ship unnoticed until a multi-worker
deployment is tested.

## Coordination mechanism: a Redis-backed lease, with a same-host fallback

### Redis lease (the primary mechanism)

`app/simulator/leader_election.py`'s `LeaderElection` class wraps one
background loop. All processes continuously bid for a shared Redis
lease instead of trusting their own in-memory state:

- `app/core/cache.py` provides two atomic primitives:
  - `try_acquire_or_renew_lock(key, holder_id, ttl_seconds)` — a Lua
    script (`EVAL`) doing an atomic "acquire if free, or renew if I
    already hold it" compare-and-set. Atomicity via `EVAL` (not a
    plain `GET` then `SET`) is what prevents two processes racing to
    acquire the same free key from both winning.
  - `release_lock(key, holder_id)` — atomic "delete only if I'm still
    the holder", used on graceful shutdown so the next standby doesn't
    have to wait out the full lease.
- Each process polls every 3s (`DEFAULT_POLL_SECONDS`); the lease TTL
  is 15s (`DEFAULT_LEASE_SECONDS`). Only the process currently holding
  the lease runs the actual loop; everyone else stays in standby.
- **Automatic failover on crash**: because the lease has a TTL, a
  leader that dies (killed, OOM, hard crash) simply stops renewing it.
  The lease expires on its own within the TTL, and the next standby
  process to poll acquires it — no separate crash-detection mechanism
  needed.
- `app/simulator/scheduler.py` creates one `LeaderElection` per loop
  instead of a bare `asyncio.Task`. The public API
  (`start_simulator`, `stop_simulator`, `start_train_tracker`,
  `stop_train_tracker`, `start_retention_job`, `stop_retention_job`,
  `is_simulator_running`, `scheduler_status`, etc.) is unchanged in
  name and shape, so callers (`app/main.py`, `app/api/v1/admin.py`,
  `app/api/v1/health.py`) needed no changes.
  `scheduler_status()` additionally reports `"leader"` / `"standby"` /
  `"crashed"` per loop, surfaced through `/health` and
  `/admin/simulator`.

### Same-host fallback: what happens when Redis is unreachable

If Redis is disabled (`CACHE_ENABLED=False`/no `REDIS_URL`) or has
never been reachable since the process started, cross-process
coordination via Redis is impossible. The naive answer — "just run the
loop locally" — is exactly the bug this section exists to prevent,
because every process independently concludes "I must be alone" with
no signal telling it that a sibling process reached the same
conclusion.

Instead, `app/simulator/local_lock.py` (zero third-party dependencies:
`os`, `fcntl`, `tempfile`, `threading` only) provides a same-host,
kernel-enforced coordinator:

- `try_acquire(name)` opens (creating if needed)
  `/tmp/metroflow_leader_<name>.lock` and attempts a non-blocking
  exclusive `fcntl.flock()` on it. `flock()` is enforced by the
  **kernel** against every other process trying to lock the same file
  — the missing cross-process signal a plain in-memory flag can never
  provide, because an `asyncio.Lock`/module global only ever guards
  tasks inside one process's own event loop.
- Returns `True` only if this process now holds the lock (idempotent —
  also `True` if it already did). Returns `False` if another local
  process holds it right now, or if `flock` isn't available on this
  platform — it never guesses "I'm probably alone."
- The OS releases the flock automatically the instant the holding
  process exits or is killed (including `SIGKILL`/OOM) — no TTL, no
  heartbeat needed; same crash-safety property as the Redis lease's
  TTL.
- `release(name)` is a best-effort early release for graceful
  shutdown.

`leader_election.py`'s fail-open branch now calls
`local_lock.try_acquire(self.name)` instead of unconditionally
starting; only the one process that wins the file lock runs the loop.
On reconnect, any held local lock is released and the code switches
back to the Redis lease as the sole source of truth.

**What this does and doesn't cover:** same host, multiple processes,
no Redis — fully fixed, the kernel guarantees exactly one winner.
Multiple *hosts* (separate container replicas with their own
filesystems) with Redis unreachable for their entire lifetime are
still not coordinable — a local file lock is local to one host by
definition. This is the one scenario that genuinely requires Redis for
guaranteed single-leader correctness; it's stated explicitly rather
than silently papered over.

A real, mid-flight Redis outage (Redis *was* reachable, then goes
down) is handled differently from "never configured" — see the next
section.

## Bounding the fail-closed window during a real outage

When Redis has been reachable and then genuinely goes down mid-flight,
the code fails **closed**: the current leader steps down rather than
risk two processes both believing they lead, since other processes may
be on different hosts where the local file lock can't help. After the
outage has lasted at least one full lease window (so any pre-outage
lease is guaranteed expired), it's safe to fall back to the same-host
local lock — treating a sustained outage like "Redis never configured."

The clock for that grace window, `_redis_down_since`, used to be
stamped with "now" at the moment a tick *first noticed* Redis was
unreachable — not at the moment Redis actually went down. Election
ticks poll every `poll_seconds` (default 3s), so this is normally a
few seconds of slack, but a slow/delayed tick or a busy event loop
could let the real outage run for nearly a full lease window before
anyone noticed, and then the code would wait *another* full lease
window from that late observation — up to ~2x the promised fail-safe
window, during which the entire simulator fleet sits idle.

Fixed by tracking `_last_connected_at` (the last tick that actually
observed `state == "connected"`). When a tick first notices the
unreachable state, `_redis_down_since` is backdated to
`_last_connected_at` — the best available estimate of when the outage
actually started — bounding the total fail-closed window to
`lease_seconds` plus at most one `poll_seconds` of detection slack,
regardless of how late any single tick's observation is. "Never
reachable since startup" and "disabled" are unaffected by this — they
don't use `_redis_down_since` at all and still fall back immediately.

## Cross-process delivery of what the leader broadcasts

Electing a single leader solves *who runs the loop*; it doesn't by
itself get that leader's broadcasts to clients connected to other
worker processes. That's a WebSocket-layer concern — see
[realtime-websocket-system.md](./realtime-websocket-system.md) for the
Redis relay that fans a leader's ticks out to every worker's clients.

## Verified behavior

Real multi-process smoke test (actual `redis-server`, 4 real OS
subprocesses each running a real `LeaderElection`, simulating
`uvicorn --workers 4`):

| Step | Action | Observed |
|---|---|---|
| 1 | Redis UP, 4 workers racing | exactly 1 leader among 4 |
| 2a | Redis killed | 0 leaders immediately (fail-closed, inside lease window) |
| 2b | Redis still down, past lease window | exactly 1 leader again, via local-lock fallback |
| 3 | Redis restarted | exactly 1 leader, resumed via the Redis lease |
| 4 | Leader process `SIGKILL`ed | a different worker takes over via lease-expiry failover |

Never zero leaders once past a fail-closed window; never more than one
leader, at any observed instant.

An 8-concurrent-process race for the same local-lock name (simulating
`uvicorn --workers 8` starting with Redis down) reliably produces
exactly 1 winner and 7 losers, and a `SIGKILL`ed local-lock holder is
immediately followed by another process acquiring the lock — automatic
OS-level failover with no manual cleanup.
