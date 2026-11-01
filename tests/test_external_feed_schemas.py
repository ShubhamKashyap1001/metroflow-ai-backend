import pytest
from pydantic import ValidationError

from app.schemas.sensor_feed import SensorCrowdBatch, TrainPositionBatch

def test_sensor_feed_requires_timezone_and_nonnegative_count():
    valid = {
        "event_id": "counter-101",
        "station_code": "STATION-1",
        "current_count": 42,
        "observed_at": "2026-10-31T08:15:00+05:30",
    }
    assert SensorCrowdBatch(records=[valid]).records[0].current_count == 42
    with pytest.raises(ValidationError):
        SensorCrowdBatch(records=[{**valid, "current_count": -1}])
    with pytest.raises(ValidationError):
        SensorCrowdBatch(records=[{**valid, "observed_at": "2026-10-31T08:15:00"}])

def test_train_feed_bounds_progress_and_batch_size():
    valid = {
        "event_id": "vehicle-event-1",
        "train_number": "TRAIN-1",
        "station_code": "STATION-1",
        "progress_ratio": 0.5,
        "observed_at": "2026-10-31T08:15:00+05:30",
    }
    assert TrainPositionBatch(records=[valid]).records[0].progress_ratio == 0.5
    with pytest.raises(ValidationError):
        TrainPositionBatch(records=[{**valid, "progress_ratio": 1.1}])
    crowd_record = {
        "event_id": "counter-101",
        "station_code": "STATION-1",
        "current_count": 42,
        "observed_at": "2026-10-31T08:15:00+05:30",
    }
    with pytest.raises(ValidationError):
        SensorCrowdBatch(records=[crowd_record] * 51)
