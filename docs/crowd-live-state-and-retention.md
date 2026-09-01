# Crowd data storage — live state, historical log, and retention

This covers how "how crowded is this station right now" and "what was
the crowd history over time" are stored, how that data reaches the
dashboard live, and how storage growth is kept bounded. For what the
numbers themselves *mean* (occupancy formula, capacity, crowd-level
thresholds), see
[crowd-data-correctness.md](./crowd-data-correctness.md).

## The data model

Two tables, each with a distinct job:

- **`station_crowd_state`** (`app/models/station_crowd_state.py`) —
  one row per station (`station_id` is the primary key), holding
  `current_count`, `crowd_level`, `updated_at`. This is **live state**:
  it's upserted, never appended to, so its size is permanently bounded
  by the number of active stations (324), not by elapsed time. Every
  dashboard/heatmap/congestion/station-monitor read comes from here.
- **`crowd_logs`** — the **historical** log, one row per station per
  sample, used for trend queries (`inflow-outflow`, `station-analytics`).
- **`crowd_logs_hourly`** (`app/models/crowd_log_hourly.py`) — an
  hourly rollup of `crowd_logs` (`avg_count`, `max_count`, `min_count`,
  `sample_count` per `(station_id, hour)`), so old raw rows can be
  aggregated and dropped instead of kept forever.

### Why the split exists

Originally `crowd_logs` was the *only* table. Every dashboard read had
to find "the most recent row per station" via a `ROW_NUMBER() OVER
(PARTITION BY station_id ORDER BY created_at DESC)` window query across
the entire table, and every simulator tick (every 10s) inserted a new
row for **all 324 stations**, unconditionally, with no upper bound —
the data model never distinguished "what is the crowd level right now"
(a fact, one value per station, that should be overwritten) from "what
was the crowd level at each point in time" (a log that should be
appended to). Splitting the two means the live-lookup path is a simple
outer join against a fixed 324-row table (no window function, no
scan of an ever-growing table), and `csv_replay_simulator._tick_sync`
does one bulk `INSERT ... ON CONFLICT (station_id) DO UPDATE` per tick
for all stations' live rows instead of relying on the historical table.

`crowd_service.get_latest_crowd` (`GET /crowd/{station_id}`) is a
primary-key lookup on `station_crowd_state` rather than an `ORDER BY
created_at DESC LIMIT 1` scan.

## Sampling the historical write

Even with the live/historical split, `crowd_logs` was still getting
one row per station per 10s tick — no product need for minute-scale
trend analytics to be sampled that finely, since the *live* dashboard
is now served from `station_crowd_state` instead.

`CROWD_HISTORY_INTERVAL_SECONDS` (default 60) gates historical writes:
`_should_write_history(station_id, now)` only allows one `CrowdLog`
insert per station per interval, independent of how often the
simulator tick itself runs. Live state is **not** gated — the
dashboard/WebSocket still update every tick as required. Manual/sensor
readings (`POST /crowd/`) and check-in/check-out are also **not**
gated — they're real, discrete events, not simulator noise, so every
one of them is recorded. At 324 active stations and a 10s tick, this
is a 6x reduction in `crowd_logs` writes (1,944/min → 324/min at the
default 60s sampling interval; raising the interval further trades
write volume for coarser trend resolution).

## Retention and rollup

A background job (`app/simulator/retention.py`, `CROWD_RETENTION_INTERVAL_SECONDS`
default 3600, gated by `ENABLE_CROWD_RETENTION_JOB`) runs hourly:

1. Aggregates every raw `crowd_logs` row older than
   `CROWD_LOG_ROLLUP_AFTER_DAYS` (default 2 days) into
   `crowd_logs_hourly` (idempotent upsert — safe to re-run over an
   overlapping window), then deletes the raw rows it just rolled up.
2. Safety-net hard delete: any raw row older than
   `CROWD_LOG_RETENTION_DAYS` (default 30 days) is deleted regardless,
   in case the rollup step is ever disabled or falls behind.
3. Hard-deletes `crowd_logs_hourly` rows older than
   `CROWD_LOG_HOURLY_RETENTION_DAYS` (default 400 days), so the rollup
   table doesn't grow forever either.

The **live** table (`station_crowd_state`) is never subject to
retention — its size is bounded by station count, not time. The job is
wired into `app/simulator/scheduler.py` / `app/main.py`'s lifespan
alongside the crowd/train simulator loops, and reported under
`GET /health`'s `scheduler.crowd_retention_job`.

Steady-state footprint at the defaults, measured against a real seeded
Postgres instance: raw `crowd_logs` settles around ~933k rows
(~118.5MB) at the 2-day rollup window; `crowd_logs_hourly` settles
around ~3.1M rows (~609.6MB) at the 400-day retention window;
`station_crowd_state` stays fixed at 324 rows. Total crowd-data
footprint is bounded (~728MB, constant) instead of growing without
limit (previously ~355.6MB/day and climbing — over 10GB after 30 days).

## Check-in/check-out: live broadcast and concurrency

### Live broadcast

A passenger check-in/check-out used to write the `CrowdLog` row and
invalidate the cache, but nothing notified connected clients — the
dashboard only reflected it on the *next* simulator tick (up to 10s
later), and only incidentally, not because the check-in itself pushed
anything. `crowd_service.broadcast_crowd_update(station, count, level)`
now pushes a `crowd_update` event in the same payload shape the
simulator's own per-tick broadcast uses, and
`journey_service.check_in`/`check_out` call it right after committing.
The live read path (`GET /crowd/{station_id}` immediately after a
check-in, no tick in between) also reflects the change immediately,
since it's already a fresh read from `station_crowd_state`.

### The lost-update race

Load-testing the broadcast fix surfaced a genuine concurrency bug:
running 15 concurrent check-ins against the same station only landed 2
of the 15 increments — the other 13 were silently lost. The delta was
computed as "read current value → add delta in Python → write it
back"; under concurrency, two transactions can both read the same
starting value before either commits, so the second write overwrites
the first instead of building on it (a textbook lost-update race).

`crowd_service.apply_live_state_delta(db, station, delta)` fixes this:

1. `INSERT ... ON CONFLICT (station_id) DO NOTHING` to bootstrap the
   station's row on its first-ever check-in (race-safe: if several
   concurrent first-timers try this, at most one actually inserts).
2. `SELECT ... FOR UPDATE` on that row — a row-level lock, so a second
   concurrent caller for the **same** station blocks until the first
   one's transaction commits, instead of reading a stale value.
   Check-ins on different stations are unaffected — they lock
   different rows.
3. Compute the new count/level from the now-locked, guaranteed-current
   value, write it back, and return it.

Reproduced-then-fixed: 15 concurrent same-station check-ins went from
2/15 correct increments landing to 15/15, with all 15 journeys still
created as distinct rows.

## Config

| Setting | Default | Purpose |
|---|---|---|
| `CROWD_HISTORY_INTERVAL_SECONDS` | `60` | Minimum seconds between `crowd_logs` writes for the same station |
| `CROWD_LOG_ROLLUP_AFTER_DAYS` | `2` | Raw rows older than this get aggregated into `crowd_logs_hourly` and deleted |
| `CROWD_LOG_RETENTION_DAYS` | `30` | Safety-net hard delete for raw `crowd_logs` |
| `CROWD_LOG_HOURLY_RETENTION_DAYS` | `400` | Hard delete for `crowd_logs_hourly` rollups |
| `ENABLE_CROWD_RETENTION_JOB` | `True` | Turn the retention background task on/off |
| `CROWD_RETENTION_INTERVAL_SECONDS` | `3600` | How often the retention job runs |

## Not covered here

Train tracking (`train_tracking.py`, `train_simulator.py`) was already
upsert-based (`TrainLocation`) before this work and needed no
equivalent change. Making the simulator itself run exactly once across
multiple worker processes is a separate concern — see
[background-jobs-and-leader-election.md](./background-jobs-and-leader-election.md).
