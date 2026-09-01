# AI recommendations — the AI Insights panel and its backend endpoint

This covers `smart_recommendations()` / `smart_recommendations_bulk()`
(`app/services/prediction_service.py`,
`app/api/v1/prediction.py`) and the frontend panel that consumes them,
`AIInsights.tsx`. It's the same request/write-storm root cause flagged
as P0-1/P2-3 in [architecture-audit.md](./architecture-audit.md).

## How it works

For a given station, `smart_recommendations()` looks up cached
crowd/delay/frequency predictions (`_cached_predict_crowd` /
`_cached_predict_delay` / `_cached_recommend_frequency`, each backed
by a 300s Redis value cache) and turns them into up to three
human-readable recommendation cards via `_build_recommendations()` —
e.g. "High crowd expected" when predicted throughput crosses the
capacity-relative threshold (see
[crowd-data-correctness.md](./crowd-data-correctness.md) for how that
threshold is derived), falling back to a "Normal operations" card when
nothing is flagged.

`AIInsights.tsx` is the dashboard panel that surfaces these cards. It
subscribes to the same live WebSocket events the rest of the dashboard
uses (`crowd_update`, `delay_alert`, `station_alert`).

## The bug: a live event was treated as "recompute everything now"

The panel re-ran its fetch effect on **every** socket event by
bumping a `refreshTick` counter. Each re-run fetched the crowd
dashboard, ranked the top 15 stations by occupancy, and fired 15
parallel `GET /predictions/recommendations/{station_id}` calls via
`Promise.all` — no debounce, no minimum interval, and no guard against
the previous batch still being in flight.

The backend batches all of a tick's crowd changes into one
`crowd_update` event (`csv_replay_simulator.py::replay_tick` calls
`manager.broadcast` once per tick with every station's update in a
single payload), so with the default 10s simulator interval this fired
roughly every 10 seconds — each time launching 15 requests against an
endpoint rate-limited to 20/minute per IP. That's up to 90
requests/minute from one browser tab, 4.5x the limit, so most calls
came back `429`.

Separately, `smart_recommendations()` unconditionally wrote 3 new
`Prediction` rows (CROWD/DELAY/FREQUENCY) on **every** call, including
calls whose values came entirely from cache and recomputed nothing.
DB writes scaled with dashboard views, not with actual new predictions
— and this compounded directly with the request storm above.

The rest of the dashboard (`KPISection.tsx`) had already solved this
same shape of problem correctly — a fixed polling interval instead of
a socket-driven refetch — but that pattern was never applied to this
panel. The backend also already had a working bulk-endpoint pattern
(`all_stations_traffic_pattern()`, built for the Passenger Analytics
widget) that was never extended to recommendations.

## The fix

**Backend:**
- `smart_recommendations_bulk(db, station_ids)` — the same per-station
  cached lookups `smart_recommendations()` already used, but for many
  stations inside one Python call. `_build_recommendations()` was
  extracted into a shared pure function used by both the single-station
  and bulk paths so they can't drift.
- `GET /predictions/recommendations/bulk?station_ids=1,2,3`, registered
  above `/recommendations/{station_id}` in the router so `bulk` is
  never swallowed by the int path param. Capped at 30 stations per
  call.
- A Redis-backed write-dedupe guard, independent of the existing 300s
  value cache: `set_nx(key, ttl)` (`app/core/cache.py`) does an atomic
  `SET key val NX EX ttl`. `_maybe_persist_recommendation_predictions()`
  is gated by `set_nx("predictions:reco-write:{station_id}", 60)`, so
  the three prediction rows are now persisted at most once per station
  per 60 seconds, no matter how many times recommendations are
  requested for it in between. Fails open (proceeds with the write) if
  Redis is unreachable, matching every other cache helper in this
  module.

**Frontend:**
- `getRecommendationsBulk(stationIds)` calls the bulk endpoint once
  instead of `getRecommendations()` N times.
- The socket-event-driven `refreshTick` was replaced with a fixed
  45-second interval — the same pattern `KPISection.tsx` already used.
  A live socket event no longer directly triggers a refetch of this
  panel.
- An `inFlightRef` guard skips an interval tick that fires while the
  previous fetch is still resolving, instead of stacking a second
  concurrent batch on top of it.

## Result

| | Before | After |
|---|---|---|
| Requests per refresh | 15 parallel HTTP calls | 1 HTTP call |
| Refresh trigger | Every socket event (~every 10s) | Fixed 45s interval |
| Peak request rate, one tab | Up to ~90/min against a 20/min limit | Up to ~1.3/min |
| Prediction rows written per call | 3, every call (cache hit or miss) | 3, at most once per station per 60s |

`getRecommendations()` (used by `CrowdHeatMap.tsx` on an explicit
user station selection) and the single-station REST endpoint are both
untouched — this fix only changed how the AI Insights panel triggers
and batches its own refreshes.

`ActivityTimeline.tsx` also refetches on every socket event, but it's
a single, unrated-limited, read-only query with no ML computation and
no writes — it doesn't exhibit the fan-out/write-storm pattern this
fix targets, so it was left as-is (a candidate for the same
fixed-interval pattern later, for consistency, not because it's
currently causing a problem).

## Tests

`tests/test_prediction.py`:
- `test_recommendations_bulk_requires_auth`
- `test_recommendations_bulk_route_not_shadowed_by_station_id_route` —
  pins router registration order so `/recommendations/bulk` doesn't
  fall through to `/recommendations/{station_id}` trying to parse
  `"bulk"` as an int.

`tests/test_prediction_service.py` (mocked DB, no network/Redis/Postgres
required):
- `test_smart_recommendations_bulk_returns_one_entry_per_station`
- `test_build_recommendations_flags_high_crowd_and_delay` /
  `test_build_recommendations_normal_operations_when_nothing_flagged` —
  confirm the extracted formatting function is behaviour-preserving.
- `test_maybe_persist_skips_save_when_dedupe_key_already_set` — the
  direct regression test: with the dedupe key already set, `db.add`/
  `db.commit` are never called.
- `test_maybe_persist_saves_once_when_dedupe_key_is_free`
- `test_smart_recommendations_bulk_dedupes_writes_per_station_not_per_request`
