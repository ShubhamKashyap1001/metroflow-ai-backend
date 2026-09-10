
from unittest.mock import MagicMock, call, patch

from app.ai_engine.prediction import delay_predictor


# --- delay_predictor fleet-stats cache (N+1 across stations) --------------

def _reset_fleet_stats_cache():
    """The module keeps a small process-local cache alongside the
    Redis-backed one - reset both between tests so they don't leak
    state across assertions."""
    delay_predictor._fleet_stats_local = None
    delay_predictor._fleet_stats_local_at = 0.0


def test_fleet_capacity_and_age_are_fetched_with_one_query_not_two():
    """Regression guard for the actual bug: _real_capacity_passengers
    and _real_train_age_days used to each run their own full-table
    `trains` scan. Calling both (as predict_delay does on every
    no-train_id call) must now hit the DB at most once between them."""
    _reset_fleet_stats_cache()
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        (1200, __import__("datetime").date(2020, 1, 1)),
        (1400, __import__("datetime").date(2021, 1, 1)),
    ]

    with patch.object(delay_predictor.cache, "get_json", return_value=None), \
         patch.object(delay_predictor.cache, "set_json"):
        capacity = delay_predictor._real_capacity_passengers(db, train_id=None)
        age = delay_predictor._real_train_age_days(db, train_id=None)

    # Exactly one `db.query(...)` call for both metrics combined -
    # previously this was two (one per metric).
    assert db.query.call_count == 1
    assert capacity == 1300.0  # avg(1200, 1400)
    assert age > 0


def test_fleet_stats_reused_across_many_stations_in_one_bulk_call():
    """The actual production scenario: prediction_service.
    smart_recommendations_bulk calls predict_delay (train_id=None)
    once per station, up to MAX_BULK_RECOMMENDATION_STATIONS=30 times
    in a single request. With the cache warm, none of those repeat
    calls should touch the DB at all - previously every single one of
    them ran two fresh full-table scans."""
    _reset_fleet_stats_cache()
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        (1000, __import__("datetime").date(2022, 6, 1)),
    ]

    with patch.object(delay_predictor.cache, "get_json", return_value=None), \
         patch.object(delay_predictor.cache, "set_json"):
        # Simulate 30 stations worth of calls in one "request" (one
        # warm process-local cache, same as smart_recommendations_bulk
        # looping over station_ids).
        for _ in range(30):
            delay_predictor._real_capacity_passengers(db, train_id=None)
            delay_predictor._real_train_age_days(db, train_id=None)

    # One combined query total for the whole batch, not 60 (2 per
    # station x 30 stations).
    assert db.query.call_count == 1


def test_fleet_stats_cache_hit_from_redis_skips_the_db_entirely():
    """A cold process (empty process-local cache) but a warm Redis
    cache must still avoid hitting Postgres."""
    _reset_fleet_stats_cache()
    db = MagicMock()

    with patch.object(
        delay_predictor.cache,
        "get_json",
        return_value={"avg_capacity": 1150.0, "avg_age_days": 900.0},
    ):
        capacity = delay_predictor._real_capacity_passengers(db, train_id=None)
        age = delay_predictor._real_train_age_days(db, train_id=None)

    db.query.assert_not_called()
    assert capacity == 1150.0
    assert age == 900.0


def test_specific_train_id_still_takes_the_single_row_lookup_path():
    """Regression guard: when a specific train_id IS given (e.g.
    forecast_delay for one train), the existing single-row db.get()
    path must be untouched - no fleet query, no cache involved."""
    _reset_fleet_stats_cache()
    db = MagicMock()
    train = MagicMock(capacity=1500, commissioned_date=__import__("datetime").date(2019, 1, 1))
    db.get.return_value = train

    capacity = delay_predictor._real_capacity_passengers(db, train_id=42)
    age = delay_predictor._real_train_age_days(db, train_id=42)

    db.get.assert_has_calls([call(delay_predictor.Train, 42), call(delay_predictor.Train, 42)])
    db.query.assert_not_called()
    assert capacity == 1500.0
    assert age > 0


def test_invalidate_fleet_stats_cache_clears_both_layers():
    """After a train create/update (train_service.create_train/
    update_train), the cache must actually drop - not just the
    process-local half - so the next fleet lookup recomputes instead
    of serving a stale average for up to FLEET_STATS_CACHE_TTL_SECONDS
    more."""
    delay_predictor._fleet_stats_local = (1234.0, 555.0)
    delay_predictor._fleet_stats_local_at = 999999.0

    with patch.object(delay_predictor.cache, "delete") as mock_delete:
        delay_predictor.invalidate_fleet_stats_cache()

    mock_delete.assert_called_once_with(delay_predictor._FLEET_STATS_CACHE_KEY)
    assert delay_predictor._fleet_stats_local is None
    assert delay_predictor._fleet_stats_local_at == 0.0


# --- schedule_service.get_upcoming_schedules (N+1 via lazy relationship) --

def test_timetable_rows_query_eager_loads_station_to_avoid_lazy_loads():
    """Can't spin up a real Postgres+ORM lazy-load in this sandbox to
    directly observe "one extra SELECT per row", so this asserts the
    fix at the query-construction level instead: the `timetable_rows`
    query in get_upcoming_schedules must request `joinedload(
    TrainSchedule.station)` - the same eager-load mechanism `base`'s
    query already uses for `s.station` a few lines up - so
    `next_stop.station` is satisfied by the JOIN instead of a fresh
    per-row query."""
    import inspect

    from app.services import schedule_service

    source = inspect.getsource(schedule_service.get_upcoming_schedules)

    # The timetable_rows block specifically (not just anywhere in the
    # function - `base`'s joinedload doesn't cover next_stop.station).
    timetable_block_start = source.index("timetable_rows = (")
    timetable_block_end = source.index(".all()", timetable_block_start)
    timetable_block = source[timetable_block_start:timetable_block_end]

    assert "joinedload(TrainSchedule.station)" in timetable_block
