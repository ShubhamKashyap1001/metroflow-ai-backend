
from unittest.mock import MagicMock, call, patch

from app.ai_engine.prediction import delay_predictor

def _reset_fleet_stats_cache():
    delay_predictor._fleet_stats_local = None
    delay_predictor._fleet_stats_local_at = 0.0

def test_fleet_capacity_and_age_are_fetched_with_one_query_not_two():
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

    assert db.query.call_count == 1
    assert capacity == 1300.0  
    assert age > 0

def test_fleet_stats_reused_across_many_stations_in_one_bulk_call():
    _reset_fleet_stats_cache()
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        (1000, __import__("datetime").date(2022, 6, 1)),
    ]

    with patch.object(delay_predictor.cache, "get_json", return_value=None), \
         patch.object(delay_predictor.cache, "set_json"):

        for _ in range(30):
            delay_predictor._real_capacity_passengers(db, train_id=None)
            delay_predictor._real_train_age_days(db, train_id=None)

    assert db.query.call_count == 1

def test_fleet_stats_cache_hit_from_redis_skips_the_db_entirely():
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
    delay_predictor._fleet_stats_local = (1234.0, 555.0)
    delay_predictor._fleet_stats_local_at = 999999.0

    with patch.object(delay_predictor.cache, "delete") as mock_delete:
        delay_predictor.invalidate_fleet_stats_cache()

    mock_delete.assert_called_once_with(delay_predictor._FLEET_STATS_CACHE_KEY)
    assert delay_predictor._fleet_stats_local is None
    assert delay_predictor._fleet_stats_local_at == 0.0

def test_timetable_rows_query_eager_loads_station_to_avoid_lazy_loads():
    import inspect

    from app.services import schedule_service

    source = inspect.getsource(schedule_service.get_upcoming_schedules)

    timetable_block_start = source.index("timetable_rows = (")
    timetable_block_end = source.index(".all()", timetable_block_start)
    timetable_block = source[timetable_block_start:timetable_block_end]

    assert "joinedload(TrainSchedule.station)" in timetable_block
