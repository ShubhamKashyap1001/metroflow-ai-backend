# Query performance & indexing

Covers N+1 query elimination and the indexes backing the
alert/notification list endpoints. Method: every claim here is backed
by `EXPLAIN (ANALYZE, BUFFERS)` against a real Postgres instance seeded
with realistic row counts, not guessed.

## Method

1. Stood up a real Postgres 16 database, seeded via `init_db.py` +
   `seed_real_data.py` (324 real stations, 92 trains, 311k historical
   schedule rows) plus `app/database/seed_load_test.py` (1,000 users,
   5,000 journeys, 1,500 enquiries, 2,000 alerts, 8,000 notifications,
   6,000 notification_logs, 300 news items) to exercise the tables
   `seed_real_data.py` doesn't touch.
2. Statically scanned every `for ... in ...:` loop in `app/` for a
   `.query()`/`.execute()`/`.get()` call inside it (the N+1 signature).
3. Cross-checked every model's `relationship()` against the service
   code that serializes it, for missing `joinedload()`.
4. For write-heavy, no-index tables, bulk-inserted realistic
   production volumes (`alerts` → 152k rows, `notifications` → 408k
   rows, `notification_logs` → 606k rows) and ran `EXPLAIN ANALYZE` on
   every query shape each service function actually issues.
5. For every candidate index, created it on a throwaway copy first,
   re-ran `EXPLAIN ANALYZE`, and only kept it if it produced a real,
   measured improvement. Several candidates (a plain `created_at`-only
   index for `unread_count`, a `(user_id, created_at)` composite) were
   tried and discarded because they measured no better than a simpler
   alternative.

## Result: no remaining N+1 queries

None found. Every list/detail endpoint's DB access was already O(1)
queries (batched `.in_()` lookups or `joinedload`) — `train_service.py`,
`schedule_service.py`, and `analytics_service.py` all pre-fetch data in
one batched query before looping over it, and every list endpoint that
touches a relationship (`Enquiry.user`, `Station.metro_lines`,
`TrainSchedule.station`) already uses `joinedload()`. No code changes
to any service's query *shape* were needed — only the indexes below.

## Result: `alerts`, `notifications`, and `notification_logs` had no indexes beyond their primary key

Despite being the DB paths for the dashboard alert feed, the bell-icon
notification center (polled every ~30s by every open client), and the
per-alert delivery log, these three tables had never had an index
added — every one of their list queries was a full sequential scan,
getting linearly slower as the tables grow (and two of the three, per
their own read-time-retention design, never shrink).

### Indexes added

| Table | Index | Query it fixes | Before | After |
|---|---|---|---|---|
| `alerts` | `ix_alerts_created_at` (created_at) | `list_alerts()` — default, no filter | 35.1ms (parallel seq scan, 152k rows) | 1.28ms |
| `alerts` | `ix_alerts_is_resolved_created_at` (is_resolved, created_at) | `list_alerts(active_only=True)` | 19.5ms | 1.28ms |
| `alerts` | `ix_alerts_station_id_created_at` (station_id, created_at) | `list_alerts(station_id=…)` | 8.3ms | 0.53ms |
| `notification_logs` | `ix_notification_logs_alert_id_created_at` (alert_id, created_at) | `list_alert_notifications()` | 40.1ms (seq scan, 606k rows) | 0.51ms |
| `notifications` | `ix_notifications_created_at_is_read_user_id` (created_at, is_read, user_id) | `list_notifications()` and `unread_count()` | list: 70.0ms · unread_count: 56.5ms (seq scan, 408k rows) | list: 1.41ms · unread_count: ~15-25ms |

All five numbers are `best-of-5` timings of the actual Python service
functions (`alert_service.list_alerts`,
`alert_service.list_alert_notifications`,
`notification_service.list_notifications`,
`notification_service.unread_count`) against the same seeded database
— the real, end-to-end improvement a request sees, not raw SQL
microbenchmarks.

### Why one composite index for `notifications`, not two simple ones

`list_notifications()` and `unread_count()` share the same base filter
(`created_at >= 7-day cutoff AND (user_id IS NULL OR user_id = :me)`);
`unread_count()` additionally filters `is_read = false` and has no
`LIMIT` (it has to visit every matching row to count it). A plain
`created_at` index — which helped `list_notifications()` enormously by
letting it stop after 100 rows — measured **no improvement at all**
for `unread_count()` (still ~44-60ms). Only once `is_read` and
`user_id` were folded into the same index as trailing columns did
Postgres switch to an **Index Only Scan** (0 heap fetches) for
`unread_count()`, cutting it from ~56ms to ~15-25ms. A composite
`(user_id, created_at)` index and a partial `WHERE is_read = false`
index were also tried and measured no better than this single
covering index for either query, so the simpler option was kept.

## Applying this to an existing database

New DB installs pick these indexes up automatically —
`app/database/init_db.py`'s `Base.metadata.create_all()` includes them
via each model's `__table_args__`. For an **existing** database created
before this change, run once:

```
cd backend
source venv/bin/activate
python -m app.database.migrate_alert_notification_indexes
```

Uses `CREATE INDEX CONCURRENTLY IF NOT EXISTS`, the same pattern as
`migrate_enquiry_indexes.py` / `migrate_journey_indexes.py` — safe to
re-run, doesn't lock writes on tables under constant live traffic
(this project doesn't use Alembic; `init_db.py`'s
`Base.metadata.create_all()` only creates tables that don't exist yet
and never adds an index to a table that already exists).

## Verification (alert/notification indexing pass)

1. `python -m app.database.init_db` on a fresh database — confirmed
   all 5 indexes are created automatically via `pg_indexes`.
2. `seed_real_data.py --reset` + `seed_load_test.py` + bulk SQL inserts
   to scale `alerts`/`notifications`/`notification_logs` to
   152k/408k/606k rows.
3. `EXPLAIN (ANALYZE, BUFFERS)` on every query shape, before adding any
   index (all full sequential/parallel-seq scans) and after (all
   index / index-only scans).
4. Re-ran the same comparison through the actual `alert_service`/
   `notification_service` Python functions (not raw SQL) to confirm
   the ORM-generated queries get the same plan — confirmed, same
   numbers within noise.
5. Full existing test suite: 227 passed, 8 pre-existing failures
   verified identical before and after this work (unrelated to
   indexing — a dev-mode auth bypass test and pagination-limit tests
   that assert exact mock call counts).

---

## A later pass: finding the 2 slowest verified queries app-wide

A follow-up pass went further than a table-by-table audit: it measured
every distinct query the running API actually issues, ranked them by
real execution time, and fixed only the two that came out slowest —
rather than guessing which queries "feel" expensive.

### Method

1. Seeded the database with realistic volume via the project's own
   `seed_real_data.py` + `seed_load_test.py` (324 stations, 92 trains,
   2,730 canonical timetable slots, ~5,800 `crowd_logs` rows, 1,000
   users, 8,000 notifications, 5,000 journeys, 2,000 alerts, 1,500
   enquiries) — an empty/near-empty table hides missing indexes, since
   Postgres will happily seq-scan a handful of rows faster than it can
   use an index.
2. Enabled `pg_stat_statements`, reset it, then exercised every GET
   endpoint in the API multiple times so the numbers reflect real,
   server-measured execution time of actual production query shapes,
   not synthetic ones.
3. Ranked every distinct query by `mean_exec_time`, then ran
   `EXPLAIN (ANALYZE, BUFFERS)` on the top candidates with real bound
   parameter values to confirm *why* each one was slow before changing
   anything. Several high-ranked queries (the `crowd_logs` bulk
   lookups used by `passenger_flow_overview`/`get_station_monitor`)
   turned out to already be using the optimal index
   (`ix_crowd_logs_station_id_created_at`) — their cost is
   proportional to result-set size, not a missing index, so they were
   left alone.

### Finding #1 (slowest): `GET /schedules/upcoming`

`schedule_service.get_upcoming_schedules()` filters `train_schedules`
on `day_type` + `departure_time >=` and orders by `departure_time`.
Highest mean execution time of any query observed (~4.8ms on a table
of only 2,730 rows — the worst of the set). `EXPLAIN ANALYZE`
confirmed two separate, stackable problems:

1. **No index touched `departure_time` at all** — every existing index
   on `train_schedules` leads with a column this query doesn't filter
   on, so it fell back to a full `Seq Scan` filtering both `day_type`
   and `departure_time` row-by-row.
2. **An unconditional `JOIN stations`** ran even when no `state`/city
   filter was requested (the Dispatch Board's default, no-city-selected
   view) — it only exists to support `Station.city IN (...)`
   filtering, but was joined regardless. This is the exact bug class
   `_scope_to_state()` elsewhere in this same file was already written
   to avoid; `get_upcoming_schedules()` had its own hand-rolled join
   that skipped that guard. The extra `Hash Join` forced an explicit
   `Sort` node after it and cost an unnecessary full scan of
   `stations` (324 rows) on every call.

**Fix:** added `ix_train_schedules_day_type_departure_time` on
`(day_type, departure_time)` (`app/models/train_schedule.py` + one-off
migration `app/database/migrate_schedule_departure_index.py`, same
`CREATE INDEX CONCURRENTLY IF NOT EXISTS` pattern as every other index
migration in this project), and made the `stations` join conditional
on `cities` actually being non-empty, matching `_scope_to_state()`'s
existing pattern (`app/services/schedule_service.py`).

**Before/after** (`EXPLAIN ANALYZE`, controlled A/B — index
dropped/recreated and join present/absent independently, same bound
values, warm cache, no state filter — the common case):

| `departure_time` filter | Before (old join + no index) | After (both fixes) |
|---|---|---|
| `>= 06:00` (low selectivity, ~all rows match) | 1.34 ms, 408 buffer hits | 0.46 ms, 244 buffer hits |
| `>= 20:00` (high selectivity, ~18% of rows match) | 0.66 ms, 408 buffer hits | 0.32 ms, 244 buffer hits |
| `>= 22:00` (very high selectivity, ~12% of rows match) | 0.55 ms (Seq Scan) | 0.26 ms (Bitmap Index Scan) |

Buffer reads drop ~40% in every case (dropping the unconditional
`stations` scan), and execution time roughly halves to a third across
the board. At very high selectivity (evening/late-night calls) the new
index additionally lets Postgres use a `Bitmap Index Scan` instead of
scanning the whole table. Postgres's own cost-based planner still
prefers `Bitmap Heap Scan` + a `Sort` step over a plain
order-preserving `Index Scan` at this table's current size (2,730
rows) — that's a correct, cost-based choice, not a remaining bug; the
index still pays off through fewer rows scanned, and matters more as
`train_schedules` grows.

### Finding #2 (2nd slowest): `GET /crowd/station-monitor` → `get_inflow_outflow_bulk()`

Second-highest mean execution time (~4.5ms, ~5,800 rows/call).
`EXPLAIN ANALYZE` showed this query *was* already using the correct
index (`ix_crowd_logs_station_id_created_at`, `Index Scan`, no seq
scan) — not a missing-index problem. The query was `db.query(CrowdLog)`
— SQLAlchemy hydrates every mapped column (`id`, `station_id`,
`current_count`, `crowd_level`, `created_at`, `updated_at`) — but the
loop below only ever reads `.station_id` and `.current_count`
(`.created_at` is used purely for `ORDER BY`, which doesn't require it
to be in the select list). The sibling function
`analytics_service.passenger_flow_overview()` already does this
correctly — it projects only the 3 columns it needs — and measured
consistently faster for the same row cardinality and same index usage,
which is what pointed at column hydration/transfer as the actual cost
here rather than the query plan.

**Fix:** changed `crowd_service.get_inflow_outflow_bulk()` to project
`CrowdLog.station_id, CrowdLog.current_count` instead of the full
entity. No plan change, no behavior change — same rows, same order,
less data hydrated and shipped per row.

### Verification

- `EXPLAIN ANALYZE` A/B tables above for Finding #1 (index + join,
  isolated independently by dropping/recreating the index and diffing
  the join on/off).
- `pg_stat_statements` reset and the same fixed battery of GET
  endpoints re-run against live `uvicorn` traffic after both fixes
  (fresh restart, no plan-cache carryover):
  `get_inflow_outflow_bulk`'s mean dropped from ~4.5 ms to ~0.6 ms with
  identical row counts, same ordering, and an unchanged index-optimal
  plan — confirms the column-hydration savings from Finding #2 in real
  traffic, not just in isolated `EXPLAIN`.
- `/schedules/upcoming`'s live mean did not drop in this same run,
  because the container's current time-of-day put `departure_time >=
  now` at ~80% selectivity, where Postgres's cost-based planner
  correctly still prefers the `Seq Scan` it always used (verified via
  `EXPLAIN` at that exact selectivity) — not a bug, just that
  particular test window landing outside the fix's `Bitmap Index Scan`
  crossover point (~17:00+ in the seeded schedule, per the A/B table
  above). The unconditional join removal still applies on every call
  regardless of time of day.
- Full test suite re-run after both changes: 235 passed, 7 skipped,
  the same 9 tests failing before and after (confirmed via a diff run
  against an untouched copy of the original code in the same
  environment) — those failures are pre-existing and unrelated to
  either change here (pagination/mocking tests unrelated to
  `train_schedules`/`crowd_logs`). No regression from either fix.
