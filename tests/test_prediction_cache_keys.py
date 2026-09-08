
import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.services import prediction_service


class _FakeRedisCache:
    """Minimal in-memory stand-in for app/core/cache.py's get_json/
    set_json, round-tripping through json.dumps/loads exactly like the
    real Redis-backed cache does (including datetime -> str via
    `default=str`, then back via `datetime.fromisoformat` in
    `_cached_prediction`), so these tests exercise the real
    serialisation path, not just the in-memory dict."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get_json(self, key):
        raw = self.store.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    def set_json(self, key, value, ttl_seconds=None):
        self.store[key] = json.dumps(value, default=str)


@pytest.fixture
def fake_cache(monkeypatch):
    fake = _FakeRedisCache()
    monkeypatch.setattr(prediction_service.cache, "get_json", fake.get_json)
    monkeypatch.setattr(prediction_service.cache, "set_json", fake.set_json)
    return fake


def _crowd_result(target_datetime):
    return {
        "station_id": 1,
        "target_datetime": target_datetime,
        "predicted_count": 321,
        "confidence": 0.8,
        "model_version": "random_forest_v1",
        "models": {},
    }


def _delay_result(target_datetime):
    return {
        "station_id": 1,
        "target_datetime": target_datetime,
        "predicted_delay_minutes": 4.2,
        "based_on_predicted_crowd": 321,
        "model_version": "random_forest_v1",
        "models": {},
    }


def _frequency_result(target_datetime):
    return {
        "station_id": 1,
        "target_datetime": target_datetime,
        "recommended_frequency_minutes": 8,
        "is_peak_hour": False,
        "model_version": "random_forest_v1",
        "models": {},
    }


# --- _cache_key_bucket (pure) ------------------------------------------------

def test_cache_key_bucket_floors_to_the_hour():
    dt = datetime(2026, 3, 4, 14, 37, 52, 123456)
    assert prediction_service._cache_key_bucket(dt) == datetime(2026, 3, 4, 14, 0, 0, 0).isoformat()


def test_cache_key_bucket_distinguishes_different_hours():
    a = prediction_service._cache_key_bucket(datetime(2026, 3, 4, 14, 59, 59, 999999))
    b = prediction_service._cache_key_bucket(datetime(2026, 3, 4, 15, 0, 0, 0))
    assert a != b


# --- _cached_predict_crowd ----------------------------------------------------

def test_predict_crowd_same_hour_is_a_cache_hit_not_a_recompute(fake_cache, monkeypatch):
    calls = []

    def fake_predict_crowd(station_id, target_datetime, light=False):
        calls.append(target_datetime)
        return _crowd_result(target_datetime)

    monkeypatch.setattr(prediction_service, "predict_crowd", fake_predict_crowd)

    t1 = datetime(2026, 3, 4, 14, 7, 23, 123456)
    t2 = datetime(2026, 3, 4, 14, 52, 59, 987654)  # same hour, different minute/sec/us

    first = prediction_service._cached_predict_crowd(1, t1)
    second = prediction_service._cached_predict_crowd(1, t2)

    assert len(calls) == 1, "second same-hour call should have been served from cache, not recomputed"
    assert second["predicted_count"] == first["predicted_count"]


def test_predict_crowd_different_hour_is_a_genuine_cache_miss(fake_cache, monkeypatch):
    calls = []

    def fake_predict_crowd(station_id, target_datetime, light=False):
        calls.append(target_datetime)
        return _crowd_result(target_datetime)

    monkeypatch.setattr(prediction_service, "predict_crowd", fake_predict_crowd)

    t1 = datetime(2026, 3, 4, 14, 7, 23)
    t2 = datetime(2026, 3, 4, 15, 7, 23)  # different hour

    prediction_service._cached_predict_crowd(1, t1)
    prediction_service._cached_predict_crowd(1, t2)

    assert len(calls) == 2, "a genuinely different target hour must not be served from the wrong cache entry"


def test_predict_crowd_light_flag_kept_as_a_separate_cache_entry(fake_cache, monkeypatch):
    """light=True (simulator-style call, no confidence pass) and
    light=False (real API call) must never share a cache entry - they
    return differently-shaped results."""
    calls = []

    def fake_predict_crowd(station_id, target_datetime, light=False):
        calls.append(light)
        return _crowd_result(target_datetime)

    monkeypatch.setattr(prediction_service, "predict_crowd", fake_predict_crowd)

    t = datetime(2026, 3, 4, 14, 7, 23)
    prediction_service._cached_predict_crowd(1, t, light=False)
    prediction_service._cached_predict_crowd(1, t, light=True)

    assert calls == [False, True]


# --- _cached_predict_delay ----------------------------------------------------

def test_predict_delay_same_hour_is_a_cache_hit(fake_cache, monkeypatch):
    calls = []

    def fake_predict_delay(station_id, target_datetime, train_id=None, db=None):
        calls.append(target_datetime)
        return _delay_result(target_datetime)

    monkeypatch.setattr(prediction_service, "predict_delay", fake_predict_delay)

    db = MagicMock()
    t1 = datetime(2026, 3, 4, 9, 1, 1)
    t2 = datetime(2026, 3, 4, 9, 58, 40)

    prediction_service._cached_predict_delay(db, 1, t1, train_id=7)
    prediction_service._cached_predict_delay(db, 1, t2, train_id=7)

    assert len(calls) == 1


def test_predict_delay_different_train_id_is_not_conflated(fake_cache, monkeypatch):
    calls = []

    def fake_predict_delay(station_id, target_datetime, train_id=None, db=None):
        calls.append(train_id)
        return _delay_result(target_datetime)

    monkeypatch.setattr(prediction_service, "predict_delay", fake_predict_delay)

    db = MagicMock()
    t = datetime(2026, 3, 4, 9, 1, 1)
    prediction_service._cached_predict_delay(db, 1, t, train_id=7)
    prediction_service._cached_predict_delay(db, 1, t, train_id=8)

    assert calls == [7, 8]


# --- _cached_recommend_frequency ----------------------------------------------

def test_recommend_frequency_same_hour_is_a_cache_hit(fake_cache, monkeypatch):
    calls = []

    def fake_recommend_frequency(station_id, target_datetime):
        calls.append(target_datetime)
        return _frequency_result(target_datetime)

    monkeypatch.setattr(prediction_service, "recommend_frequency", fake_recommend_frequency)

    t1 = datetime(2026, 3, 4, 18, 0, 5)
    t2 = datetime(2026, 3, 4, 18, 44, 12)

    prediction_service._cached_recommend_frequency(1, t1)
    prediction_service._cached_recommend_frequency(1, t2)

    assert len(calls) == 1


# --- smart_recommendations (integration through the fixed wrappers) ----------

def test_smart_recommendations_reuses_cache_across_calls_in_the_same_hour(fake_cache, monkeypatch):
    """Simulates the AI Insights panel refreshing on a live WebSocket
    event a few seconds after the previous refresh (same wall-clock
    hour): the underlying crowd/delay/frequency models must each run
    at most once, not once per refresh."""
    crowd_calls = []
    delay_calls = []
    freq_calls = []

    monkeypatch.setattr(
        prediction_service, "predict_crowd",
        lambda station_id, target_datetime, light=False: (crowd_calls.append(1), _crowd_result(target_datetime))[1],
    )
    monkeypatch.setattr(
        prediction_service, "predict_delay",
        lambda station_id, target_datetime, train_id=None, db=None: (delay_calls.append(1), _delay_result(target_datetime))[1],
    )
    monkeypatch.setattr(
        prediction_service, "recommend_frequency",
        lambda station_id, target_datetime: (freq_calls.append(1), _frequency_result(target_datetime))[1],
    )
    monkeypatch.setattr(prediction_service, "_maybe_persist_recommendation_predictions", lambda *a, **k: None)

    station = MagicMock(capacity=1000)
    db = MagicMock()
    db.get.return_value = station

    class _FixedHourDatetime(datetime):
        _sequence = [
            datetime(2026, 3, 4, 11, 2, 0),
            datetime(2026, 3, 4, 11, 9, 45),  # same hour as above, a few minutes later
        ]
        _i = 0

        @classmethod
        def utcnow(cls):
            value = cls._sequence[cls._i]
            cls._i += 1
            return value

    monkeypatch.setattr(prediction_service, "datetime", _FixedHourDatetime)

    prediction_service.smart_recommendations(db, station_id=1)
    prediction_service.smart_recommendations(db, station_id=1)

    assert len(crowd_calls) == 1
    assert len(delay_calls) == 1
    assert len(freq_calls) == 1
