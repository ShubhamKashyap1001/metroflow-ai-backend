
from unittest.mock import MagicMock, patch

from app.services import prediction_service as ps

def _fake_crowd(count=100):
    return {
        "predicted_count": count,
        "confidence": 0.8,
        "target_datetime": "2026-01-01T00:00:00",
        "model_version": "random_forest_v1",
    }

def _fake_delay(minutes=1):
    return {
        "predicted_delay_minutes": minutes,
        "target_datetime": "2026-01-01T00:00:00",
        "model_version": "random_forest_v1",
    }

def _fake_frequency(minutes=5, peak=False):
    return {
        "recommended_frequency_minutes": minutes,
        "is_peak_hour": peak,
        "target_datetime": "2026-01-01T00:00:00",
        "model_version": "random_forest_v1",
    }

def test_build_recommendations_flags_high_crowd_and_delay():

    recs = ps._build_recommendations(
        station_id=1,
        crowd=_fake_crowd(900),
        delay=_fake_delay(5),
        frequency=_fake_frequency(),
        capacity=2400,
    )
    titles = {r["title"] for r in recs}
    assert "High crowd expected" in titles
    assert "Delay risk" in titles
    assert all(r["station_id"] == 1 for r in recs)

def test_build_recommendations_normal_operations_when_nothing_flagged():
    recs = ps._build_recommendations(
        station_id=1,
        crowd=_fake_crowd(100),
        delay=_fake_delay(0),
        frequency=_fake_frequency(),
        capacity=2400,
    )
    titles = [r["title"] for r in recs]
    assert "High crowd expected" not in titles
    assert "Delay risk" not in titles

    assert "Frequency suggestion" in titles

def test_build_recommendations_is_capacity_relative_not_flat():
    small_station_recs = ps._build_recommendations(
        station_id=1, crowd=_fake_crowd(500), delay=_fake_delay(0),
        frequency=_fake_frequency(), capacity=1000,  
    )
    large_station_recs = ps._build_recommendations(
        station_id=2, crowd=_fake_crowd(500), delay=_fake_delay(0),
        frequency=_fake_frequency(), capacity=5000,  
    )
    assert "High crowd expected" in {r["title"] for r in small_station_recs}
    assert "High crowd expected" not in {r["title"] for r in large_station_recs}

def test_build_recommendations_falls_back_when_capacity_missing():
    recs = ps._build_recommendations(
        station_id=1, crowd=_fake_crowd(100), delay=_fake_delay(0),
        frequency=_fake_frequency(), capacity=None,
    )
    assert isinstance(recs, list) and len(recs) >= 1

def test_maybe_persist_skips_save_when_dedupe_key_already_set():
    db = MagicMock()
    with patch.object(ps.cache, "set_nx", return_value=False) as mock_set_nx:
        ps._maybe_persist_recommendation_predictions(
            db,
            station_id=1,
            crowd=_fake_crowd(),
            delay=_fake_delay(),
            frequency=_fake_frequency(),
        )
    mock_set_nx.assert_called_once_with(
        "predictions:reco-write:1", ps.RECOMMENDATION_WRITE_DEDUPE_SECONDS
    )
    db.add.assert_not_called()
    db.commit.assert_not_called()

def test_maybe_persist_saves_once_when_dedupe_key_is_free():
    db = MagicMock()
    with patch.object(ps.cache, "set_nx", return_value=True):
        ps._maybe_persist_recommendation_predictions(
            db,
            station_id=1,
            crowd=_fake_crowd(),
            delay=_fake_delay(),
            frequency=_fake_frequency(),
        )
    assert db.add.call_count == 3
    db.commit.assert_called_once()

def _db_with_capacities(capacities: dict[int, int]) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = list(capacities.items())
    return db

def test_smart_recommendations_bulk_returns_one_entry_per_station():
    db = _db_with_capacities({1: 2400, 2: 2400, 3: 2400})
    with patch.object(ps, "_cached_predict_crowd", return_value=_fake_crowd()), \
         patch.object(ps, "_cached_predict_delay", return_value=_fake_delay()), \
         patch.object(ps, "_cached_recommend_frequency", return_value=_fake_frequency()), \
         patch.object(ps.cache, "set_nx", return_value=True):
        results = ps.smart_recommendations_bulk(db, [1, 2, 3])

    assert set(results.keys()) == {1, 2, 3}
    for station_id, recs in results.items():
        assert all(r["station_id"] == station_id for r in recs)

def test_smart_recommendations_bulk_dedupes_writes_per_station_not_per_request():
    db = _db_with_capacities({1: 2400, 2: 2400})
    with patch.object(ps, "_cached_predict_crowd", return_value=_fake_crowd()), \
         patch.object(ps, "_cached_predict_delay", return_value=_fake_delay()), \
         patch.object(ps, "_cached_recommend_frequency", return_value=_fake_frequency()), \
         patch.object(ps.cache, "set_nx", return_value=True) as mock_set_nx:
        ps.smart_recommendations_bulk(db, [1, 2])

    assert mock_set_nx.call_count == 2
    assert db.commit.call_count == 2

def test_smart_recommendations_bulk_uses_real_per_station_capacity():
    db = _db_with_capacities({1: 1000, 2: 5000})  
    with patch.object(ps, "_cached_predict_crowd", return_value=_fake_crowd(500)), \
         patch.object(ps, "_cached_predict_delay", return_value=_fake_delay(0)), \
         patch.object(ps, "_cached_recommend_frequency", return_value=_fake_frequency()), \
         patch.object(ps.cache, "set_nx", return_value=True):
        results = ps.smart_recommendations_bulk(db, [1, 2])

    assert "High crowd expected" in {r["title"] for r in results[1]}
    assert "High crowd expected" not in {r["title"] for r in results[2]}

def test_smart_recommendations_bulk_empty_list_returns_empty_dict():
    db = MagicMock()
    assert ps.smart_recommendations_bulk(db, []) == {}
    db.add.assert_not_called()
    db.commit.assert_not_called()
