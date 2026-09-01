# MetroFlow — architecture & correctness audit

Scope: `metroflow-ai-backend` (FastAPI/SQLAlchemy/Redis/WebSocket) +
`metroflow-ai` (Next.js/RTK Query frontend). This is a point-in-time
audit — findings only, cited against the exact file/function they were
verified in. Where a finding could not be verified (e.g. it needed a
live Postgres/Redis instance), that's stated explicitly rather than
assumed. Each finding below links to the doc that covers its actual
fix.

---

## P0 — breaks the product under normal operation

### P0-1. The AI Insights panel causes a request/DB-write storm on every live event

**File:** `frontend/src/components/dashboard/AIInsights.tsx`.
Also involves `backend/app/services/prediction_service.py::smart_recommendations`
and the `20/minute` rate limit on `/predictions/recommendations/{station_id}`.

The component re-ran its fetch effect on **every** `crowd_update` /
`delay_alert` / `station_alert` WebSocket event, ranked up to 15
stations, and fired 15 parallel `getRecommendations()` calls with no
debounce and no in-flight guard. With the simulator batching updates
into one `crowd_update` per ~10s tick, that's up to 90 requests/minute
from a single tab against a 20/minute limit — most calls came back
`429`. Every successful call also wrote 3 new `Prediction` rows,
regardless of whether the values were freshly computed or served from
cache.

Fixed in [ai-recommendations.md](./ai-recommendations.md): a bulk
recommendations endpoint collapses 15 requests into 1, the panel moved
to a fixed-interval refresh with an in-flight guard, and prediction
writes are now deduplicated per station.

### P0-2. The WebSocket heartbeat was never acknowledged by the server

**File:** `backend/app/main.py` (`/ws/monitor` handler); also
`frontend/src/providers/LiveSocketProvider.tsx`.

The frontend sends `{"type":"ping"}` every 15s and force-closes the
socket if nothing arrives within 10s. The server read the ping and
discarded it — no `pong`, ever. This was masked whenever the
simulator was broadcasting real events on its own cadence (which reset
the client's timeout as a side effect), but any genuine lull —
`ENABLE_SIMULATOR=False`, an admin pausing the simulator, off-peak
hours — caused a healthy socket to be closed and reconnected roughly
every 25 seconds.

Fixed in [realtime-websocket-system.md](./realtime-websocket-system.md):
the server now replies to a client ping with an explicit `pong`.

---

## P1 — real correctness/staleness bugs, narrower blast radius

### P1-1. Creating/updating a schedule never invalidated the schedule cache

`create_schedule`/`update_schedule` never called
`_invalidate_schedule_caches`, unlike `handle_delay`/`adjust_frequency`
which already did. An operator's new or edited schedule row could stay
invisible on the Dispatch Board for up to `SCHEDULE_CACHE_TTL_SECONDS`
(30s). Straightforward fix: call the same invalidation helper from all
four write paths.

### P1-2. `handle_delay` crashed with a `ValueError` when a delay pushed the arrival time past hour 23

`backend/app/services/schedule_service.py::handle_delay` did manual
hour/minute rollover arithmetic that didn't handle crossing midnight
(e.g. a 23:50 departure with a 30-minute delay computes
`hour=24`, which `datetime.replace()` rejects). Any late-night
schedule receiving a delay large enough to cross midnight made "Report
Delay" fail outright with an unhandled 500. Fixed by doing the
arithmetic via `timedelta` addition on the full `datetime` instead of
manual hour/minute math.

### P1-3. Naive vs. timezone-aware `datetime` comparisons throughout the services layer

Most service modules called naive `datetime.utcnow()` and compared the
result against `DateTime(timezone=True)` columns, while
`notification_service.py` correctly used `datetime.now(timezone.utc)`.
With a Postgres session whose timezone isn't pinned to UTC, every
`hours`-based window query (inflow/outflow, station analytics, traffic
reports, prediction cache keys) could be silently off by the session's
UTC offset — a bug that looks fine in a UTC-default dev database and
produces subtly wrong analytics in a differently configured
environment. It's also a Python 3.12+ deprecation
(`datetime.datetime.utcnow()` is deprecated). Standardized on
`datetime.now(timezone.utc)` everywhere this was found; the crowd-data
path's instances of this were cleaned up as part of
[crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md).

### P1-4. Check-in/check-out didn't push a live WebSocket update

`journey_service.check_in`/`check_out` wrote the `CrowdLog` row and
invalidated the station cache, but never broadcast a `crowd_update` —
so a passenger's own check-in only showed up on the dashboard once the
next simulator tick ran (up to 10s later), even though the underlying
data was already correct and committed instantly. Fixed in
[crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md):
check-in/check-out now broadcast synchronously with the request.

### P1-5. `/api/v1/health/` leaked raw exception text to unauthenticated callers

`db_status = f"error: {exc}"` embedded the raw `psycopg2` exception
text — commonly including DB host/port — in a JSON response with no
auth on the route. Minor information disclosure, worth fixing: log the
full exception server-side and return a generic `"unreachable"` status
in the response body.

---

## P2 — architectural/scaling concerns

### P2-1. `ConnectionManager` and simulator state were process-local

Neither the WebSocket connection registry nor the simulator's tick
state was backed by Redis pub/sub or any cross-process mechanism,
despite Redis already being a dependency elsewhere in the codebase.
Under `uvicorn --workers N` (or multiple replicas), a client connected
to worker A never received a broadcast generated on worker B, and each
worker ran its own independent copy of the simulator loop with its own
replay cursor — two tabs on different workers would see different
crowd numbers for the same station at the same instant.

Fixed by [background-jobs-and-leader-election.md](./background-jobs-and-leader-election.md)
(exactly one simulator leader across all workers) together with
[realtime-websocket-system.md](./realtime-websocket-system.md) (a
Redis-backed relay so every worker's clients receive every event).

### P2-2. Unbounded `crowd_logs` growth from the simulator

Every tick inserted a new `CrowdLog` row for every active station,
unconditionally. At 324 stations and a 10s tick, that's ~2.8M
rows/day and no retention — hundreds of millions of rows over a few
months, with no archival or rollup job anywhere in the codebase.

Fixed in [crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md):
live state and historical log were split into separate tables, writes
to the historical log were sampled, and an hourly rollup + retention
job now bounds total storage.

### P2-3. `smart_recommendations` unconditionally wrote 3 `Prediction` rows on every call

Unlike the read path (which skips recomputation on a cache hit), the
three `_save_prediction()` calls ran on **every** invocation — a cache
hit still produced 3 new rows. This compounds directly with P0-1.
Fixed alongside P0-1 in [ai-recommendations.md](./ai-recommendations.md)
with a write-dedupe window independent of the value cache.

---

## P3 — lower severity / code quality

- `requirements.txt` was UTF-16-LE with a BOM and CRLF line endings,
  unlike every other text file in the repo. Harmless to `pip install
  -r` but unusual; worth normalizing to UTF-8/LF.
- `README.md` was stale relative to the running app in several places
  (described the app as further behind than it was, referenced the
  simulator module that had since been replaced by
  `csv_replay_simulator.py`).
- `_hydrate_profile`/`_hydrate_schedule` reconstruct detached,
  session-less ORM instances from cached JSON. No current call site
  touches a relationship attribute on these (which would raise
  `DetachedInstanceError`), so this isn't a live bug, but it's a trap
  for the next person who adds a relationship access on a cached
  object.
- `train_service.py`/`train_tracking.py` never invalidate the
  `train:positions:*` Redis cache on create/update. Bounded by the
  short default `CACHE_TTL_SECONDS=5`, so impact is minor — same class
  of gap as P1-1, lower severity.
- `current_count` in `CrowdLog` was computed as `entries + exits`
  (period footfall, not simultaneous occupancy) — this matched the
  ML training-data convention, so it was internally consistent, not a
  bug relative to the model. See
  [crowd-data-correctness.md](./crowd-data-correctness.md) for how
  occupancy and "predicted throughput" ended up correctly separated.

---

## What the audit didn't cover

Not audited to the same depth in this pass: `analytics_service.py`,
`enquiry_service.py`, `news_service.py`; the `delay_predictor`/
`frequency_predictor`/metrics modules under `app/ai_engine/prediction/`;
the seed/migration scripts beyond `seed_real_data.py`; the full
`models`/`schemas` layer; several frontend hooks and dashboard/admin
pages. A full security pass (IDOR checks across every `{id}`-scoped
route, CORS configuration review) was also not completed here.

## Recommended fix order (as scoped at audit time)

1. P0-1 (AI Insights request/write storm) — highest combined
   user-visible + backend-load impact, frontend-only and low-risk.
2. P0-2 (WebSocket heartbeat never acked) — small, isolated,
   protects the core "no page refresh" guarantee.
3. P1-2 (`handle_delay` midnight crash) — small fix, highest "actual
   crash" severity of the P1s.
4. P1-1 (schedule cache invalidation gaps).
5. P1-5 (health endpoint info disclosure).
6. P1-4 (check-in/out don't push a live update).
7. P1-3 (naive/aware datetime sweep) — mechanical but touches many
   files; do as one focused pass.
8. P2-3 (dedupe recommendation writes) — pairs with P0-1.
9. P2-2 (crowd_logs retention) — needs a design decision (rolling
   delete vs. partitioning) before implementation.
10. P2-1 (multi-worker/replica WebSocket + simulator state) — the
    biggest architectural item; its own milestone.
11. P3 items — opportunistic, alongside whichever P0/P1/P2 work
    touches the same files.
